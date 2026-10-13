"""A real restore, supervised: progress through the phases, and two cancels.

test_org_snapshot_bcdr.py proves the round-trip carries the data. This proves
the operator can watch it happen and stop it — and, more importantly, that
what they are told afterwards is true. The two cancels are the point: one
before the database transaction commits, which leaves the org exactly as it
was, and one after, which does not. A UI that reported those the same way
would send someone away from a half-restored org believing it was untouched.

memory-api and the embedder aren't running under the unit harness, so httpx
is stubbed — the same recorder the BCDR test uses.
"""
import json

import pytest

from restore_jobs import RestoreCancelled, RestoreControl


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def bundle(monkeypatch, tmp_path):
    """A real encrypted bundle for org RSUP, with several memories and wiki
    files so the per-item phases have boundaries to be interrupted at.

    Its own org id, deliberately: these tests restore over the org repeatedly
    and would otherwise leave rows behind for whoever shares the fixture DB.
    """
    import org_backup
    from config_db import (SessionLocal, Tenant, UserAccount, WikiRevision,
                           TenantSettings, SoftDeletedPage)

    monkeypatch.setattr(org_backup, "_org_backup_dir", lambda slug: tmp_path / slug)

    posted = {"memories": [], "org_import": 0}
    memories = [
        {"id": f"mem-{i}", "content": f"memory {i}", "tags": [], "type": "fact",
         "source": "claude"} for i in range(6)
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        if "/internal/org-export" in url:
            return _Resp(200, {"tables": {"board_columns": [], "cards": [],
                                          "memory_hot": [], "memory_revisions": []}})
        if "/api/memories" in url:
            return _Resp(200, {"memories": memories})
        return _Resp(200, {})

    def fake_post(url, json=None, headers=None, timeout=None):
        if "/internal/org-import" in url:
            posted["org_import"] += 1
            return _Resp(200, {"restored": {}})
        if url.endswith("/api/memories"):
            posted["memories"].append(json.get("id"))
        return _Resp(201, {})

    monkeypatch.setattr(org_backup.httpx, "get", fake_get)
    monkeypatch.setattr(org_backup.httpx, "post", fake_post)

    # Object storage isn't running here, so stand in for it at both ends: the
    # snapshot needs a real wiki.tar.gz to put in the bundle, and the restore
    # needs somewhere to write the files back to. Without this the wiki phase
    # has nothing to iterate and the tests that interrupt it prove nothing.
    wiki_dir = tmp_path / "wiki-archives"
    wiki_dir.mkdir()
    wiki_files = {f"AI/page-{i}.md": f"# page {i}".encode() for i in range(4)}

    def fake_build_wiki_archive(slug):
        import io
        import tarfile
        archive = wiki_dir / f"wiki-{slug}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name, blob in wiki_files.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tf.addfile(info, io.BytesIO(blob))
        return {"filename": archive.name, "file_count": len(wiki_files)}

    written: dict = {}

    class _S3Stub:
        def provision_wiki(self, slug, seed_welcome=True):
            return None

    monkeypatch.setattr(org_backup, "_build_wiki_archive", fake_build_wiki_archive)
    monkeypatch.setattr(org_backup, "_wiki_backup_dir", lambda slug: wiki_dir)
    monkeypatch.setattr(org_backup, "get_s3_storage", lambda: _S3Stub())
    monkeypatch.setattr(org_backup, "_s3_write",
                        lambda org, key, data, *a, **kw: written.__setitem__(key, data))

    with SessionLocal() as s:
        s.query(Tenant).filter(Tenant.id == "RSUP").delete(synchronize_session=False)
        s.query(UserAccount).filter(UserAccount.org_id == "RSUP").delete(synchronize_session=False)
        s.query(WikiRevision).filter_by(tenant_id="RSUP").delete()
        s.query(SoftDeletedPage).filter_by(tenant_id="RSUP").delete()
        s.query(TenantSettings).filter_by(tenant_slug="rsuporg").delete()
        s.add(Tenant(id="RSUP", name="Rsup Org", slug="rsuporg"))
        for i in range(3):
            s.add(UserAccount(id=f"rsu-{i}", org_id="RSUP", username=f"u{i}",
                              full_username=f"u{i}@rsuporg", auth_method="local"))
        s.add(WikiRevision(tenant_id="RSUP", path="AI/x.md", content="old"))
        org_backup._set_platform_backup_passcode(s, "test-pass-1234")
        s.commit()

    with SessionLocal() as s:
        meta = org_backup._create_org_snapshot("RSUP", s)
    path = (tmp_path / "rsuporg") / meta["filename"]
    assert path.exists()
    return {"path": path, "passcode": "test-pass-1234", "posted": posted,
            "memory_count": len(memories), "wiki_written": written,
            "wiki_count": len(wiki_files)}


def _restore(bundle, ctl):
    import org_backup
    from config_db import SessionLocal
    with SessionLocal() as s:
        return org_backup._restore_org_snapshot(bundle["path"], "RSUP", s,
                                          passcode=bundle["passcode"], ctl=ctl)


# ── progress ───────────────────────────────────────────────────────────────

def test_a_restore_walks_every_phase_and_finishes_at_a_hundred(bundle):
    seen = []
    ctl = RestoreControl(on_update=lambda s: seen.append(s["phase"]), emit_interval=0)
    result = _restore(bundle, ctl)

    assert result["status"] == "restored"
    assert ctl.phases_done == ["preparing", "database", "memories", "boards", "wiki"]
    assert ctl.percent == 100.0
    # The listener heard about every phase, not just the first and last.
    assert {"preparing", "database", "memories", "boards", "wiki"} <= set(seen)


def test_the_memories_phase_knows_how_many_there_are(bundle):
    """Without a total there is no bar and no ETA for the phase that takes
    almost all the time."""
    totals = {}

    def watch(snap):
        if snap["phase"] and snap["total"]:
            totals.setdefault(snap["phase"], snap["total"])

    _restore(bundle, RestoreControl(on_update=watch, emit_interval=0))
    assert totals["memories"] == bundle["memory_count"]
    assert totals["wiki"] == bundle["wiki_count"]
    assert totals["database"] > 0


def test_a_restore_with_no_supervisor_still_runs(bundle):
    """The default control is the no-op path; the CLI and the scheduler use it."""
    assert _restore(bundle, None)["status"] == "restored"


# ── cancelling before the commit ───────────────────────────────────────────

def _cancel_at(ctl, phase, after=0):
    """Cancel the first time `phase` has processed `after` items. Fires once:
    cancel() emits as well, and a watcher that re-entered on its own emit
    would recurse rather than test anything."""
    fired = []

    def watch(snap):
        if fired:
            return
        if snap["phase"] == phase and snap["processed"] >= after:
            fired.append(True)
            ctl.cancel()
    return watch


def test_cancelling_during_the_database_phase_leaves_the_org_untouched(bundle):
    from config_db import SessionLocal, Tenant, WikiRevision

    with SessionLocal() as s:
        s.query(Tenant).filter_by(id="RSUP").first().name = "Renamed Before Restore"
        s.commit()

    ctl = RestoreControl(emit_interval=0)
    ctl._on_update = _cancel_at(ctl, "database", after=1)
    with pytest.raises(RestoreCancelled):
        _restore(bundle, ctl)

    assert ctl.touched_org is False, "a pre-commit cancel must not claim a partial restore"
    with SessionLocal() as s:
        # The restore had already applied the bundle's org row in-session; the
        # rolled-back transaction is what puts it back.
        assert s.query(Tenant).filter_by(id="RSUP").first().name == "Renamed Before Restore"
        assert s.query(WikiRevision).filter_by(tenant_id="RSUP").count() == 1
    assert bundle["posted"]["memories"] == [], "memories were re-embedded after a cancel"


# ── cancelling after the commit ────────────────────────────────────────────

def test_cancelling_during_re_embedding_stops_but_admits_it_is_partial(bundle):
    ctl = RestoreControl(emit_interval=0)
    ctl._on_update = _cancel_at(ctl, "memories", after=2)
    with pytest.raises(RestoreCancelled):
        _restore(bundle, ctl)

    assert ctl.touched_org is True, "a post-commit cancel must own up to a partial restore"
    # It stopped where it was told to, rather than draining the queue.
    assert 0 < len(bundle["posted"]["memories"]) < bundle["memory_count"]
    assert bundle["posted"]["org_import"] == 0, "later phases ran after the cancel"


def test_cancelling_during_the_wiki_phase_does_not_look_like_a_wiki_failure(bundle):
    """The wiki loop swallows exceptions so a broken bucket doesn't fail the
    whole restore. A cancel must not be swallowed with them."""
    ctl = RestoreControl(emit_interval=0)
    ctl._on_update = _cancel_at(ctl, "wiki", after=1)
    with pytest.raises(RestoreCancelled):
        _restore(bundle, ctl)
    assert len(bundle["wiki_written"]) < bundle["wiki_count"]


# ── pausing ────────────────────────────────────────────────────────────────

def test_a_pause_mid_restore_holds_and_then_finishes_cleanly(bundle):
    import threading

    ctl = RestoreControl(emit_interval=0)
    held = threading.Event()

    def watch(snap):
        # Once only: resume() emits too, and a watcher keyed on the item count
        # alone would pause again the instant it was released.
        if held.is_set():
            return
        if snap["phase"] == "memories" and snap["processed"] == 2:
            held.set()
            ctl.pause()

    ctl._on_update = watch
    out = {}
    worker = threading.Thread(target=lambda: out.update(_restore(bundle, ctl)), daemon=True)
    worker.start()

    assert held.wait(5.0), "the restore never reached the memories phase"
    worker.join(0.5)
    assert worker.is_alive(), "the restore ran on through the pause"
    partial = len(bundle["posted"]["memories"])
    assert partial < bundle["memory_count"]

    ctl.resume()
    worker.join(10.0)
    assert not worker.is_alive(), "the restore did not resume"
    assert out.get("status") == "restored"
    assert len(bundle["posted"]["memories"]) == bundle["memory_count"]
