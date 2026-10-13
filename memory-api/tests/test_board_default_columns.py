"""The default columns and states a NEW board starts with.

The change itself is two literals. What needs guarding is the blast radius,
because `stage` is the architected vocabulary that dispatch, the blocker
traversal and every existing board bind to, and every board that exists today
is mapped to the vocabulary this replaced.

The card asked for new defaults for a NEW board. It did not ask for anybody's
board to be remapped, and a rename that quietly did so would move somebody's
work. So the guards here are about what must STILL work afterwards:

  * an existing column can still be saved, under its old stage,
  * the picker a board is offered always includes that board's own stages,
  * a finished blocker resolves under either vocabulary,
  * card_add still finds a backlog column on a board seeded either way.
"""
import inspect

import main


# ── What a new board gets ─────────────────────────────────────────────────

def test_the_seeded_columns_are_the_ones_the_card_asked_for():
    assert main._SEED_COLUMNS == [
        ("Backlog",     "backlog"),
        ("Up Next",     "selected-for-development"),
        ("In Progress", "in-progress"),
        ("Scheduled",   "scheduled"),
        ("Blocked",     "blocked"),
        ("Review",      "review"),
        ("Done",        "done"),
    ]


def test_every_stage_gets_a_column_and_every_column_a_stage():
    """One column per stage, nothing offered that nothing maps to."""
    seeded = [stage for _, stage in main._SEED_COLUMNS]
    assert seeded == main.BOARD_STAGES


def test_the_columns_are_seeded_in_board_order():
    """Position is assigned by enumeration order, so the literal IS the layout:
    a reordering of the list silently reorders every new board."""
    src = inspect.getsource(main._ensure_seed_columns)
    assert "enumerate(_SEED_COLUMNS, start=1)" in src
    assert '"pos": float(i)' in src


# ── What an existing board keeps ──────────────────────────────────────────

def test_an_existing_column_can_still_be_saved():
    """Validation accepts the old vocabulary. Without this, renaming a column
    on any board that predates the new defaults would 400 on its own current stage."""
    for fn in (main.column_add, main.column_update):
        src = inspect.getsource(fn)
        assert "VALID_BOARD_STAGES" in src, \
            f"{fn.__name__} validates against the current list alone"


def test_the_refusal_still_names_the_stages_to_use():
    """It offers the CURRENT vocabulary, not the union — the old names are
    accepted, not recommended."""
    src = inspect.getsource(main.column_add)
    assert "', '.join(BOARD_STAGES)" in src


def test_the_offered_stages_include_the_ones_this_board_uses():
    src = inspect.getsource(main.board_columns)
    assert "LEGACY_BOARD_STAGES" in src
    assert "BOARD_STAGES + legacy_in_use" in src


def test_a_board_that_never_used_the_old_vocabulary_is_not_shown_it():
    """`legacy_in_use` is filtered by what the board's own columns carry, so a
    new board's picker offers seven stages, not fourteen."""
    src = inspect.getsource(main.board_columns)
    assert "if st in in_use" in src


def test_card_add_finds_a_backlog_column_under_either_name():
    src = inspect.getsource(main.card_add)
    assert "stage IN ('backlog', 'spark')" in src
    # …and prefers the current one where a board somehow has both.
    assert "ORDER BY (stage <> 'backlog')" in src


def test_card_add_still_falls_back_to_the_leftmost_column():
    """A user who remapped every column away from backlog must still be able to
    add a card."""
    src = inspect.getsource(main.card_add)
    assert src.count("ORDER BY position LIMIT 1") >= 1
    assert "No columns available for this tenant" in src


# ── The one place the vocabularies are reconciled ─────────────────────────

def test_terminal_stages_cover_both_vocabularies():
    assert set(main.TERMINAL_STAGES) == {"done", "shipped", "dropped"}


def test_a_blocker_in_the_new_terminal_stage_resolves():
    assert main._link_resolved("done", None) is True


def test_a_blocker_in_the_old_terminal_stage_still_resolves():
    """The consequence of getting this wrong is not cosmetic: a finished
    blocker that did not resolve would stall every card waiting on it."""
    assert main._link_resolved("shipped", None) is True


def test_a_blocker_that_is_not_finished_does_not_resolve():
    for stage in ("backlog", "in-progress", "review", "building", "designed"):
        assert main._link_resolved(stage, None) is False, stage


def test_the_blocker_traversal_skips_terminal_columns():
    """Same set, interpolated into the SQL — so it stays in step with the Python
    one rather than being a second hand-written list that can drift."""
    src = inspect.getsource(main._unresolved_blockers)
    assert "TERMINAL_STAGES" in src
    assert "bc.stage NOT IN" in src


# ── No silent remapping ───────────────────────────────────────────────────

def test_there_is_no_translation_from_old_stages_to_new():
    """Deliberate: `shaping` and `dropped` have no counterpart, and mapping the
    rest would move somebody's column out from under them. The old names stay
    valid instead."""
    assert isinstance(main.LEGACY_BOARD_STAGES, list)
    assert not any(isinstance(x, dict) for x in main.LEGACY_BOARD_STAGES)


def test_blocked_survived_the_rename_and_is_not_duplicated():
    assert "blocked" in main.BOARD_STAGES
    assert "blocked" not in main.LEGACY_BOARD_STAGES
    assert len(main.VALID_BOARD_STAGES) == len(set(main.VALID_BOARD_STAGES))
