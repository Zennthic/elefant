"""The restore control: one honest percentage, and a stop button that works.

These are the parts the operator actually feels — a progress bar that doesn't
sit at 3% for four minutes and then jump to done, a pause that stops the
machine chewing, a cancel that takes effect and says what it left behind.
Pure object, no HTTP, no database, so the awkward cases (pause then cancel,
cancel while the worker is between items, a phase whose size isn't known
until it's already running) are pinned down properly.
"""
import threading
import time

import pytest

from restore_jobs import PHASES, RestoreCancelled, RestoreControl


def test_a_fresh_control_is_a_no_op_supervisor():
    """The restore has one code path whether or not anybody is watching, so
    the default control must never pause and never cancel."""
    ctl = RestoreControl()
    ctl.begin("memories", total=3)
    ctl.advance(); ctl.advance(); ctl.advance()
    assert not ctl.paused and not ctl.cancelled


# ── the percentage ─────────────────────────────────────────────────────────

def test_phase_weights_sum_to_a_hundred():
    """Otherwise a finished restore reports something other than 100%."""
    assert sum(weight for _name, _label, weight in PHASES) == 100


def test_percent_counts_finished_phases_and_the_current_one():
    ctl = RestoreControl()
    ctl.begin("preparing", total=1)
    ctl.advance()                       # 5% of the way through
    ctl.begin("database", total=10)
    assert ctl.percent == 5.0
    for _ in range(5):
        ctl.advance()
    assert ctl.percent == 10.0          # 5 + half of database's 10


def test_finish_takes_it_to_a_hundred():
    ctl = RestoreControl()
    for name, _label, _w in PHASES:
        ctl.begin(name, total=1)
        ctl.advance()
    ctl.finish()
    assert ctl.percent == 100.0


def test_a_phase_with_no_known_total_still_advances_the_bar_when_it_ends():
    """Some phases can't count their work up front. They must not stall the
    bar permanently, but they also must not claim progress they can't see."""
    ctl = RestoreControl()
    ctl.begin("preparing", total=0)
    assert ctl.percent == 0.0
    ctl.begin("database", total=0)
    assert ctl.percent == 5.0           # preparing's weight banked on exit


def test_re_entering_the_running_phase_only_revises_its_total():
    """The wiki phase can't count its files until it has opened the archive,
    so it begins twice. Charging its weight twice would push the bar past
    where the restore actually is."""
    ctl = RestoreControl()
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("wiki")
    ctl.begin("wiki", total=4)
    ctl.advance(); ctl.advance()
    assert ctl.phases_done == ["preparing"]
    assert ctl.percent == 15.0          # 5 + half of wiki's 20


def test_overshooting_a_total_does_not_exceed_the_phase():
    ctl = RestoreControl()
    ctl.begin("wiki", total=2)
    for _ in range(10):
        ctl.advance()
    assert ctl.percent == 20.0


# ── the ETA ────────────────────────────────────────────────────────────────

def test_no_eta_is_offered_before_the_estimate_means_anything():
    ctl = RestoreControl()
    ctl.begin("database", total=100)
    ctl.advance()
    assert ctl.eta_seconds is None


def test_an_eta_appears_once_there_is_enough_to_extrapolate_from():
    ctl = RestoreControl()
    ctl._started = time.monotonic() - 30      # 30s of work already done
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("database", total=10)
    for _ in range(5):
        ctl.advance()
    # 10% done in 30s ⇒ roughly 270s to go. Loose bound: it's an estimate.
    assert 200 < ctl.eta_seconds < 350


def test_time_spent_paused_does_not_inflate_the_eta():
    """Park a restore until the evening and the estimate you come back to
    should be the one you left, not one that counted the hours it was idle."""
    ctl = RestoreControl()
    ctl._started = time.monotonic() - 30
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("database", total=10)
    for _ in range(5):
        ctl.advance()
    before = ctl.eta_seconds

    # An hour on the shelf: the wall clock moves, the working clock must not.
    ctl._started -= 3600
    ctl.pause()
    ctl._paused_at -= 3600
    ctl.resume()

    assert abs(ctl.eta_seconds - before) < 20
    assert ctl.working_seconds < 60


