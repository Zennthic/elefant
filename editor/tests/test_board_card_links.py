"""The editor's half of card relationships: the pass-throughs, and that the
board UI and this service agree about them.

The board UI talks only to the editor, so a route the UI calls and the editor
does not serve is a button that does nothing, and a payload field the editor
drops on the floor is a link that silently never gets made. Both are checks only
a test that reads both sides can make.
"""
import inspect
import re
from pathlib import Path

import pytest

import main
from served_routes import served_paths
from routers import boards

_ROOT = Path(main.__file__).resolve().parents[1]
_BOARDS_JS = _ROOT / "memory-ui" / "static" / "boards.js"
_BOARDS_CSS = _ROOT / "memory-ui" / "static" / "boards.css"


def test_the_link_and_next_card_routes_are_served():
    paths = served_paths(main.app)
    assert "/api/board/cards/{card_slug}/link" in paths
    assert "/api/board/next-card" in paths


def test_the_link_route_forwards_every_field_the_ui_sends():
    """FastAPI takes the body as an untyped dict here, so a field the editor
    forgets to forward is dropped in SILENCE — the call succeeds, and the link
    is simply not what was asked for."""
    src = inspect.getsource(boards.board_card_link)
    for field in ("other", "kind", "remove"):
        assert f'body.get("{field}")' in src or f'body.get("{field}"' in src, \
            f"the link pass-through drops {field!r}"


def test_ui_originated_links_are_stamped_as_ui():
    """Same rule as every other board write from a browser: a card's history
    should say a person did this, not "unknown"."""
    assert '"source": "ui"' in inspect.getsource(boards.board_card_link)


def test_next_card_forwards_its_filters():
    src = inspect.getsource(boards.board_next_card)
    for field in ("column", "stage", "repo", "label"):
        assert f'"{field}": {field}' in src, f"next-card drops the {field!r} filter"


def test_next_card_resolves_the_tenant_from_the_session():
    """It returns a whole card. Taking the tenant from the caller would let any
    logged-in user pull work off another org's board."""
    src = inspect.getsource(boards.board_next_card)
    assert "_effective_tenant_slug" in src
    # Compared against a sibling route rather than named: the gate is a closure
    # built by require_role, so its __name__ says nothing. What matters is that
    # this route is gated by the SAME dependency as the rest of the board API,
    # which is a claim that survives the gate being renamed or rebuilt.
    def gate(fn):
        return inspect.signature(fn).parameters["claims"].default.dependency
    assert gate(boards.board_next_card) is gate(boards.board_cards_list)
    assert gate(boards.board_card_link) is gate(boards.board_cards_list)


def test_next_card_names_its_people():
    """It hands back a card, and a card that renders `user:9f2c…` instead of a
    name is a worse answer than the list view gives for the same card."""
    assert "_enrich_people" in inspect.getsource(boards.board_next_card)


# ── Cross-service: the UI calls what the editor serves ────────────────────────

def _js() -> str:
    if not _BOARDS_JS.exists():
        pytest.skip("memory-ui is not checked out beside the editor")
    return _BOARDS_JS.read_text()


def test_the_board_ui_calls_the_link_route_the_editor_serves():
    src = _js()
    assert "/link'" in src or "/link`" in src, "the board UI never calls the link route"
    # The UI builds it as `/cards/${slug}/link`; the editor serves
    # /api/board/cards/{card_slug}/link under the /api/board prefix the UI uses.
    assert re.search(r"/cards/\$\{encodeURIComponent\(\w+\)\}/link", src), \
        "the board UI's link call does not match the editor's route shape"


def test_the_board_ui_sends_the_fields_the_route_forwards():
    src = _js()
    call = src.split("/link`,", 1)
    assert len(call) == 2, "boards.js no longer posts to the link route"
    body = call[1].split(")", 1)[0]
    for field in ("other", "kind", "remove"):
        assert field in body, f"the board UI's link call omits {field!r}"


def test_a_blocked_card_is_marked_in_the_board_ui():
    """The design is explicit that a blocked card does NOT move to a Blocked
    column — it stays put and carries a flag. That makes the marker the only
    thing distinguishing a card you can start from one you cannot, so its
    absence is not cosmetic."""
    src = _js()
    assert "b-card-blocked" in src
    assert "c.blocked" in src, "the UI never reads the blocked flag the API sends"
    assert "blocked_by" in src, "the marker must name the blocker, not just say blocked"


def test_the_blocked_marker_is_styled():
    if not _BOARDS_CSS.exists():
        pytest.skip("memory-ui is not checked out beside the editor")
    css = _BOARDS_CSS.read_text()
    for cls in (".b-card-blocked", ".b-blocker", ".b-card.blocked"):
        assert cls in css, f"{cls} is used in boards.js but has no style"


def test_the_blocker_chip_cannot_execute_a_hostile_slug():
    """Same rule as the label, repo and people chips. An HTML attribute is
    entity-decoded BEFORE the JS inside it is parsed, so esc() alone does not
    contain a value interpolated into an onclick argument. The value has to ride
    in a data attribute and be read back from there."""
    src = _js()
    block = src.split("const blockers = ", 1)
    assert len(block) == 2, "boards.js no longer builds the blocked marker here"
    marker = block[1].split("`;", 1)[0]
    assert "data-slug=" in marker
    assert "this.dataset.slug" in marker, \
        "the blocker chip must read its slug from the data attribute, not an inlined argument"
    assert "openCard('" not in marker and "openCard(\"" not in marker, \
        "a slug interpolated straight into the onclick argument is executable"


def test_blocked_ness_is_in_the_poll_signature():
    """Linking two cards changes nothing else about either of them. Without this
    an open board would keep showing a card as startable long after something
    was put in its way — and the marker exists precisely to prevent that."""
    src = _js()
    sig = src.split("function _sigOf(", 1)[1].split("}", 1)[0]
    assert "c.blocked" in sig
    assert "blocked_by" in sig


def test_the_relationship_editor_reloads_rather_than_guessing(  ):
    """`blocked-by` is stored as the OTHER card's `blocks`, so what the edge
    became is the server's answer. Patching the local copy from what was asked
    for is how the modal and the board start disagreeing."""
    src = _js()
    fn = src.split("async function saveLink(", 1)[1].split("\n  }", 1)[0]
    assert "apiGet(" in fn, "saveLink must re-read the card rather than patch it locally"
    assert "refresh()" in fn, "the board behind the modal must pick up the new marker"
