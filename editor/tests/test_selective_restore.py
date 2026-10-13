"""Selective and point-in-time restores, against a real bundle.

test_restore_plan.py proves the plan object refuses the wrong things. This
proves the restore obeys it — which is the half that matters, because a plan
that says "leave memories alone" and a restore that reverts them anyway is
worse than no plan at all.

The two properties under test are opposites and both are easy to get wrong:
an unselected area must be touched in NO way, and a `replace` area must end up
holding nothing the backup does not.
"""
import json

import pytest

from restore_jobs import RestorePlan


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A bundle for org SEL, plus recorders for everything a restore touches."""
    import org_backup
    from config_db import (SessionLocal, Tenant, UserAccount, WikiRevision,
                           TenantSettings, SoftDeletedPage)

    monkeypatch.setattr(org_backup, "_org_backup_dir", lambda slug: tmp_path / slug)

    seen = {"memories": [], "cleared": 0, "org_import": 0,
            "wiki_written": {}, "wiki_deleted": []}
    memories = [{"id": f"mem-{i}", "content": f"memory {i}", "tags": [],
                 "type": "fact", "source": "claude"} for i in range(3)]
    bundle_wiki = {f"AI/kept-{i}.md": f"# kept {i}".encode() for i in range(2)}
    # Live in the bucket but NOT in the backup — the file a point-in-time
    # restore has to remove and a merge has to leave alone.
    live_only = "AI/written-after-the-backup.md"

    def fake_get(url, params=None, headers=None, timeout=None):
        if "/internal/org-export" in url:
            return _Resp(200, {"tables": {"board_columns": [], "cards": [],
                                          "memory_hot": [], "memory_revisions": []}})
        if "/api/memories" in url:
            return _Resp(200, {"memories": memories})
        return _Resp(200, {})

    def fake_post(url, json=None, params=None, headers=None, timeout=None):
        if "/internal/org-import" in url:
            seen["org_import"] += 1
            return _Resp(200, {"restored": {"cards": 0}})
        if "/api/internal/memories/clear" in url:
            seen["cleared"] += 1
            return _Resp(200, {"deleted": 7})
        if url.endswith("/api/memories"):
            seen["memories"].append(json)
        return _Resp(201, {})

    monkeypatch.setattr(org_backup.httpx, "get", fake_get)
    monkeypatch.setattr(org_backup.httpx, "post", fake_post)

    wiki_dir = tmp_path / "wiki-archives"
    wiki_dir.mkdir()

    def fake_build_wiki_archive(slug):
        import io
        import tarfile
        archive = wiki_dir / f"wiki-{slug}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name, blob in bundle_wiki.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tf.addfile(info, io.BytesIO(blob))
        return {"filename": archive.name, "file_count": len(bundle_wiki)}

    class _S3Stub:
        def provision_wiki(self, slug, seed_welcome=True):
            return None

        def walk(self, slug, path=""):
            # What is in the bucket right now: the backup's pages plus one
            # written since.
            return list(bundle_wiki) + [live_only]

    monkeypatch.setattr(org_backup, "_build_wiki_archive", fake_build_wiki_archive)
    monkeypatch.setattr(org_backup, "_wiki_backup_dir", lambda slug: wiki_dir)
    monkeypatch.setattr(org_backup, "get_s3_storage", lambda: _S3Stub())
    monkeypatch.setattr(org_backup, "_s3_write",
                        lambda org, key, data, *a, **kw: seen["wiki_written"].__setitem__(key, data))
    monkeypatch.setattr(org_backup, "_s3_delete",
                        lambda org, key, *a, **kw: seen["wiki_deleted"].append(key))

    with SessionLocal() as s:
        s.query(Tenant).filter(Tenant.id == "SEL").delete(synchronize_session=False)
        s.query(UserAccount).filter(UserAccount.org_id == "SEL").delete(synchronize_session=False)
        s.query(WikiRevision).filter_by(tenant_id="SEL").delete()
        s.query(SoftDeletedPage).filter_by(tenant_id="SEL").delete()
        s.query(TenantSettings).filter_by(tenant_slug="selorg").delete()
        s.add(Tenant(id="SEL", name="Backup Era Name", slug="selorg"))
        s.add(UserAccount(id="sel-u1", org_id="SEL", username="jon",
                          full_username="jon@selorg", auth_method="local"))
        s.add(WikiRevision(tenant_id="SEL", path="AI/x.md", content="from the backup"))
        org_backup._set_platform_backup_passcode(s, "test-pass-1234")
        s.commit()

    with SessionLocal() as s:
        meta = org_backup._create_org_snapshot("SEL", s)
    return {"path": (tmp_path / "selorg") / meta["filename"], "seen": seen,
            "memories": memories, "bundle_wiki": bundle_wiki, "live_only": live_only}


def _restore(world, **plan_kw):
    import org_backup
    from config_db import SessionLocal
    plan = RestorePlan.parse(plan_kw) if plan_kw else None
    with SessionLocal() as s:
        return org_backup._restore_org_snapshot(world["path"], "SEL", s,
                                          passcode="test-pass-1234", plan=plan)


def _drift(name="Renamed Since The Backup"):
    """Move the org on from the backup, so a restore that touches identity is
    visible and one that doesn't is equally visible."""
    from config_db import SessionLocal, Tenant, UserAccount, WikiRevision
    with SessionLocal() as s:
        s.query(Tenant).filter_by(id="SEL").first().name = name
        s.add(UserAccount(id="sel-new", org_id="SEL", username="newhire",
                          full_username="newhire@selorg", auth_method="local"))
        s.query(WikiRevision).filter_by(tenant_id="SEL").delete()
        s.commit()


