"""The job registry: what a browser finds when it comes back.

A restore outlives the tab that started it, so the record of it has to outlive
the tab too. These cover the three ways an operator loses their handle on a
job — refreshing the page, closing it entirely, and the service restarting
underneath them — plus the staged-bundle housekeeping, because a staged
bundle is a full org backup sitting on disk waiting for a confirmation that
may never come.
"""
import time
from datetime import datetime

import pytest

from restore_jobs import (RestoreControl, RestoreJobRegistry, sweep_staging)


@pytest.fixture
def reg():
    # config_db binds SessionLocal lazily; importing main is what runs
    # init_db() in this harness, the same as every other editor test.
    import main  # noqa: F401
    import config_db
    with config_db.SessionLocal() as s:
        s.query(config_db.OrgRestoreJob).delete()
        s.commit()
    return RestoreJobRegistry(session_factory=config_db.SessionLocal,
                              flush_interval=0.0)


def _preview(slug="srcorg", creates=False):
    return {"bundle": {"org_slug": slug, "memory_count": 7}, "creates_org": creates,
            "encrypted": True, "effects": ["…"]}


# ── the record ─────────────────────────────────────────────────────────────

def test_a_previewed_restore_is_staged_and_has_not_run(reg):
    reg.create("job1", "ORG", org_slug="acme", source_label="org-backup-1",
               preview=_preview())
    job = reg.get("job1")
    assert job["status"] == "staged"
    assert job["preview"]["bundle"]["memory_count"] == 7
    assert job["started_at"] is None


def test_the_passcode_is_held_in_memory_and_never_in_the_record(reg):
    """The bundle is encrypted at rest precisely so the passcode isn't lying
    around; writing it into the job row would undo that."""
    reg.create("job1", "ORG", preview=_preview(), passcode="hunter2")
    assert reg.passcode("job1") == "hunter2"
    assert "hunter2" not in str(reg.get("job1"))


def test_an_unknown_job_is_absent_rather_than_an_error(reg):
    assert reg.get("nope") is None


# ── live state wins over the stored copy ───────────────────────────────────

def test_a_running_job_reports_the_live_control_not_the_last_flush(reg):
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("memories", total=100)
    for _ in range(50):
        ctl.advance()

    job = reg.get("job1")
    assert job["phase"] == "memories"
    assert job["processed"] == 50
    assert job["percent"] == 35.0       # 5 + half of memories' 60
    assert job["status"] == "running"


def test_a_paused_job_says_paused_the_instant_it_is_paused(reg):
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    ctl.begin("memories", total=10)
    assert reg.pause("job1") is True
    assert reg.get("job1")["status"] == "paused"
    assert reg.resume("job1") is True
    assert reg.get("job1")["status"] == "running"


def test_progress_survives_the_control_going_away(reg):
    """Once the worker thread is gone the stored row is all there is, so the
    flush has to have kept up while it was running."""
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    ctl.begin("memories", total=10)
    for _ in range(4):
        ctl.advance()
    reg.progress("job1", ctl.snapshot(), force=True)
    reg.update("job1", status="error", error="embedder went away")
    reg.detach("job1")

    job = reg.get("job1")
    assert job["processed"] == 4 and job["phase"] == "memories"
    assert job["error"] == "embedder went away"


def test_flushing_is_throttled_so_a_restore_is_not_a_write_storm(reg):
    reg._flush_every = 60
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    ctl.begin("memories", total=1000)
    reg.progress("job1", ctl.snapshot(), force=True)
    for _ in range(500):
        ctl.advance()
        reg.progress("job1", ctl.snapshot())
    reg.detach("job1")
    assert reg.get("job1")["processed"] == 0, "the throttle let every item through"


# ── finding a job again ────────────────────────────────────────────────────

def test_the_active_list_is_how_a_refreshed_browser_reattaches(reg):
    reg.create("old", "ORG", status="done")
    reg.create("live", "ORG", status="running")
    active = reg.list(org_id="ORG", active_only=True)
    assert [j["job_id"] for j in active] == ["live"]


def test_listing_is_scoped_to_one_org(reg):
    reg.create("a", "ORG-A", status="running")
    reg.create("b", "ORG-B", status="running")
    assert [j["job_id"] for j in reg.list(org_id="ORG-A")] == ["a"]
    assert len(reg.list(active_only=True)) == 2


def test_finished_jobs_are_kept_as_the_record_of_a_destructive_act(reg):
    reg.create("job1", "ORG", status="running")
    reg.update("job1", status="done", result={"memories_restored": 3},
               finished_at=datetime.utcnow())
    job = reg.get("job1")
    assert job["status"] == "done"
    assert job["result"]["memories_restored"] == 3
    assert job["finished_at"]


# ── cancelling ─────────────────────────────────────────────────────────────

def test_cancelling_a_running_job_goes_through_the_control(reg):
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    assert reg.cancel("job1") is True
    assert ctl.cancelled is True
    assert reg.get("job1")["status"] == "cancelling"


