"""The wiki page's back-link to its board card is wired up in the SPA.

The card→wiki direction has a chip; the reverse didn't exist, so a page could
be part of a card's thread with no way back to the board. These are cheap
structural guards on the single-file SPA — the pieces are far apart in a
9k-line document, and losing any one of them silently removes the back-link
without breaking anything else.
"""
from pathlib import Path

import pytest

SPA = Path(__file__).resolve().parent.parent.parent / "memory-ui" / "static" / "index.html"


@pytest.fixture(scope="module")
def html():
    return SPA.read_text()


def test_action_bar_hosts_the_chip(html):
    """The chip lives in the wiki action row, alongside Share/History/Edit."""
    # Located by the chip rather than by the bar's inline style: the bar has
    # been restyled twice already (the actions moved into their own cluster so
    # they wrap as a group on a phone, then the bar was pinned to the top of
    # the viewer), and a guard pinned to a style attribute — or to the exact
    # class list — fails on cosmetics while missing the thing it guards.
    chip = html.index('id="wiki-card-refs"')
    bar = html[html.rindex('<div class="file-path-bar', 0, chip):html.index("</div>", chip)]
    assert 'id="wiki-card-refs"' in bar
    for action in ("openShareModal", "showWikiHistory", "startInlineEdit"):
        assert action in bar


def test_render_file_view_asks_for_the_cards(html):
    assert "renderWikiCardRefs(currentFilePath)" in html
    assert "async function renderWikiCardRefs(" in html


def test_chip_targets_the_boards_tab(html):
    """#boards/<slug> is the SPA's card deep-link (the hashchange router opens
    the card); board.html is the retired standalone page."""
    assert "href=\"#boards/${encodeURIComponent(c.slug)}\"" in html
    fn = html[html.index("async function renderWikiCardRefs("):]
    fn = fn[:fn.index("\n}\n")]
    assert "board.html" not in fn


def test_chip_escapes_card_supplied_text(html):
    """Card titles and slugs are user input rendered via innerHTML."""
    fn = html[html.index("async function renderWikiCardRefs("):]
    fn = fn[:fn.index("\n}\n")]
    assert "esc(c.title || c.slug)" in fn
    assert "esc(tip)" in fn
    # The column colour goes into a style attribute — allow only a hex literal.
    assert "/^#[0-9a-fA-F]{3,8}$/.test(c.color)" in fn


def test_empty_chip_host_collapses(html):
    """A page with no card must render the row exactly as it always did — an
    empty flex item would otherwise leave a stray gap before the buttons."""
    assert "#wiki-card-refs:empty { display:none;" in html


def test_long_titles_truncate(html):
    """A long card title can't be allowed to push the actions off the row."""
    assert ".card-ref .card-chip-label" in html
    css = html[html.index(".card-ref .card-chip-label"):]
    css = css[:css.index("\n")]
    assert "text-overflow:ellipsis" in css


def test_archived_lozenge_uses_a_theme_token(html):
    """It used to be a fixed pale red — invisible on a light background."""
    css = html[html.index(".card-chip .card-chip-loz.arch"):]
    css = css[:css.index("\n")]
    assert "var(--danger)" in css
