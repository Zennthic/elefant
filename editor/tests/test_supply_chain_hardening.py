"""Container and CI supply-chain hardening (CQ-03), held in place.

Each property below was a finding in the open-source readiness review, and
each is the kind that erodes silently: a new Dockerfile without a USER line, a
new workflow without a `permissions:` block, or an action added by tag. Nothing
breaks when that happens. It only widens what a compromised dependency can do.
So these tests fail instead.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
BASE = REPO / "deploy" / "k8s" / "base"

# Every image this repository builds, and the UID it runs as. The Python
# services share 10001 because the editor and the embedder share volumes; the
# nginx images are 101 because that is what nginx-unprivileged ships.
IMAGE_UIDS = {
    "editor": 10001,
    "embedder": 10001,
    "mcp": 10001,
    "memory-api": 10001,
    "memory-ui": 101,
    "nginx": 101,
    # Owns no volume; 10001 for consistency with the other services we write.
    "diagram-renderer": 10001,
    "diagram-validator": 10001,
}

pytestmark = pytest.mark.skipif(
    not WORKFLOWS.exists(), reason="the repo root is not checked out beside the editor"
)


def _dockerfile(svc):
    return (REPO / svc / "Dockerfile").read_text()


def test_every_dockerfile_is_accounted_for():
    """A new image has to be added above, which is where its UID gets decided."""
    found = {p.parent.name for p in REPO.glob("*/Dockerfile")}
    assert found == set(IMAGE_UIDS), f"Dockerfiles not listed here: {found ^ set(IMAGE_UIDS)}"


@pytest.mark.parametrize("svc", sorted(IMAGE_UIDS))
def test_image_runs_as_its_fixed_non_root_uid(svc):
    users = re.findall(r"^USER\s+(\S+)", _dockerfile(svc), re.M)
    assert users, f"{svc}/Dockerfile has no USER, so the image runs as root"
    # The LAST USER wins. Numeric, so k8s' runAsNonRoot can verify it without
    # looking the name up in the image's /etc/passwd.
    uid = users[-1].split(":")[0]
    assert uid.isdigit(), f"{svc}: USER {users[-1]} is a name; use the numeric UID"
    assert int(uid) == IMAGE_UIDS[svc], f"{svc} runs as {uid}, expected {IMAGE_UIDS[svc]}"


@pytest.mark.parametrize("svc", sorted(IMAGE_UIDS))
def test_base_image_is_an_exact_version(svc):
    for ref in re.findall(r"^FROM\s+(\S+)", _dockerfile(svc), re.M):
        tag = ref.split("@")[0].rsplit("/", 1)[-1].partition(":")[2]
        assert tag and tag not in ("latest", "alpine", "slim"), f"{svc}: FROM {ref} floats"
        # At least major.minor.patch somewhere in the tag: `3.11-slim` or
        # `alpine3.19` still moves under a rebuild.
        assert re.search(r"\d+\.\d+\.\d+", tag), f"{svc}: FROM {ref} is not an exact release"


@pytest.mark.parametrize("svc", sorted(IMAGE_UIDS))
def test_build_context_has_a_dockerignore(svc):
    path = REPO / svc / ".dockerignore"
    assert path.exists(), f"{svc}/ has no .dockerignore"
    rules = {line.strip() for line in path.read_text().splitlines()}
    for must in ("tests/", ".git", ".env", ".env.*", "__pycache__/", "*.backup"):
        assert must in rules, f"{svc}/.dockerignore does not exclude {must}"


def test_no_floating_third_party_image_in_compose():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for name, svc in compose["services"].items():
        image = svc.get("image", "")
        if image.startswith("ghcr.io/zennthic/elefant/") or "EMBEDDER_IMAGE" in image:
            continue  # ours: pinned per deploy through ELEFANT_TAG
        last = image.split("@")[0].rsplit("/", 1)[-1]
        assert ":" in last and not last.endswith(":latest"), f"{name}: {image} floats"


# ── workflows ──────────────────────────────────────────────────────────────

WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_token_is_read_only_by_default(path):
    """Top-level `contents: read` and nothing more, so a job that forgets its
    own block gets a token that cannot write. Jobs widen their own scope."""
    wf = yaml.safe_load(path.read_text())
    assert wf.get("permissions") == {"contents": "read"}, (
        f"{path.name}: top-level permissions are {wf.get('permissions')!r}"
    )


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_by_commit_sha(path):
    """A tag can be moved to another commit by whoever controls the action's
    repository; a SHA cannot. The trailing comment is what Dependabot and a
    human read to know which version it is."""
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
        if not m or m.group(1).startswith("./"):
            continue
        ref = m.group(1).partition("@")[2]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{path.name}: {m.group(1)} is not SHA-pinned"
        assert re.match(r"\s+#\s*v\d", m.group(2)), f"{path.name}: {m.group(1)} has no version comment"


def test_dependabot_keeps_the_pins_moving():
    cfg = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in cfg["updates"]}
    assert {"github-actions", "docker"} <= ecosystems
    docker = next(u for u in cfg["updates"] if u["package-ecosystem"] == "docker")
    assert {d.strip("/") for d in docker["directories"]} == set(IMAGE_UIDS)


# ── kubernetes ─────────────────────────────────────────────────────────────

def _deployments():
    out = {}
    for path in BASE.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "Deployment":
                out[doc["metadata"]["name"]] = doc["spec"]["template"]["spec"]
    return out


@pytest.mark.parametrize("svc", sorted(IMAGE_UIDS))
def test_k8s_pod_runs_as_the_image_uid(svc):
    pod = _deployments()[svc]
    sc = pod.get("securityContext", {})
    assert sc.get("runAsNonRoot") is True, f"{svc}: runAsNonRoot is not set"
    assert sc.get("runAsUser") == IMAGE_UIDS[svc], f"{svc}: runAsUser {sc.get('runAsUser')}"
    for c in pod["containers"]:
        csc = c.get("securityContext", {})
        assert csc.get("allowPrivilegeEscalation") is False, f"{svc}/{c['name']}"
        assert csc.get("capabilities", {}).get("drop") == ["ALL"], f"{svc}/{c['name']}"


@pytest.mark.parametrize("svc", ["editor", "embedder"])
def test_pods_with_claims_fix_ownership_before_starting(svc):
    """Claims created by the old root images are root-owned. Without the init
    container, the upgrade's first boot cannot write its own data."""
    pod = _deployments()[svc]
    claims = {v["name"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
    inits = pod.get("initContainers", [])
    assert inits, f"{svc} has claims and no ownership fix"
    fixed = {m["name"] for m in inits[0]["volumeMounts"]}
    assert claims <= fixed, f"{svc}: claims not chowned: {claims - fixed}"
    assert "10001:10001" in inits[0]["args"][0]
    assert pod["securityContext"].get("fsGroup") == 10001