# ── pause ──────────────────────────────────────────────────────────────────

def test_pause_blocks_the_worker_and_resume_releases_it():
    ctl = RestoreControl()
    ctl.begin("memories", total=100)
    ctl.pause()

    done = threading.Event()
    threading.Thread(target=lambda: (ctl.advance(), done.set()), daemon=True).start()

    assert not done.wait(0.4), "advance() returned while the job was paused"
    ctl.resume()
    assert done.wait(2.0), "advance() stayed blocked after resume"


def test_a_pause_does_not_lose_the_work_already_done():
    ctl = RestoreControl()
    ctl.begin("memories", total=10)
    ctl.advance(); ctl.advance()
    ctl.pause()
    assert ctl.processed == 2
    ctl.resume()
    ctl.advance()
    assert ctl.processed == 3


# ── cancel ─────────────────────────────────────────────────────────────────

def test_cancel_raises_at_the_next_item_boundary():
    ctl = RestoreControl()
    ctl.begin("memories", total=10)
    ctl.advance()
    ctl.cancel()
    with pytest.raises(RestoreCancelled):
        ctl.advance()
    assert ctl.processed == 1, "the cancelled item must not be counted as done"


def test_cancel_wakes_a_paused_worker_instead_of_waiting_for_a_resume():
    """A restore paused overnight still has to be cancellable."""
    ctl = RestoreControl()
    ctl.begin("memories", total=10)
    ctl.pause()

    raised = threading.Event()

    def worker():
        try:
            ctl.advance()
        except RestoreCancelled:
            raised.set()

    threading.Thread(target=worker, daemon=True).start()
    time.sleep(0.2)
    ctl.cancel()
    assert raised.wait(2.0), "a paused worker did not act on the cancel"


def test_checkpoint_alone_is_enough_to_stop_a_phase_with_no_items():
    ctl = RestoreControl()
    ctl.cancel()
    with pytest.raises(RestoreCancelled):
        ctl.checkpoint()


# ── which cancel was it ────────────────────────────────────────────────────

def test_a_cancel_before_the_commit_reports_an_untouched_org():
    """The whole editor-DB half is one transaction. Before it commits there
    is nothing to clean up, and the operator must be told exactly that."""
    ctl = RestoreControl()
    ctl.begin("database", total=5)
    ctl.advance()
    assert ctl.touched_org is False


def test_a_cancel_after_the_commit_reports_a_partial_restore():
    ctl = RestoreControl()
    ctl.begin("database", total=5)
    ctl.mark_committed()
    ctl.begin("memories", total=100)
    assert ctl.touched_org is True


def test_the_boundary_is_the_commit_not_the_phase_order():
    """Reordering the phases must not silently change what a cancel claims —
    which it would if 'has it touched the org' were inferred from position."""
    ctl = RestoreControl()
    ctl.begin("wiki", total=3)      # a late phase, reached without a commit
    ctl.advance()
    assert ctl.touched_org is False


# ── reporting ──────────────────────────────────────────────────────────────

def test_updates_are_throttled_but_phase_changes_always_get_through():
    """A restore advances thousands of times; the listener must not be asked
    to write thousands of times. A new phase is news, so it is never dropped."""
    seen = []
    ctl = RestoreControl(on_update=seen.append, emit_interval=60)
    ctl.begin("memories", total=1000)
    for _ in range(50):
        ctl.advance()
    assert len(seen) == 1, "per-item advances leaked through the throttle"
    ctl.begin("wiki", total=2)
    assert len(seen) == 2


def test_a_failing_listener_never_fails_the_restore():
    def explode(_snap):
        raise RuntimeError("the progress sink is down")
    ctl = RestoreControl(on_update=explode)
    ctl.begin("memories", total=1)
    ctl.advance()                       # must not raise


def test_the_snapshot_carries_what_the_ui_needs():
    ctl = RestoreControl()
    ctl.begin("memories", total=4)
    ctl.advance()
    snap = ctl.snapshot()
    assert snap["phase"] == "memories"
    assert snap["phase_label"] == "Re-embedding memories"
    assert (snap["processed"], snap["total"]) == (1, 4)
    assert snap["paused"] is False and snap["cancelled"] is False
