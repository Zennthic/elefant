"""The agent-facing half of card relationships.

This surface exists for one reader: an unattended routine deciding what to work
next. So the checks that matter are about whether a blocked card is
UNMISTAKABLY blocked in what the agent actually reads — in the list as well as
on the card, since a routine that scans a column and takes the top of it never
opens the others.
"""
import inspect

import main


# ── Rendering a card's relationships ──────────────────────────────────────────

def test_an_unresolved_blocker_leads_and_says_not_to_start():
    out = main._fmt_relationships(
        {"blocked_by": [{"slug": "bravo", "number": 7, "title": "Bravo", "resolved": False}]},
        None)
    assert "BLOCKED" in out
    assert "#7" in out and "bravo" in out, "the blocker has to be NAMED, not just counted"
    # The instruction matters as much as the fact. An agent reading "blocked by
    # #7" may still start the card; "finish those first" is the actual ask.
    assert "first" in out


def test_a_resolved_blocker_does_not_read_as_blocking():
    out = main._fmt_relationships(
        {"blocked_by": [{"slug": "bravo", "number": 7, "title": "Bravo", "resolved": True}]},
        None)
    assert "BLOCKED" not in out
    # Still shown, because "this was waiting on that" is the card's history.
    assert "bravo" in out and "now done" in out


def test_a_mix_reports_only_the_live_blockers_as_blocking():
    out = main._fmt_relationships({"blocked_by": [
        {"slug": "done", "number": 1, "title": "Done", "resolved": True},
        {"slug": "live", "number": 2, "title": "Live", "resolved": False},
    ]}, None)
    blocked_line = [ln for ln in out.splitlines() if "BLOCKED" in ln][0]
    assert "live" in blocked_line
    assert "done" not in blocked_line


def test_what_a_card_blocks_is_shown_too():
    out = main._fmt_relationships(
        {"blocks": [{"slug": "downstream", "number": 9, "title": "D", "resolved": False}]},
        None)
    assert "Blocks:" in out and "downstream" in out


def test_related_cards_are_shown_without_an_ordering_claim():
    out = main._fmt_relationships(
        {"related": [{"slug": "sibling", "number": 4, "title": "S", "resolved": False}]},
        None)
    assert "Related:" in out
    assert "BLOCKED" not in out and "first" not in out, \
        "a loose link must not read as a dependency"


def test_a_card_with_no_relationships_renders_nothing():
    assert main._fmt_relationships({}, None) == ""
    assert main._fmt_relationships(None, None) == ""


def test_a_card_from_the_list_still_renders_as_blocked():
    """card_list carries only the summary, not the full relationship set. A card
    rendered from it must not come out looking startable just because the
    detailed view was not fetched."""
    out = main._fmt_relationships(None, [{"slug": "bravo", "number": 7, "title": "Bravo"}])
    assert "BLOCKED" in out and "bravo" in out


def test_a_blocker_with_no_number_still_renders():
    """Cards written before the number backfill have none. `#None` would be
    worse than the slug alone."""
    out = main._fmt_relationships(
        {"blocked_by": [{"slug": "old-card", "number": None, "title": "Old", "resolved": False}]},
        None)
    assert "old-card" in out
    assert "#None" not in out and "None" not in out


# ── The list view ─────────────────────────────────────────────────────────────

def test_the_list_marks_blocked_cards():
    """The important one. A dispatch routine reads the column and takes the top
    card; if blocked-ness only showed on card_get it would pick up work it
    cannot do and find out afterwards."""
    out = main._fmt_card_list([
        {"title": "Alpha", "slug": "alpha", "column": "Up Next", "stage": "designed",
         "source": "claude", "number": 1, "blocked": True,
         "blocked_by": [{"slug": "bravo", "number": 2, "title": "Bravo"}]},
        {"title": "Bravo", "slug": "bravo", "column": "Up Next", "stage": "designed",
         "source": "claude", "number": 2, "blocked": False},
    ])
    # Selected by the card's own number at the start of its line: bravo's slug
    # also appears ON alpha's line, as the blocker.
    alpha = [ln for ln in out.splitlines() if ln.startswith("- #1 ")][0]
    bravo = [ln for ln in out.splitlines() if ln.startswith("- #2 ")][0]
    assert "blocked" in alpha and "#2" in alpha
    assert "blocked" not in bravo


def test_a_list_from_an_older_api_without_the_flag_still_renders():
    out = main._fmt_card_list([
        {"title": "Alpha", "slug": "alpha", "column": "Up Next", "stage": "designed",
         "source": "claude"},
    ])
    assert "alpha" in out and "blocked" not in out


# ── The tools ─────────────────────────────────────────────────────────────────

def test_card_link_and_card_next_are_registered_tools():
    assert callable(main.card_link)
    assert callable(main.card_next)


def test_card_link_needs_board_write_and_card_next_only_board_read():
    """Picking the next card is a read. Gating it behind board_write would stop
    a read-only routine from choosing work correctly, which is the one thing it
    most needs to do."""
    assert '"board_write"' in inspect.getsource(main.card_link)
    assert '"board_read"' in inspect.getsource(main.card_next)
    assert '"board_write"' not in inspect.getsource(main.card_next)


def test_card_link_documents_all_three_kinds_from_this_cards_point_of_view():
    """The tool's docstring is its whole interface for an agent. `blocks` and
    `blocked-by` are opposites, and getting them the wrong way round writes a
    dependency that is backwards — which then reorders work incorrectly and
    looks like the feature working."""
    doc = main.card_link.__doc__
    for kind in ("related", "blocks", "blocked-by"):
        assert kind in doc
    assert "point of view" in doc or "perspective" in doc


def test_card_next_tells_the_agent_when_to_reach_for_it():
    doc = main.card_next.__doc__
    assert "unattended" in doc
    assert "card_list" in doc, \
        "it has to say what it is INSTEAD OF, or it will not get used"


def test_card_next_explains_what_resolves_a_blocker():
    doc = main.card_next.__doc__
    assert "shipped" in doc and "dropped" in doc and "closed" in doc


def test_card_next_says_why_it_handed_back_a_different_card():
    """When the answer is not the top of the column, the agent has to be able to
    say why — both in its own reasoning and to whoever reads the result."""
    src = inspect.getsource(main.card_next)
    assert "chain" in src
    assert "blocker rather than the top" in src


def test_card_link_stamps_the_agents_source():
    assert 'token_info.get("source")' in inspect.getsource(main.card_link)


def test_card_get_passes_relationships_to_the_renderer():
    src = inspect.getsource(main.card_get)
    assert '"relationships"' in src, \
        "card_get fetches relationships but never hands them to the renderer"
