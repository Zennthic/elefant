"""Slim-image guard: asking the slim embedder (no sentence-transformers/torch)
to run the local backend must fail with an actionable message, not a bare
ModuleNotFoundError.

The slim image is built with --build-arg INCLUDE_LOCAL_EMBEDDINGS=false for
deployments that use an external/custom embedding API. If such a deployment is
misconfigured back to EMBEDDING_PROVIDER=local, _build_backend hits the missing
import — and should explain the image/config mismatch.

We simulate the missing package by blocking its import, so the test holds
regardless of whether torch happens to be installed in the dev/CI env.
"""
import builtins

import pytest

import main


def test_local_backend_without_sentence_transformers_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    with pytest.raises(RuntimeError) as excinfo:
        main._build_backend({"provider": "local", "model": "multi-qa-distilbert-cos-v1"})

    msg = str(excinfo.value)
    assert "slim embedder image" in msg
    assert "EMBEDDING_PROVIDER" in msg


def test_missing_package_is_not_masked_for_external_providers(monkeypatch):
    # The guard must be scoped to the local branch only — a custom-provider
    # build never touches sentence-transformers, so blocking it changes nothing.
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    # A custom backend with an unreachable base_url still builds its callable
    # (the probe just defaults the vector size); it must NOT raise the
    # slim-image RuntimeError.
    fn, vsize = main._build_backend(
        {"provider": "custom", "model": "nomic-embed", "base_url": "http://127.0.0.1:1"}
    )
    assert callable(fn) and isinstance(vsize, int)
