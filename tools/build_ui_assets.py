#!/usr/bin/env python3
"""Build the UI's shared theme assets from one source of truth.

    python3 tools/build_ui_assets.py           # write the generated files
    python3 tools/build_ui_assets.py --check   # exit 1 if any are stale

WHY THIS EXISTS. Elefant's colours used to be hand-maintained in fifteen
places: an inline dark palette on every server-rendered page, a light palette
in theme.css, a second light-and-dark pair inside the SPA, and a third pair in
boards.css under its own `--b-*` names. They had drifted into two different
dark themes, and changing an accent meant finding every copy.

Now there is one: `editor/static/palettes.json`. Every colour the UI uses is a
token generated from it into `appearance.css`, and a page never defines a
colour of its own. Adding a palette is a JSON edit and a rerun of this script.

TWO INDEPENDENT AXES: `data-theme` (light | dark) and
`data-palette` (indigo, forest, …) on <html>. Every palette has both modes, so
a choice on one axis never constrains the other.

WHAT IS GENERATED, AND WHERE. The editor and memory-ui are separate images
with separate build contexts — neither can read the other's directory at
build time — so each ships its own copy of the shared assets. This script is
what keeps those copies identical; `--check` is what CI uses to prove it.

  editor/static/appearance.css       generated from palettes.json
  memory-ui/static/appearance.css    identical copy
  memory-ui/static/palettes.json     mirrored  (the SPA's palette picker reads it)
  memory-ui/static/theme.js          mirrored  (canonical copy: editor/static)
  memory-ui/static/elefant-ui.css    mirrored  (canonical copy: editor/static)
  memory-ui/static/elefant_transparent.png   mirrored  (canonical copy: editor/static)

Edit the canonical copies in editor/static, never the mirrors. Each shared
asset has exactly one source file; editor/tests/test_ui_palettes.py fails if a
third copy appears anywhere else in the tree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EDITOR_STATIC = REPO / "editor" / "static"
UI_STATIC = REPO / "memory-ui" / "static"
PALETTES = EDITOR_STATIC / "palettes.json"

DEFAULT_PALETTE = "indigo"
DEFAULT_MODE = "dark"          # the Zennthic theme; matches theme.js and the account default

# Every palette must supply exactly these, for both modes.
PALETTE_KEYS = (
    "stage", "bg", "panel", "raised", "line",
    "text", "muted",
    "accent", "accentbg", "on-accent", "accent2",
    "success", "successbg",
)

# Status colours that mean the same thing whatever the palette. A red that
# changed hue with the palette would stop reading as "wrong".
COMMON = {
    "light": {
        "danger": "#ad3434", "dangerbg": "#fbeaea",
        "warning": "#94600f", "warningbg": "#fbf0da",
        "on-danger": "#ffffff", "on-warning": "#ffffff",
    },
    "dark": {
        "danger": "#f19a9a", "dangerbg": "#4a2a2d",
        "warning": "#f1c175", "warningbg": "#4a3c24",
        "on-danger": "#1a0f10", "on-warning": "#1c1406",
    },
}

MIRRORED = ("palettes.json", "theme.js", "elefant-ui.css",
            "elefant_transparent.png")


# ── colour arithmetic ──────────────────────────────────────────────────────

def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """`a` moved `t` of the way towards `b`, in sRGB."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def luminance(hex_: str) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ── the token set ──────────────────────────────────────────────────────────

def tokens(pal: dict, mode: str) -> dict[str, str]:
    """Every colour token for one palette in one mode, in emission order.

    The palette supplies the base; everything else is DERIVED here, so a hover
    shade or a tinted border can never drift from the colour it belongs to.
    """
    base = {**pal[mode], **COMMON[mode]}
    dark = mode == "dark"
    # Hover moves AWAY from the background: lighter on dark, darker on light.
    toward = "#ffffff" if dark else "#000000"
    panel, text = base["panel"], base["text"]
    t = dict(base)
    # The border of a CONTROL (an input, a checkbox) has to clear 3:1 against
    # what it sits on to count as visible (WCAG 1.4.11). --line is decorative
    # and deliberately fainter; 0.5 is the smallest mix that clears 3:1 on
    # every palette with a margin, asserted in test_ui_palettes.py.
    t["line-strong"] = mix(base["line"], text, 0.5)
    t["field"] = base["bg"]
    t["link"] = base["accent"]
    t["accent-hover"] = mix(base["accent"], toward, 0.16 if dark else 0.14)
    t["accent2-hover"] = mix(base["accent2"], toward, 0.16 if dark else 0.14)
    t["danger-hover"] = mix(base["danger"], toward, 0.16 if dark else 0.14)
    t["accent2bg"] = mix(base["accent2"], panel, 0.84 if dark else 0.9)
    # "…line" is the thin border drawn around a tinted box: the colour at
    # about half strength, so the box reads as coloured without shouting.
    for name in ("accent", "accent2", "success", "danger", "warning"):
        t[f"{name}line"] = mix(t[name], panel, 0.5)
    t["scrollbar"] = mix(base["line"], text, 0.15)
    t["selection"] = mix(base["accent"], panel, 0.62 if dark else 0.72)
    return t


# Non-colour tokens: the same in every palette and mode.
SHAPE = {
    "font-sans": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "font-mono": "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, Consolas, monospace",
    "radius-xs": "5px",      # chips, tags, inline code
    "radius-sm": "7px",      # buttons, inputs — the control radius
    "radius-md": "9px",      # small cards (a board card), menus, popovers
    "radius": "11px",        # cards and panels — the card radius
    "radius-lg": "14px",     # dialogs
}

