#!/usr/bin/env bash
# Install the Elefant MCP server and skill into Claude Code.
#
# Invoked by `make install-claude`, but can be run directly.  Kept as a
# script rather than a Makefile recipe because the make shipped with
# macOS (GNU Make 3.81) predates .ONESHELL, so multi-line recipes run
# one line per shell there and lose variables, traps, and if-blocks.
#
# Must stay compatible with bash 3.2 (macOS /bin/bash) and BSD
# mktemp/install.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
ELEFANT_URL="${ELEFANT_URL:-}"
[[ -n "$ELEFANT_URL" ]] || { echo "Error: set ELEFANT_URL to your Elefant deployment, e.g. make install-claude ELEFANT_URL=https://elefant.example.com" >&2; exit 1; }
ELEFANT_MCP_URL="${ELEFANT_MCP_URL:-$ELEFANT_URL/mcp}"
ELEFANT_SKILL_FILE="${ELEFANT_SKILL_FILE:-$repo_root/skill/SKILL.md}"
ELEFANT_MCP_NAME="${ELEFANT_MCP_NAME:-elefant}"

command -v claude >/dev/null 2>&1 || { echo "Error: Claude Code is not installed or is not on PATH." >&2; exit 1; }
[[ -f "$ELEFANT_SKILL_FILE" ]] || { echo "Error: Elefant skill not found at $ELEFANT_SKILL_FILE." >&2; exit 1; }

echo "Elefant needs a personal API token."
echo "  1. Open $ELEFANT_URL and sign in."
echo "  2. Open My Settings (your name, top right) -> API tokens -> + New token."
echo "     Name it for this machine and for Claude Code."
echo "  3. Enable Memory Read, Memory Write, and Wiki Read."
echo "     Enable Wiki Write and Board Write if you want Claude to author pages and cards."
echo "  4. Copy the token when it is shown; Elefant displays it only once."
echo
read -r -s -p "Paste the Elefant token: " elefant_token
echo
[[ -n "$elefant_token" ]] || { echo "Error: no token was entered." >&2; exit 1; }

skill_tmp="$(mktemp)"
trap 'rm -f "$skill_tmp"' EXIT
install -m 0600 "$ELEFANT_SKILL_FILE" "$skill_tmp"
[[ "$(head -n 1 "$skill_tmp")" == "---" ]] && grep -q '^name: elefant$' "$skill_tmp" || {
	echo "Error: $ELEFANT_SKILL_FILE is not a valid Elefant skill." >&2
	exit 1
}

if claude mcp get "$ELEFANT_MCP_NAME" >/dev/null 2>&1; then
	echo "Replacing the existing user-scoped '$ELEFANT_MCP_NAME' MCP configuration..."
	claude mcp remove --scope user "$ELEFANT_MCP_NAME" >/dev/null 2>&1 || {
		echo "Error: an Elefant MCP entry exists outside user scope; remove it manually and retry." >&2
		exit 1
	}
fi
claude mcp add --scope user --transport http \
	"$ELEFANT_MCP_NAME" "$ELEFANT_MCP_URL" \
	--header "Authorization: Bearer $elefant_token"
unset elefant_token

claude_config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
skill_dir="$claude_config_dir/skills/elefant"
install -d -m 0755 "$skill_dir"
install -m 0644 "$skill_tmp" "$skill_dir/SKILL.md"

echo
echo "Elefant is installed for Claude Code."
echo "Restart Claude Code, then use /mcp to verify the server and /skills to verify the skill."
echo "The bearer token is stored by Claude Code in its user MCP configuration."
