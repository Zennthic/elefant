"""Issue #269: a sync moved the checkout forward but not the content index.

On the Elefant source, `source_tree` listed 284 files while `list_sources`
said 184, `git_read` answered "file not indexed" for everything added since
mid-August, and files that were indexed still served their mid-August content.

The incremental sync took its work from `git diff old_head HEAD`, then kept
only the paths `_is_indexable` accepted. The diff hands back repo-relative
paths, `_is_indexable` stats the file, and a bare relative path is resolved
against the process's cwd rather than the checkout. Nothing was found, so
every added and modified file was dropped, every sync, while the clone kept
moving. Deleted paths were dropped the same way, so removed files lingered.

These drive `sync_one_source` over real git repositories with an in-memory
stand-in for the memory-api lexical index.
"""
import hashlib
import os
import re
import subprocess

import pytest

import main


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], check=True, capture_output=True)


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _write(repo, "README.md", "Private repository. External contributions are not accepted.\n")
    _write(repo, "editor/capability.py", "KEY = 'old-signing-key'\n")
    _write(repo, "old.txt", "going away\n")
    _commit(repo, "A")
    return repo


class _Index:
    """The memory-api git_files table, as the embedder sees it."""

    def __init__(self):
        self.files = {}
        self.manifest_up = True

    def index(self, slug, sid, name, rel, content, lang):
        self.files[rel] = content

    def delete(self, slug, sid, rel):
        self.files.pop(rel, None)

    def delete_source(self, slug, sid):
        self.files.clear()

    def count(self, slug, sid):
        return len(self.files)

    def manifest(self, slug, sid):
        if not self.manifest_up:
            return None
        return {rel: hashlib.md5(c.encode("utf-8")).hexdigest()
                for rel, c in self.files.items()}


@pytest.fixture
def index(tmp_path, monkeypatch):
    idx = _Index()
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", tmp_path / "sources")
    # These syncs pull from a local upstream over file://, which a real
    # source may not name. Accept it here so the sync still runs real git.
    monkeypatch.setattr(main, "_GIT_URL_RE",
                        re.compile(main._GIT_URL_RE.pattern + r"|file://\S+"))
    monkeypatch.setattr(main, "GIT_SOURCE_EMBED", False)
    monkeypatch.setattr(main, "_memory_api_git_index", idx.index)
    monkeypatch.setattr(main, "_memory_api_git_delete_file", idx.delete)
    monkeypatch.setattr(main, "_memory_api_git_delete_source", idx.delete_source)
    monkeypatch.setattr(main, "_memory_api_git_indexed_count", idx.count)
    monkeypatch.setattr(main, "_memory_api_git_manifest", idx.manifest, raising=False)
    monkeypatch.setattr(main, "_remove_git_source_file", lambda rel, sid: None)
    return idx


@pytest.fixture
def sync(upstream, index):
    source = {"id": "s1", "org_slug": "acme", "name": "Elefant",
              "repo_url": f"file://{upstream}", "branch": "main"}

    def _go(**kw):
        main.sync_one_source(source, **kw)
        st = main._git_source_states["s1"]
        assert st["status"] == "idle", st.get("error")
        return st
    return _go


def _move_to_b(upstream):
    """B adds a file and modifies another, as main did around 2026-08-18."""
    _write(upstream, "CONTRIBUTING.md", "Contributions welcome under the DCO.\n")
    _write(upstream, "README.md", "Open source. See CONTRIBUTING.md.\n")
    _commit(upstream, "B")


def test_a_sync_at_b_indexes_the_added_file_and_the_modified_one(upstream, index, sync):
    """The acceptance test on the card: sync at A, then at B."""
    sync()
    assert "CONTRIBUTING.md" not in index.files
    _move_to_b(upstream)
    sync()
    assert index.files["CONTRIBUTING.md"] == "Contributions welcome under the DCO.\n"
    assert index.files["README.md"] == "Open source. See CONTRIBUTING.md.\n"


def test_the_diff_fallback_also_picks_up_adds_and_edits(upstream, index, sync, monkeypatch):
    """Without a manifest the sync falls back to the diff. This is the exact
    code path that dropped every changed file, so it gets its own test."""
    index.manifest_up = False
    sync()
    _move_to_b(upstream)
    sync()
    assert index.files["CONTRIBUTING.md"] == "Contributions welcome under the DCO.\n"
    assert index.files["README.md"] == "Open source. See CONTRIBUTING.md.\n"