def test_cancelling_a_staged_job_discards_it_and_says_nothing_changed(reg, tmp_path):
    staged = tmp_path / "bundle.zip"
    staged.write_bytes(b"x")
    reg.create("job1", "ORG", staged_path=staged, preview=_preview())
    assert reg.cancel("job1") is True
    job = reg.get("job1")
    assert job["status"] == "cancelled"
    assert "nothing was changed" in job["error"]
    assert not staged.exists(), "the staged bundle was left on disk"


def test_a_finished_job_cannot_be_cancelled(reg):
    reg.create("job1", "ORG", status="done")
    assert reg.cancel("job1") is False


def test_a_cancel_cannot_overwrite_the_worker_s_own_verdict(reg):
    """The race that stranded jobs at "cancelling" forever (#122).

    `cancel()` wakes the worker and then stamps the row. The worker can get
    all the way to its terminal write in between — and that write is the one
    that matters, because it carries finished_at and the sentence saying
    whether the org was left half-restored. If the stamp lands on top of it
    the job reports "cancelling" with the control already detached, so nothing
    is left that could ever finish the transition.

    Simulated by doing the two writes in the losing order deliberately, which
    is the only way to make a thread race deterministic.
    """
    reg.create("job1", "ORG", status="running")
    ctl = RestoreControl()
    reg.attach("job1", ctl)
    # The worker beat the canceller to the row.
    reg.update("job1", status="cancelled", finished_at=datetime.utcnow(),
               error="Cancelled before anything was written — this org is unchanged.")

    assert reg.cancel("job1") is True, "the cancel was still honoured"
    job = reg.get("job1")
    assert job["status"] == "cancelled", "the stamp overwrote a finished job"
    assert "unchanged" in job["error"], "the operator lost the answer they cancelled for"


def test_a_cancel_racing_a_successful_restore_cannot_unfinish_it(reg):
    """Same window, against the happy path: a cancel arriving as a restore
    lands must not turn a completed restore into one that reports cancelling.
    """
    reg.create("job1", "ORG", status="running")
    reg.attach("job1", RestoreControl())
    reg.update("job1", status="done", finished_at=datetime.utcnow())
    reg.cancel("job1")
    assert reg.get("job1")["status"] == "done"


def test_pause_and_resume_cannot_restart_a_finished_job_either(reg):
    """The same unguarded write, on the two other steering verbs."""
    for verb, status in ((reg.pause, "paused"), (reg.resume, "running")):
        reg.create(f"job-{status}", "ORG", status="running")
        reg.attach(f"job-{status}", RestoreControl())
        reg.update(f"job-{status}", status="done", finished_at=datetime.utcnow())
        verb(f"job-{status}")
        assert reg.get(f"job-{status}")["status"] == "done", (
            f"{verb.__name__} moved a finished job to {status}")


def test_pausing_a_job_this_process_is_not_running_is_refused(reg):
    """Nothing here can reach a thread in another process, and pretending
    otherwise would show the operator a paused job that is still running."""
    reg.create("job1", "ORG", status="running")
    assert reg.pause("job1") is False


# ── the service restarting ─────────────────────────────────────────────────

def test_reaping_tells_the_truth_about_a_restore_killed_mid_flight(reg):
    reg.create("job1", "ORG", status="running")
    assert reg.reap_orphans() == 1
    job = reg.get("job1")
    assert job["status"] == "error"
    assert "service restart" in job["error"]
    assert job["finished_at"]


def test_reaping_distinguishes_a_staged_job_from_a_running_one(reg, tmp_path):
    """A job that never started changed nothing; saying it was 'interrupted'
    would send an operator looking for damage that isn't there."""
    staged = tmp_path / "bundle.zip"
    staged.write_bytes(b"x")
    reg.create("job1", "ORG", staged_path=staged, preview=_preview())
    reg.reap_orphans()
    job = reg.get("job1")
    assert job["status"] == "cancelled"
    assert "nothing was changed" in job["error"]
    assert not staged.exists()


def test_reaping_leaves_finished_jobs_alone(reg):
    reg.create("job1", "ORG", status="done")
    assert reg.reap_orphans() == 0
    assert reg.get("job1")["status"] == "done"


# ── staged bundles on disk ─────────────────────────────────────────────────

def test_abandoned_staged_bundles_are_swept(tmp_path):
    old = tmp_path / "abandoned"; old.write_bytes(b"a full org backup")
    fresh = tmp_path / "just-uploaded"; fresh.write_bytes(b"b")
    import os
    os.utime(old, (time.time() - 48 * 3600, time.time() - 48 * 3600))

    assert sweep_staging(tmp_path, max_age_hours=24) == 1
    assert not old.exists() and fresh.exists()


def test_sweeping_a_directory_that_was_never_used_is_harmless(tmp_path):
    assert sweep_staging(tmp_path / "missing") == 0
