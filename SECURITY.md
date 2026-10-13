# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report privately through GitHub's **[Private Vulnerability
Reporting](https://github.com/Zennthic/elefant/security/advisories/new)** —
the "Report a vulnerability" button on the repository's **Security** tab — or by
email to **security@zennthic.com**.

Please include:

- a description of the issue and its impact,
- the affected component (`editor`, `embedder`, `mcp`, `memory-api`,
  `memory-ui`, `nginx`, `diagram-validator`, `diagram-renderer`),
- steps to reproduce or a proof of concept,
- any suggested remediation.

We aim to acknowledge a report within 3 business days and to keep you updated as
we investigate. Please give us a reasonable window to ship a fix before any
public disclosure.

## Scope

Elefant is a multi-tenant platform. We are especially interested in reports
touching tenant isolation, authentication and session handling, the OAuth
authorization server, and the public MCP endpoint.

## Supported versions

Elefant ships as numbered releases, `vX.Y.Z`, listed on the
[Releases](https://github.com/zennthic/elefant/releases) page. Run one of them,
as the README says.

| What you run | Security fixes |
|---|---|
| The latest minor release (the newest `vX.Y`) | **Yes.** Fixes ship as a patch release, `vX.Y.Z+1`. The moving `vX.Y` tag follows it. |
| An older release | **No.** Upgrade to the latest release. |
| `main`, `latest` or a `main-<sha7>` build | **No.** These are development builds. Fixes land there first, but they are not a supported way to run Elefant. |

To find the version you run, check the image tags with
`docker compose images` (or the `ELEFANT_TAG` in your `.env`). Please put it in
a report.
