"""Issue #269: list_sources said `files: 184` for a tree of 284 and nothing else.

A stale or partial index read exactly like a complete one. When the embedder
reports how many files the checkout should hold and the index holds a
different number, list_sources says so.
"""
import pytest

_RW = {"user_id": "u1", "tenant_slug": "acme", "name": "agent", "wiki_read": True}


@pytest.fixture
def listing(monkeypatch):
    import main

    def _go(**fields):
        src = {"id": "s1", "name": "Elefant", "repo_url": "https://example.invalid/e",
               "branch": "main", "enabled": True, "last_synced_at": "2026-09-28T00:38:00Z",
               **fields}

        async def _sources(ctx, token_info):
            return [src]
        monkeypatch.setattr(main, "_token", lambda ctx: _RW)
        monkeypatch.setattr(main, "_session_sources", _sources)
        import asyncio
        return asyncio.run(main.list_sources(ctx=None))
    return _go


def test_a_gap_between_index_and_tree_is_called_out(listing):
    out = listing(file_count=184, indexable_files=284)
    assert "files: 184" in out
    assert "index out of step with checkout" in out
    assert "284" in out


def test_a_matching_index_says_nothing_extra(listing):
    out = listing(file_count=284, indexable_files=284)
    assert "files: 284" in out
    assert "out of step" not in out


def test_an_unknown_tree_size_is_not_reported_as_drift(listing):
    """After a restart the embedder has no tree figure until the next sync."""
    out = listing(file_count=184, indexable_files=None)
    assert "out of step" not in out