def test_the_diff_fallback_does_not_depend_on_the_process_cwd(upstream, index, sync,
                                                               tmp_path, monkeypatch):
    """The root cause: indexability was judged against cwd, not the checkout."""
    index.manifest_up = False
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    sync()
    _move_to_b(upstream)
    sync()
    assert "CONTRIBUTING.md" in index.files


@pytest.mark.parametrize("manifest_up", [True, False])
def test_deleted_and_renamed_files_leave_the_index(upstream, index, sync, manifest_up):
    index.manifest_up = manifest_up
    sync()
    _git(upstream, "rm", "-q", "old.txt")
    _git(upstream, "mv", "editor/capability.py", "editor/caps.py")
    _commit(upstream, "C")
    sync()
    assert "old.txt" not in index.files
    assert "editor/capability.py" not in index.files
    assert "editor/caps.py" in index.files


def test_an_index_left_stale_by_earlier_syncs_heals_on_the_next_one(upstream, index, sync):
    """Production state: the clone is already at B but the index still holds A.
    The diff from here on is empty, so only comparing content can repair it."""
    sync()
    _move_to_b(upstream)
    sync()
    # Put the index back to what the broken sync left behind.
    index.files.pop("CONTRIBUTING.md")
    index.files["README.md"] = "Private repository. External contributions are not accepted.\n"
    index.files["deploy/gone.yml"] = "left over\n"
    st = sync()
    assert index.files["README.md"] == "Open source. See CONTRIBUTING.md.\n"
    assert "CONTRIBUTING.md" in index.files
    assert "deploy/gone.yml" not in index.files
    assert st["file_count"] == st["indexable_files"] == 4


def test_a_current_index_is_not_rewritten_by_a_no_change_sync(upstream, index, sync):
    """Reconciling by hash must not turn every routine sync into a full re-index."""
    sync()
    st = sync()
    assert st["indexed_this_run"] == 0


def test_a_rebuild_reindexes_everything_at_head(upstream, index, sync):
    sync()
    _move_to_b(upstream)
    index.files["stray.md"] = "x"
    st = sync(force=True)
    assert "stray.md" not in index.files
    assert index.files["README.md"] == "Open source. See CONTRIBUTING.md.\n"
    assert st["file_count"] == st["indexable_files"] == 4


def test_the_count_reports_what_the_tree_should_hold(upstream, index, sync):
    """list_sources compares these two; a gap between them is how drift shows."""
    st = sync()
    assert st["indexable_files"] == 3
    assert st["file_count"] == 3


def test_path_prefix_is_respected_by_the_reconcile(upstream, index, monkeypatch):
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", upstream.parent / "sources2")
    source = {"id": "s2", "org_slug": "acme", "name": "Prefixed",
              "repo_url": f"file://{upstream}", "branch": "main", "path_prefix": "editor"}
    main.sync_one_source(source)
    main.sync_one_source(source)
    st = main._git_source_states["s2"]
    assert set(index.files) == {"editor/capability.py"}
    assert st["indexed_this_run"] == 0
    assert st["indexable_files"] == 1


def test_the_hash_matches_what_is_sent_to_the_index(tmp_path):
    """The manifest side hashes stored content with Postgres md5(content), the
    UTF-8 bytes of the text. The embedder must hash the same text it sends."""
    p = tmp_path / "f.md"
    p.write_bytes("café — ok\n".encode("utf-8"))
    assert main._git_file_md5(p) == hashlib.md5("café — ok\n".encode("utf-8")).hexdigest()


def test_an_unreachable_memory_api_gives_no_manifest(monkeypatch):
    """None, not {}: an empty manifest would re-index the whole source."""
    monkeypatch.setattr(main, "MEMORY_API_URL", "http://memory-api.invalid")
    def boom(*a, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(main.httpx, "get", boom)
    assert main._memory_api_git_manifest("acme", "s1") is None


def test_a_non_2xx_gives_no_manifest(monkeypatch):
    class _Resp:
        status_code = 404
        text = "not found"
    monkeypatch.setattr(main, "MEMORY_API_URL", "http://memory-api.invalid")
    monkeypatch.setattr(main.httpx, "get", lambda *a, **kw: _Resp())
    assert main._memory_api_git_manifest("acme", "s1") is None
