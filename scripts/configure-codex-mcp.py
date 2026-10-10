#!/usr/bin/env python3
"""Replace a Codex MCP bearer-token environment reference with a static header."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: configure-codex-mcp.py CONFIG_PATH SERVER_NAME",
            file=sys.stderr,
        )
        return 2

    token = os.environ.get("ELEFANT_INSTALL_TOKEN", "")
    if not token:
        print("ELEFANT_INSTALL_TOKEN is empty", file=sys.stderr)
        return 2

    config_path = Path(sys.argv[1])
    server_name = sys.argv[2]
    section = f"[mcp_servers.{server_name}]"

    lines = config_path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(section)
    except ValueError:
        print(f"Codex did not create the expected {section} section", file=sys.stderr)
        return 1

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("["):
            end = index
            break

    server_lines = [
        line
        for line in lines[start + 1 : end]
        if not line.lstrip().startswith(
            ("http_headers =", "bearer_token_env_var =")
        )
    ]
    header = json.dumps(f"Bearer {token}")
    updated = (
        lines[: start + 1]
        + [f"http_headers = {{ Authorization = {header} }}"]
        + server_lines
        + lines[end:]
    )

    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_path.parent, delete=False
    ) as handle:
        handle.write("\n".join(updated) + "\n")
        temporary_path = Path(handle.name)

    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
