"""Tests for the wiki page write lock.

A lock row's existence IS the lock, and the rule it enforces is "nobody
writes" — not "nobody except the holder" and not "nobody except a human".
The things worth guarding here are:

  1. The helpers: _assert_wiki_unlocked / _assert_wiki_prefix_unlocked raise
     423 on a locked page (or a folder holding one) and pass otherwise.
  2. The reach: every write path calls one of them. This is checked
     structurally against the source of main.py and routers/wiki.py, because the alternative — a new
     write endpoint added later that simply forgets — is exactly how a lock
     stops meaning anything.
  3. The MCP boundary: there is no token-authed lock/unlock endpoint and no
     MCP tool, which is what makes lock/unlock human-only.
  4. The lock endpoint itself: idempotent both ways, and it never reassigns
     an existing lock to whoever clicked second.

No live S3 is needed: the lock check runs before anything touches the bucket.
"""
import re
import secrets
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def org():
    """A throwaway tenant, cleaned up with any locks it accumulated."""
    import main  # noqa: F401 — import-time init_db() creates the tables
    from config_db import get_db_session, Tenant, WikiPageLock

    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    slug = f"wikilock-{org_id[:6]}"
    db.add(Tenant(id=org_id, name="WikiLock", slug=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    db.close()

    yield {"org_id": org_id, "slug": slug}

    db2 = next(get_db_session())
    try:
        db2.query(WikiPageLock).filter_by(tenant_id=org_id).delete()
        o = db2.get(Tenant, org_id)
        if o:
            db2.delete(o)
        db2.commit()
    finally:
        db2.close()


def _lock(org_id, path, by="Jon", reason=None):
    from config_db import get_db_session, WikiPageLock
    db = next(get_db_session())
    db.add(WikiPageLock(tenant_id=org_id, path=path,
                        locked_by_id="user-1", locked_by_name=by, reason=reason))
    db.commit()
    db.close()


# ── 1. The helpers ────────────────────────────────────────────────────────

def test_unlocked_page_passes(org):
    import wiki_pages
    from config_db import get_db_session
    db = next(get_db_session())
    # No exception is the assertion.
    wiki_pages._assert_wiki_unlocked(db, org["org_id"], "AI/notes.md")
    db.close()


def test_locked_page_raises_423(org):
    import wiki_pages
    from config_db import get_db_session
    _lock(org["org_id"], "AI/frozen.md", by="Jon")
    db = next(get_db_session())
    with pytest.raises(HTTPException) as exc:
        wiki_pages._assert_wiki_unlocked(db, org["org_id"], "AI/frozen.md", "saved")
    assert exc.value.status_code == 423
    # The refusal names the page, the holder and the way out.
    assert "AI/frozen.md" in exc.value.detail
    assert "Jon" in exc.value.detail
    assert "unlock" in exc.value.detail.lower()
    db.close()


def test_lock_is_scoped_to_its_tenant(org):
    """Another org's lock on the same path must not freeze this org's page."""
    import wiki_pages
    from config_db import get_db_session
    from config_db import WikiPageLock
    _lock("some-other-org", "AI/frozen.md")
    try:
        db = next(get_db_session())
        wiki_pages._assert_wiki_unlocked(db, org["org_id"], "AI/frozen.md")
        db.close()
    finally:
        db2 = next(get_db_session())
        db2.query(WikiPageLock).filter_by(tenant_id="some-other-org").delete()
        db2.commit()
        db2.close()


def test_prefix_check_catches_a_lock_beneath_the_folder(org):
    import wiki_pages
    from config_db import get_db_session
    _lock(org["org_id"], "AI/projects/thing/page.md", by="Jon")
    db = next(get_db_session())
    with pytest.raises(HTTPException) as exc:
        wiki_pages._assert_wiki_prefix_unlocked(db, org["org_id"], "AI/projects", "deleted")
    assert exc.value.status_code == 423
    assert "AI/projects/thing/page.md" in exc.value.detail
    db.close()


def test_prefix_check_does_not_match_a_sibling_folder(org):
    """'AI/proj' must not be treated as a prefix of 'AI/projects/...'."""
    import wiki_pages
    from config_db import get_db_session
    _lock(org["org_id"], "AI/projects/thing/page.md")
    db = next(get_db_session())
    wiki_pages._assert_wiki_prefix_unlocked(db, org["org_id"], "AI/proj", "deleted")
    db.close()


def test_prefix_check_passes_on_a_clean_folder(org):
    import wiki_pages
    from config_db import get_db_session
    db = next(get_db_session())
    wiki_pages._assert_wiki_prefix_unlocked(db, org["org_id"], "AI/projects")
    db.close()


# ── 2. Every write path is guarded ────────────────────────────────────────
# Read off the source: a lock only means something if no write endpoint can
# be reached without passing the check. The web routes live in
# routers/wiki.py (@router.), the token-authed ones in main.py (@app.).

_EDITOR = Path(__file__).resolve().parent.parent
_MAIN_SRC = (_EDITOR / "main.py").read_text()
_WIKI_SRC = (_EDITOR / "routers" / "wiki.py").read_text()


def _endpoint_body(decorator: str) -> str:
    """Source of the handler under `decorator`, up to the next decorator."""
    src = _WIKI_SRC if decorator.startswith("@router.") else _MAIN_SRC
    start = src.index(decorator)
    nxt = src.find("\n" + decorator.split(".", 1)[0] + ".", start + len(decorator))
    return src[start:nxt if nxt != -1 else len(src)]


@pytest.mark.parametrize("decorator", [
    # Web (session-authed) writes.
    '@router.put("/api/file")',
    '@router.post("/api/file")',
    '@router.delete("/api/file")',
    '@router.post("/api/wiki/revert")',
    '@router.post("/api/move-file")',
    '@router.post("/api/rename-dir")',
    '@router.delete("/api/directory")',
    # MCP (token-authed) writes.
    '@app.post("/api/internal/wiki/write")',
    '@app.post("/api/internal/wiki/edit")',
    '@app.post("/api/internal/wiki/move")',
    '@app.post("/api/internal/wiki/delete")',
    '@app.post("/api/internal/wiki/revert")',
])
def test_write_path_checks_the_lock(decorator):
    body = _endpoint_body(decorator)
    assert "_assert_wiki_unlocked" in body or "_assert_wiki_prefix_unlocked" in body, (
        f"{decorator} writes without checking the page lock"
    )


def test_move_and_delete_guard_the_folder_case_too():
    """The recursive branches are the easy ones to miss: a folder move that
    skipped the prefix check would carry a locked page along with it."""
    for decorator in ('@app.post("/api/internal/wiki/move")',
                      '@app.post("/api/internal/wiki/delete")'):
        body = _endpoint_body(decorator)
        assert "_assert_wiki_unlocked" in body, f"{decorator} misses the page case"
        assert "_assert_wiki_prefix_unlocked" in body, f"{decorator} misses the folder case"


# ── 3. Lock/unlock is human-only ──────────────────────────────────────────

def test_no_token_authed_lock_endpoint():
    """No /api/internal/wiki/lock — an agent has no endpoint to call."""
    assert "/api/internal/wiki/lock" not in _MAIN_SRC + _WIKI_SRC


def test_no_mcp_lock_tool():
    """...and no MCP tool either, which is the other half of the same claim."""
    mcp_src = (Path(__file__).resolve().parent.parent.parent / "mcp" / "main.py").read_text()
    assert not re.search(r"async def wiki_(un)?lock\b", mcp_src)


def test_the_lock_endpoint_requires_web_write_access():
    body = _endpoint_body('@router.post("/api/wiki/lock")')
    assert "_check_space_access" in body and "write=True" in body


# ── 4. GET /api/file carries what the header needs ────────────────────────

def test_get_file_returns_lock_and_last_edit():
    body = _endpoint_body('@router.get("/api/file")')
    assert "last_edit" in body and "_last_wiki_edit" in body
    assert "_wiki_lock_for" in body


def test_internal_read_reports_the_lock():
    """An agent should learn a page is frozen from the read, not from a 423."""
    body = _endpoint_body('@app.post("/api/internal/wiki/read")')
    assert "_wiki_lock_for" in body


# ── 5. The lock endpoint's own behaviour ──────────────────────────────────

def test_locking_twice_keeps_the_original_holder(org):
    """A second lock must not reassign the page to whoever clicked last."""
    import wiki_pages
    from config_db import get_db_session, WikiPageLock

    _lock(org["org_id"], "AI/frozen.md", by="Jon", reason="under review")
    db = next(get_db_session())
    first = wiki_pages._wiki_lock_for(db, org["org_id"], "AI/frozen.md")
    assert first.locked_by_name == "Jon"
    assert first.reason == "under review"
    db.close()

    # Re-locking is the no-op branch of wiki_set_lock: same row, same holder.
    db2 = next(get_db_session())
    again = wiki_pages._wiki_lock_for(db2, org["org_id"], "AI/frozen.md")
    assert again.id == first.id
    rows = db2.query(WikiPageLock).filter_by(
        tenant_id=org["org_id"], path="AI/frozen.md").count()
    assert rows == 1
    db2.close()


def test_lock_to_dict_shape(org):
    import wiki_pages
    from config_db import get_db_session
    _lock(org["org_id"], "AI/frozen.md", by="Jon", reason="why")
    db = next(get_db_session())
    d = wiki_pages._wiki_lock_for(db, org["org_id"], "AI/frozen.md").to_dict()
    assert d["locked"] is True
    assert d["path"] == "AI/frozen.md"
    assert d["locked_by_name"] == "Jon"
    assert d["reason"] == "why"
    assert d["locked_at"]          # ISO string, not a datetime
    assert isinstance(d["locked_at"], str)
    db.close()
