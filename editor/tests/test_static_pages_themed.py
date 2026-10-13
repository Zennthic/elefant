"""Every page opts into the shared theme, the same way, in the same order.

The theme is three shared assets: theme.js resolves the stored preferences and
sets data-theme / data-palette before paint; appearance.css holds every colour
token (generated from palettes.json); elefant-ui.css is the foundation rules.
A page that forgets one renders unstyled or in the wrong mode while the rest
of the app does not — which is exactly the bug the shared theme replaced, so
it's worth a guard rather than trusting review.

The pages themselves define no colours any more. That half of the contract is
guarded in test_ui_palettes.py.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
UI_STATIC = Path(__file__).resolve().parent.parent.parent / "memory-ui" / "static"

# board.html is a redirect shim: it replaces location on load and never paints.
EXEMPT = {"board.html"}

PAGES = sorted(p for p in STATIC.glob("*.html") if p.name not in EXEMPT)
if UI_STATIC.is_dir():
    PAGES += sorted(UI_STATIC.glob("*.html"))

ASSETS = ('<script src="/theme.js">',
          '<link rel="stylesheet" href="/appearance.css">',
          '<link rel="stylesheet" href="/elefant-ui.css">')


def test_shared_theme_assets_exist():
    for name in ("theme.js", "appearance.css", "elefant-ui.css", "palettes.json"):
        assert (STATIC / name).is_file(), name


def test_the_old_theme_sheet_is_gone():
    """theme.css supplied light-only overrides on top of each page's inline
    dark palette. Both halves of that arrangement are gone; a stray copy would
    be loaded by nothing and edited by somebody."""
    assert not (STATIC / "theme.css").exists()
    if UI_STATIC.is_dir():
        assert not (UI_STATIC / "theme.css").exists()


@pytest.mark.parametrize("page", PAGES, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_page_loads_the_shared_assets_by_absolute_path(page):
    """Absolute, because a relative href resolves against the page's own URL:
    /platform/admin asked for /platform/theme.js, which nothing serves, and
    silently ran with no theme script at all for as long as it was relative."""
    html = page.read_text()
    for tag in ASSETS:
        assert tag in html, f"{page.name} does not load {tag}"
    assert 'href="theme.css"' not in html and 'href="/theme.css"' not in html


@pytest.mark.parametrize("page", PAGES, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_theme_script_runs_first_and_page_styles_come_last(page):
    """theme.js must run before anything paints, or the page flashes the wrong
    mode. The page's own <style> must come AFTER the foundation, so a page can
    refine elefant-ui.css without fighting it on specificity."""
    # Comments first: the note above these tags itself mentions "<style>".
    html = re.sub(r"<!--.*?-->", "", page.read_text(), flags=re.S)
    positions = [html.index(tag) for tag in ASSETS]
    assert positions == sorted(positions), f"{page.name} loads the theme assets out of order"
    if "<style" in html:
        assert html.index(ASSETS[-1]) < html.index("<style"), (
            f"{page.name}'s own styles load before the shared foundation"
        )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_no_page_pins_itself_to_a_mode(page):
    """A page must not force the theme; theme.js owns it. admin.html used to
    hardcode class="dark" and style light mode as html:not(.dark)."""
    html = page.read_text()
    assert "classList.add('dark')" not in html
    assert 'class="dark"' not in re.sub(r"<!--.*?-->", "", html.split("<body", 1)[0], flags=re.S)
    assert "html:not(.dark)" not in html


def test_no_dead_stylesheet_links():
    """Every local stylesheet a page references must actually exist — mfa.html
    shipped a 404ing auth.css link for some time. Absolute links resolve to the
    memory-ui static root, which is where nginx serves them from."""
    missing = []
    for page in PAGES:
        for href in re.findall(r'<link[^>]+href="([^"#?:]+\.css)"', page.read_text()):
            if "//" in href:
                continue
            base = UI_STATIC if href.startswith("/") else page.parent
            if not (base / href.lstrip("/")).is_file():
                missing.append(f"{page.name} -> {href}")
    assert not missing, f"dead stylesheet links: {missing}"
