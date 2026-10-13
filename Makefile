# Recipes are one line each on purpose: the make that ships with macOS
# is GNU Make 3.81, which predates .ONESHELL and .SHELLFLAGS, so a
# multi-line recipe runs one line per shell there and loses variables,
# traps, and if-blocks.  The real logic lives in scripts/.

# Your Elefant deployment, e.g. make install-claude ELEFANT_URL=https://elefant.example.com
ELEFANT_URL ?=
ELEFANT_MCP_URL ?= $(ELEFANT_URL)/mcp
ELEFANT_SKILL_FILE ?= $(CURDIR)/skill/SKILL.md
ELEFANT_MCP_NAME ?= elefant

export ELEFANT_URL ELEFANT_MCP_URL ELEFANT_SKILL_FILE ELEFANT_MCP_NAME

.PHONY: install-claude install-codex

install-claude:
	@$(CURDIR)/scripts/install-claude.sh

install-codex:
	@$(CURDIR)/scripts/install-codex.sh
