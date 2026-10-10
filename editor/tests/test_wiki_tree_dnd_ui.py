"""Drag-and-drop reorganising in the wiki tree is wired up in the SPA (#121).

The behaviour itself is exercised for real in memory-ui/test/wiki-tree-dnd.mjs,
which runs the module in jsdom. These are the cheap structural guards for the
pieces that live far apart in a 4k-line single-file SPA and that break the
feature silently when one of them goes: the modal markup, the two call sites in
renderTree, the root drop zone, and the poll interlock.
"""
from pathlib import Path

import pytest

SPA = Path(__file__).resolve().parent.parent.parent / "memory-ui" / "static" / "index.html"


@pytest.fixture(scope="module")
def html():
    return SPA.read_text()


@pytest.fixture(scope="module")
def dnd(html):
    """The drag-and-drop module, carved out of the SPA's script."""
    start = html.index("// WIKI TREE DRAG-AND-DROP REORGANISE")
    return html[start:html.index("// SETTINGS SUB-TABS", start)]


def test_confirmation_modal_exists(html):
    """A drop is confirmed, not applied — the modal is the whole point of the card."""
    modal = html[html.index('id="tree-move-modal"'):]
    modal = modal[:modal.index("<!-- Share modal")]
    for el in ("tree-move-modal-title", "tree-move-from", "tree-move-to", "tree-move-confirm"):
        assert f'id="{el}"' in modal, f"the confirmation lost #{el}"
    assert "closeTreeMoveModal()" in modal


def test_render_tree_wires_both_row_kinds(html):
    """Files drag; folders drag AND accept. Losing either call site removes half
    the feature with nothing else breaking."""
    fn = html[html.index("function renderTree("):html.index("async function loadFileContent(")]
    assert "wireTreeDragSource(header, { kind: 'dir', path: dirPath })" in fn
    assert "wireTreeDropTarget(header, dirPath)" in fn
    assert "wireTreeDragSource(fileEl, { kind: 'file', path: item.path || item.name })" in fn
    # A page row is not a destination, but it must still swallow the drag or the
    # root zone behind it accepts a drop aimed at the page.
    assert "wireTreeDropInert(fileEl)" in fn


def test_root_drop_zone_is_wired_once(dnd, html):
    """loadWikiTree() replaces the container's contents, not the container, and
    the tree re-renders every 15s — re-wiring per render would stack a listener
    per refresh until a single drop fired dozens of handlers."""
    assert "wireTreeRootDropZone();" in html
    assert "if (wikiRootDropWired) return;" in dnd


def test_poll_stands_down_mid_drag(html):
    """The 15s tree poll re-renders the tree. Doing that during a drag destroys
    the drag source and every drop target under the cursor."""
    fn = html[html.index("async function refreshWikiTreeIfChanged("):]
    fn = fn[:fn.index("\n}\n")]
    assert "if (wikiDragItem || wikiPendingMove) return;" in fn


def test_a_drop_opens_the_confirmation_rather_than_moving(dnd):
    """No drop handler may call the API directly."""
    for handler in ("wireTreeDropTarget", "wireTreeRootDropZone"):
        fn = dnd[dnd.index(f"function {handler}("):]
        fn = fn[:fn.index("\n}\n")]
        assert "apiFetch" not in fn, f"{handler} moves something without confirming"
        assert "openTreeMoveModal(" in fn


def test_a_folder_cannot_be_dropped_into_its_own_subtree(dnd):
    """The move is a prefix copy-then-delete server-side; into its own subtree it
    would walk keys it is still creating."""
    fn = dnd[dnd.index("function wikiDropRefusal("):]
    fn = fn[:fn.index("\n}\n")]
    assert "item.path === destDir" in fn
    # The trailing slash is what keeps a sibling named "AI-archive" out of it.
    assert "destDir.startsWith(item.path + '/')" in fn
    # A no-op move is refused too, so a jittery drag can't churn S3 keys.
    assert "wikiParentDir(item.path) === destDir" in fn


def test_each_kind_goes_to_its_own_endpoint(dnd):
    """A folder is a key prefix, not a file — /api/move-file would 404 on it."""
    fn = dnd[dnd.index("async function confirmTreeMove("):]
    fn = fn[:fn.index("\n}\n")]
    assert "move.kind === 'dir' ? '/api/rename-dir' : '/api/move-file'" in fn


def test_a_refused_row_does_not_fall_through_to_the_root(dnd):
    """Without stopPropagation on the refused path, hovering a page's own folder
    bubbles to the tree background and arms "move to the wiki root" instead."""
    fn = dnd[dnd.index("function wireTreeDropTarget("):]
    fn = fn[:fn.index("\n}\n")]
    over = fn[fn.index("'dragover'"):fn.index("'dragleave'")]
    assert over.index("e.stopPropagation();") < over.index("wikiDropRefusal("), (
        "the refusal must not return before the event is stopped"
    )
    assert "unpaintTreeRoot();" in over


def test_paths_reach_the_confirmation_as_text(dnd):
    """Page and folder names are user input; the confirmation must not build
    markup out of them."""
    fn = dnd[dnd.index("function openTreeMoveModal("):]
    fn = fn[:fn.index("\n}\n")]
    assert "innerHTML" not in fn
    assert "document.getElementById('tree-move-from').textContent = item.path;" in fn


def test_the_open_page_follows_its_own_move(dnd):
    """Otherwise the viewer keeps showing a path that no longer exists, and the
    next save writes the page back to where it was moved from."""
    fn = dnd[dnd.index("async function confirmTreeMove("):]
    fn = fn[:fn.index("\n}\n")]
    assert "currentFilePath.startsWith(move.path + '/')" in fn   # moved with its folder
    assert "currentFilePath === move.path" in fn                 # moved on its own
    assert "loadFileContent(reopen)" in fn


def test_the_root_drop_band_sticks_to_the_scrollport(html):
    """#wiki-tree is the scroll container. A band that scrolled away would
    strand "move to the wiki root" as a destination halfway down a long tree —
    and jsdom does no layout, so the rule itself is what can be checked."""
    band = html[html.index("#wiki-tree.tree-dragging::before"):]
    band = band[:band.index("}")]
    assert "position: sticky" in band
    assert "top: 0" in band
    # Rows scroll UNDER it, so it needs a background of its own.
    assert "background: var(--panel)" in band


def test_inline_folder_rename_suspends_the_drag(html):
    """Renaming a folder puts an <input> inside the header, which is now a drag
    source: left draggable, a drag inside the input starts a folder move instead
    of selecting text."""
    fn = html[html.index("async function renameDir("):]
    fn = fn[:fn.index("\n}\n")]
    assert "headerEl.draggable = false;" in fn
    assert "headerEl.draggable = true;" in fn
    # Every teardown path goes through restoreName(), so none of them can be
    # the one that leaves a folder permanently un-draggable. The only put-back
    # of the text node is the one inside restoreName itself.
    assert fn.count("input.replaceWith(nameTextNode)") == 1
    assert fn.count("restoreName()") == 4   # empty/unchanged, failed, Escape, blur
