"""Every Stage deploy runs a tag that names the commit it came from.

`build` only builds the services that CHANGED in a push, so `main-<sha7>`
exists for those and nobody else. Deploying `:latest` therefore worked, but
recorded nothing: `docker compose ps` on the Stage could not tell you which
build was running, and two Stages pulling `:latest` at different moments could
end up on different images while both runs reported success.

`pin-release` fixes that by retagging all six services to `main-<sha7>` and the
deploy asking for that tag. The parts of it that are easy to undo by accident
are asserted here:

  * the compose default stays `latest`, so a hand-run `docker compose up` on
    the prod host behaves exactly as it did before this existed;
  * the deploy actually sets ELEFANT_TAG, rather than inheriting whatever is
    in the shell;
  * `pin-release` gates the deploy, so a half-pinned registry cannot ship;
  * the third-party images carry real versions.

A hyphen in a job id is the specific trap this file also guards: `needs.pin-release`
is an ARITHMETIC expression in a workflow, so a reference written that way
evaluates to a number and the condition it guards passes no matter what the
job did.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "build-deploy.yml"
COMPOSE = REPO / "docker-compose.yml"

SERVICES = ("editor", "embedder", "mcp", "memory-api", "memory-ui", "nginx",
            "diagram-renderer", "diagram-validator")


@pytest.fixture(scope="module")
def build_deploy():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def workflow_text():
    return WORKFLOW.read_text()


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def _steps(job):
    return job.get("steps", [])


def _run_text(job):
    return "\n".join(s.get("run", "") for s in _steps(job))


# ── the compose default ────────────────────────────────────────────────────

def test_every_elefant_image_is_tag_pinnable(compose):
    """A service left on a hardcoded `:latest` silently ignores ELEFANT_TAG,
    so a deploy would ship five pinned images and one floating one."""
    for svc in SERVICES:
        image = compose["services"][svc]["image"]
        assert "ELEFANT_TAG" in image, (
            f"{svc} is not pinnable: {image!r}. A deploy asking for a release "
            "tag would still get whatever `latest` points at for this one."
        )


def test_the_compose_default_is_still_latest(compose):
    """The prod host runs this same file by hand, with no ELEFANT_TAG set.
    That must keep meaning exactly what it meant before pinning existed."""
    for svc in SERVICES:
        image = compose["services"][svc]["image"]
        assert "ELEFANT_TAG:-latest" in image, (
            f"{svc} has no `latest` fallback: {image!r}. Running this file with "
            "no ELEFANT_TAG would resolve to an empty tag."
        )


def test_the_embedder_image_override_still_wins(compose):
    """EMBEDDER_IMAGE names a WHOLE image (the torch-free slim build lives on
    its own tag). Pinning the default tag must not take that knob away."""
    image = compose["services"]["embedder"]["image"]
    assert image.startswith("${EMBEDDER_IMAGE:-"), (
        f"the embedder override was lost: {image!r}"
    )
    assert "ELEFANT_TAG:-latest" in image


def test_third_party_images_name_a_version(compose):
    """`--pull always` on a floating tag lets an upstream release move under a
    deploy, and lets two Stages that pull minutes apart run different versions.
    Qdrant holds the vector store, so that is not a cosmetic difference."""
    floating = []
    for name, svc in compose["services"].items():
        image = svc.get("image", "")
        if "ELEFANT_TAG" in image or "${" in image:
            continue  # ours, covered above
        if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
            floating.append(f"{name}={image}")
    assert not floating, f"unpinned third-party images: {floating}"


# ── the pin-release job ────────────────────────────────────────────────────

def test_there_is_a_pin_release_job(build_deploy):
    assert "pin-release" in build_deploy["jobs"], (
        "without it, only the services that changed have a main-<sha> tag and "
        "the deploy has no single release to ask for"
    )


def test_pin_release_runs_after_build(build_deploy):
    needs = build_deploy["jobs"]["pin-release"]["needs"]
    assert "build" in needs and "changes" in needs


def test_pin_release_covers_every_service(build_deploy):
    """Pinning five of six is worse than pinning none: the deploy asks for a
    tag that does not exist and fails at pull time, on the Stage."""
    run = _run_text(build_deploy["jobs"]["pin-release"])
    for svc in SERVICES:
        assert svc in run, f"pin-release never mentions {svc}"
    assert "-slim" in run, (
        "the embedder's slim variant needs a pinned tag too, or EMBEDDER_IMAGE"
        "=...:slim floats while everything else is pinned"
    )


def test_pin_release_retags_and_never_rebuilds(build_deploy):
    """`imagetools create` copies a manifest server-side. Anything that builds
    here would produce a DIFFERENT image from the one already tested."""
    run = _run_text(build_deploy["jobs"]["pin-release"])
    assert "imagetools create" in run
    assert "build-push-action" not in str(build_deploy["jobs"]["pin-release"])


def test_the_builds_push_by_digest_and_tag_nothing(build_deploy):
    """The change that makes a multi-architecture image possible. Two runners
    cannot both push `:main-<sha>` and have the second be anything but a
    replacement of the first, so each pushes an anonymous per-architecture
    manifest and `pin-release` assembles them into one tagged list."""
    for job in ("build", "build-amd64"):
        for step in _steps(build_deploy["jobs"][job]):
            if "build-push-action" not in step.get("uses", ""):
                continue
            with_ = step["with"]
            assert "push-by-digest=true" in with_.get("outputs", ""), (
                f"{job} pushes without push-by-digest, so two architectures "
                "would overwrite each other"
            )
            assert "tags" not in with_, (
                f"{job} still pushes a tag; the merge job owns the tags now"
            )


def test_every_build_hands_its_digest_to_the_merge(build_deploy):
    """A digest that is built and not uploaded is an image nothing can find:
    the merge would publish a manifest list missing that architecture, and the
    failure would land on a user's `docker pull`, not in CI."""
    uploaded = set()
    for job in ("build", "build-amd64"):
        for step in _steps(build_deploy["jobs"][job]):
            if "upload-artifact" in step.get("uses", ""):
                uploaded.add(step["with"]["name"])
                assert step["with"].get("if-no-files-found") == "error", (
                    "a missing digest file must fail the build, not upload nothing"
                )
    # One per architecture for the matrix, plus the embedder's slim variant.
    assert "img-${{ matrix.service }}-arm64" in uploaded
    assert "img-${{ matrix.service }}-amd64" in uploaded
    assert "imgslim-embedder-arm64" in uploaded
    assert "imgslim-embedder-amd64" in uploaded


