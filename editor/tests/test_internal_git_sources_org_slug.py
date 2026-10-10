"""Issue #136: /api/internal/git-sources dropped org_slug, and clones went missing.

Checkouts live at /git-sources/<org_slug>/<source_id>/. This endpoint backs the
agent-facing `list_sources` and `source_tree`, so when it answered without an
org_slug both of those looked under the wrong directory: source_tree returned
"Source has not been synced yet" and list_sources reported files: 0 for repos
that were cloned, current and sitting right there on the volume.

/api/internal/config has always carried the field. These tests hold the two
internal endpoints to the same contract so they cannot drift apart again.
"""
import types

import pytest


class _Row:
    def __init__(self, sid, org_id):
        self.id = sid
        self.name = f"src-{sid}"
        self.repo_url = f"https://github.com/acme/{sid}"
        self.branch = "main"
        self.path_prefix = ""
        self.enabled = True
        self.tool_availability = "on"
        self.org_id = org_id


class _Tenant:
    def __init__(self, tid, slug):
        self.id = tid
        self.slug = slug


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **kw):
        return _FakeQuery([r for r in self._rows
                           if all(getattr(r, k) == v for k, v in kw.items())])

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, sources, tenants):
        self._sources = sources
        self._tenants = tenants

    def query(self, model):
        import main
        if model is main.Tenant:
            return _FakeQuery(self._tenants)
        return _FakeQuery(self._sources)


@pytest.fixture
def call(monkeypatch):
    from routers import git_sources
    monkeypatch.setattr(git_sources, "user_accessible_git_sources",
                        lambda session, user_id, org_id, ids: set(ids))

    def _call(sources, tenants, org_id="org-1"):
        return git_sources.internal_git_sources(
            user_id="u-1", org_id=org_id,
            session=_FakeSession(sources, tenants),
        )
    return _call


def test_every_source_carries_its_org_slug(call):
    out = call([_Row("s1", "org-1"), _Row("s2", "org-1")],
               [_Tenant("org-1", "acme")])
    assert [s["org_slug"] for s in out["sources"]] == ["acme", "acme"]


def test_the_slug_is_the_owning_org_not_the_caller_s(call):
    """Two orgs on one host must not be handed each other's clone path."""
    out = call([_Row("s1", "org-2")],
               [_Tenant("org-1", "acme"), _Tenant("org-2", "globex")],
               org_id="org-2")
    assert out["sources"][0]["org_slug"] == "globex"


def test_an_org_with_no_tenant_row_falls_back_to_the_default_slug(call):
    """Mirrors /api/internal/config: never emit a null slug, which would make
    the embedder join None into a path."""
    import main
    out = call([_Row("s1", "org-orphan")], [], org_id="org-orphan")
    assert out["sources"][0]["org_slug"] == main.DEFAULT_TENANT_SLUG


def test_no_sources_still_returns_cleanly(call):
    assert call([], [_Tenant("org-1", "acme")]) == {"sources": []}


def test_the_field_names_match_what_the_embedder_reads():
    """The embedder resolves a clone from `org_slug`; a rename here that the
    embedder doesn't follow puts every checkout back out of reach."""
    import inspect
    from routers import git_sources
    src = inspect.getsource(git_sources.internal_git_sources)
    assert '"org_slug"' in src


def test_both_internal_endpoints_agree_on_the_key():
    import inspect
    import main
    from routers import git_sources
    for fn in (git_sources.internal_git_sources, main.get_internal_config):
        assert '"org_slug"' in inspect.getsource(fn), fn.__name__
