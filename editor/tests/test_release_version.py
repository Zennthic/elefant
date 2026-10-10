"""Cutting a release must not ship a version number that is a lie.

`deploy/k8s/base/kustomization.yaml` carries a `newTag` per image, and that is
what a stranger installs with `kubectl apply -k` at a tag. Nothing ties that
file to the git tag except discipline, and discipline does not survive a
release cut at the end of a long day: tag `v1.2.3` while the manifests still
say `v1.2.2` and the install is silently wrong, for everybody who is not us.

`.github/scripts/check-release-version.sh` is what refuses. It is a FILE rather
than a `run:` block precisely so this can drive it without pushing a tag — the
same reasoning as ci-in-container.sh. So "the version check fails a
deliberately mismatched tag" is demonstrated here rather than asserted.
"""
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
CHECK = REPO / ".github" / "scripts" / "check-release-version.sh"
KUSTOMIZATION = REPO / "deploy" / "k8s" / "base" / "kustomization.yaml"
WORKFLOWS = REPO / ".github" / "workflows"
RELEASE_WF = WORKFLOWS / "release.yml"

SERVICES = ("editor", "embedder", "mcp", "memory-api", "memory-ui", "nginx",
            "diagram-renderer", "diagram-validator")

pytestmark = pytest.mark.skipif(
    not CHECK.exists(), reason="the release scripts are not checked out beside the editor"
)


def _run(tag, kustomization):
    return subprocess.run(
        ["bash", str(CHECK), tag, str(kustomization)],
        capture_output=True, text=True, cwd=REPO,
    )


@pytest.fixture
def pinned(tmp_path):
    """A kustomization with every image pinned to v1.2.3."""
    def _make(version="v1.2.3", leave_behind=None):
        text = KUSTOMIZATION.read_text().replace("newTag: latest", f"newTag: {version}")
        if leave_behind:
            # Bump five of six, the way a hand edit goes wrong.
            head, sep, tail = text.partition(f"newTag: {version}")
            text = head + sep.replace(version, leave_behind) + tail
        path = tmp_path / "kustomization.yaml"
        path.write_text(text)
        return path
    return _make


# ── the check itself ───────────────────────────────────────────────────────

def test_a_matching_tag_passes(pinned):
    r = _run("v1.2.3", pinned())
    assert r.returncode == 0, r.stderr
    assert "All 8 images agree" in r.stdout


def test_a_mismatched_tag_fails(pinned):
    """The case the whole script exists for."""
    r = _run("v1.2.4", pinned("v1.2.3"))
    assert r.returncode != 0
    assert "but the tag is 'v1.2.4'" in r.stderr


def test_a_partial_bump_fails(pinned):
    """Five of six updated by hand is the realistic mistake, and it is the one
    that would otherwise slip through a spot check of the first entry."""
    r = _run("v1.2.3", pinned(leave_behind="v1.2.2"))
    assert r.returncode != 0
    assert "v1.2.2" in r.stderr


def test_the_repo_default_fails_because_it_says_latest():
    """`newTag: latest` on main is correct — main is not a release. Tagging
    without bumping it must therefore be refused, and this proves the check is
    live against the real file rather than only against fixtures."""
    r = _run("v9.9.9", KUSTOMIZATION)
    assert r.returncode != 0
    assert "latest" in r.stderr


def test_a_malformed_tag_is_refused(pinned):
    """`v1.2` is a moving minor tag the release job publishes, not something to
    cut a release from."""
    for bad in ("v1.2", "1.2.3", "latest", "release-1.2.3"):
        r = _run(bad, pinned())
        assert r.returncode != 0, f"{bad!r} was accepted as a release tag"
        assert "not a release tag" in r.stderr


def test_a_prerelease_tag_is_accepted(pinned):
    r = _run("v1.2.3-rc1", pinned("v1.2.3-rc1"))
    assert r.returncode == 0, r.stderr


def test_a_renamed_images_block_fails_loudly(tmp_path):
    """Silence would be the dangerous answer: a check that finds no images and
    says nothing would pass every release for ever."""
    path = tmp_path / "kustomization.yaml"
    path.write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n")
    r = _run("v1.2.3", path)
    assert r.returncode != 0
    assert "no ghcr.io/zennthic/elefant images found" in r.stderr


def test_the_check_covers_every_service(pinned):
    r = _run("v1.2.3", pinned())
    for svc in SERVICES:
        assert svc in r.stdout, f"{svc} is not checked"


def test_the_script_needs_no_exec_bit():
    """It is invoked as `bash <path>`, so a file mode that did not survive a
    checkout cannot break a release."""
    wf = RELEASE_WF.read_text()
    assert "bash .github/scripts/check-release-version.sh" in wf


# ── the release workflow ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def release_wf():
    return yaml.safe_load(RELEASE_WF.read_text())


def test_the_release_runs_only_on_a_version_tag(release_wf):
    """PyYAML reads a bare `on:` as the boolean True, so this looks it up the
    awkward way rather than asserting against nothing."""
    triggers = release_wf.get("on", release_wf.get(True))
    assert set(triggers) == {"push"}
    assert triggers["push"]["tags"] == ["v*"]
    assert "pull_request" not in str(triggers)


def test_the_version_check_runs_before_anything_is_published(release_wf):
    """A release that retagged first and validated second would leave a wrong
    version in the registry for anyone quick enough to pull it."""
    steps = release_wf["jobs"]["release"]["steps"]
    check = next(i for i, s in enumerate(steps)
                 if "check-release-version.sh" in s.get("run", ""))
    publish = next(i for i, s in enumerate(steps)
                   if "imagetools create" in s.get("run", ""))
    assert check < publish, "the version check runs after images are retagged"


def test_a_release_retags_and_never_rebuilds(release_wf):
    """The images for a tagged commit were already built, tested and deployed
    to Stage. Rebuilding would produce different bytes from the ones that were
    actually exercised, which is the opposite of what a version means."""
    body = str(release_wf["jobs"]["release"])
    assert "imagetools create" in body
    assert "build-push-action" not in body


def test_a_release_leaves_latest_alone(release_wf):
    """`latest` means the head of main, and a release is usually behind it by
    the time it is cut. Moving it backwards would downgrade every deployment
    that tracks it — including prod."""
    run = "\n".join(s.get("run", "") for s in release_wf["jobs"]["release"]["steps"])
    for line in run.splitlines():
        if "imagetools create" in line or "--tag" in line:
            assert ":latest" not in line, f"a release moves latest: {line.strip()}"


def test_a_prerelease_does_not_move_the_minor_tag(release_wf):
    """`v1.3` should mean the newest STABLE 1.3, or `ELEFANT_TAG=v1.3` hands
    somebody a release candidate without their asking."""
    run = "\n".join(s.get("run", "") for s in release_wf["jobs"]["release"]["steps"])
    assert "MOVE_MINOR" in run
    assert "Prerelease" in run


def test_the_release_uses_the_keychain_free_auth(release_wf):
    """This runs on the same headless Mac as `build`, where docker/login-action
    fails to store the credential against a locked login keychain."""
    steps = release_wf["jobs"]["release"]["steps"]
    assert not any("login-action" in s.get("uses", "") for s in steps)
    assert "DOCKER_CONFIG" in "\n".join(s.get("run", "") for s in steps)


def test_the_release_can_write_packages_and_contents(release_wf):
    perms = release_wf["jobs"]["release"]["permissions"]
    assert perms["packages"] == "write", "retagging pushes a manifest"
    assert perms["contents"] == "write", "creating a GitHub Release needs it"