def test_the_slim_artifact_prefix_cannot_collide_with_the_full_one(build_deploy):
    """pin-release globs `img-<service>-*` to find one service's architectures.
    Naming the slim artifacts `img-embedder-slim-*` would make them match that
    glob, and the embedder's full image would be merged with a torch-free
    manifest inside it."""
    names = [s["with"]["name"] for job in ("build", "build-amd64")
             for s in _steps(build_deploy["jobs"][job])
             if "upload-artifact" in s.get("uses", "")]
    slim = [n for n in names if "slim" in n]
    assert slim, "the slim variant uploads no digest"
    for n in slim:
        assert not n.startswith("img-"), (
            f"{n} matches the `img-<service>-*` glob the full merge uses"
        )


def test_pin_release_collects_the_digests(build_deploy):
    steps = _steps(build_deploy["jobs"]["pin-release"])
    dl = next(s for s in steps if "download-artifact" in s.get("uses", ""))
    assert dl["with"]["pattern"].startswith("img")
    # One directory, whatever the number of artifacts. With per-artifact
    # directories, a run that builds a single image gets its one artifact
    # extracted straight into `path`, and the merge finds no digest.
    assert dl["with"]["merge-multiple"] is True
    assert '"${DIGESTS}/${prefix}"-*.txt' in _run_text(
        build_deploy["jobs"]["pin-release"])