# ── an unselected area is not touched at all ───────────────────────────────

def test_a_wiki_only_restore_leaves_identity_alone(world):
    from config_db import SessionLocal, Tenant, UserAccount
    _drift()
    _restore(world, areas=["wiki"])
    with SessionLocal() as s:
        org = s.query(Tenant).filter_by(id="SEL").first()
        assert org.name == "Renamed Since The Backup", \
            "a wiki-only restore reverted the org's own row"
        assert s.query(UserAccount).filter_by(id="sel-new").first() is not None, \
            "a wiki-only restore should not touch accounts"


def test_a_wiki_only_restore_does_put_page_history_back(world):
    """The point of selecting the wiki is that the wiki IS restored."""
    from config_db import SessionLocal, WikiRevision
    _drift()
    _restore(world, areas=["wiki"])
    with SessionLocal() as s:
        assert s.query(WikiRevision).filter_by(tenant_id="SEL").count() == 1


def test_an_identity_only_restore_does_not_touch_the_wiki_or_memories(world):
    from config_db import SessionLocal, WikiRevision
    _drift()
    _restore(world, areas=["identity"])
    assert world["seen"]["wiki_written"] == {}, "wiki files were written anyway"
    assert world["seen"]["memories"] == [], "memories were re-embedded anyway"
    with SessionLocal() as s:
        assert s.query(WikiRevision).filter_by(tenant_id="SEL").count() == 0, \
            "page history was restored by a plan that excluded the wiki"


def test_an_identity_only_restore_does_restore_the_org_row(world):
    from config_db import SessionLocal, Tenant
    _drift()
    _restore(world, areas=["identity"])
    with SessionLocal() as s:
        assert s.query(Tenant).filter_by(id="SEL").first().name == "Backup Era Name"


def test_leaving_boards_out_skips_the_memory_api_import_entirely(world):
    """That import DELETES the tenant's cards before inserting, so calling it
    with an empty table set would wipe the board rather than skip it."""
    _restore(world, areas=["memories"])
    assert world["seen"]["org_import"] == 0


def test_leaving_memories_out_skips_them(world):
    _restore(world, areas=["wiki"])
    assert world["seen"]["memories"] == []


# ── replace really does delete what the backup lacks ───────────────────────

def test_merging_the_wiki_leaves_pages_written_since_the_backup(world):
    _restore(world, areas=["wiki"], modes={"wiki": "merge"})
    assert world["seen"]["wiki_deleted"] == []
    assert set(world["seen"]["wiki_written"]) == set(world["bundle_wiki"])


def test_a_point_in_time_wiki_restore_removes_what_is_not_in_the_backup(world):
    result = _restore(world, areas=["wiki"], modes={"wiki": "replace"})
    assert world["seen"]["wiki_deleted"] == [world["live_only"]]
    assert result["wiki_files_removed"] == 1
    # And it still wrote everything the backup did hold.
    assert set(world["seen"]["wiki_written"]) == set(world["bundle_wiki"])


def test_a_point_in_time_wiki_restore_never_deletes_what_it_just_wrote(world):
    """Strays are the live set minus what was actually restored — so a page
    the bundle contains can never end up in the delete list, however the two
    sets were gathered."""
    _restore(world, areas=["wiki"], modes={"wiki": "replace"})
    for key in world["bundle_wiki"]:
        assert key not in world["seen"]["wiki_deleted"]


