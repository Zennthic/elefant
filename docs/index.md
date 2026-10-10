# Elefant documentation

Elefant is a self-hosted second brain for people and their AI assistants: a
wiki, two-tier AI memory, a Kanban board and git knowledge sources, behind one
MCP endpoint. The [README](../README.md) gives the overview. This page lists
the documentation.

## Install

- [Docker Compose](install/docker-compose.md): one machine, running in a few
  minutes. It also covers TLS, [one organisation or many](install/docker-compose.md#one-organisation-or-many),
  upgrades, backup and uninstall.
- [Kubernetes](install/kubernetes.md): k3s, kubeadm, EKS, GKE or AKS, with
  ingress, upgrades, backup and uninstall.

## Connect your AI

- [Connect your AI client](connect/README.md): Claude (web, mobile, Desktop and
  Code), ChatGPT, Cursor, Codex and Open WebUI. Each section has the config,
  where the token comes from, and the base prompt.
- [Base prompts](../base-prompts/README.md): the per-client system prompts,
  and when to use `memory_capture` and when `memory_add`.
- [The Elefant skill](../skill/SKILL.md) for Claude Code and Codex.
- [MCP config examples](../mcp/examples/): Claude Desktop, Cursor and Open
  WebUI.

## Use it

- [Your first ten minutes](getting-started/overview.md): the wiki, the board,
  memories and search, from first sign-in.
- [Diagrams](diagrams.md): Mermaid diagrams that agents create, patch and
  render through MCP, and the optional renderer service.
- [User guide](onboarding/user-guide.html) and
  [admin guide](onboarding/admin-guide.html): printable guides for people who
  are invited to an org, and for the org admin.

## Configuration

- [Configuration reference](configuration.md) lists every environment
  variable each service reads: its default, whether it is required, what it
  does, and whether a change needs a restart or a reindex.
- [`.env.example`](../.env.example) is the starting `.env` for the Compose
  stack, with a comment on each setting.
- The install guides cover the settings that most installs change:
  `ELEFANT_PUBLIC_DOMAIN`, `ELEFANT_MODE` and the secrets.

## Upgrade and backup

- Upgrades: [Docker Compose](install/docker-compose.md#upgrading) and
  [Kubernetes](install/kubernetes.md#upgrading). `scripts/prod-preflight.sh`
  checks a Compose host before and after an upgrade.
- Backup: [Docker Compose](install/docker-compose.md#backup) and
  [Kubernetes](install/kubernetes.md#backup).
- [Data management](../security/data-management.md): what Elefant stores,
  export, and deletion.

## Architecture

- [Architecture](getting-started/elefant-architecture-breadcrumb.md): one
  codebase for one organisation or many, and tenant isolation.
- The services and their ports are listed in the
  [README](../README.md#services).

## Security

- [Security policy](../SECURITY.md): how to report a vulnerability. Do not
  open a public issue.
- [Tenant isolation](../security/tenant-isolation.md).

## Troubleshooting and support

- [Troubleshooting](../TROUBLESHOOTING.md): common failure modes.
- [When the connection fails](connect/README.md#when-it-does-not-connect):
  client-side errors.
- [Support](../SUPPORT.md): where questions and bug reports go.

## Contributing

- [Contributing](../CONTRIBUTING.md): the development setup, building local
  changes, running the tests, and pull requests.
- [Code of conduct](../CODE_OF_CONDUCT.md).
