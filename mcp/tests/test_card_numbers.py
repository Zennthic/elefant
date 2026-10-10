"""The card `number` over MCP.

A card's number is its short, immutable handle — "#33" said out loud, and what
a permalink is built from. The agent-facing property that matters is narrow but
real: a human says "look at #33", and the agent has to be able to join that back
to a slug it can actually call tools with. That only works if the number is
rendered on every card an agent sees, not just on `card_get`.

The number is allocated by memory-api and is never client-settable, so there is
no write surface here to test — only that reads carry it, and that a card
predating the backfill renders cleanly without one.
"""
import pytest


@pytest.fixture
def helpers():
    import main
    return main


_CARD = {"title": "Network discovery tool", "slug": "netdisco",
         "column": "Backlog", "stage": "spark", "source": "claude",
         "aliases": [], "body": "an idea"}


def _card(**over):
    c = dict(_CARD)
    c.update(over)
    return c


# ── the number is visible on read ──────────────────────────────────────────

def test_fmt_card_shows_the_number(helpers):
    out = helpers._fmt_card(_card(number=33))
    assert "#33" in out


def test_fmt_card_puts_the_number_beside_the_slug(helpers):
    """The number and the slug are the two handles for one card, and the join
    between them is the thing an agent needs when a human quotes a number."""
    out = helpers._fmt_card(_card(number=33))
    head = out.splitlines()[0]
    assert "#33" in head and "`netdisco`" in head


def test_fmt_card_omits_the_number_when_the_card_has_none(helpers):
    """A card written before the backfill ran has no number. It must render
    nothing rather than "#None"."""
    assert "#" not in helpers._fmt_card(_card())
    assert "None" not in helpers._fmt_card(_card(number=None))


def test_fmt_card_list_shows_the_number(helpers):
    """card_list is where an agent actually scans the board, so a number that
    only appeared on card_get would be invisible in the one view that matters."""
    out = helpers._fmt_card_list([_card(number=33), _card(slug="other", number=34)])
    assert "#33" in out
    assert "#34" in out


def test_fmt_card_list_omits_the_number_when_absent(helpers):
    # Scoped to the card's own line: the list renders a `### <column>` heading
    # above it, which legitimately contains a hash.
    line = [l for l in helpers._fmt_card_list([_card()]).splitlines()
            if l.startswith("- ")][0]
    assert "#" not in line


def test_zero_is_not_treated_as_absent(helpers):
    """Numbers start at 1 so #0 should never occur, but the absence check has
    to be `is None` rather than falsiness — a `0` swallowed by a truthiness
    test is the classic version of this bug, and it would hide a real card."""
    assert "#0" in helpers._fmt_card(_card(number=0))
