"""The app shell keeps its header pinned on every tab.

The SPA pins the header by never letting the document scroll (body has
overflow:hidden) and giving each tab an internally-scrolling body. Boards was
the one tab that sized itself with `height:100%` against a flex-sized parent
instead of filling by flex — a percentage engines resolve differently, and
where it falls back to `auto` the board grows past the viewport and drags the
header up with it. These hold the two invariants that keep that from returning.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent / "memory-ui" / "static"


def _boards_root_declarations() -> str:
    """The #boards-root rule's declarations, comments and spaces stripped —
    the comment explains the very thing we're asserting isn't there."""
    css = (STATIC / "boards.css").read_text()
    start = css.index("#boards-root {")
    block = css[start:css.index("}", start)]
    return re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", block, flags=re.S))


def test_boards_fills_by_flex_not_percentage_height():
    decls = _boards_root_declarations()
    assert "flex:1 1 auto".replace(" ", "") in decls
    assert "height:100%" not in decls, \
        "#boards-root must fill by flex; a percentage height against a " \
        "flex-sized parent is what unpinned the header on Boards"


def test_boards_root_can_shrink_so_inner_scrollers_work():
    """Without min-height:0 a flex item refuses to shrink below its content,
    so .b-column-cards never gets a bounded box to scroll inside."""
    assert "min-height:0" in _boards_root_declarations()


def test_header_is_explicitly_pinned():
    """Don't leave the pinning to body{overflow:hidden} alone — that's an
    invisible dependency, and iOS Safari scrolls its visual viewport anyway."""
    html = re.sub(r"/\*.*?\*/", "", (STATIC / "index.html").read_text(), flags=re.S)
    header = html[html.index("    header {"):]
    header = header[:header.index("}")]
    assert "position: sticky" in header
    assert "top: 0" in header
