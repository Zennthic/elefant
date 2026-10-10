"""The restore API: nothing runs until the operator says so.

The old flow had one shape — POST a file, the restore is already happening.
These cover the shape that replaced it: analyse and stage, show what it would
do, and only then start. Plus the controls that a several-minute job needs to
be liveable (pause, resume, cancel) and the listing that a refreshed browser
uses to find a job whose id it no longer has.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def admin_cookie():
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_session_token
    db = next(get_db_session())
    oid, uid = secrets.token_hex(8), secrets.token_hex(8)
    slug = f"padm-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    db.add(UserAccount(id=uid, org_id=oid, username="root",
                       full_username=f"root@{slug}.test", role="platform_admin",
                       is_active=True, auth_method="local"))
    db.commit()
    tok = make_session_token(db.get(UserAccount, uid))
    db.close()
    return {"elefant_session": tok}


@pytest.fixture
def org():
    from config_db import get_db_session, Tenant
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"rst-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    db.close()
    return {"id": oid, "slug": slug}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A valid encrypted bundle for `org`, built without touching S3."""
    import org_backup
    from config_db import get_db_session

    monkeypatch.setattr(org_backup, "_org_backup_dir", lambda slug: tmp_path / slug)
    monkeypatch.setattr(org_backup, "_build_wiki_archive",
                        lambda slug: (_ for _ in ()).throw(RuntimeError("no s3 here")))

    class _Resp:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {"memories": [], "tables": {}}

    monkeypatch.setattr(org_backup.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(org_backup.httpx, "post", lambda *a, **k: _Resp())
    # The preview counts what is live alongside what is in the bundle. Object
    # storage is not running here, and waiting out its connect timeout per
    # preview would dominate the suite.
    monkeypatch.setattr(org_backup, "_live_wiki_file_count", lambda org: 0)

    def _build(org_id, org_slug):
        db = next(get_db_session())
        org_backup._set_platform_backup_passcode(db, "test-pass-1234")
        db.commit()
        meta = org_backup._create_org_snapshot(org_id, db)
        db.close()
        return (tmp_path / org_slug) / meta["filename"]
    return _build


def _preview(client, cookie, org, path, passcode="test-pass-1234"):
    with open(path, "rb") as fh:
        return client.post(
            f"/api/platform/orgs/{org['id']}/restore-preview",
            files={"file": (path.name, fh, "application/zip")},
            data={"passcode": passcode},
            cookies=cookie,
        )


# ── the preview ────────────────────────────────────────────────────────────

def test_previewing_stages_the_restore_without_running_it(client, admin_cookie, org, bundle):
    r = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "staged"
    assert body["preview"]["bundle"]["org_slug"] == org["slug"]

    job = client.get(f"/api/platform/restore-jobs/{body['job_id']}", cookies=admin_cookie).json()
    assert job["status"] == "staged" and job["started_at"] is None


def test_the_preview_spells_out_every_area_and_mode(client, admin_cookie, org, bundle):
    """An operator gets to read what each choice does before they commit, not
    after — including the modes they have not picked yet."""
    body = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()
    effects = body["preview"]["area_effects"]
    assert "DELETED" in effects["wiki:replace"], "replace must name what it destroys"
    assert "merge" in effects["identity:merge"].lower()
    assert "DELETED" in effects["boards:replace"]
    assert "semantic search" in body["preview"]["embed_effects"]["later"]


def test_the_preview_sizes_each_area_both_ways(client, admin_cookie, org, bundle):
    """"Replace" is only judgeable next to what is live now."""
    body = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()
    areas = {a["area"]: a for a in body["preview"]["areas"]}
    assert set(areas) == {"identity", "wiki", "boards", "memories"}
    assert areas["identity"]["live_now"] is not None
    assert areas["wiki"]["modes"] == ["merge", "replace"]
    assert areas["boards"]["modes"] == ["replace"], "boards have no merge mode"
    assert areas["identity"]["modes"] == ["merge"], "identity must never wipe"


def test_the_preview_counts_what_is_there_now_alongside_what_is_coming(
        client, admin_cookie, org, bundle):
    body = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()
    p = body["preview"]
    assert p["target"]["org_slug"] == org["slug"]
    assert p["target"]["user_count"] is not None
    assert p["bundle"]["user_count"] is not None
    assert p["same_org"] is True


def test_a_backup_taken_over_http_can_be_previewed_by_id(client, admin_cookie, org, bundle):
    """The operator's round trip, all through the routes: take an org backup,
    then preview restoring that stored backup. It crosses both halves of the
    backup router and the org_backup machinery behind them."""
    import org_backup
    from config_db import get_db_session
    db = next(get_db_session())
    org_backup._set_platform_backup_passcode(db, "test-pass-1234")
    db.commit(); db.close()

    r = client.post(f"/api/platform/orgs/{org['id']}/backup", cookies=admin_cookie)
    assert r.status_code == 202, r.text
    backup_id = r.json()["id"]

    r = client.post(f"/api/platform/orgs/{org['id']}/restore-preview",
                    data={"backup_id": backup_id}, cookies=admin_cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "staged"
    assert body["preview"]["bundle"]["org_slug"] == org["slug"]
    assert body["preview"]["same_org"] is True


def test_a_bundle_from_another_org_is_called_out(client, admin_cookie, org, bundle):
    from config_db import get_db_session, Tenant
    other_id = secrets.token_hex(8)
    other_slug = f"oth-{other_id[:6]}"
    db = next(get_db_session())
    db.add(Tenant(id=other_id, name=other_slug, slug=other_slug, is_active=True))
    db.commit(); db.close()

    path = bundle(other_id, other_slug)
    body = _preview(client, admin_cookie, org, path).json()
    assert body["preview"]["same_org"] is False
    assert any("DIFFERENT org" in w for w in body["preview"]["warnings"])


def test_a_wrong_passcode_is_refused_at_preview_not_mid_restore(
        client, admin_cookie, org, bundle):
    r = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"]),
                 passcode="not-the-passcode")
    assert r.status_code == 400
    assert not client.get(
        f"/api/platform/restore-jobs?org_id={org['id']}",
        cookies=admin_cookie).json()["jobs"], "a bundle we couldn't read was still staged"


def test_something_that_is_not_a_backup_is_rejected(client, admin_cookie, org, tmp_path):
    junk = tmp_path / "holiday.jpg"
    junk.write_bytes(b"not a backup")
    r = _preview(client, admin_cookie, org, junk)
    assert r.status_code == 400
    assert "valid org backup" in r.json()["detail"]


def test_discarding_a_staged_restore_changes_nothing(client, admin_cookie, org, bundle):
    job_id = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()["job_id"]
    r = client.post(f"/api/platform/restore-jobs/{job_id}/cancel", cookies=admin_cookie)
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "cancelled"
    assert "nothing was changed" in job["error"]


def test_a_discarded_restore_cannot_then_be_started(client, admin_cookie, org, bundle):
    job_id = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()["job_id"]
    client.post(f"/api/platform/restore-jobs/{job_id}/cancel", cookies=admin_cookie)
    r = client.post(f"/api/platform/restore-jobs/{job_id}/start", cookies=admin_cookie)
    assert r.status_code == 409


# ── starting, and steering ─────────────────────────────────────────────────

@pytest.fixture
def held_restore(monkeypatch):
    """Freeze the restore inside its first phase so the running-job endpoints
    have something real to act on."""
    import org_backup
    import threading
    gate = threading.Event()

    def _slow(bundle_path, target_org_id, session, passcode=None, ctl=None, plan=None):
        ctl.begin("preparing", total=1)
        ctl.advance()
        ctl.begin("memories", total=1000)
        while not gate.is_set():
            ctl.advance()          # the checkpoint a pause/cancel acts on
            gate.wait(0.01)
        return {"status": "restored", "org_slug": "x", "memories_restored": 0}

    monkeypatch.setattr(org_backup, "_restore_org_snapshot", _slow)
    yield gate
    gate.set()


def _start(client, cookie, org, bundle):
    job_id = _preview(client, cookie, org, bundle(org["id"], org["slug"])).json()["job_id"]
    r = client.post(f"/api/platform/restore-jobs/{job_id}/start", cookies=cookie)
    assert r.status_code == 202, r.text
    return job_id


def _await_status(client, cookie, job_id, wanted, tries=200):
    import time
    for _ in range(tries):
        job = client.get(f"/api/platform/restore-jobs/{job_id}", cookies=cookie).json()
        if job["status"] in wanted:
            return job
        time.sleep(0.02)
    pytest.fail(f"job stayed at {job['status']!r}, never reached {wanted}")


def _await_progress(client, cookie, job_id, tries=200):
    """Wait for the job to report progress, and hand back the job that does.

    "running" is written by the worker BEFORE the restore function is entered,
    so there is a real window — widened by a loaded machine — where the status
    is running and the control has not begun a phase yet. Sampling once inside
    that window reads 0% and says the restore is not moving, when all that has
    happened is that the test looked first. Waiting for the condition is what
    the assertion means; asserting it instantly is a race.
    """
    import time
    _await_status(client, cookie, job_id, {"running"})
    for _ in range(tries):
        job = client.get(f"/api/platform/restore-jobs/{job_id}", cookies=cookie).json()
        if job["percent"] > 0:
            return job
        time.sleep(0.02)
    pytest.fail(f"job reached {job['status']!r} but never reported progress")


def test_starting_reports_progress_that_moves(client, admin_cookie, org, bundle, held_restore):
    job_id = _start(client, admin_cookie, org, bundle)
    job = _await_progress(client, admin_cookie, job_id)
    assert job["phase"] in ("preparing", "memories")
    assert job["percent"] > 0


def test_a_running_restore_can_be_paused_and_resumed(
        client, admin_cookie, org, bundle, held_restore):
    job_id = _start(client, admin_cookie, org, bundle)
    _await_status(client, admin_cookie, job_id, {"running"})

    paused = client.post(f"/api/platform/restore-jobs/{job_id}/pause", cookies=admin_cookie)
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    held = client.get(f"/api/platform/restore-jobs/{job_id}", cookies=admin_cookie).json()
    import time
    time.sleep(0.15)
    again = client.get(f"/api/platform/restore-jobs/{job_id}", cookies=admin_cookie).json()
    assert again["processed"] == held["processed"], "a paused restore kept working"

    resumed = client.post(f"/api/platform/restore-jobs/{job_id}/resume", cookies=admin_cookie)
    assert resumed.json()["status"] == "running"


def test_cancelling_a_running_restore_stops_it_and_says_what_it_left(
        client, admin_cookie, org, bundle, held_restore):
    job_id = _start(client, admin_cookie, org, bundle)
    _await_status(client, admin_cookie, job_id, {"running"})
    assert client.post(f"/api/platform/restore-jobs/{job_id}/cancel",
                       cookies=admin_cookie).status_code == 200
    job = _await_status(client, admin_cookie, job_id, {"cancelled"})
    # This stub never commits, so the honest answer is "nothing was written".
    assert "unchanged" in job["error"]


def test_cancelling_in_the_queued_window_is_honoured(
        client, admin_cookie, org, bundle, held_restore):
    """A cancel that lands before the worker thread is scheduled must stop the
    job, not fall through to the discard path and delete the bundle out from
    under the thread that is about to open it."""
    job_id = _start(client, admin_cookie, org, bundle)
    assert client.post(f"/api/platform/restore-jobs/{job_id}/cancel",
                       cookies=admin_cookie).status_code == 200
    job = _await_status(client, admin_cookie, job_id, {"cancelled"})
    assert "unchanged" in job["error"]


def test_pausing_a_job_that_is_not_running_is_refused_rather_than_faked(
        client, admin_cookie, org, bundle):
    job_id = _preview(client, admin_cookie, org, bundle(org["id"], org["slug"])).json()["job_id"]
    r = client.post(f"/api/platform/restore-jobs/{job_id}/pause", cookies=admin_cookie)
    assert r.status_code == 409


def test_an_unknown_job_is_a_404(client, admin_cookie):
    assert client.get("/api/platform/restore-jobs/nope",
                      cookies=admin_cookie).status_code == 404


# ── reattaching ────────────────────────────────────────────────────────────

def test_a_running_restore_is_findable_by_org_when_the_job_id_is_lost(
        client, admin_cookie, org, bundle, held_restore):
    job_id = _start(client, admin_cookie, org, bundle)
    _await_progress(client, admin_cookie, job_id)

    listed = client.get(
        f"/api/platform/restore-jobs?active=true&org_id={org['id']}",
        cookies=admin_cookie).json()["jobs"]
    assert [j["job_id"] for j in listed] == [job_id]
    assert listed[0]["percent"] > 0, "the listing must carry live progress too"


def test_the_listing_is_scoped_to_the_org_asked_about(
        client, admin_cookie, org, bundle, held_restore):
    _start(client, admin_cookie, org, bundle)
    other = client.get("/api/platform/restore-jobs?active=true&org_id=no-such-org",
                       cookies=admin_cookie).json()["jobs"]
    assert other == []


# ── access ─────────────────────────────────────────────────────────────────

def test_restore_controls_are_platform_admin_only(client, org):
    """These endpoints move an org's data wholesale; an unauthenticated caller
    must not even be able to enumerate them."""
    for path, method in (
        (f"/api/platform/orgs/{org['id']}/restore-preview", "post"),
        ("/api/platform/restore-jobs", "get"),
        ("/api/platform/restore-jobs/abc123", "get"),
        ("/api/platform/restore-jobs/abc123/pause", "post"),
        ("/api/platform/restore-jobs/abc123/cancel", "post"),
        ("/api/platform/restore-jobs/abc123/start", "post"),
    ):
        r = getattr(client, method)(path, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403), f"{path} answered {r.status_code}"
