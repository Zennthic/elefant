"""Regression tests for the wiki-indexing gap (issue #83).

Two defects let a wiki page be counted as a "doc on disk" yet never become
searchable, silently:

  A) The embedder only indexes from the /docs mirror, which the editor writes
     best-effort; a swallowed mirror-write left a page in S3 (counted) but off
     /docs (never embedded).  Fix: reindex reconciles /docs from S3 first.
  B) index_file deleted a page's vectors BEFORE embedding and a single
     transient embed error was swallowed with no retry — so a momentary
     backend blip destructively blanked a working page.  Fix: embed all chunks
     first (transactional replace) + retry transient faults only.

These tests pin both behaviours.
"""
import httpx
import pytest


@pytest.fixture
def main_mod():
    import main
    return main


# ── (B1) transient-vs-permanent classification ──────────────────────────────

def test_is_transient_embed_error_classification(main_mod):
    f = main_mod._is_transient_embed_error
    # Transient → retry
    assert f(httpx.TimeoutException("slow"))
    assert f(httpx.ConnectError("refused"))
    resp503 = httpx.Response(503, request=httpx.Request("POST", "http://x/embeddings"))
    assert f(httpx.HTTPStatusError("boom", request=resp503.request, response=resp503))
    # Permanent → fail fast
    assert not f(ValueError("Expected float list, got <class 'str'>"))
    resp401 = httpx.Response(401, request=httpx.Request("POST", "http://x/embeddings"))
    assert not f(httpx.HTTPStatusError("auth", request=resp401.request, response=resp401))


# ── (B2) get_embedding retries transient, fails fast on permanent ────────────

def test_get_embedding_retries_transient_then_succeeds(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_EMBED_BACKOFF_BASE", 0.0)  # no real sleeping
    calls = {"n": 0}

    def flaky(_text):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("backend restarting")
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(main_mod, "_embed_fn", flaky)
    assert main_mod.get_embedding("hello") == [0.1, 0.2, 0.3]
    assert calls["n"] == 3  # two failures, then success


def test_get_embedding_fails_fast_on_permanent_error(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_EMBED_BACKOFF_BASE", 0.0)
    calls = {"n": 0}

    def wrong_dim(_text):
        calls["n"] += 1
        raise ValueError("Expected float list, got <class 'str'>")

    monkeypatch.setattr(main_mod, "_embed_fn", wrong_dim)
    with pytest.raises(ValueError):
        main_mod.get_embedding("hello")
    assert calls["n"] == 1  # not retried


def test_get_embedding_exhausts_retries_then_raises(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_EMBED_BACKOFF_BASE", 0.0)
    calls = {"n": 0}

    def always_timeout(_text):
        calls["n"] += 1
        raise httpx.TimeoutException("never comes back")

    monkeypatch.setattr(main_mod, "_embed_fn", always_timeout)
    with pytest.raises(httpx.TimeoutException):
        main_mod.get_embedding("hello")
    assert calls["n"] == main_mod._EMBED_MAX_ATTEMPTS


# ── (B3) index_file replaces vectors TRANSACTIONALLY ─────────────────────────

def _write_doc(main_mod, rel: str, body: str):
    p = main_mod.DOCS_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_index_file_success_deletes_then_upserts_and_returns_count(main_mod, monkeypatch):
    mock_q = _install_mock_qdrant(main_mod, monkeypatch)
    monkeypatch.setattr(main_mod, "get_embedding", lambda _t: [0.0] * 8)
    p = _write_doc(main_mod, "acme/AI/ok.md", "# Title\n\nbody one\n\n## Two\n\nbody two\n")

    n = main_mod.index_file(p, "acme")

    assert n >= 1
    assert mock_q.upsert.called
    assert mock_q.delete.called  # old vectors cleared as part of the swap


def test_index_file_embed_failure_leaves_index_untouched(main_mod, monkeypatch):
    """The crux of #83's destructive variant: a transient embed failure must
    NOT delete the page's existing vectors, and must NOT upsert partial data —
    it must raise, leaving the previously-searchable page intact."""
    mock_q = _install_mock_qdrant(main_mod, monkeypatch)

    def boom(_t):
        raise httpx.TimeoutException("backend down")

    monkeypatch.setattr(main_mod, "get_embedding", boom)
    p = _write_doc(main_mod, "acme/AI/broken.md", "# Title\n\nsome body\n")

    with pytest.raises(httpx.TimeoutException):
        main_mod.index_file(p, "acme")

    assert not mock_q.delete.called, "existing vectors must be preserved on embed failure"
    assert not mock_q.upsert.called, "no partial upsert on embed failure"


def _install_mock_qdrant(main_mod, monkeypatch):
    from unittest.mock import MagicMock
    mock_q = MagicMock()
    monkeypatch.setattr(main_mod, "qdrant", lambda: mock_q)
    return mock_q


# ── (A) reindex reconciles /docs from S3 ─────────────────────────────────────

class _FakeBody:
    def __init__(self, data): self._d = data
    def read(self): return self._d


class _FakePaginator:
    def __init__(self, pages): self._pages = pages
    def paginate(self, **_kw): return iter(self._pages)


def _fake_s3(pages, objects):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginator(pages)
    client.get_object.side_effect = lambda Bucket, Key: {"Body": _FakeBody(objects[Key])}
    return client


def test_reconcile_pulls_missing_doc_from_s3(main_mod, monkeypatch):
    body = b"# Caracal\n\ndesign notes\n"
    pages = [{"Contents": [{"Key": "AI/projects/caracal/architecture-decisions.md",
                            "Size": len(body)}]}]
    client = _fake_s3(pages, {"AI/projects/caracal/architecture-decisions.md": body})
    monkeypatch.setattr(main_mod, "_get_s3_client", lambda: client)

    dest = main_mod.DOCS_DIR / "acme" / "AI/projects/caracal/architecture-decisions.md"
    if dest.exists():
        dest.unlink()

    pulled = main_mod._reconcile_docs_from_s3("acme")
    assert pulled == 1
    assert dest.exists()
    assert dest.read_bytes() == body

    # Second run: file present at the same size → nothing pulled.
    assert main_mod._reconcile_docs_from_s3("acme") == 0


def test_reconcile_noop_without_s3_client(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_get_s3_client", lambda: None)
    assert main_mod._reconcile_docs_from_s3("acme") == 0
