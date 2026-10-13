"""The platform-admin restore UI: review first, and never lose the job.

The regression these guard against is the one the feature exists to fix — a
restore that began the instant a file was chosen. So the checks are mostly
about what is NOT wired up any more: no entry point may call the one-shot
restore endpoint, and no entry point may go straight from a file picker to a
running job.
"""
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "static" / "platform_admin.html"


@pytest.fixture(scope="module")
def html():
    return PAGE.read_text()


def _fn(html, name):
    """The body of one JS function, so a check about it can't be satisfied by
    a coincidence elsewhere on a 1,500-line page."""
    start = html.index(f"function {name}(")
    return html[start:html.index("\n}\n", start)]


# ── nothing starts on file choice ──────────────────────────────────────────

@pytest.mark.parametrize("entry", [
    "restoreOrgBackup", "handleRestoreUpload", "handleImportOrgUpload",
])
def test_every_entry_point_stages_for_review_instead_of_restoring(html, entry):
    body = _fn(html, entry)
    assert "stageRestore(" in body
    assert "restore-from-backup" not in body, f"{entry} still starts a restore outright"
    assert "restore-from-upload" not in body


def test_the_one_shot_endpoints_are_gone_from_the_page(html):
    """They survive on the server for CLI and automation callers; the browser
    must not use them, or the review step is one click away from bypassed."""
    assert "/restore-from-backup" not in html
    assert "/restore-from-upload" not in html


def test_the_upload_control_no_longer_promises_an_immediate_restore(html):
    assert "Upload &amp; review restore" in html


# ── the review step ────────────────────────────────────────────────────────

def test_review_shows_the_bundle_the_target_and_the_consequences(html):
    body = _fn(html, "renderRestoreReview")
    # Where the bundle came from and when — the header.
    for field in ("b.org_name", "b.created_at", "b.size_bytes", "b.version"):
        assert field in body, f"the review never shows {field}"
    # How big each area is, in the bundle and live, so a mode can be judged.
    assert "a.in_bundle" in body and "a.live_now" in body
    assert "t.wiki_revisions" in body, "the review never says what is there today"


def test_review_flags_a_bundle_from_a_different_org(html):
    """Composed server-side so the warning and the job record cannot disagree."""
    body = _fn(html, "renderRestoreReview")
    assert "p.warnings" in body and "restore-warn" in body


def test_proceed_is_a_separate_deliberate_click(html):
    assert "proceedWithRestore()" in html
    assert "Proceed with restore" in html
    assert "/start" in _fn(html, "proceedWithRestore")


def test_declining_the_review_discards_the_staged_bundle(html):
    """Walking away must not leave a full org backup parked on the server."""
    assert "/cancel" in _fn(html, "discardStagedRestore")


# ── while it runs ──────────────────────────────────────────────────────────

def test_progress_shows_the_phase_the_counts_and_an_eta(html):
    body = _fn(html, "renderRestoreProgress")
    assert "restore-bar-fill" in body
    assert "j.processed" in body and "j.total" in body
    assert "eta" in body
    assert "j.phases_done" in body


def test_the_phase_list_matches_the_server(html):
    """The phases are named in two places; a rename in one leaves the other
    labelling the wrong step, which is worse than no label."""
    from restore_jobs import PHASES
    block = html[html.index("const RESTORE_PHASES"):html.index("];", html.index("const RESTORE_PHASES"))]
    for name, label, _weight in PHASES:
        assert f"'{name}'" in block, f"the UI does not know the {name!r} phase"
        assert label in block, f"the UI labels {name!r} differently from the server"


def test_pause_resume_and_cancel_are_all_reachable(html):
    body = _fn(html, "renderRestoreControls")
    assert "'pause'" in body and "'resume'" in body
    assert "cancelRunningRestore()" in body


def test_only_one_of_pause_and_resume_is_offered_at_a_time(html):
    body = _fn(html, "renderRestoreControls")
    assert "j.status === 'paused'" in body and "j.status === 'running'" in body


def test_cancelling_a_running_restore_warns_it_may_leave_a_partial_org(html):
    body = _fn(html, "cancelRunningRestore")
    assert "confirm(" in body
    assert "PARTIALLY" in body


def test_the_outcome_repeats_whatever_the_server_says_about_a_cancel(html):
    """The server knows whether the cancel landed before or after the commit;
    the UI must print that rather than a canned "cancelled"."""
    body = _fn(html, "renderRestoreOutcome")
    assert "j.error" in body


# ── losing the tab ─────────────────────────────────────────────────────────

def test_closing_a_running_restore_only_looks_away(html):
    """Closing must not cancel a job that is already changing data — and the
    button has to say so, because every other × on this page ends something."""
    assert "Close — keeps running" in html
    body = _fn(html, "closeRestoreModal")
    assert "/cancel" not in body
    # The one exception is the review pane, where closing means declining.
    assert "restore-review" in body and "discardStagedRestore()" in body


def test_opening_the_backups_panel_reattaches_to_a_running_restore(html):
    assert "reattachRestore(orgId)" in _fn(html, "openBackupModal")
    body = _fn(html, "reattachRestore")
    assert "active=true" in body and "org_id=" in body


