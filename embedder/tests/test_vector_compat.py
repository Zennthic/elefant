"""The installed local embedding stack must reproduce the reference vectors.

See vector_compat.py for why and for how to bump the stack. The embedding test is marked slow: it
needs sentence-transformers (the full image's requirements-local.txt, which
the CI test job does not install) and the model, which it downloads on first
use unless HF_HOME already holds it. It skips when either is missing, so run
it explicitly with `-m slow` when bumping the stack.
"""
import importlib.util

import pytest
import vector_compat as vc


def test_reference_matches_corpus():
    """Cheap and always on: the committed reference covers the committed
    corpus, so an edit to one without the other cannot pass silently."""
    ref = vc.load_reference()
    corpus = vc.load_corpus()
    assert ref["model"] == vc.MODEL
    assert ref["corpus_size"] == len(corpus) == len(ref["vectors"])
    assert all(len(vc.unpack(v)) == ref["dim"] for v in ref["vectors"])


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("sentence_transformers") is None,
                    reason="sentence-transformers not installed (slim image / CI test job)")
def test_installed_stack_reproduces_reference_vectors():
    ref = vc.load_reference()
    try:
        vectors = vc.embed_corpus(vc.load_corpus())
    except OSError as exc:  # model not cached and not downloadable
        pytest.skip(f"model {vc.MODEL} unavailable: {exc}")
    sims = [vc.cosine(vc.unpack(r), v) for r, v in zip(ref["vectors"], vectors)]
    worst = min(range(len(sims)), key=sims.__getitem__)
    assert sims[worst] >= vc.MIN_COSINE, (
        f"chunk {worst} cosine {sims[worst]:.6f} < {vc.MIN_COSINE} against the "
        f"reference from {ref['stack']}; installed {vc.stack_versions()}. "
        "Existing collections need a reindex: document it before shipping."
    )
