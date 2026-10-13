"""One palette source, and nothing on any page that routes around it.

Every colour in the UI is a token generated from editor/static/palettes.json by
tools/build_ui_assets.py into appearance.css. Before that, colours were
hand-maintained in fifteen places — an inline dark palette per page, a light
sheet, a second pair inside the SPA and a third inside boards.css — and had
drifted into two different dark themes. The guards below are what keep it one:

  * the generated files are current, and the per-image copies identical;
  * every palette is complete in both modes, and readable (WCAG contrast);
  * no page defines a colour token of its own, uses a retired token name,
    references a token nobody defines, or hardcodes a colour outside a short,
    named allow-list of things that are colours as DATA rather than as style.
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
EDITOR_STATIC = REPO / "editor" / "static"
UI_STATIC = REPO / "memory-ui" / "static"
TOOL = REPO / "tools" / "build_ui_assets.py"

pytestmark = pytest.mark.skipif(
    not (TOOL.exists() and UI_STATIC.is_dir()),
    reason="tools/ and memory-ui/ are not checked out beside the editor",
)


def _tool():
    spec = importlib.util.spec_from_file_location("build_ui_assets", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _tool()


@pytest.fixture(scope="module")
def palettes(tool):
    return tool.load_palettes()


def _pages():
    """Every file a colour could hide in, minus the generated sheet itself and
    the vendored third-party bundles."""
    out = []
    for d in (EDITOR_STATIC, UI_STATIC):
        for p in sorted(d.iterdir()):
            if p.suffix not in (".html", ".css", ".js"):
                continue
            if p.name in ("appearance.css", "tiptap.bundle.js", "marked.min.js", "mermaid.min.js"):
                continue
            out.append(p)
    return out


def _style_text(path):
    """The parts of a file that are CSS or build CSS: <style> blocks, style=
    attributes, and — because the board and the SPA build markup in JS — the
    script too. Comments are stripped, since they are full of issue numbers
    like #121 that look exactly like a colour."""
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    text = re.sub(r"(?<=[;{}\s])//[^\n'\"`]*$", "", text, flags=re.M)
    return text


# ── the generator ──────────────────────────────────────────────────────────

