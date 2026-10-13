"""The UI palettes, as the editor knows them.

The palettes themselves live in static/palettes.json — the one file every
colour in the UI is generated from (see tools/build_ui_assets.py). The editor
reads the same file to validate a user's saved choice, so the list of palettes
it will accept can never disagree with the list the CSS actually defines.
"""
from __future__ import annotations

import json
from pathlib import Path

PALETTES = json.loads((Path(__file__).parent / "static" / "palettes.json").read_text())
PALETTE_IDS = frozenset(p["id"] for p in PALETTES)
DEFAULT_PALETTE = "indigo"

assert DEFAULT_PALETTE in PALETTE_IDS, "the default palette is not in palettes.json"
