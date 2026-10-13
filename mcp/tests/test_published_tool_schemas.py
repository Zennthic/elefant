"""docs/reference/mcp-tools.json is the published tool surface, and it is true.

The house publication standard: a consumer must not have to read the code to
learn its shape, and a published description is generated from what actually
routes, never kept by hand. tools/publish_mcp_tools.py writes the file from the
registered tools; this fails when a tool changes and the file was not
regenerated, with the command that fixes it.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "tools" / "publish_mcp_tools.py"
OUT = REPO / "docs" / "reference" / "mcp-tools.json"


def _publisher():
    spec = importlib.util.spec_from_file_location("publish_mcp_tools", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_published_schemas_match_the_registered_tools():
    assert OUT.exists(), f"{OUT.relative_to(REPO)} is missing: run python {SCRIPT.relative_to(REPO)}"
    assert OUT.read_text() == _publisher().published(), (
        f"{OUT.relative_to(REPO)} is out of date with mcp/main.py. "
        f"Run `python {SCRIPT.relative_to(REPO)}` and commit the result.")


def test_the_diagram_tools_are_published_with_their_annotations():
    import json
    tools = {t["name"]: t for t in json.loads(OUT.read_text())["tools"]}
    for name in ("diagram_create", "diagram_get", "diagram_patch", "diagram_render", "diagram_list"):
        assert name in tools, name
        assert "readOnlyHint" in tools[name]["annotations"], name
    assert "if_version" in tools["diagram_patch"]["inputSchema"]["required"]
