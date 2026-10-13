"""Relationships between cards: the pure logic, the shapes, and the wiring.

The SQL and the traversal are covered against a real PostgreSQL in
test_card_links_integration.py — a mocked connection can tell you the Python
around a query is right and nothing whatever about the query. What lives here is
what can be checked without a database: the normalisation that makes mirroring
true by construction, the summary every surface reads, and the routes existing.
"""
import inspect

import pytest

import main


# ── Normalisation: why mirroring cannot drift ─────────────────────────────────

def test_blocked_by_is_stored_as_the_other_card_blocking_this_one():
    """The whole design rests on this. `A blocked-by B` and `B blocks A` are the
    same fact, so they must normalise to the same row — otherwise the two sides
    are two rows and can be removed apart, leaving the board asserting a
    dependency from one end that the other end denies."""
    assert main._normalize_link("blocked-by", "A", "B") == ("blocks", "B", "A")
    assert main._normalize_link("blocks", "B", "A") == ("blocks", "B", "A")


def test_related_is_stored_in_one_canonical_direction():
    """Symmetric, so A-B and B-A have to be one row rather than two that could
    be deleted apart."""
    assert main._normalize_link("related", "aaa", "bbb") == ("related", "aaa", "bbb")
    assert main._normalize_link("related", "bbb", "aaa") == ("related", "aaa", "bbb")


def test_link_kind_spelling_is_forgiving():
    """An agent writing `blocked_by` means `blocked-by`. Refusing over a
    separator would be a tool that fails on the obvious spelling."""
    for spelling in ("blocked-by", "blocked_by", "BLOCKED-BY", " Blocked-By "):
        assert main._normalize_link(spelling, "A", "B") == ("blocks", "B", "A")
    for spelling in ("related", "relates-to", "related-to", "Related"):
        assert main._normalize_link(spelling, "A", "B")[0] == "related"


def test_an_unknown_relationship_is_refused_by_name():
    with pytest.raises(main.HTTPException) as exc:
        main._normalize_link("depends-on", "A", "B")
    assert exc.value.status_code == 400
    assert "depends-on" in str(exc.value.detail)
    # The message has to say what IS allowed, or the caller is left guessing.
    for kind in main.LINK_KINDS:
        assert kind in str(exc.value.detail)


def test_only_two_kinds_are_actually_stored():
    """Three spellings in, two rows out. If a third stored kind ever appears,
    every read path that switches on `blocks` vs `related` needs revisiting."""
    assert set(main.LINK_KINDS_STORED) == {"blocks", "related"}


# ── Resolution: when does a blocker stop blocking ─────────────────────────────

def test_a_blocker_is_resolved_when_it_is_done_or_archived():
    assert main._link_resolved("shipped", None) is True
    assert main._link_resolved("dropped", None) is True
    assert main._link_resolved("designed", None) is False
    # Archived beats stage: a card taken off the board blocks nothing.
    assert main._link_resolved("designed", "2026-09-11T00:00:00Z") is True


def test_dropped_counts_as_done_for_blocking():
    """A card nobody is going to do blocks nothing. Treating `dropped` as still
    blocking would stall a column behind a decision already taken."""
    assert "dropped" in main.TERMINAL_STAGES
    assert "shipped" in main.TERMINAL_STAGES
    assert "blocked" not in main.TERMINAL_STAGES, (
        "the `blocked` STAGE is a column a card sits in, not a finished state — "
        "counting it as done would unblock everything waiting on a stuck card")


# ── The summary every surface reads ───────────────────────────────────────────

def test_blocked_summary_counts_only_unresolved_blockers():
    rel = {"blocked_by": [
        {"slug": "done-one", "number": 1, "title": "Done", "resolved": True},
        {"slug": "live-one", "number": 2, "title": "Live", "resolved": False},
    ]}
    out = main._blocked_summary(rel)
    assert out["blocked"] is True
    assert [b["slug"] for b in out["blocked_by"]] == ["live-one"]


def test_a_card_whose_blockers_are_all_done_is_not_blocked():
    rel = {"blocked_by": [{"slug": "x", "number": 1, "title": "X", "resolved": True}]}
    assert main._blocked_summary(rel)["blocked"] is False


def test_blocked_summary_handles_a_card_with_no_relationships():
    for empty in (None, {}, {"blocked_by": []}):
        assert main._blocked_summary(empty) == {"blocked": False, "blocked_by": []}


def test_the_summary_is_small_enough_to_ride_on_a_list():
    """It goes on every card in card_list, so it carries what a chip needs and
    not a second copy of the blocking card."""
    rel = {"blocked_by": [{"slug": "x", "number": 3, "title": "X", "resolved": False,
                           "stage": "designed", "archived": False, "created": "..."}]}
    entry = main._blocked_summary(rel)["blocked_by"][0]
    assert set(entry) == {"slug", "number", "title"}


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_the_link_and_next_card_routes_exist():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert "/board/cards/{card_slug}/link" in paths
    assert "/board/next-card" in paths


def test_next_card_does_not_collide_with_the_card_by_slug_route():
    """`/board/next-card` and `/board/cards/{slug}` must not be able to shadow
    one another — a card slugged "next-card" would otherwise be unreachable, or
    worse, swallow the picker."""
    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert "/board/next-card" in paths
    assert not any(p.startswith("/board/cards/") and p.endswith("next-card") for p in paths)


def test_card_list_reports_blocked_ness_for_the_whole_page_in_one_query():
    """One query for the page, not one per card. The board polls this and the
    stream re-reads it on every change, so a per-card query would be a query per
    sticky on every board event."""
    src = inspect.getsource(main.card_list)
    assert "_relationships_by_card" in src
    assert src.count("_relationships_by_card") == 1, (
        "blocked-ness must be resolved once for the whole page")


def test_card_get_returns_the_full_relationship_set():
    src = inspect.getsource(main.card_get)
    assert "_relationships_by_card" in src and '"relationships"' in src


def test_a_card_cannot_be_linked_to_itself():
    src = inspect.getsource(main.card_link)
    assert "cannot be related to itself" in src


def test_a_cycle_is_warned_about_rather_than_refused():
    """A cycle is a real statement about a real disagreement. Refusing the
    second link tells the person nothing about the first, and leaves the board
    unable to express what somebody actually believes."""
    src = inspect.getsource(main.card_link)
    assert "_blocking_cycle" in src
    assert "raise HTTPException" not in src.split("_blocking_cycle", 1)[1], (
        "the cycle check must warn, not refuse")


def test_the_traversal_guards_against_a_cycle():
    """Without this an unattended routine asked for the next card against a
    cyclic board does not return a wrong answer — it never returns at all."""
    src = inspect.getsource(main.card_next)
    assert "seen" in src and "cycle" in src


def test_blockers_are_offered_in_the_boards_own_order():
    """Which blocker to work first is a priority question, and the board owner
    has already answered it by arranging the board. Ordering by anything else
    (id, creation time) would quietly override them."""
    src = inspect.getsource(main._unresolved_blockers)
    assert "ORDER BY bc.position, c.position" in src


def test_the_traversal_ignores_resolved_blockers():
    src = inspect.getsource(main._unresolved_blockers)
    assert "archived_at IS NULL" in src
    assert "NOT IN" in src
    # Built from the shared constant rather than a hand-written stage list, so
    # the SQL cannot come to disagree with _link_resolved about what "done"
    # means — which would show as a card the board calls blocked and the picker
    # hands you anyway.
    assert "TERMINAL_STAGES" in src
