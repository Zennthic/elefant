"""An admin-supplied git branch or repo URL cannot smuggle options into git.

Both end up as git arguments in the embedder. A branch or URL starting with
`-` would be read as an option, and a URL like `ext::sh -c ...` runs a
command, so the admin API refuses anything outside a plain shape.
"""
import pytest
from fastapi import HTTPException

BAD_BRANCHES = ["-upload-pack=touch /tmp/x", "--help", "main;id", "a b", "ma$in", "ma\nin"]
GOOD_BRANCHES = ["main", "release/1.2", "feature_x-y", "FlySuper8"]
BAD_URLS = ["ext::sh -c touch% /tmp/x", "-oProxyCommand=id", "file:///etc",
            "http://example.com/r", "https://-oProxyCommand=id/x",
            "git@-oProxyCommand=id:x", "https://ex ample.com/r",
            "https://example.com/r\nx", "ssh://", ""]
GOOD_URLS = ["https://github.com/zennthic/elefant", "https://github.com/AngelLM/Thor.git",
             "https://git.example.org/team/project",
             "https://oauth2:tok@gitlab.example.com:8443/g/r.git",
             "ssh://git@github.com/zennthic/elefant.git", "git@github.com:zennthic/elefant.git"]


class _Session:
    def __init__(self, src=None):
        self.added, self.committed, self.src = [], False, src

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        pass


class _Src:
    def __init__(self):
        self.repo_url, self.branch = "https://github.com/a/b", "main"

    def to_dict(self):
        return {"repo_url": self.repo_url, "branch": self.branch}


@pytest.fixture
def api():
    from routers import git_sources
    return git_sources


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_create_refuses_a_bad_branch(api, branch):
    s = _Session()
    with pytest.raises(HTTPException) as e:
        api.create_git_source({"repo_url": GOOD_URLS[0], "branch": branch},
                              session=s, claims={"oid": "o1"})
    assert e.value.status_code == 422
    assert not s.added


@pytest.mark.parametrize("url", BAD_URLS)
def test_create_refuses_a_bad_url(api, url):
    s = _Session()
    with pytest.raises(HTTPException) as e:
        api.create_git_source({"repo_url": url}, session=s, claims={"oid": "o1"})
    assert e.value.status_code == 422
    assert not s.added


@pytest.mark.parametrize("url", GOOD_URLS)
@pytest.mark.parametrize("branch", GOOD_BRANCHES)
def test_validators_accept_ordinary_values(api, url, branch):
    assert api._valid_git_repo_url(url) == url
    assert api._valid_git_branch(branch) == branch


def test_blank_branch_defaults_to_main(api):
    assert api._valid_git_branch("  ") == "main"


def test_update_refuses_a_bad_branch_and_url(api, monkeypatch):
    src = _Src()
    monkeypatch.setattr(api, "_git_source_or_404", lambda *_: src)
    for body in ({"branch": "-x"}, {"repo_url": "ext::sh -c id"}):
        s = _Session()
        with pytest.raises(HTTPException) as e:
            api.update_git_source("s1", body, session=s, claims={"oid": "o1"})
        assert e.value.status_code == 422
        assert not s.committed
    assert (src.repo_url, src.branch) == ("https://github.com/a/b", "main")
