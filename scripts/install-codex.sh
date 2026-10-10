#!/usr/bin/env bash
# Install the Elefant MCP server and skill into Codex.
#
# Invoked by `make install-codex`, but can be run directly.  Kept as a
# script rather than a Makefile recipe because the make shipped with
# macOS (GNU Make 3.81) predates .ONESHELL, so multi-line recipes run
# one line per shell there and lose variables, traps, and if-blocks.
#
# Must stay compatible with bash 3.2 (macOS /bin/bash) and BSD
# mktemp/install.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
ELEFANT_URL="${ELEFANT_URL:-}"
[[ -n "$ELEFANT_URL" ]] || { echo "Error: set ELEFANT_URL to your Elefant deployment, e.g. make install-codex ELEFANT_URL=https://elefant.example.com" >&2; exit 1; }
ELEFANT_MCP_URL="${ELEFANT_MCP_URL:-$ELEFANT_URL/mcp}"
ELEFANT_SKILL_FILE="${ELEFANT_SKILL_FILE:-$repo_root/skill/SKILL.md}"
ELEFANT_MCP_NAME="${ELEFANT_MCP_NAME:-elefant}"

command -v codex >/dev/null 2>&1 || { echo "Error: Codex is not installed or is not on PATH." >&2; exit 1; }
[[ -f "$ELEFANT_SKILL_FILE" ]] || { echo "Error: Elefant skill not found at $ELEFANT_SKILL_FILE." >&2; exit 1; }

skill_tmp="$(mktemp)"
trap 'rm -f "$skill_tmp"' EXIT
install -m 0600 "$ELEFANT_SKILL_FILE" "$skill_tmp"
[[ "$(head -n 1 "$skill_tmp")" == "---" ]] && grep -q '^name: elefant$' "$skill_tmp" || {
	echo "Error: $ELEFANT_SKILL_FILE is not a valid Elefant skill." >&2
	exit 1
}

if codex mcp get "$ELEFANT_MCP_NAME" >/dev/null 2>&1; then
	echo "Keeping the existing '$ELEFANT_MCP_NAME' MCP configuration."
	configured_with_token=false
else
	command -v python3 >/dev/null 2>&1 || { echo "Error: python3 is required to update Codex's TOML configuration safely." >&2; exit 1; }
	[[ -f "$repo_root/scripts/configure-codex-mcp.py" ]] || { echo "Error: Codex configuration helper is missing." >&2; exit 1; }

	echo "Elefant needs a personal API token."
	echo "  1. Open $ELEFANT_URL and sign in."
	echo "  2. Open My Settings (your name, top right) -> API tokens -> + New token."
	echo "     Name it for this machine and for Codex."
	echo "  3. Enable Memory Read, Memory Write, and Wiki Read."
	echo "     Enable Wiki Write and Board Write if you want Codex to author pages and cards."
	echo "  4. Copy the token when it is shown; Elefant displays it only once."
	echo
	read -r -s -p "Paste the Elefant token: " elefant_token
	echo
	[[ -n "$elefant_token" ]] || { echo "Error: no token was entered." >&2; exit 1; }

	ELEFANT_MCP_TOKEN="$elefant_token" codex mcp add \
		"$ELEFANT_MCP_NAME" --url "$ELEFANT_MCP_URL" \
		--bearer-token-env-var ELEFANT_MCP_TOKEN

	codex_config_dir="${CODEX_HOME:-$HOME/.codex}"
	ELEFANT_INSTALL_TOKEN="$elefant_token" \
		python3 "$repo_root/scripts/configure-codex-mcp.py" \
		"$codex_config_dir/config.toml" "$ELEFANT_MCP_NAME"
	unset elefant_token
	configured_with_token=true
fi

skill_dir="$HOME/.agents/skills/elefant"
install -d -m 0755 "$skill_dir"
install -m 0644 "$skill_tmp" "$skill_dir/SKILL.md"

echo
echo "Elefant is installed for Codex."
echo "Restart Codex, then use /mcp to verify the server and /skills to verify the skill."
if [[ "$configured_with_token" == true ]]; then
	echo "The bearer token is stored in $codex_config_dir/config.toml with file mode 600."
else
	echo "The existing MCP authentication was preserved."
fi
