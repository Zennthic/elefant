#!/usr/bin/env python3
"""Publish the MCP server's tool schemas to docs/reference/mcp-tools.json.

The file is GENERATED from the tools the server actually registers (the same
objects `tools/list` serves), so a client author, or another service, can read
the surface without reading mcp/main.py or connecting first. It is never
edited by hand.

    python tools/publish_mcp_tools.py           rewrite the file
    python tools/publish_mcp_tools.py --check   exit 1 if it is out of date

Run it with the mcp service's requirements installed (the mcp test suite
checks it, so CI fails when a tool changes and this file does not).
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "reference" / "mcp-tools.json"


def published() -> str:
    # main.py reads these at import time; none of them affect the schemas.
    for k, v in {"INTERNAL_SERVICE_TOKEN": "publish", "EDITOR_URL": "http://editor:8080",
                 "EMBEDDER_URL": "http://embedder:8001",
                 "MEMORY_API_URL": "http://memory-api:8002"}.items():
        os.environ.setdefault(k, v)
    sys.path.insert(0, str(REPO / "mcp"))
    import main  # noqa: E402

    tools = []
    for t in sorted(main.mcp._tool_manager.list_tools(), key=lambda t: t.name):
        entry = {
            "name": t.name,
            "description": (t.description or "").strip(),
            "inputSchema": t.parameters,
        }
        if t.title:
            entry["title"] = t.title
        if t.annotations:
            entry["annotations"] = t.annotations.model_dump(exclude_none=True)
        tools.append(entry)
    doc = {
        "server": main.mcp.name,
        "generated_by": "tools/publish_mcp_tools.py",
        "note": "Generated from the registered tools. Do not edit by hand.",
        "tools": tools,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


if __name__ == "__main__":
    text = published()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(REPO)} is out of date with mcp/main.py. "
                  "Run `python tools/publish_mcp_tools.py` and commit it.", file=sys.stderr)
            sys.exit(1)
        print(f"{OUT.relative_to(REPO)} is up to date")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(REPO)}: {text.count(chr(10))} lines")