def test_each_digest_file_is_named_after_its_artifact(build_deploy):
    """Merged into one directory, the file name is the only thing that tells
    one service's architecture from another's. Two uploads of `digest.txt`
    would overwrite each other."""
    for job in ("build", "build-amd64"):
        for step in _steps(build_deploy["jobs"][job]):
            if "upload-artifact" in step.get("uses", ""):
                name = step["with"]["name"]
                assert step["with"]["path"].endswith(f"/{name}.txt"), (
                    f"{job} uploads {name} from {step['with']['path']}"
                )


def test_a_missing_digest_fails_the_merge(build_deploy):
    """Publishing a manifest list with one architecture silently missing is
    worse than failing: the tag exists, the deploy succeeds, and only the
    users on the other architecture find out."""
    run = _run_text(build_deploy["jobs"]["pin-release"])
    assert "no digests found" in run
    assert "return 1" in run or "exit 1" in run


def test_latest_only_moves_on_main(build_deploy):
    """`latest` means the head of `main` and is what docker-compose.yml
    defaults to. A workflow_dispatch from another ref must not move it, or a
    branch build becomes what every unpinned deployment pulls."""
    run = _run_text(build_deploy["jobs"]["pin-release"])
    assert "refs/heads/main" in run, "nothing guards the moving tags"
    assert "MOVING" in run


def test_pin_release_survives_a_skipped_amd64_build(build_deploy):
    """While the repo is private the hosted job is skipped, and a skipped
    dependency skips the job that needs it — which would take `deploy` with it
    and stop the Stage updating at all."""
    job = build_deploy["jobs"]["pin-release"]
    assert "build-amd64" in job["needs"]
    guard = job["if"]
    assert "!cancelled()" in guard or "always()" in guard, (
        "without an override, a skipped build-amd64 skips this job and the deploy"
    )
    assert "needs['build-amd64'].result != 'failure'" in guard, (
        "the override alone would let a FAILED amd64 build through"
    )


def test_pin_release_does_not_use_docker_login_action(build_deploy):
    """The mac runner is headless and its login keychain is locked, so
    `docker/login-action` fails to store the credential (errSecInteraction-
    NotAllowed). The file-store seed in `build` is the way round it."""
    job = build_deploy["jobs"]["pin-release"]
    uses = [s.get("uses", "") for s in _steps(job)]
    assert not any("login-action" in u for u in uses), (
        "use the file-store auth step, as `build` does"
    )
    assert "DOCKER_CONFIG" in _run_text(job)


def test_pin_release_uses_its_own_docker_config_dir(build_deploy):
    """It can run on the same Mac as another job of this workflow. Sharing
    $RUNNER_TEMP/docker-cfg would have two jobs rewriting one config.json."""
    build_cfg = re.findall(r'DOCKER_CONFIG="([^"]+)"', _run_text(build_deploy["jobs"]["build"]))
    pin_cfg = re.findall(r'DOCKER_CONFIG="([^"]+)"', _run_text(build_deploy["jobs"]["pin-release"]))
    assert build_cfg and pin_cfg
    assert set(build_cfg).isdisjoint(pin_cfg), f"shared docker config dir: {pin_cfg}"


def test_pin_release_can_write_packages(build_deploy):
    perms = build_deploy["jobs"]["pin-release"].get("permissions", {})
    assert perms.get("packages") == "write", "retagging pushes a manifest"


# ── the deploy ─────────────────────────────────────────────────────────────