def test_generated_assets_are_current_and_the_mirrors_identical():
    """The editor and memory-ui are separate images that cannot read each
    other's files, so each ships a copy. A copy edited by hand, or a JSON
    change without a rerun, is what this catches."""
    r = subprocess.run([sys.executable, str(TOOL), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_each_shared_asset_has_exactly_one_source_and_one_mirror(tool):
    """The shared assets live in editor/static and are mirrored into
    memory-ui/static; nowhere else. The editor once carried a second logo
    under static/logos/ that nothing referenced and nothing kept in step. A
    third copy is one --check cannot see, so it is refused here instead."""
    allowed = {EDITOR_STATIC, UI_STATIC}
    skip = {".git", "node_modules", ".venv", "__pycache__"}
    names = set(tool.MIRRORED) | {"appearance.css"}
    strays = sorted(
        str(p.relative_to(REPO))
        for p in REPO.rglob("*")
        if p.name in names and p.is_file() and p.parent not in allowed
        and not skip.intersection(p.relative_to(REPO).parts)
    )
    assert not strays, f"shared UI asset copied outside the mirror pair: {strays}"


def test_the_editor_validates_against_the_same_palette_file(palettes):
    sys.path.insert(0, str(REPO / "editor"))
    try:
        import appearance
    finally:
        sys.path.pop(0)
    assert appearance.PALETTE_IDS == {p["id"] for p in palettes}
    assert appearance.DEFAULT_PALETTE == "indigo"


def test_every_palette_is_complete_in_both_modes(tool, palettes):
    for p in palettes:
        for mode in ("light", "dark"):
            assert set(p[mode]) == set(tool.PALETTE_KEYS), f"{p['id']}/{mode}"
            for key, value in p[mode].items():
                assert re.fullmatch(r"#[0-9a-f]{6}", value), f"{p['id']}/{mode}/{key}={value}"


def test_appearance_css_has_both_axes_for_every_palette(palettes):
    css = (EDITOR_STATIC / "appearance.css").read_text()
    for p in palettes:
        for mode in ("light", "dark"):
            assert f"html[data-theme='{mode}'][data-palette='{p['id']}']" in css
            # A theme island: an element carrying data-theme takes that mode in
            # the page's palette. The memory graph depends on it.
            assert f"html[data-palette='{p['id']}'] [data-theme='{mode}']" in css


def test_a_page_whose_script_never_ran_still_has_every_token():
    """The unqualified :root block is the fallback. If it were missing a token,
    a page that loaded appearance.css but not theme.js would silently lose
    that colour."""
    css = (EDITOR_STATIC / "appearance.css").read_text()
    blocks = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    fallback = next(b for s, b in blocks if s.strip().startswith(":root, [data-theme='dark']"))
    palette_block = next(b for s, b in blocks if "data-palette='forest'" in s)
    names = lambda b: set(re.findall(r"--([\w-]+):", b))
    assert names(palette_block) <= names(fallback)


# ── readability ────────────────────────────────────────────────────────────

# (foreground, background, minimum). 4.5:1 is WCAG AA for body text; 3:1 is
# the non-text minimum for the boundary of a control (WCAG 1.4.11).
PAIRS = [
    ("text", "panel", 4.5), ("text", "bg", 4.5), ("text", "raised", 4.5),
    ("muted", "panel", 4.5), ("muted", "bg", 4.5), ("muted", "raised", 4.5),
    ("accent", "panel", 4.5), ("accent", "bg", 4.5), ("accent", "accentbg", 4.5),
    ("on-accent", "accent", 4.5), ("accent2", "panel", 4.5),
    ("danger", "panel", 4.5), ("danger", "dangerbg", 4.5), ("on-danger", "danger", 4.5),
    ("warning", "panel", 4.5), ("warning", "warningbg", 4.5), ("on-warning", "warning", 4.5),
    ("success", "panel", 4.5), ("success", "successbg", 4.5),
    ("line-strong", "panel", 3.0), ("line-strong", "bg", 3.0),
]


@pytest.mark.parametrize("fg,bg,minimum", PAIRS, ids=lambda v: str(v))
def test_every_palette_is_readable_in_both_modes(tool, palettes, fg, bg, minimum):
    failures = []
    for p in palettes:
        for mode in ("light", "dark"):
            t = tool.tokens(p, mode)
            c = tool.contrast(t[fg], t[bg])
            if c < minimum:
                failures.append(f"{p['id']}/{mode}: {fg} on {bg} is {c:.2f}")
    assert not failures, f"below {minimum}:1 — " + "; ".join(failures)


# ── nothing routes around the palette ──────────────────────────────────────

def test_no_page_defines_a_colour_token_of_its_own():
    """A page-local `--accent: #…` is exactly how fifteen palettes happened.
    The only custom properties a page may set are ones it computes at runtime
    for a single element, and none of those currently exist."""
    offenders = []
    for p in _pages():
        for m in re.finditer(r"(?<![\w-])(--[a-z][\w-]*)\s*:\s*[^;{}]+;", _style_text(p)):
            offenders.append(f"{p.name}: {m.group(0)[:60]}")
    assert not offenders, offenders


RETIRED = {
    # the pre-palette names, each with what replaced it
    "surface": "panel", "card-bg": "panel", "border": "line", "border-vis": "line-strong",
    "input-bg": "field", "input-bg-soft": "raised", "input-focus": "accentbg",
    "hover-bg": "raised", "accent-h": "accent-hover", "danger-h": "danger-hover",
    "danger-bg": "dangerbg", "danger-br": "dangerline", "ok": "success", "ok-bg": "successbg",
    "ok-br": "successline", "warn": "warning", "warn-bg": "warningbg", "note-bg": "accentbg",
    "note-br": "accentline", "error": "danger", "pink": "accent2", "blue": "accent",
    "blue-dark": "accent-hover", "blue-light": "link", "slate": "panel", "slate-light": "raised",
    "on-slate": "text", "on-slate-dim": "muted", "on-slate-hover": "text", "code-bg": "raised",
    "code-fg": "text", "amber": "accent2 or warning", "amber-dark": "accent2-hover",
    "amber-bg": "warningbg", "amber-br": "warningline",
}


def test_no_page_uses_a_retired_token():
    used = []
    for p in _pages():
        for name in re.findall(r"var\(--([\w-]+)", p.read_text()):
            if name in RETIRED or name.startswith("b-"):
                used.append(f"{p.name}: --{name} (now --{RETIRED.get(name, 'see boards.css')})")
    assert not used, used


def test_every_token_a_page_uses_is_defined():
    """A var() naming a token nobody defines renders as the property's initial
    value — usually transparent or black — with no error anywhere."""
    css = (EDITOR_STATIC / "appearance.css").read_text()
    defined = set(re.findall(r"--([\w-]+):", css))
    undefined = []
    for p in _pages():
        for name in re.findall(r"var\(--([\w-]+)", p.read_text()):
            if name not in defined:
                undefined.append(f"{p.name}: --{name}")
    assert not undefined, sorted(set(undefined))


# Colours that are DATA, not style, and so must NOT follow the palette: each
# means the same thing in every palette and mode, or it means nothing. Each is
# marked where it is defined ("Allow-listed in test_ui_palettes.py").
ALLOWED = [
    # memory-type colours: a categorical encoding shared by the graph and its legend
    (r"GRAPH_TYPE_COLOR\s*=\s*\{[^}]*\}", "graph type colours"),
    (r"GRAPH_TYPE_COLOR\[n\.type\]\s*\|\|\s*'#6366F1'", "graph type fallback"),
    (r'<span class="(?:graph-legend-dot|legend-dot)" style="background:#[0-9A-Fa-f]{6}">', "graph legend"),
    # column colours a person picks, stored on the column
    (r"const SWATCHES = \[[^\]]*\]", "column colour swatches"),
    # a QR code needs a light quiet zone to scan, whatever the theme
    (r"\.qr \{ background:#fff;", "QR code quiet zone"),
]

COLOUR = re.compile(r"(?<![&\w])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgba?\(\s*\d")


def test_no_page_hardcodes_a_colour():
    """Every colour is a token, so it changes with the palette and the mode.
    A hardcoded one is the thing that stays bright green on a white page."""
    offenders = []
    for p in _pages():
        text = _style_text(p)
        for pattern, _why in ALLOWED:
            text = re.sub(pattern, "", text)
        for m in COLOUR.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            ctx = text[max(0, m.start() - 40):m.end() + 20].replace("\n", " ")
            offenders.append(f"{p.name}:{line}: …{ctx}…")
    assert not offenders, "\n".join(offenders)


def test_the_allow_list_still_matches_something():
    """An allow-list entry that no longer matches is a hole waiting to be
    reused for something it was never meant to excuse."""
    blob = "\n".join(_style_text(p) for p in _pages())
    for pattern, why in ALLOWED:
        assert re.search(pattern, blob), f"allow-list entry for {why!r} matches nothing"


# ── the runtime half ───────────────────────────────────────────────────────

def test_theme_js_applies_the_palette_axis():
    js = (EDITOR_STATIC / "theme.js").read_text()
    assert "elefant-palette" in js
    assert "data-palette" in js
    # A stored value reaches an HTML attribute, so it is shape-checked first.
    assert re.search(r"/\^\[a-z\]\[a-z0-9-\]\{0,23\}\$/", js), "the stored palette is not validated"


def test_the_shared_assets_are_reachable_without_a_session():
    """The login page, a /share/<token> viewer and the /graph embed are all
    seen by people who are NOT logged in, and no page carries a palette of its
    own any more. Behind the catch-all's auth_request these assets would come
    back as a login redirect, and those pages would render with no colours."""
    conf = (REPO / "nginx" / "nginx.conf").read_text()
    for asset in ("theme.js", "appearance.css", "elefant-ui.css", "palettes.json"):
        m = re.search(r"location = /" + re.escape(asset) + r" \{([^}]*)\}", conf)
        assert m, f"no public location for /{asset}"
        assert "auth_request" not in m.group(1), f"/{asset} requires a session"
        assert f"proxy_pass         http://memory_ui/{asset};" in m.group(1)


def test_the_palette_picker_reads_the_same_file_the_css_is_built_from():
    html = (UI_STATIC / "index.html").read_text()
    assert "fetch('/palettes.json')" in html
    assert 'id="palette-toggle"' in html and 'id="palette-menu"' in html
    assert "Palette.adopt(data.palette)" in html, "the account's palette is never adopted"


def test_the_graph_is_a_dark_island_in_both_modes():
    """The 3D graph stays dark in both modes by design (see graph.html), but in
    the current palette's dark — not a private near-black of its own."""
    assert 'class="graph-stage" data-theme="dark"' in (UI_STATIC / "index.html").read_text()
    assert '<body data-theme="dark">' in (UI_STATIC / "graph.html").read_text()


def test_the_editor_image_ships_the_palette_module():
    """The Dockerfile copies an explicit list of modules. Leave appearance.py
    off it and every palette save 500s in the image while passing every test
    here, which imports from the source tree."""
    df = (REPO / "editor" / "Dockerfile").read_text()
    assert re.search(r"^COPY [^\n]*\bappearance\.py\b", df, re.M)