def test_a_wiki_write_failure_cancels_the_deletions_entirely(world, monkeypatch):
    """The dangerous combination is a wiki that could not be fully written and
    then got pruned anyway — half a restore, with the other half deleted. A
    failure has to take the deletions down with it."""
    import org_backup
    written = world["seen"]["wiki_written"]

    def flaky_write(org, key, data, *a, **kw):
        if key == sorted(world["bundle_wiki"])[0]:
            raise RuntimeError("object storage hiccup")
        written[key] = data

    monkeypatch.setattr(org_backup, "_s3_write", flaky_write)
    result = _restore(world, areas=["wiki"], modes={"wiki": "replace"})
    assert world["seen"]["wiki_deleted"] == []
    assert result["wiki_files_removed"] == 0


def test_a_wiki_failure_is_reported_and_not_just_logged(world, monkeypatch):
    """Being told "restored" after a partial wiki pass is a materially worse
    thing to be told once point-in-time is on the table."""
    import org_backup
    monkeypatch.setattr(org_backup, "_s3_write",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bucket gone")))
    result = _restore(world, areas=["wiki"], modes={"wiki": "replace"})
    assert result["wiki_error"] and "bucket gone" in result["wiki_error"]


def test_a_clean_wiki_restore_reports_no_error(world):
    assert _restore(world, areas=["wiki"])["wiki_error"] is None


def test_merging_memories_does_not_clear_the_corpus_first(world):
    _restore(world, areas=["memories"], modes={"memories": "merge"})
    assert world["seen"]["cleared"] == 0
    assert len(world["seen"]["memories"]) == len(world["memories"])


def test_a_point_in_time_memory_restore_clears_before_it_writes(world):
    result = _restore(world, areas=["memories"], modes={"memories": "replace"})
    assert world["seen"]["cleared"] == 1
    assert result["memories_cleared"] == 7
    assert len(world["seen"]["memories"]) == len(world["memories"])


def test_a_failed_clear_aborts_rather_than_silently_merging(world, monkeypatch):
    """Carrying on would merge into a corpus the operator asked to have
    replaced, and report it as a point-in-time restore."""
    import org_backup

    def boom(url, json=None, params=None, headers=None, timeout=None):
        if "/api/internal/memories/clear" in url:
            return _Resp(500, {"detail": "qdrant is down"})
        return _Resp(201, {})

    monkeypatch.setattr(org_backup.httpx, "post", boom)
    with pytest.raises(RuntimeError, match="clear existing memories"):
        _restore(world, areas=["memories"], modes={"memories": "replace"})
    assert world["seen"]["memories"] == [], "it wrote memories after the clear failed"


# ── deferred embedding ─────────────────────────────────────────────────────

def test_embedding_is_requested_by_default(world):
    _restore(world, areas=["memories"])
    assert all(m["embed"] is True for m in world["seen"]["memories"])


def test_deferring_tells_the_embedder_not_to_embed(world):
    _restore(world, areas=["memories"], embed_memories=False)
    assert world["seen"]["memories"], "no memories were restored at all"
    assert all(m["embed"] is False for m in world["seen"]["memories"])


def test_the_result_counts_the_debt_a_deferred_restore_leaves(world):
    """A number the operator can act on, not a footnote they can miss."""
    result = _restore(world, areas=["memories"], embed_memories=False)
    assert result["embeddings_pending"] == len(world["memories"])


def test_embedding_now_leaves_no_debt(world):
    assert _restore(world, areas=["memories"])["embeddings_pending"] == 0


def test_deferring_still_restores_every_memory(world):
    """Speed is the point; losing memories is not an acceptable way to get it."""
    result = _restore(world, areas=["memories"], embed_memories=False)
    assert result["memories_restored"] == len(world["memories"])


# ── the plan is recorded with the result ───────────────────────────────────

def test_the_result_says_what_plan_produced_it(world):
    result = _restore(world, areas=["wiki"], modes={"wiki": "replace"})
    assert result["plan"]["areas"] == ["wiki"]
    assert result["plan"]["modes"]["wiki"] == "replace"


def test_no_plan_still_restores_everything(world):
    """Every pre-existing caller passes no plan and must keep working."""
    result = _restore(world)
    assert world["seen"]["org_import"] == 1
    assert len(world["seen"]["memories"]) == len(world["memories"])
    assert set(world["seen"]["wiki_written"]) == set(world["bundle_wiki"])
    assert world["seen"]["wiki_deleted"] == [], "a default restore must not delete"
    assert result["plan"]["areas"] == ["identity", "wiki", "boards", "memories"]
