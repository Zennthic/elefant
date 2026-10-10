"""The repo's community files: issue forms, PR template, CODEOWNERS, SUPPORT.

GitHub reads these from fixed paths and fails quietly when one is wrong: a
misnamed template is simply not offered, and a CODEOWNERS rule naming an owner
that does not exist routes nothing. These tests pin the paths and the promises
the files make (no blank issues, a private route for security reports, one
named place to ask a question).

The editor image has no YAML parser, so the issue forms are checked as text.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
GH = REPO / ".github"

DISCUSSIONS = "https://github.com/Zennthic/elefant/discussions"
ADVISORY = "https://github.com/Zennthic/elefant/security/advisories/new"

PLACEHOLDER = "@OWNER"


def _text(rel):
    return (REPO / rel).read_text()


def test_issue_chooser_offers_no_blank_issue_and_both_contact_links():
    config = _text(".github/ISSUE_TEMPLATE/config.yml")
    assert re.search(r"^blank_issues_enabled:\s*false\s*$", config, re.M)
    assert "name: Report a security vulnerability" in config
    assert f"url: {DISCUSSIONS}" in config


def test_security_contact_link_matches_security_md():
    """One private reporting URL, stated once in SECURITY.md and echoed here."""
    assert ADVISORY in _text("SECURITY.md")
    assert f"url: {ADVISORY}" in _text(".github/ISSUE_TEMPLATE/config.yml")


@pytest.mark.parametrize("form", ["bug_report.yml", "feature_request.yml"])
def test_issue_forms_have_the_top_level_keys_github_requires(form):
    text = _text(f".github/ISSUE_TEMPLATE/{form}")
    for key in ("name", "description", "body"):
        assert re.search(rf"^{key}:", text, re.M), f"{form} lacks {key}:"
    # Every input needs a unique id, or GitHub refuses the whole form.
    ids = re.findall(r"^\s+id:\s*(\S+)\s*$", text, re.M)
    assert ids and len(ids) == len(set(ids)), ids


def test_bug_form_asks_what_triage_needs():
    text = _text(".github/ISSUE_TEMPLATE/bug_report.yml")
    for needle in ("label: Elefant version", "/health", "Docker Compose",
                   "Kubernetes", "amd64", "arm64", "label: Which service",
                   "label: Steps to reproduce", "label: Expected behaviour",
                   "label: Actual behaviour", "label: Relevant logs",
                   "Redact tokens"):
        assert needle in text, needle


def test_pr_template_checklist():
    text = _text(".github/PULL_REQUEST_TEMPLATE.md")
    for needle in ("## What and why", "## How it was tested", "git commit -s",
                   "Tests added or updated", "Docs updated", "memory-ui/src/",
                   "tiptap.bundle.js"):
        assert needle in text, needle


def _codeowners_rules():
    """Live rules: each non-blank, non-comment line, split into pattern and
    owners, the way GitHub reads it."""
    rules = []
    for n, line in enumerate(_text(".github/CODEOWNERS").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            rules.append((n, pattern, owners))
    return rules


def test_codeowners_live_rules_name_real_looking_owners():
    """GitHub shows an error banner for an owner it cannot resolve. The
    placeholder is the one we know it cannot."""
    for n, pattern, owners in _codeowners_rules():
        assert owners, f"line {n}: {pattern} has no owner"
        for owner in owners:
            assert owner != PLACEHOLDER, (
                f"line {n}: replace {PLACEHOLDER} with the maintainer handle")
            assert re.fullmatch(r"@[\w-]+(/[\w-]+)?|[^@\s]+@[^@\s]+", owner), owner


def test_codeowners_covers_the_sensitive_paths():
    text = _text(".github/CODEOWNERS")
    for path in ("editor/auth*.py", "editor/routers/oauth.py",
                 "editor/routers/sso.py", ".github/workflows/", "deploy/"):
        assert path in text, path
    for path in ("editor/routers/oauth.py", "editor/routers/sso.py"):
        assert (REPO / path).is_file(), f"CODEOWNERS names a missing file: {path}"


def test_codeowners_placeholder_is_still_pending():
    """Reported, not failed, while the maintainer handle is undecided: the
    file is valid as it stands and routes nothing. Once the rules are live
    this passes."""
    if not _codeowners_rules():
        assert PLACEHOLDER in _text(".github/CODEOWNERS")
        pytest.xfail("CODEOWNERS routes nothing until the maintainer handle "
                     "replaces the placeholder")


@pytest.mark.parametrize("doc", ["README.md", "TROUBLESHOOTING.md", "SUPPORT.md"])
def test_docs_name_the_help_channel(doc):
    assert DISCUSSIONS in _text(doc)
