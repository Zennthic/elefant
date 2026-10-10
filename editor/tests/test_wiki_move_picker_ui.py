"""The page Move dialog picks a destination folder rather than typing one.

The behaviour runs for real in memory-ui/test/wiki-move-picker.mjs (jsdom).
These are the structural guards CI can run, and they exist because the way
this regresses is quiet: put a text input back and moves keep working, right
up until someone types "Hadware/" and the page is filed under a folder that
did not exist a moment ago.
"""
from pathlib import Path

import pytest

SPA = Path(__file__).resolve().parent.parent.parent / "memory-ui" / "static" / "index.html"


@pytest.fixture(scope="module")
def html():
    return SPA.read_text()


@pytest.fixture(scope="module")
def modal(html):
    start = html.index("<!-- Move-file modal")
    return html[start:html.index("<!-- Drag-and-drop reorganise", start)]


def test_the_folder_half_is_a_picker_not_a_text_field(modal):
    """S3 has no directories, so a typo'd folder is not an error — it is a new
    folder. The only defence is not being able to type one."""
    assert '<select id="move-modal-folder"' in modal
    assert 'id="move-modal-dest"' not in modal, "the free-text path field is back"
    # The filter is a text field, but it filters — it is not the destination.
    assert 'id="move-modal-filter"' in modal


def test_the_page_name_is_still_free_text(modal):
    """Only the folder half was ever the problem; a move that could not rename
    would be a feature removed, not a bug fixed."""
    assert '<input type="text" id="move-modal-name"' in modal


def test_the_resulting_path_is_shown_before_it_is_applied(modal):
    assert 'id="move-modal-preview"' in modal


def test_making_a_folder_is_pointed_somewhere_real(modal):
    """A picker with no escape hatch is a dead end the first time someone wants
    a folder that does not exist yet."""
    assert "made in the tree" in modal


def test_the_destination_is_assembled_in_exactly_one_place(html):
    """The preview and the Move button must not be able to disagree."""
    fn = html[html.index("function moveModalDestination("):]
    fn = fn[:fn.index("\n}\n")]
    assert "folder.value" in fn and "name.value" in fn
    for caller in ("function updateMoveModalPreview(", "async function confirmMove("):
        body = html[html.index(caller):]
        body = body[:body.index("\n}\n")]
        assert "moveModalDestination()" in body, f"{caller} builds its own path"


def test_the_folder_list_comes_from_the_tree_that_is_loaded(html):
    """Built with the page index, so a folder renamed away cannot still be
    offered, and the internal assets folder is not offered at all."""
    fn = html[html.index("function buildWikiIndex("):html.index("function _joinWikiPath(")]
    assert "_wikiDirs = dirs;" in fn
    assert "HIDDEN_TREE_DIRS.has(" in fn
    assert "const dirs = [''];" in fn, "the wiki root must be a destination"


def test_folder_names_reach_the_picker_as_text(html):
    """Folder names are user input and the options are built by hand."""
    fn = html[html.index("function renderMoveModalFolders("):]
    fn = fn[:fn.index("\n}\n")]
    assert "opt.textContent = d ||" in fn
    assert "innerHTML = ''" in fn, "clearing is the only innerHTML use allowed here"
    assert "opt.innerHTML" not in fn


def test_the_current_folder_cannot_be_filtered_away(html):
    """Otherwise typing in the filter silently re-points the move at whatever
    happens to be first in the narrowed list."""
    fn = html[html.index("function moveModalFolders("):]
    fn = fn[:fn.index("\n}\n")]
    assert "out.unshift(keep)" in fn


def test_a_move_to_nowhere_is_not_offered(html):
    fn = html[html.index("function updateMoveModalPreview("):]
    fn = fn[:fn.index("\n}\n")]
    assert "confirm.disabled = !dest || dest === currentFilePath;" in fn
