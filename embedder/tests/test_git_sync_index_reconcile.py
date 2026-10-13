"""Issue #136: a sync reported success while the index stayed empty.

Two independent faults put 13 of 14 sources at `files: 0` with a green
`last synced` stamp:

  * the clone resolver disagreed with itself about where a checkout lives, so
    readers looked under the wrong directory and answered "not cloned" /
    "Source has not been synced yet" about a repo that was present;
  * an incremental sync derived its work from `git diff old_head HEAD` alone,
    which is empty whenever the clone is current, including when the clone is
    current and the index holds nothing at all. Such a source stayed
    unsearchable through every later sync, reporting success each time.

These cover the resolver's agreement and the reconciliation that breaks the
second loop.
"""
import subprocess

import pytest

import main


def _make_clone(root, slug, sid, files=("a.md", "b.py")):
    d = root / slug / sid if slug else root / sid
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for f in files:
        (d / f).write_text("hello\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return d


@pytest.fixture
def sources_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", tmp_path)
    return tmp_path


# ── The clone resolver ───────────────────────────────────────────────────

def test_the_per_org_clone_is_found(sources_dir):
    _make_clone(sources_dir, "acme", "s1")
    assert main._git_source_clone_dir({"id": "s1", "org_slug": "acme"}) is not None


def test_the_legacy_unslugged_clone_is_still_found(sources_dir):
    """Clones sit at GIT_SOURCES_DIR/<id> until a sync migrates them."""
    _make_clone(sources_dir, "", "s2")
    assert main._git_source_clone_dir({"id": "s2", "org_slug": "acme"}) is not None


def test_a_source_with_no_slug_does_not_look_inside_the_root_twice(sources_dir):
    """The empty slug used to join to GIT_SOURCES_DIR itself, so a caller with
    no org_slug checked the legacy path twice and never the real one. It must
    not claim a checkout that isn't there."""
    _make_clone(sources_dir, "acme", "s3")
    assert main._git_source_clone_dir({"id": "s3"}) is None


def test_a_missing_clone_resolves_to_nothing(sources_dir):
    assert main._git_source_clone_dir({"id": "nope", "org_slug": "acme"}) is None


def test_a_directory_without_a_git_dir_is_not_a_clone(sources_dir):
    (sources_dir / "acme" / "s4").mkdir(parents=True)
    assert main._git_source_clone_dir({"id": "s4", "org_slug": "acme"}) is None


def test_disk_state_and_source_tree_resolve_through_the_same_helper():
    """Two hand-rolled copies of "try both layouts" is how they drifted apart.
    One resolver, or the same bug comes back."""
    import inspect
    for fn in (main._git_source_disk_state, main.git_source_tree):
        assert "_git_source_clone_dir" in inspect.getsource(fn), fn.__name__


def test_disk_state_finds_a_per_org_clone_end_to_end(sources_dir):
    _make_clone(sources_dir, "acme", "s5")
    st = main._git_source_disk_state({"id": "s5", "org_slug": "acme"})
    assert st["file_count"] == 2
    assert st["status"] != "not cloned"


# ── The index count helper ───────────────────────────────────────────────

def test_an_unreachable_memory_api_says_unknown_not_zero(monkeypatch):
    """None and 0 must stay distinguishable: treating a failed lookup as an
    empty index would re-index every source on every sync whenever memory-api
    hiccups."""
    def boom(*a, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(main.httpx, "get", boom)
    assert main._memory_api_git_indexed_count("acme", "s1") is None


def test_a_non_2xx_says_unknown_not_zero(monkeypatch):
    class _Resp:
        status_code = 503
        text = "upstream down"
    monkeypatch.setattr(main.httpx, "get", lambda *a, **kw: _Resp())
    assert main._memory_api_git_indexed_count("acme", "s1") is None


def test_an_empty_index_reports_zero(monkeypatch):
    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"files": 0}
    monkeypatch.setattr(main.httpx, "get", lambda *a, **kw: _Resp())
    assert main._memory_api_git_indexed_count("acme", "s1") == 0


def test_a_populated_index_reports_its_count(monkeypatch):
    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"files": 106}
    monkeypatch.setattr(main.httpx, "get", lambda *a, **kw: _Resp())
    assert main._memory_api_git_indexed_count("acme", "s1") == 106


def test_the_count_is_asked_per_source(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"files": 1}

    def _get(url, params=None, **kw):
        seen["url"] = url
        seen["params"] = params
        return _Resp()

    monkeypatch.setattr(main.httpx, "get", _get)
    main._memory_api_git_indexed_count("acme", "s1")
    assert seen["url"].endswith("/git/stats")
    assert seen["params"]["source_id"] == "s1"
    assert seen["params"]["tenant_slug"] == "acme"


# ── The reconciliation itself ────────────────────────────────────────────

@pytest.fixture
def synced(sources_dir, monkeypatch):
    """Run sync_one_source over a real clone that is already up to date, and
    report which files got indexed."""
    indexed = []

    def _run(*args, **kw):
        # No network in the test: the fetch/merge are no-ops and the diff is
        # empty, which is exactly the up-to-date incremental case.
        if args and args[0] == "diff":
            return ""
        if args and args[0] == "rev-parse":
            return "deadbeef"
        return ""

    monkeypatch.setattr(main, "_run_git", _run)
    monkeypatch.setattr(main, "index_git_source_file",
                        lambda fp, sd, src: indexed.append(fp.name))

    def _go(index_count):
        indexed.clear()
        monkeypatch.setattr(main, "_memory_api_git_indexed_count",
                            lambda slug, sid: index_count)
        main.sync_one_source({"id": "s1", "org_slug": "acme", "name": "Src",
                              "repo_url": "https://example.invalid/acme/src",
                              "branch": "main"})
        return indexed
    return _go


def test_an_empty_index_is_backfilled_from_the_checkout(sources_dir, synced):
    """The bug: clone current, index empty, diff empty, nothing indexed, ever."""
    _make_clone(sources_dir, "acme", "s1")
    assert sorted(synced(0)) == ["a.md", "b.py"]


def test_a_populated_index_is_left_alone(sources_dir, synced):
    """Reconciliation must not turn every routine sync into a full re-index."""
    _make_clone(sources_dir, "acme", "s1")
    assert synced(2) == []


def test_an_unknown_index_count_does_not_trigger_a_rebuild(sources_dir, synced):
    """memory-api being unreachable is not evidence the index is empty."""
    _make_clone(sources_dir, "acme", "s1")
    assert synced(None) == []


def test_file_count_reports_the_index_total_not_this_run_s_delta(sources_dir, synced):
    """list_sources presents file_count as the size of the repo in the index.
    Reporting the per-run delta made a healthy source advertise files: 0 after
    every no-change sync."""
    _make_clone(sources_dir, "acme", "s1")
    synced(106)
    st = main._git_source_states["s1"]
    assert st["file_count"] == 106
    assert st["indexed_this_run"] == 0
    assert st["status"] == "idle"


def test_the_backfill_is_reflected_in_the_reported_count(sources_dir, synced):
    _make_clone(sources_dir, "acme", "s1")
    synced(0)
    st = main._git_source_states["s1"]
    assert st["indexed_this_run"] == 2


def test_an_unknown_count_falls_back_to_the_run_s_own_figure(sources_dir, synced):
    _make_clone(sources_dir, "acme", "s1")
    synced(None)
    assert main._git_source_states["s1"]["file_count"] == 0