def test_a_skipped_optional_job_cannot_skip_the_deploy(build_deploy):
    """The regression this file exists to prevent a second time.

    A SKIPPED job propagates down the `needs` graph TRANSITIVELY, not just to
    its direct dependents. `build-amd64` is skipped on every run while the repo
    is private. `pin-release` overrode that for itself with a conditional — and
    the skip carried straight on through to `deploy` and `smoke`, which had
    not. The result was a pipeline that built images, pushed them, reported
    GREEN, and deployed nothing: Stage silently stayed on the previous release.

    So every job downstream of the optional one needs its own override, all the
    way to the end of the chain. `!cancelled()` rather than `always()`, because
    a cancelled run must not deploy."""
    optional = "build-amd64"
    assert build_deploy["jobs"][optional].get("if"), f"{optional} is not optional"

    # Everything reachable from build-amd64 through `needs`.
    downstream, frontier = set(), {optional}
    while frontier:
        nxt = set()
        for name, job in build_deploy["jobs"].items():
            if name in downstream:
                continue
            if frontier & set(job.get("needs", [])):
                downstream.add(name)
                nxt.add(name)
        frontier = nxt

    assert {"pin-release", "deploy", "smoke"} <= downstream, (
        f"expected the deploy chain downstream of {optional}, got {downstream}"
    )
    for name in sorted(downstream):
        guard = build_deploy["jobs"][name].get("if", "")
        assert "!cancelled()" in guard or "always()" in guard, (
            f"{name} is downstream of the optional {optional} but has no "
            "transitive-skip override, so a skipped amd64 build silently "
            "skips it and the pipeline still reports green"
        )


def test_notify_does_not_call_a_missing_deploy_a_success(build_deploy):
    """The second half of the same regression. `notify` only ever asked whether
    the deploy FAILED, so a deploy that never ran at all read as green — which
    is how the skip went unnoticed until someone looked at the job list."""
    run = _run_text(build_deploy["jobs"]["notify"])
    assert '"$DEPLOY" == "skipped"' in run, (
        "a build that produced images and then did not deploy them reports green"
    )


def test_the_deploy_pins_a_tag(build_deploy):
    run = _run_text(build_deploy["jobs"]["deploy"])
    assert "ELEFANT_TAG=main-" in run, (
        "the deploy does not pin a release tag, so it ships whatever `latest` "
        "points at and nothing records which build is running"
    )
    assert "github.sha" in run, "the tag must be derived from this commit"


def test_the_deploy_prints_the_tag_it_shipped(build_deploy):
    """The job log is where someone looks to answer 'what is on stage'."""
    run = _run_text(build_deploy["jobs"]["deploy"])
    assert run.count("${ELEFANT_TAG}") >= 1


def test_the_deploy_needs_pin_release(build_deploy):
    """A failed pin leaves the registry half-tagged. Deploying into that state
    fails at pull time, on the Stage, after the pipeline said it was fine."""
    assert "pin-release" in build_deploy["jobs"]["deploy"]["needs"]


def test_hyphenated_job_ids_are_referenced_with_brackets(workflow_text):
    """`needs.pin-release.result` is SUBTRACTION in a workflow expression: it
    evaluates to a number, so the condition guarding on it is true whatever the
    job actually did. Same trap `needs.test-ui` was already written around."""
    for job_id in ("pin-release", "test-ui"):
        bad = f"needs.{job_id}."
        assert bad not in workflow_text, (
            f"`{bad}result` parses as subtraction; use needs['{job_id}'].result"
        )


def test_notify_reports_the_pin(build_deploy):
    """A job nothing reports on can fail while the run looks green."""
    notify = build_deploy["jobs"]["notify"]
    assert "pin-release" in notify["needs"]
    assert "pin-release" in _run_text(notify)


# ── what the images claim to be ────────────────────────────────────────────

def test_both_image_builds_state_their_platform(build_deploy):
    """The Mac builds natively, so these are arm64 today whether or not it is
    written down. Saying so means a runner change fails the BUILD rather than
    turning up as `no matching manifest` on a deploy, and it is what the k3s
    standard asks for before these images are applied to a cluster."""
    steps = [s for s in _steps(build_deploy["jobs"]["build"])
             if "build-push-action" in s.get("uses", "")]
    assert len(steps) == 2, f"expected the full and slim builds, found {len(steps)}"
    for step in steps:
        assert step["with"].get("platforms") == "linux/arm64", (
            f"a build step does not state its platform: {step.get('id') or step['uses']}"
        )
