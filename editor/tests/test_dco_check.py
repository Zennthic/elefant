"""The DCO check is wired up and actually distinguishes good from bad.

CONTRIBUTING.md and the README both promise CI checks for a sign-off. A
promise nobody enforces is worse than no promise: it tells a contributor their
certification was collected when it wasn't, and leaves us with no record on the
day provenance is questioned.

The script is exercised against a throwaway repo rather than mocked, because
what matters is the git plumbing — trailer parsing, author matching, which
commits are exempt — and that is exactly what a mock would paper over.
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check-dco.sh"
WORKFLOW = REPO / ".github" / "workflows" / "dco.yml"


def _git(cwd, *args, **env):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True,
                          env={"HOME": str(cwd), "PATH": "/usr/bin:/bin",
                               "GIT_CONFIG_NOSYSTEM": "1", **env})


def _commit(cwd, message, name="Ada L", email="ada@example.com"):
    (cwd / "f.txt").write_text(message)
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-m", message,
         GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
         GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.name", "Ada L")
    _git(tmp_path, "config", "user.email", "ada@example.com")
    base = _commit(tmp_path, "base")
    return tmp_path, base


def _check(repo_dir, base, head="HEAD"):
    return subprocess.run(["bash", str(SCRIPT), base, head],
                          cwd=repo_dir, capture_output=True, text=True)


SIGNED = "Signed-off-by: Ada L <ada@example.com>"


def test_signed_commit_passes(repo):
    cwd, base = repo
    _commit(cwd, f"a change\n\n{SIGNED}")
    r = _check(cwd, base)
    assert r.returncode == 0, r.stdout + r.stderr


def test_unsigned_commit_fails(repo):
    cwd, base = repo
    _commit(cwd, "a change with no sign-off")
    r = _check(cwd, base)
    assert r.returncode == 1
    assert "No `Signed-off-by` trailer" in r.stdout


def test_sign_off_by_someone_else_fails(repo):
    """Otherwise anyone could certify on another person's behalf."""
    cwd, base = repo
    _commit(cwd, "a change\n\nSigned-off-by: Grace H <grace@example.com>")
    r = _check(cwd, base)
    assert r.returncode == 1
    assert "does not match the commit author" in r.stdout


def test_sign_off_matching_is_case_insensitive(repo):
    """Git preserves the case a contributor typed; email is not case-sensitive
    in the part that matters, and failing on it would be pure friction."""
    cwd, base = repo
    _commit(cwd, "a change\n\nSigned-off-by: Ada L <ADA@Example.COM>")
    assert _check(cwd, base).returncode == 0


def test_one_bad_commit_fails_the_whole_range(repo):
    """A PR is only as certified as its least-certified commit."""
    cwd, base = repo
    _commit(cwd, f"good\n\n{SIGNED}")
    _commit(cwd, "bad")
    _commit(cwd, f"good again\n\n{SIGNED}")
    r = _check(cwd, base)
    assert r.returncode == 1
    assert "bad" in r.stdout


def test_bot_commits_are_exempt(repo):
    """A bot can't certify something it can't read; its commits belong to
    whoever ran the automation."""
    cwd, base = repo
    _commit(cwd, "bump a dep", name="dependabot[bot]",
            email="49699333+dependabot[bot]@users.noreply.github.com")
    r = _check(cwd, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "(bot)" in r.stdout


def test_empty_range_passes(repo):
    cwd, base = repo
    assert _check(cwd, base, base).returncode == 0


def test_failure_tells_you_how_to_fix_it(repo):
    """A contributor hitting this is usually a first-timer; the message has to
    carry the remedy or the PR just gets abandoned."""
    cwd, base = repo
    _commit(cwd, "unsigned")
    out = _check(cwd, base).stdout
    assert "git commit -s" in out
    assert "git rebase --signoff" in out
    assert "developercertificate.org" in out


# ── wiring ─────────────────────────────────────────────────────────────────

def test_workflow_runs_on_pull_requests():
    """build-deploy.yml deliberately skips PRs, so this must not rely on it."""
    y = WORKFLOW.read_text()
    assert "pull_request:" in y
    assert "check-dco.sh" in y


def test_workflow_reads_the_full_pr_range():
    """A shallow checkout would only see the tip commit and pass a PR whose
    earlier commits are unsigned — the same class of bug as the change
    detection in build-deploy.yml."""
    y = WORKFLOW.read_text()
    assert "fetch-depth: 0" in y
    assert "pull_request.base.sha" in y and "pull_request.head.sha" in y


def test_contributor_docs_promise_what_ci_enforces():
    for doc in ("README.md", "CONTRIBUTING.md"):
        # Markdown wraps the link text across lines; compare on one line.
        text = " ".join((REPO / doc).read_text().split())
        assert "Developer Certificate of Origin" in text, doc
        assert "git commit -s" in text, doc
