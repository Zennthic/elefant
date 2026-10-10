"""Git branch and URL checks at the point of use: the embedder's git-source
sync.

The admin API validates on the way in; these hold for rows stored before
that existed. A value starting with `-` would be read by git as an option,
and `ext::` URLs run a command.
"""
import pytest

BAD = [("-upload-pack=touch /tmp/x", "https://github.com/a/b"),
       ("main;id", "https://github.com/a/b"),
       ("main", "ext::sh -c touch% /tmp/x"),
       ("main", "-oProxyCommand=id"),
       ("main", "file:///etc"),
       ("main", "git@-oProxyCommand=id:x"),
       ("main", "https://-x/y")]
GOOD = [("main", "https://github.com/zennthic/elefant"),
        ("FlySuper8", "https://github.com/AngelLM/Asgard.git"),
        ("release/1.2", "ssh://git@github.com/a/b.git"),
        ("main", "git@github.com:a/b.git")]


@pytest.mark.parametrize("branch,url", BAD)
def test_check_git_ref_refuses(branch, url):
    import main
    with pytest.raises(ValueError):
        main._check_git_ref(branch, url)


@pytest.mark.parametrize("branch,url", GOOD)
def test_check_git_ref_accepts(branch, url):
    import main
    main._check_git_ref(branch, url)


def test_sync_refuses_a_bad_branch_before_running_git(monkeypatch, tmp_path):
    import main
    calls = []
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", tmp_path)
    monkeypatch.setattr(main, "_run_git", lambda *a, **k: calls.append(a) or "")
    main.sync_one_source({"id": "s1", "repo_url": "https://github.com/a/b",
                          "branch": "--upload-pack=touch /tmp/x", "org_slug": "acme"})
    assert calls == []
    state = main._git_source_states["s1"]
    assert state["status"] == "error" and "branch" in state["error"]


def test_sync_ends_option_parsing_before_the_url(monkeypatch, tmp_path):
    import main
    calls = []

    def fake_git(*args, **kw):
        calls.append(args)
        raise RuntimeError("stop after the first call")
    monkeypatch.setattr(main, "GIT_SOURCES_DIR", tmp_path)
    monkeypatch.setattr(main, "_run_git", fake_git)
    main.sync_one_source({"id": "s2", "repo_url": "https://github.com/a/b",
                          "branch": "main", "org_slug": "acme"})
    clone = calls[0]
    assert "clone" in clone and "--branch=main" in clone
    assert clone[clone.index("--") + 1] == "https://github.com/a/b"

