"""Issue #93 — list_sources reported "never synced, 0 files" for working repos.

_git_source_states is a plain dict in process memory. Every restart empties it
while the clone and its index both survive on the mounted volume, so
list_sources claimed a source had never synced at the same moment git_search
and git_read were happily serving it. The checkout itself is the durable
record; these cover reading it back.
"""
import datetime
import subprocess

import pytest

import main


def _make_clone(tmp_path, slug, sid, files=("a.md", "b.md", "c.md")):
    d = tmp_path / slug / sid
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for f in files:
        (d / f).write_text("x")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return d


@pytest.fixture
def sources_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", tmp_path)
    return tmp_path


def test_a_real_clone_reports_its_file_count(sources_dir):
    _make_clone(sources_dir, "acme", "src1")
    st = main._git_source_disk_state({"id": "src1", "org_slug": "acme"})
    assert st["file_count"] == 3


def test_a_real_clone_reports_a_last_sync_time(sources_dir):
    _make_clone(sources_dir, "acme", "src1")
    st = main._git_source_disk_state({"id": "src1", "org_slug": "acme"})
    assert "last_sync" in st
    # Parseable, and not from the future.
    ts = datetime.datetime.fromisoformat(st["last_sync"].rstrip("Z"))
    assert ts <= datetime.datetime.utcnow() + datetime.timedelta(minutes=1)


def test_status_says_the_answer_came_from_disk(sources_dir):
    """A reader should be able to tell a recovered figure from a live one."""
    _make_clone(sources_dir, "acme", "src1")
    st = main._git_source_disk_state({"id": "src1", "org_slug": "acme"})
    assert "disk" in st["status"]


def test_a_missing_clone_says_so_rather_than_guessing(sources_dir):
    st = main._git_source_disk_state({"id": "nope", "org_slug": "acme"})
    assert st == {"status": "not cloned"}
    assert "file_count" not in st


def test_the_legacy_unslugged_layout_is_still_found(sources_dir):
    """Older clones sit at GIT_SOURCES_DIR/<id> without the org segment."""
    d = sources_dir / "src2"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    st = main._git_source_disk_state({"id": "src2", "org_slug": "acme"})
    assert st["status"] != "not cloned"


def test_a_source_without_an_id_is_ignored(sources_dir):
    assert main._git_source_disk_state({"org_slug": "acme"}) == {}


def test_disk_state_never_raises_on_a_broken_checkout(sources_dir):
    """Best-effort by contract: list_sources must not 500 because a clone is
    half-written or the volume is unreadable."""
    d = sources_dir / "acme" / "broken"
    (d / ".git").mkdir(parents=True)          # a .git that is not a repository
    st = main._git_source_disk_state({"id": "broken", "org_slug": "acme"})
    assert isinstance(st, dict)
    assert "file_count" not in st              # git failed; don't invent a number


def test_live_state_is_preferred_over_disk(sources_dir, monkeypatch):
    """Disk is the fallback, not the source of truth — a process that just
    synced knows better than the checkout's mtime."""
    import inspect
    src = inspect.getsource(main.git_sources_for_user)
    assert 'if not st.get("last_sync")' in src, \
        "disk should only be consulted when memory has nothing"
    assert 'st.get("last_sync") or disk.get("last_sync")' in src
