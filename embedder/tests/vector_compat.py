"""Vector compatibility check for the local embedding stack.

A new transformers / sentence-transformers / torch can produce different
vectors for the same model and the same text. Nothing errors when that
happens: every existing Qdrant collection simply drifts away from the queries
made against it, and search quietly gets worse. So a bump of that stack is
measured, not assumed.

How it works:

* fixtures/vector_compat_corpus.json is a fixed, varied corpus (prose, code,
  tables, logs, other languages, emoji, a one-letter chunk and one long enough
  to be truncated).
* fixtures/vector_compat_reference.json holds the vectors the PREVIOUS stack
  produced for it, with that stack's versions recorded beside them.
* test_vector_compat.py embeds the corpus with the installed stack, the same
  way main.py does, and requires every chunk's cosine similarity to its
  reference vector to be at least MIN_COSINE.

Bumping the stack:

1. Before changing any pin, regenerate the reference with the CURRENT stack
   (full image, or a venv with requirements-local.txt and the Dockerfile's
   CPU torch):  python embedder/tests/vector_compat.py --write
2. Change the pins, install them, and run the test:
       pytest embedder/tests/test_vector_compat.py -m slow -v
   It prints the minimum cosine per chunk.
3. At or above MIN_COSINE, nothing needs reindexing. Below it, the release
   must document a reindex (docs/install/*, "Upgrading").
4. Commit the reference written in step 1, so the next bump is measured
   against the stack that is shipping now.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS_FILE = FIXTURES / "vector_compat_corpus.json"
REFERENCE_FILE = FIXTURES / "vector_compat_reference.json"

# Same model and revision main.py locks the bundled embedder to
# (LOCKED_LOCAL_MODEL_REPO, LOCKED_LOCAL_MODEL_REVISION).
MODEL = "multi-qa-distilbert-cos-v1"
REVISION = "bc2339dca6eba2b116cc7c2cfa06f63e4ff87e3f"

# At or above this, old and new vectors are interchangeable for search and no
# reindex is needed.
MIN_COSINE = 0.999

STACK_PACKAGES = ("sentence-transformers", "transformers", "tokenizers", "torch")


def stack_versions() -> dict:
    from importlib.metadata import version
    return {p: version(p) for p in STACK_PACKAGES}


def load_corpus() -> list[str]:
    return json.loads(CORPUS_FILE.read_text(encoding="utf-8"))


def embed_corpus(corpus: list[str]) -> list[list[float]]:
    """Embed each chunk exactly as main.py's local backend does: CPU, one
    text per encode() call, normalized."""
    import torch
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(f"sentence-transformers/{MODEL}", revision=REVISION, device="cpu")
    torch.set_num_threads(int(os.environ.get("EMBED_CPU_THREADS", "2")))
    return [model.encode(t, normalize_embeddings=True).tolist() for t in corpus]


def _pack(vec: list[float]) -> str:
    import numpy as np
    return base64.b64encode(np.asarray(vec, dtype="<f4").tobytes()).decode("ascii")


def unpack(b64: str) -> list[float]:
    import numpy as np
    return np.frombuffer(base64.b64decode(b64), dtype="<f4").astype("float64").tolist()


def load_reference() -> dict:
    return json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))


def write_reference() -> None:
    corpus = load_corpus()
    vectors = embed_corpus(corpus)
    REFERENCE_FILE.write_text(json.dumps({
        "model": MODEL,
        "stack": stack_versions(),
        "corpus_size": len(corpus),
        "dim": len(vectors[0]),
        "encoding": "base64 little-endian float32, one per corpus entry",
        "vectors": [_pack(v) for v in vectors],
    }, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {REFERENCE_FILE} for {stack_versions()}")


def cosine(a: list[float], b: list[float]) -> float:
    import numpy as np
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true",
                    help="regenerate the reference vectors with the installed stack")
    args = ap.parse_args(argv)
    if args.write:
        write_reference()
        return 0
    ref = load_reference()
    sims = [cosine(unpack(r), v) for r, v in zip(ref["vectors"], embed_corpus(load_corpus()))]
    print(f"reference stack: {ref['stack']}")
    print(f"installed stack: {stack_versions()}")
    for i, s in enumerate(sims):
        print(f"  chunk {i:2d}: cosine {s:.7f}")
    print(f"min cosine {min(sims):.7f} (threshold {MIN_COSINE})")
    return 0 if min(sims) >= MIN_COSINE else 1


if __name__ == "__main__":
    sys.exit(main())
