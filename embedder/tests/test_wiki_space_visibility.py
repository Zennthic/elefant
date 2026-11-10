"""Wiki search applies space ACLs to the space the editor means.

In the multi-tenant docs layout the indexer stores each chunk's path relative
to /docs, so it starts with the tenant's directory (`acme/AI/page.md`). The
editor names spaces below that (`AI`). Reading the first path segment as the
space made every native wiki hit invisible to every non-admin caller.
"""
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main


def _ctx(spaces, role="user"):
    return {"user_id": "u1", "org_id": "o1", "org_slug": "acme", "role": role,
            "_allowed_spaces": set(spaces)}


def _hit(path, tenant="acme"):
    return {"path": path, "tenant_id": tenant, "title": path, "content": "x"}


@pytest.fixture
def multi_tenant(monkeypatch):
    monkeypatch.setattr(main, "_MULTI_TENANT_DOCS", True)


def test_a_granted_space_is_visible_below_the_tenant_directory(multi_tenant):
    assert main._wiki_hit_visible(_hit("acme/AI/page.md"), _ctx({"AI"}), None, "acme")


def test_an_ungranted_space_stays_hidden(multi_tenant):
    assert not main._wiki_hit_visible(_hit("acme/Finance/q3.md"), _ctx({"AI"}), None, "acme")


def test_a_page_at_the_tenant_root_belongs_to_no_space(multi_tenant):
    assert main._wiki_hit_visible(_hit("acme/index.md"), _ctx(set()), None, "acme")


def test_another_tenants_chunk_is_never_visible(multi_tenant):
    assert not main._wiki_hit_visible(_hit("other/AI/page.md", tenant="other"),
                                      _ctx({"AI"}), None, "acme")


def test_single_tenant_paths_are_read_as_they_are(monkeypatch):
    monkeypatch.setattr(main, "_MULTI_TENANT_DOCS", False)
    assert main._wiki_hit_visible(_hit("AI/page.md", tenant=None), _ctx({"AI"}), None, "default")
    # A space that happens to share the slug's name is not stripped away.
    assert not main._wiki_hit_visible(_hit("default/page.md", tenant=None),
                                      _ctx({"AI"}), None, "default")


def test_admins_see_every_space(multi_tenant):
    assert main._wiki_hit_visible(_hit("acme/Finance/q3.md"), _ctx(set(), role="org_admin"),
                                  None, "acme")


def test_a_failed_lookup_hides_space_scoped_pages(monkeypatch, multi_tenant):
    def down(*_a, **_k):
        raise OSError("editor unreachable")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    ctx = {"user_id": "u1", "org_id": "o1", "role": "user"}
    assert main._resolve_allowed_spaces(ctx) == set()
    assert not main._wiki_hit_visible(_hit("acme/AI/page.md"), ctx, None, "acme")
    assert main._wiki_hit_visible(_hit("acme/index.md"), ctx, None, "acme")


def test_search_returns_a_granted_page_to_a_non_admin(monkeypatch, multi_tenant):
    q = MagicMock()
    q.search.return_value = [SimpleNamespace(score=0.9, payload=_hit("acme/AI/page.md"))]
    monkeypatch.setattr(main, "qdrant", lambda: q)
    monkeypatch.setattr(main, "get_embedding", lambda _q: [0.0])
    monkeypatch.setattr(main, "_resolve_allowed_spaces",
                        lambda ctx: ctx.setdefault("_allowed_spaces", {"AI"}))
    r = TestClient(main.app).get(
        "/api/search", params={"q": "page", "tenant_slug": "acme"},
        headers={"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1", "X-Auth-Org-Id": "o1"})
    assert r.status_code == 200, r.text
    assert [h["path"] for h in r.json()["results"]] == ["acme/AI/page.md"]