SHADOWS = {
    # A whisper, not a drop shadow: cards sit on the page by edge and
    # tone, and the shadow only stops them looking pasted on.
    "light": {
        "shadow-sm": "0 1px 2px rgba(16, 24, 40, 0.05)",
        "shadow": "0 1px 2px rgba(16, 24, 40, 0.04), 0 4px 18px rgba(16, 24, 40, 0.04)",
        "shadow-md": "0 8px 28px rgba(16, 24, 40, 0.12)",
        "shadow-lg": "0 24px 60px rgba(16, 24, 40, 0.22)",
        "overlay": "rgba(20, 24, 40, 0.38)",
    },
    "dark": {
        "shadow-sm": "0 1px 2px rgba(0, 0, 0, 0.35)",
        "shadow": "0 1px 2px rgba(0, 0, 0, 0.25), 0 4px 18px rgba(0, 0, 0, 0.18)",
        "shadow-md": "0 10px 30px rgba(0, 0, 0, 0.5)",
        "shadow-lg": "0 24px 64px rgba(0, 0, 0, 0.62)",
        "overlay": "rgba(0, 0, 0, 0.6)",
    },
}


def _block(selector: str, mode: str, toks: dict[str, str]) -> str:
    lines = [f"{selector} {{", f"  color-scheme: {mode};"]
    lines += [f"  --{k}: {v};" for k, v in toks.items()]
    lines += [f"  --{k}: {v};" for k, v in SHADOWS[mode].items()]
    lines.append("}")
    return "\n".join(lines)


def load_palettes() -> list[dict]:
    pals = json.loads(PALETTES.read_text())
    ids = [p["id"] for p in pals]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"duplicate palette ids in {PALETTES}: {ids}")
    if DEFAULT_PALETTE not in ids:
        raise SystemExit(f"default palette {DEFAULT_PALETTE!r} is not in {PALETTES}")
    for p in pals:
        for mode in ("light", "dark"):
            missing = set(PALETTE_KEYS) - set(p.get(mode, {}))
            extra = set(p.get(mode, {})) - set(PALETTE_KEYS)
            if missing or extra:
                raise SystemExit(f"{p['id']}/{mode}: missing {sorted(missing)}, unknown {sorted(extra)}")
    return pals


def render_appearance(pals: list[dict]) -> str:
    default = next(p for p in pals if p["id"] == DEFAULT_PALETTE)
    out = [
        "/* GENERATED by tools/build_ui_assets.py from editor/static/palettes.json.",
        " * Do not edit: change the JSON and rerun the script.",
        " *",
        " * Two independent axes on <html>: data-theme (light | dark) and",
        " * data-palette. theme.js sets both before first paint.",
        " *",
        f" * The first two blocks are the {DEFAULT_PALETTE} palette, so a page whose",
        " * script never ran still gets a complete, readable token set rather than",
        " * an empty one. Every palette block below outranks them.",
        " *",
        " * A descendant carrying data-theme is a THEME ISLAND: it takes that mode",
        " * in the page's palette (see build_ui_assets.py). */",
        "",
        ":root {",
    ]
    out += [f"  --{k}: {v};" for k, v in SHAPE.items()]
    out.append("}")
    out.append("")
    # THEME ISLANDS. An element inside the page carrying data-theme="dark" (or
    # "light") gets that mode's tokens IN THE CURRENT PALETTE, whatever mode
    # the page is in. The 3D memory graph is the reason: it stays dark in both
    # modes by design (see graph.html), and an island is what lets that dark
    # still follow the chosen palette rather than being a private one of its
    # own. Declared on the element, the island's tokens win over what it
    # would otherwise inherit from <html>.
    other = "light" if DEFAULT_MODE == "dark" else "dark"
    out.append(_block(f":root, [data-theme='{DEFAULT_MODE}']", DEFAULT_MODE,
                      tokens(default, DEFAULT_MODE)))
    out.append(_block(f":root[data-theme='{other}'], [data-theme='{other}']", other,
                      tokens(default, other)))
    for p in pals:
        for mode in ("light", "dark"):
            sel = (f"html[data-theme='{mode}'][data-palette='{p['id']}'],\n"
                   f"html[data-palette='{p['id']}'] [data-theme='{mode}']")
            out.append(_block(sel, mode, tokens(p, mode)))
    return "\n".join(out) + "\n"


def planned_outputs() -> dict[Path, bytes]:
    pals = load_palettes()
    css = render_appearance(pals).encode()
    outputs = {
        EDITOR_STATIC / "appearance.css": css,
        UI_STATIC / "appearance.css": css,
    }
    for name in MIRRORED:
        # Bytes, not text: the logo is a PNG.
        outputs[UI_STATIC / name] = (EDITOR_STATIC / name).read_bytes()
    return outputs


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, content in planned_outputs().items():
        current = path.read_bytes() if path.exists() else None
        if current == content:
            continue
        stale.append(path.relative_to(REPO))
        if not check:
            path.write_bytes(content)
    if check:
        if stale:
            print("stale UI assets (run python3 tools/build_ui_assets.py):")
            for p in stale:
                print(f"  {p}")
            return 1
        print("UI assets up to date.")
        return 0
    for p in stale:
        print(f"wrote {p}")
    if not stale:
        print("UI assets already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
