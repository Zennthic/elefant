"""Boards MCP helpers: scope checks and card formatting."""
import main


def test_check_permission_board_scopes():
    assert main._check_permission({"board_read": True}, "board_read") is True
    assert main._check_permission({"board_read": False}, "board_read") is False
    assert main._check_permission({}, "board_write") is False


def test_permission_denied_names_board_scope():
    assert "board_write" in main._permission_denied("board_write")


def test_fmt_card_renders_key_fields():
    card = {"title": "Network discovery tool", "slug": "netdisco",
            "column": "Backlog", "stage": "spark", "source": "claude",
            "aliases": [], "body": "an idea"}
    out = main._fmt_card(card)
    for expected in ("Network discovery tool", "netdisco", "Backlog", "spark", "an idea"):
        assert expected in out


def test_fmt_card_list_groups_and_handles_empty():
    assert main._fmt_card_list([]) == "No cards."
    cards = [
        {"title": "A", "slug": "a", "column": "Backlog", "stage": "spark", "source": "ui"},
        {"title": "B", "slug": "b", "column": "Backlog", "stage": "spark", "source": "claude"},
        {"title": "C", "slug": "c", "column": "In Progress", "stage": "building", "source": "claude"},
    ]
    out = main._fmt_card_list(cards)
    # Grouped by column header, not repeated per card.
    assert out.count("### Backlog") == 1
    assert "### In Progress" in out
    assert "`a`" in out and "`c`" in out
