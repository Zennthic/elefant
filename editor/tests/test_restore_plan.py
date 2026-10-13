"""The restore plan: what gets put back, how, and what that costs.

A backup is one bundle but the things inside it fail independently — a wiki
tree gets mangled without the board being wrong. Restoring all of it to fix
one of them is a bigger blast radius than the problem, so the plan is the
object that keeps the blast radius to what was asked for.

The rules worth pinning are the refusals. A mode an area cannot honour must be
refused rather than quietly downgraded: an operator who asked for a
point-in-time wiki restore and silently got a merge walks away believing the
tree is clean.
"""
import pytest

from restore_jobs import (AREA_EFFECT, AREA_MODES, AREAS, EMBED_EFFECT,
                          PHASE_WEIGHT, RestoreControl, RestorePlan)


# ── defaults ───────────────────────────────────────────────────────────────

def test_the_default_plan_is_the_whole_org_merged_and_embedded():
    """Every existing caller passes no plan, and must keep getting exactly
    what _restore_org_snapshot has always done."""
    plan = RestorePlan()
    assert set(plan.areas) == set(AREAS)
    assert plan.is_full_restore and plan.embed_memories
    assert all(plan.mode(a) == AREA_MODES[a][0] for a in AREAS)
    assert not plan.replaces("wiki"), "merge is the default; replace is opted into"


def test_parsing_nothing_gives_the_default():
    assert RestorePlan.parse(None).is_full_restore
    assert RestorePlan.parse({}).is_full_restore


# ── selection ──────────────────────────────────────────────────────────────

def test_an_unselected_area_is_excluded():
    plan = RestorePlan.parse({"areas": ["wiki"]})
    assert plan.includes("wiki")
    assert not plan.includes("memories")
    assert not plan.includes("identity")
    assert not plan.is_full_restore


def test_selecting_nothing_is_refused_rather_than_run_as_a_no_op():
    with pytest.raises(ValueError, match="at least one area"):
        RestorePlan.parse({"areas": []})


def test_an_unknown_area_is_named_along_with_the_real_ones():
    with pytest.raises(ValueError) as e:
        RestorePlan.parse({"areas": ["wiki", "sekrets"]})
    assert "sekrets" in str(e.value)
    for area in AREAS:
        assert area in str(e.value)


# ── modes ──────────────────────────────────────────────────────────────────

def test_replace_is_available_where_a_point_in_time_restore_makes_sense():
    plan = RestorePlan.parse({"areas": ["wiki"], "modes": {"wiki": "replace"}})
    assert plan.replaces("wiki")


def test_identity_cannot_be_replaced():
    """Replacing it would delete every account created since the backup — up
    to and including the operator running the restore."""
    with pytest.raises(ValueError, match="'merge' only"):
        RestorePlan.parse({"areas": ["identity"], "modes": {"identity": "replace"}})


def test_boards_cannot_be_merged():
    """memory-api's import has no merge path; offering one here would promise
    something the other side cannot do."""
    with pytest.raises(ValueError, match="'replace' only"):
        RestorePlan.parse({"areas": ["boards"], "modes": {"boards": "merge"}})


def test_a_nonsense_mode_is_refused_not_coerced():
    with pytest.raises(ValueError):
        RestorePlan.parse({"areas": ["wiki"], "modes": {"wiki": "point-in-time"}})


def test_boards_default_to_the_only_thing_they_can_do():
    assert RestorePlan.parse({"areas": ["boards"]}).mode("boards") == "replace"


# ── phases and weights ─────────────────────────────────────────────────────

def test_a_partial_restore_only_runs_the_phases_it_needs():
    plan = RestorePlan.parse({"areas": ["boards"]})
    assert plan.phases() == ("preparing", "boards")
    assert "memories" not in plan.phases()


def test_the_wiki_needs_the_database_phase_too():
    """Page history and the recycle bin are editor-DB rows, not S3 objects."""
    assert plan_phases(["wiki"]) == ("preparing", "database", "wiki")


def plan_phases(areas):
    return RestorePlan.parse({"areas": areas}).phases()


@pytest.mark.parametrize("areas", [
    ["identity"], ["wiki"], ["boards"], ["memories"],
    ["wiki", "boards"], list(AREAS),
])
def test_the_bar_always_reaches_a_hundred_whatever_was_selected(areas):
    """A two-area restore that stopped at 30% would read as a failed one."""
    plan = RestorePlan.parse({"areas": areas})
    assert sum(plan.weights().values()) == pytest.approx(100.0)


def test_deferring_the_embedding_stops_it_dominating_the_bar():
    """Writing a memory without a vector is a fraction of the cost of
    embedding one, so the phase must stop claiming most of the time."""
    # Compared against a restore with other work to do — with memories as the
    # only phase, normalising would hand it 100% either way and prove nothing.
    now   = RestorePlan.parse({"areas": ["wiki", "memories"], "embed_memories": True})
    later = RestorePlan.parse({"areas": ["wiki", "memories"], "embed_memories": False})
    assert now.weights()["memories"] > later.weights()["memories"] * 2
    assert later.weights()["wiki"] > now.weights()["wiki"]


def test_a_control_built_from_a_plan_scores_against_that_plan():
    plan = RestorePlan.parse({"areas": ["boards"]})
    ctl = RestoreControl(plan=plan)
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("boards", total=1); ctl.advance()
    ctl.finish()
    assert ctl.percent == 100.0


def test_a_control_with_no_plan_still_uses_the_full_table():
    ctl = RestoreControl()
    ctl.begin("preparing", total=1); ctl.advance()
    ctl.begin("database", total=1)
    assert ctl.percent == PHASE_WEIGHT["preparing"]


# ── what the operator is told ──────────────────────────────────────────────

def test_every_area_and_mode_has_wording():
    """A combination with no sentence would show the operator a blank where
    the consequences should be."""
    for area in AREAS:
        for mode in AREA_MODES[area]:
            assert AREA_EFFECT.get(f"{area}:{mode}"), f"no wording for {area}:{mode}"


def test_every_destructive_mode_says_so_in_capitals():
    for key, text in AREA_EFFECT.items():
        if key.endswith(":replace"):
            assert "DELETED" in text, f"{key} does not say what it destroys"


def test_the_effects_name_what_is_being_left_alone():
    effects = " ".join(RestorePlan.parse({"areas": ["wiki"]}).effects())
    assert "Left completely untouched" in effects
    assert "Durable memories" in effects


def test_a_full_restore_has_nothing_to_report_as_skipped():
    assert not any("untouched" in e for e in RestorePlan().effects())


def test_deferring_warns_that_search_will_not_find_them():
    effects = " ".join(RestorePlan.parse(
        {"areas": ["memories"], "embed_memories": False}).effects())
    assert "semantic search" in effects
    assert EMBED_EFFECT["later"] in effects


def test_the_embedding_note_is_absent_when_no_memories_are_restored():
    effects = " ".join(RestorePlan.parse({"areas": ["wiki"]}).effects())
    assert "semantic search" not in effects


# ── round trip ─────────────────────────────────────────────────────────────

def test_a_plan_survives_being_recorded_and_read_back():
    """The job row is the record of a destructive act, so what it stores has
    to be what was run."""
    plan = RestorePlan.parse({"areas": ["wiki", "memories"],
                              "modes": {"wiki": "replace"},
                              "embed_memories": False})
    again = RestorePlan.parse(plan.to_dict())
    assert again.areas == plan.areas
    assert again.replaces("wiki") and not again.replaces("memories")
    assert again.embed_memories is False
