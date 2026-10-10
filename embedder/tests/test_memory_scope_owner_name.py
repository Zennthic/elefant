"""Owner-by-name memory visibility (issue #85).

Compactor-promoted memories carry no author_id — only an `owner:<token-name>`
tag — so a UUID-only scope check hid every promoted memory from the token that
captured them.  The read path now also admits a memory whose owner name-tag
matches the reader's token name (forwarded by the MCP, stripped by nginx on
browser routes so it can't be spoofed).  These tests pin that behaviour and
prove the existing UUID/author_id and tenant/admin paths are unaffected.
"""
import pytest


@pytest.fixture
def in_scope():
    import main
    return main._memory_in_scope


def _ctx(user_id="u-uuid", owner_name="", role="user"):
    return {"user_id": user_id, "owner_name": owner_name, "role": role,
            "org_id": "o", "org_slug": "acme"}


def test_owner_name_match_makes_promoted_memory_visible(in_scope):
    # Compactor promotion: no author_id, only the owner:<name> tag.
    payload = {"tags": ["owner:Claude", "hot-tier-promoted"], "tenant_id": "acme"}
    ctx = _ctx(owner_name="Claude")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is True


def test_owner_name_mismatch_still_hidden(in_scope):
    payload = {"tags": ["owner:SomeoneElse"], "tenant_id": "acme"}
    ctx = _ctx(owner_name="Claude")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is False


def test_author_id_uuid_path_unaffected(in_scope):
    # A UI-created memory whose author_id matches the reader is still visible,
    # even without an owner_name.
    payload = {"author_id": "u-uuid", "tags": [], "tenant_id": "acme"}
    ctx = _ctx(owner_name="")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is True


def test_browser_reader_without_owner_name_cannot_name_match(in_scope):
    # nginx strips X-Auth-Owner-Name on browser routes → no name-match, so a
    # browser reader falls back to UUID scoping (owner "Claude" not a UUID).
    payload = {"tags": ["owner:Claude"], "tenant_id": "acme"}
    ctx = _ctx(owner_name="")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is False


def test_tenant_mismatch_hidden_even_with_name_match(in_scope):
    # Cross-tenant defence runs first and unconditionally.
    payload = {"tags": ["owner:Claude"], "tenant_id": "victim"}
    ctx = _ctx(owner_name="Claude")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is False


def test_admin_sees_everything(in_scope):
    payload = {"tags": ["owner:Whoever"], "tenant_id": "acme"}
    ctx = _ctx(role="org_admin")
    assert in_scope(payload, ctx, None, "acme") is True


def test_name_with_colon_preserved(in_scope):
    # owner:<name> where the name itself contains a colon.
    payload = {"tags": ["owner:team:bot"], "tenant_id": "acme"}
    ctx = _ctx(owner_name="team:bot")
    assert in_scope(payload, ctx, {"scope_user_ids": {"u-uuid"}}, "acme") is True


# ── The same name-match governs edit/delete authorization ───────────────────

@pytest.fixture
def owner_match():
    import main
    return main._owner_name_matches


def test_owner_name_matches_helper(owner_match):
    assert owner_match({"tags": ["owner:Claude"]}, {"owner_name": "Claude"}) is True
    assert owner_match({"tags": ["owner:Other"]}, {"owner_name": "Claude"}) is False
    assert owner_match({"tags": ["owner:Claude"]}, {"owner_name": ""}) is False   # browser (stripped)
    assert owner_match({"tags": ["owner:Claude"]}, None) is False
    assert owner_match({"tags": [], "author_id": "u"}, {"owner_name": "Claude"}) is False
