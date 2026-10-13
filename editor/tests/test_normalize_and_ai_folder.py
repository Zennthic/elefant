"""Path-handling helpers: the guarantee that agents stay confined to the AI/
folder, plus the universal path normaliser that backs every
wiki write surface.  These three are the entire reason an agent with
wiki_write can't escape to a non-AI page.
"""
import pytest
from fastapi import HTTPException


@pytest.fixture
def normalize():
    import wiki_storage
    return wiki_storage._normalize_path


@pytest.fixture
def is_in_ai():
    import wiki_storage
    return wiki_storage._is_within_ai_folder


# ── _normalize_path ──────────────────────────────────────────────────────

def test_normalize_strips_leading_slash(normalize):
    assert normalize("/AI/page.md") == "AI/page.md"


def test_normalize_collapses_dots(normalize):
    assert normalize("AI/./page.md") == "AI/page.md"


@pytest.mark.parametrize("bad", [
    "AI/../page.md",       # parent escape
    "../etc/passwd",        # bare parent escape
    "/AI/../page.md",       # parent after leading slash
    "AI/foo/../../page.md", # nested
])
def test_normalize_rejects_dotdot(normalize, bad):
    with pytest.raises(HTTPException) as exc:
        normalize(bad)
    assert exc.value.status_code in (400, 422)


def test_normalize_strips_leading_slash_then_ai_folder_rejects(normalize, is_in_ai):
    """The normaliser silently strips a leading "/" — by design, since
    many UI templates send POSIX-style absolute paths.  The defence
    against /etc/passwd-style escapes lives in the AI-folder check
    (and, on disk, in S3 keys being bucket-relative).  Confirm both
    halves of the contract together."""
    rel = normalize("/etc/passwd")
    assert rel == "etc/passwd"          # leading slash stripped
    assert is_in_ai(rel, "AI") is False  # but still refused by AI-folder check


def test_normalize_rejects_null_byte(normalize):
    with pytest.raises(HTTPException):
        normalize("AI/page.md\x00.txt")


# ── _is_within_ai_folder ─────────────────────────────────────────────────

def test_in_ai_subpath_accepted(is_in_ai):
    assert is_in_ai("AI/runbooks/db-restore.md", "AI") is True


def test_in_ai_deep_subpath_accepted(is_in_ai):
    assert is_in_ai("AI/a/b/c/d.md", "AI") is True


def test_in_ai_top_level_root_rejected(is_in_ai):
    """The folder ITSELF is not a page — rejected so an agent can't
    write to 'AI' as if it were a file."""
    assert is_in_ai("AI", "AI") is False


def test_substring_trick_rejected(is_in_ai):
    """`AI-evil/...` startswith('AI') but NOT startswith('AI/').  The
    trailing slash in the check is THE reason this attack fails."""
    assert is_in_ai("AI-evil/page.md", "AI") is False
    assert is_in_ai("AI-fake/runbooks/page.md", "AI") is False


def test_unrelated_folder_rejected(is_in_ai):
    assert is_in_ai("other/page.md", "AI") is False
    assert is_in_ai("Notes/AI/x.md", "AI") is False  # AI nested elsewhere


def test_custom_folder_name_respected(is_in_ai):
    """Per-tenant ai_wiki_folder override (e.g. 'agent-pages')."""
    assert is_in_ai("agent-pages/x.md", "agent-pages") is True
    assert is_in_ai("AI/x.md", "agent-pages") is False