def test_a_running_restore_is_announced_even_with_every_dialog_closed(html):
    """The operator who navigated away has no other way back to it."""
    assert 'id="restore-banner"' in html
    body = _fn(html, "checkActiveRestores")
    assert "/api/platform/restore-jobs?active=true" in body
    assert "watchRestoreJob" in body


def test_a_transient_poll_failure_does_not_abandon_the_job(html):
    body = _fn(html, "pollRestoreOnce")
    assert "catch (_) { return; }" in body


def test_staged_jobs_are_not_mistaken_for_running_ones(html):
    """A staged job has not started; announcing it as in-progress would tell
    the operator something is happening to their org when nothing is."""
    assert "job.status === 'staged'" in _fn(html, "reattachRestore")
    assert "'staged'" in _fn(html, "checkActiveRestores")


# ── choosing what to restore ───────────────────────────────────────────────
# The review step stopped being a yes/no. These cover the part that turns it
# into a plan, and the parts that stop a plan being misread.

def test_each_area_can_be_switched_off(html):
    assert "toggleRestoreArea(" in html
    body = _fn(html, "renderRestoreReview")
    assert "p.areas" in body and "type=\"checkbox\"" in body


def test_the_mode_control_only_appears_where_there_is_a_choice(html):
    """Boards are replace-only and identity merge-only. Offering a second
    button that refuses would be worse than offering none."""
    body = _fn(html, "renderRestoreReview")
    assert "a.modes.length < 2" in body
    assert "point-in-time only" in body or "merge only" in body


def test_the_destructive_mode_is_styled_as_destructive(html):
    body = _fn(html, "renderRestoreReview")
    assert "m === 'replace'" in body and "danger" in body
    assert ".restore-mode.on.danger" in html


def test_the_effects_list_is_recomputed_from_the_current_selection(html):
    """A static list would describe the default plan while the operator looks
    at a different one."""
    body = _fn(html, "renderRestoreReview")
    assert "p.area_effects[" in body
    assert "_restorePlan.areas[a].mode" in body


def test_the_review_names_what_is_being_left_alone(html):
    assert "Left completely untouched" in _fn(html, "renderRestoreReview")


def test_an_area_an_import_requires_cannot_be_unticked(html):
    """Creating an org means writing its row; a checkbox that silently refused
    would read as a broken control."""
    body = _fn(html, "renderRestoreReview")
    assert "a.required" in body and "disabled" in body


def test_selecting_nothing_disables_the_go_button(html):
    body = _fn(html, "renderRestoreReview")
    assert "go.disabled = chosen.length === 0" in body


def test_a_fresh_bundle_does_not_inherit_the_last_plan(html):
    assert "_restorePlan = null;" in _fn(html, "stageRestore")


def test_counts_absent_from_an_older_bundle_are_not_shown_as_zero(html):
    body = _fn(html, "renderRestoreReview")
    assert "a.in_bundle == null" in body
    assert "a.live_now == null" in body


def test_the_plan_is_sent_with_the_start_call(html):
    body = _fn(html, "proceedWithRestore")
    assert "_planToRequest()" in body and "JSON.stringify" in body


def test_a_point_in_time_restore_is_confirmed_by_name(html):
    """"Replace" deletes work. The operator should read which areas before it
    happens, not find out from the result."""
    body = _fn(html, "proceedWithRestore")
    assert "confirm(" in body
    assert "meta.label" in body, "the confirmation must name the areas"
    assert "deleted, not merged" in body


def test_a_merge_only_plan_is_not_gated_behind_a_scare_dialog(html):
    """Confirming everything trains people to click through the one that
    mattered."""
    body = _fn(html, "proceedWithRestore")
    assert "if (replacing.length)" in body


# ── the debt a deferred restore leaves ─────────────────────────────────────

def test_the_embedding_choice_is_offered(html):
    body = _fn(html, "renderRestoreReview")
    assert "setRestoreEmbed(true)" in body and "setRestoreEmbed(false)" in body
    assert "Restore now, embed later" in body


def test_the_embedding_choice_is_hidden_when_no_memories_are_restored(html):
    body = _fn(html, "renderRestoreReview")
    assert "mem && mem.enabled" in body


def test_the_outcome_reports_what_a_point_in_time_restore_deleted(html):
    """The count that matters most is the one for what is now gone."""
    body = _fn(html, "renderRestoreOutcome")
    assert "wiki_files_removed" in body and "memories_cleared" in body
    assert "not in the backup" in body


def test_pending_embeddings_are_surfaced_with_the_way_to_clear_them(html):
    body = _fn(html, "refreshPendingEmbeddings")
    assert "not embedded" in body
    assert "semantic search will not find them" in body
    assert "startPendingEmbeddings(" in body


def test_the_pending_panel_polls_while_it_runs_and_stops_after(html):
    body = _fn(html, "refreshPendingEmbeddings")
    assert "setInterval" in body and "clearInterval" in body


def test_the_debt_is_visible_from_the_backups_panel_not_just_after_a_restore(html):
    """An operator who defers on Friday needs to find it again on Monday."""
    assert "backup-pending-embeds" in _fn(html, "openBackupModal")
    assert 'id="backup-pending-embeds"' in html
