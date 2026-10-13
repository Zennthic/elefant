"""CI stays on the project's own self-hosted runners.

The org ran out of GitHub-hosted Actions minutes with weeks left in the cycle,
and the failure mode is nasty: hosted jobs die in two seconds with no logs,
the self-hosted jobs that `needs:` them are skipped, and commits merge with CI
having never run. Nothing announces it — the run is simply red, and it does
not look like a quota problem.

So "everything is self-hosted" is an invariant worth asserting rather than
remembering. A single `runs-on: ubuntu-latest` pasted back in from another
repo's workflow is how it comes undone, and it would look perfectly normal in
review.

There are now THREE deliberate exceptions, and each is pinned here because it
is exactly the kind of thing a later tidy-up would "finish" without reading why
it was left:

  * `dco`, which is the only job a FORK can reach, so it belongs on a
    disposable machine rather than a private network;
  * `build-amd64`, because hosted runners ARE amd64 and the Mac is not, so it
    is the only way `latest` can carry an image an ordinary self-hoster can
    pull. It is free once the repo is public, and it carries a guard that keeps
    it switched off — and unbilled — until then;
  * every job in `pr-checks.yml`, which runs lint and the test suites on pull
    requests. A PR can come from a fork, so those jobs MUST be hosted: the LAN
    is the one place they may never run. They carry the same private-repo
    guard as `build-amd64`.

No exception is "hosted is fine after all". The rule stands for everything
else, and the tests below say so by name.
"""
from pathlib import Path

import pytest

from internal_names import LAB_HOST_MARKERS

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
SCRIPTS = WORKFLOWS.parent / "scripts"


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


@pytest.fixture(scope="module")
def build_deploy():
    return _load("build-deploy.yml")


def _workflow_files():
    """Every workflow GitHub would run: both spellings of the extension, so a
    `.yaml` file cannot slip past a check written for `.yml`."""
    return sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml")))


def _jobs_on(workflow, predicate):
    return [n for n, j in workflow["jobs"].items() if predicate(j.get("runs-on"))]


def _is_hosted(runs_on):
    return isinstance(runs_on, str) and runs_on.startswith("ubuntu")


# ── the invariant ──────────────────────────────────────────────────────────

# The only job in build-deploy.yml allowed on a hosted runner. Adding a second
# name here is a decision, not a tidy-up.
HOSTED_EXCEPTION = "build-amd64"


def test_the_pipeline_uses_no_github_hosted_runners(build_deploy):
    hosted = _jobs_on(build_deploy, _is_hosted)
    assert hosted == [HOSTED_EXCEPTION], (
        f"{hosted} run on GitHub's hosted runners, and only {HOSTED_EXCEPTION!r} "
        "may. Those are metered on a private repo, the org has run out before, "
        "and when it does these jobs fail in seconds with no logs while build "
        "and deploy silently skip."
    )


def test_every_job_names_a_real_runner(build_deploy):
    """A typo'd label does not fail — the job queues forever."""
    known = {("self-hosted", "macOS"), ("self-hosted", "linux", "docker-swarm")}
    for name, job in build_deploy["jobs"].items():
        runs_on = job["runs-on"]
        if name == HOSTED_EXCEPTION:
            assert runs_on == "ubuntu-latest", f"{name}: {runs_on}"
            continue
        assert tuple(runs_on) in known, f"{name} targets an unknown runner: {runs_on}"


def test_the_hosted_job_bills_nothing_while_the_repo_is_private(build_deploy):
    """The whole basis of the exception. Hosted minutes are free for PUBLIC
    repositories; the billing incident that produced the never-hosted rule was
    on a private one. This guard is what makes both statements true at once,
    and it flips on its own when the repo is published — no commit needed, and
    nothing to remember on the day."""
    guard = build_deploy["jobs"][HOSTED_EXCEPTION].get("if", "")
    assert "github.event.repository.private" in guard, (
        f"{HOSTED_EXCEPTION} has no private-repo guard, so it starts spending "
        "metered minutes immediately"
    )
    assert "!" in guard.split("github.event.repository.private")[0].strip()[-1:], (
        "the guard must be NEGATED — as written it would run only while private"
    )


def test_the_hosted_job_touches_no_self_hosted_machine(build_deploy):
    """It is the one job that could run a public repo's code somewhere we do
    not control. It must therefore need nothing from the LAN: no kubeconfig, no
    deploy, no smoke against an internal host."""
    job = build_deploy["jobs"][HOSTED_EXCEPTION]
    body = str(job)
    for forbidden in ("KUBECONFIG", "SMOKE_SESSION_COOKIE", *LAB_HOST_MARKERS):
        assert forbidden not in body, f"{HOSTED_EXCEPTION} reaches for {forbidden}"


def test_nothing_deploys_from_the_hosted_job(build_deploy):
    """`deploy` must depend on work done on our own machines. If the hosted job
    could gate a deploy, a hosted outage would become a Stage outage."""
    assert HOSTED_EXCEPTION not in build_deploy["jobs"]["deploy"].get("needs", [])


def test_a_dispatch_on_main_deploys_what_it_rebuilt(build_deploy):
    """A manual dispatch rebuilds every service, and it is the runbook's answer
    when a push's run failed: change detection diffs only the range just
    pushed, so the next push never rebuilds what the failed run left behind.
    Until 2026-10-13 the deploy job ran on `push` alone, so a dispatch built
    eight images and put none of them on Stage. Any other ref still never
    deploys, dispatch or not."""
    cond = " ".join(build_deploy["jobs"]["deploy"]["if"].split())
    assert "github.event_name == 'push'" in cond
    assert "github.event_name == 'workflow_dispatch'" in cond
    assert "github.ref == 'refs/heads/main'" in cond
    notify = build_deploy["jobs"]["notify"]["steps"][-1]["run"]
    assert '"${{ github.event_name }}" == "workflow_dispatch"' in notify, (
        "a dispatch that built images and did not deploy must be reported as a "
        "pipeline fault, like a push"
    )


def test_dco_stays_hosted_and_says_why():
    """A FORK can reach it, and on `pull_request` the checkout
    is the fork's own code — including its copy of the script this job runs.
    That belongs on a disposable machine, not on a private network."""
    dco = _load("dco.yml")
    assert _jobs_on(dco, _is_hosted) == ["dco"]
    text = (WORKFLOWS / "dco.yml").read_text()
    assert "STAYS THERE" in text, "the exception must be documented where it is made"
    assert "fork" in text.lower()


def test_nothing_else_in_the_repo_is_hosted():
    """Catches a workflow added later without reading any of this. The release
    workflow in particular: it holds a packages:write token and runs on the
    LAN, and there is no amd64-shaped reason for it to be hosted, because it
    retags manifests rather than building anything."""
    for path in _workflow_files():
        if path.name in ("dco.yml", "pr-checks.yml"):
            continue
        hosted = _jobs_on(yaml.safe_load(path.read_text()), _is_hosted)
        allowed = [HOSTED_EXCEPTION] if path.name == "build-deploy.yml" else []
        assert hosted == allowed, f"{path.name}: {hosted} are on hosted runners"


# ── pr-checks.yml: the one workflow that runs a stranger's code ───────────
# The opposite rule to everything above: here a self-hosted runner would be the
# bug, because a pull request can come from a fork.

# What the private-repo guard looks like; the one way past it is a manual
# dispatch, which only somebody with write access can start.
PR_GUARD = "!github.event.repository.private || github.event_name == 'workflow_dispatch'"


@pytest.fixture(scope="module")
def pr_checks():
    return _load("pr-checks.yml")


def test_pr_checks_runs_lint_and_every_suite(pr_checks):
    jobs = pr_checks["jobs"]
    assert set(jobs) == {"lint", "test", "test-ui"}
    assert set(jobs["test"]["strategy"]["matrix"]["service"]) == {
        "editor", "embedder", "memory-api", "mcp"}


def test_every_pr_check_is_hosted(pr_checks):
    """A PR's code on a LAN machine is arbitrary code execution by anyone who
    can open one."""
    for name, job in pr_checks["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", \
            f"pr-checks.yml/{name} runs on {job['runs-on']}, not a hosted runner"


def test_the_hosted_set_is_exactly_the_named_exceptions(build_deploy, pr_checks):
    hosted = set(_jobs_on(build_deploy, _is_hosted)) | {
        f"pr-checks/{n}" for n in _jobs_on(pr_checks, _is_hosted)}
    assert hosted == {HOSTED_EXCEPTION} | {f"pr-checks/{n}" for n in pr_checks["jobs"]}


def test_pr_checks_never_uses_pull_request_target(pr_checks):
    """`pull_request_target` runs with the base repo's token and secrets against
    code the PR author controls. See the PyYAML `on:` note further down."""
    triggers = pr_checks.get("on", pr_checks.get(True))
    assert set(triggers) == {"pull_request", "workflow_dispatch"}


def test_pr_checks_uses_no_secret_and_no_environment(pr_checks):
    text = (WORKFLOWS / "pr-checks.yml").read_text()
    assert "secrets." not in text, "a PR check must not touch a secret"
    assert "GITHUB_TOKEN" not in text
    assert pr_checks["permissions"] == {"contents": "read"}
    for name, job in pr_checks["jobs"].items():
        assert "environment" not in job, f"{name} declares an environment"
        assert "permissions" not in job, f"{name} widens its token"


def test_every_pr_check_carries_the_private_repo_guard(pr_checks):
    """Hosted minutes are billed while the repo is private. The guard keeps
    these jobs off until it is published, and flips on by itself after."""
    for name, job in pr_checks["jobs"].items():
        guard = " ".join(str(job.get("if", "")).split())
        assert PR_GUARD in guard, f"pr-checks.yml/{name} has no private-repo guard"


def test_pr_checks_run_what_main_runs(build_deploy, pr_checks):
    """Same images, same script, same modes: a PR is checked by what will
    check it after merge."""
    assert pr_checks["env"]["CI_IMAGE"] == build_deploy["env"]["CI_IMAGE"]
    assert pr_checks["env"]["NODE_CI_IMAGE"] == build_deploy["env"]["NODE_CI_IMAGE"]
    modes = set()
    for job in pr_checks["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            if "docker run" in run:
                assert '-v "$PWD:/src:ro"' in run
                assert "< .github/scripts/ci-in-container.sh" in run
                modes.add(run.split("bash -s ")[1].split()[0])
    assert modes == {"lint", "test", "test-ui"}


def test_superseded_pr_runs_cancel(pr_checks):
    conc = pr_checks["concurrency"]
    assert "pull_request.number" in conc["group"]
    assert conc["cancel-in-progress"] is True


# ── the things that make self-hosted safe ──────────────────────────────────

def test_there_is_a_concurrency_group(build_deploy):
    """Without one, two merges close together start two deploys side by side.
    They can finish out of order and leave the cluster on the EARLIER image,
    with both runs reporting success and nothing looking wrong."""
    group = build_deploy.get("concurrency", {}).get("group", "")
    assert "github.workflow" in group and "github.ref" in group


def test_pushes_queue_rather_than_cancel_each_other(build_deploy):
    """Cancelling a superseded push would abandon a deploy half-done. Only
    pull requests may cancel, because those are only checks."""
    assert "pull_request" in str(build_deploy["concurrency"]["cancel-in-progress"])


def test_the_checks_carry_a_fork_guard(build_deploy):
    """Inert while this workflow has no `pull_request:` trigger, and there so
    that adding one — which is a reasonable thing to do — does not silently
    start running strangers' code on a machine on a private network."""
    for job in ("lint", "test", "test-ui"):
        guard = build_deploy["jobs"][job].get("if", "")
        assert "head.repo.full_name == github.repository" in guard, \
            f"{job} would run a fork's code on a self-hosted machine"


def test_every_container_is_named_and_uniquely_so(build_deploy):
    """Two things at once. An ANONYMOUS container that outlives a cancelled job
    cannot be killed by name and wedges every later run on a machine that is
    never wiped. And matrix jobs can land on sibling runner processes on the
    same Mac, so a shared name would have them fighting over one container."""
    names = []
    for job in build_deploy["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "docker run" in run:
                assert "--name " in run, f"anonymous container in: {run[:60]}"
                names.append(run.split("--name ")[1].split()[0])
    assert names, "no containerised job found — did the CI script stop being used?"
    assert len(names) == len(set(names)), f"container names collide: {names}"


def test_a_cancelled_job_still_cleans_up_its_container(build_deploy):
    """`docker run --rm` covers the normal exit. A cancel kills the step, not
    the container, so each containerised job needs an `if: always()` teardown."""
    for name, job in build_deploy["jobs"].items():
        steps = job.get("steps", [])
        if not any("docker run" in s.get("run", "") for s in steps):
            continue
        teardown = [s for s in steps
                    if "docker rm -f" in s.get("run", "") and s.get("if") == "always()"]
        assert teardown, f"{name} starts a container with no always() teardown"


# ── the script the jobs run ────────────────────────────────────────────────

def test_the_ci_script_exists_and_covers_every_mode_used(build_deploy):
    script = SCRIPTS / "ci-in-container.sh"
    assert script.exists()
    body = script.read_text()
    used = set()
    for job in build_deploy["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "ci-in-container.sh" in run:
                used.add(run.split("bash -s ")[1].split()[0])
    assert used, "the workflow no longer calls the CI script"
    for mode in used:
        assert f"  {mode})" in body, f"the script has no '{mode}' mode"


def test_the_script_needs_no_exec_bit():
    """It is piped in on stdin (`bash -s`), so a file mode that did not survive
    a checkout cannot break CI."""
    for job_run in (SCRIPTS / "ci-in-container.sh",):
        assert job_run.exists()
    text = (WORKFLOWS / "build-deploy.yml").read_text()
    assert "bash -s" in text
    assert "./.github/scripts/ci-in-container.sh" not in text, \
        "calling it as an executable reintroduces the file-mode dependency"


def test_the_workspace_is_mounted_read_only(build_deploy):
    """The container runs as root and the build job later does `git clean -ffdx`
    as the runner user. Root-owned leftovers break a deploy minutes later,
    somewhere that looks nothing to do with the test job."""
    for job in build_deploy["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "docker run" in run and "/src" in run:
                assert '-v "$PWD:/src:ro"' in run, f"writable workspace mount: {run[:80]}"


def test_ci_runs_the_python_the_dockerfiles_ship(build_deploy):
    """The whole reason for the container: CI on the same runtime as production.
    If a Dockerfile moves to a new Python and this does not, the drift is
    silent — the suite simply stops testing what ships."""
    image = build_deploy["env"]["CI_IMAGE"]
    repo = WORKFLOWS.parent.parent
    shipped = {
        p.read_text().split("FROM python:")[1].split("-")[0].split()[0]
        for p in repo.glob("*/Dockerfile")
        if "FROM python:" in p.read_text()
    }
    assert len(shipped) == 1, f"services ship different Pythons: {shipped}"
    assert image.startswith(f"python:{shipped.pop()}"), \
        f"CI_IMAGE {image} is not the Python the Dockerfiles ship"


# ── what a public repo changes ─────────────────────────────────────────────
# The repo is public, and these self-hosted runners sit
# on a private network with a kubeconfig for the cluster within reach.

def test_the_pipeline_never_gains_a_pull_request_trigger(build_deploy):
    """The single most dangerous line anyone could add to this file once the
    repo is public. `pull_request` runs the PR head's code — a stranger's — on
    a machine on a private network, and on the one runner that can reach the
    cluster API. The fork guards on `lint` and `test` are a second line of
    defence, not a substitute: they do not cover `build`, `deploy` or `smoke`.

    Note that PyYAML parses a bare `on:` key as the BOOLEAN True, which is why
    this looks the key up the awkward way instead of writing `workflow["on"]`
    and quietly asserting against nothing."""
    triggers = build_deploy.get("on", build_deploy.get(True))
    assert triggers is not None, "the workflow has no triggers at all"
    assert set(triggers) == {"push", "workflow_dispatch"}, (
        f"triggers are {sorted(triggers)}; only push-to-main and manual dispatch "
        "are safe on runners that sit on a private network"
    )
    assert triggers["push"]["branches"] == ["main"]


def test_a_k3s_deploy_declares_its_environment(build_deploy):
    """Arms itself when a k3s deploy job is added. The cluster API address, the Stage
    hostname and KUBECONFIG_B64 live in the `stage-k3s` GitHub Environment,
    which is restricted to `main` — not in this file, which becomes public, and
    not at repo level, where every job can read them.

    Written to pass today on purpose: asserting a job that does not exist yet
    would be a red suite rather than a guard."""
    for name, job in build_deploy["jobs"].items():
        if "k3s" not in name:
            continue
        assert job.get("environment") == "stage-k3s", (
            f"{name} touches the cluster without declaring an Environment, so "
            "its credentials would have to sit at repo level"
        )


def test_no_internal_address_is_written_into_the_workflow(build_deploy):
    """Environment *variables* carry these, for the same reason. A public
    workflow file should not publish the cluster's LAN address or the internal
    hostname of a Stage."""
    text = (WORKFLOWS / "build-deploy.yml").read_text()
    for leak in LAB_HOST_MARKERS:
        assert leak not in text, f"the workflow names {leak!r}; use an Environment variable"



# ── inert in a fork ────────────────────────────────────────────────────────
# A fork of the public repo that enables Actions would otherwise get jobs that
# push images to our registry path, deploy to our Stage and smoke it. They
# would queue forever for runners the fork does not have, which is noise and
# reads as if our Stage were part of the product. Exactly these jobs carry the
# guard; tests and lint keep running in a fork.

FORK_GUARD = "github.repository == 'zennthic/elefant'"
FORK_INERT = {
    "build-deploy.yml": {"build", "build-amd64", "pin-release", "deploy", "smoke"},
    "release.yml": {"release"},
}


@pytest.mark.parametrize("wf", sorted(FORK_INERT))
def test_exactly_the_deploy_family_is_inert_in_a_fork(wf):
    jobs = _load(wf)["jobs"]
    guarded = {n for n, j in jobs.items() if FORK_GUARD in str(j.get("if", ""))}
    assert guarded == FORK_INERT[wf], (
        f"{wf}: jobs guarded to this repo are {sorted(guarded)}, expected "
        f"{sorted(FORK_INERT[wf])}. A job that deploys, smokes Stage or pushes "
        "images must be skipped in a fork; a test or lint job must not be."
    )


STAGE_DIR = "${{ secrets.STAGE_DEPLOY_DIR || vars.STAGE_DEPLOY_DIR }}"
STAGE_URL = "${{ secrets.STAGE_BASE_URL || vars.STAGE_BASE_URL }}"


def test_the_stage_specifics_come_from_secrets_first(build_deploy):
    """The deploy host's directory and the Stage's URL are ours, not the
    product's, so the public workflow does not name them. They are read from
    SECRETS first: the job log is public as well, and GitHub masks a secret
    wherever it appears but prints a variable in full (every step's env
    header showed the deploy path, and the smoke job's showed the Stage URL).
    The variable is only a fallback for a repo that has not moved it yet."""
    deploy, smoke = build_deploy["jobs"]["deploy"], build_deploy["jobs"]["smoke"]
    assert deploy["env"]["STAGE_DEPLOY_DIR"] == STAGE_DIR
    smoke_env = [s.get("env", {}).get("BASE_URL") for s in smoke["steps"]]
    assert STAGE_URL in smoke_env
    assert all(u in (None, STAGE_URL) for u in smoke_env), smoke_env


def test_no_step_writes_a_stage_value_into_its_own_script(build_deploy):
    """A `${{ }}` inside `run:` is expanded before the step's script is
    printed, so it would show the value even where the env header masks it."""
    for name, job in build_deploy["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            for leak in ("vars.STAGE_", "secrets.STAGE_"):
                assert leak not in run, f"{name}: a step expands {leak}* in its script"


def test_the_deploy_log_hides_the_hosts_own_addresses(build_deploy):
    """The verify step prints the front door as the deploy host's LAN address
    (`front door http://10.x.x.x:8321`). The first shell step masks every
    address the host has, before any step can print one."""
    runs = [s.get("run", "") for s in build_deploy["jobs"]["deploy"]["steps"]]
    first = next(r for r in runs if r)
    assert "hostname -I" in first and "::add-mask::" in first
    assert first.index("::add-mask::") < first.index("::error::")


def test_no_image_carries_the_push_event(build_deploy):
    """Buildx's provenance attestation records the whole GitHub event: the
    pusher's own email and every pushed commit message. It is published with
    the image, so anyone who can pull can read it, and build-push-action
    prints it in the job log. Found on run 496 (2026-10-13), where the build
    log carried the pusher's personal address. Every image build turns it off,
    and the release retag (imagetools) adds none of its own."""
    steps = [(name, s) for name, job in build_deploy["jobs"].items()
             for s in job.get("steps", [])
             if "docker/build-push-action" in s.get("uses", "")]
    assert len(steps) == 4, [n for n, _ in steps]
    for name, step in steps:
        assert step["with"].get("provenance") is False, f"{name}: provenance is not off"
        assert step["with"].get("sbom") in (None, False), f"{name}: an SBOM would carry it too"


def test_no_build_log_prints_the_push_event(build_deploy):
    """`provenance: false` keeps the record out of the registry, but buildx
    still writes it into its local metadata file, which build-push-action
    prints in the job log. Run 497 (2026-10-13) showed the pusher's email
    there after the attestation was already off."""
    assert build_deploy["env"].get("BUILDX_METADATA_PROVENANCE") == "disabled"
    for name, job in build_deploy["jobs"].items():
        assert "BUILDX_METADATA_PROVENANCE" not in (job.get("env") or {}), (
            f"{name} overrides BUILDX_METADATA_PROVENANCE")


def test_the_smoke_log_hides_the_stage_hostname(build_deploy):
    runs = [s.get("run", "") for s in build_deploy["jobs"]["smoke"]["steps"]]
    first = next(r for r in runs if r)
    assert '::add-mask::$host' in first


def test_an_unset_stage_variable_fails_before_anything_runs(build_deploy):
    """Unset, `$STAGE_DEPLOY_DIR/docker-compose.yml` is `/docker-compose.yml`:
    the deploy would write to the root of the host. The check has to come
    first, and say which variable to set."""
    for job, var in (("deploy", "STAGE_DEPLOY_DIR"), ("smoke", "STAGE_BASE_URL")):
        steps = build_deploy["jobs"][job]["steps"]
        runs = [s.get("run", "") for s in steps]
        first = next(i for i, r in enumerate(runs) if r)
        assert "::error::" in runs[first] and var in runs[first], (
            f"{job}'s first shell step does not refuse an unset {var}"
        )
        assert "exit 1" in runs[first]

# ── memory-api's real-PostgreSQL integration tests ─────────────────────────
# Eight memory-api modules test the board against a real database, and for a
# long time CI gave them none: they skipped, the job was green, and nothing
# said so. These pin the wiring that runs them, on `main` and on a PR alike.

PG_SCRIPT = SCRIPTS / "ci-postgres.sh"


def _test_steps(workflow):
    return workflow["jobs"]["test"]["steps"]


@pytest.mark.parametrize("wf", ["build-deploy.yml", "pr-checks.yml"])
def test_memory_api_gets_a_postgres(wf):
    steps = _test_steps(_load(wf))
    up = [s for s in steps if "ci-postgres.sh up" in s.get("run", "")]
    assert len(up) == 1, f"{wf}: the test job starts no PostgreSQL"
    assert up[0].get("if") == "matrix.service == 'memory-api'"
    # Not `services:`: it is Linux-only and does nothing on the macOS runner.
    assert "services" not in _load(wf)["jobs"]["test"]


@pytest.mark.parametrize("wf", ["build-deploy.yml", "pr-checks.yml"])
def test_the_test_container_joins_the_database_and_knows_it_is_ci(wf):
    """`-e CI` is what turns a missing database into a failure rather than a
    skip, and PG_DOCKER_ARGS is how the container reaches the database."""
    runs = [s["run"] for s in _test_steps(_load(wf)) if "ci-in-container.sh" in s.get("run", "")]
    assert len(runs) == 1
    assert "-e CI " in runs[0]
    assert "$PG_DOCKER_ARGS" in runs[0]


@pytest.mark.parametrize("wf", ["build-deploy.yml", "pr-checks.yml"])
def test_the_database_is_torn_down_even_on_cancel(wf):
    down = [s for s in _test_steps(_load(wf))
            if "ci-postgres.sh down" in s.get("run", "") and s.get("if") == "always()"]
    assert down, f"{wf}: the PostgreSQL container has no always() teardown"


def test_the_postgres_script_names_its_containers_for_the_cleanup():
    body = PG_SCRIPT.read_text()
    assert 'name="elefant-ci-pg-' in body
    assert 'net="elefant-ci-net-' in body


def test_ci_tests_against_the_postgres_compose_ships_pinned_by_digest():
    body = PG_SCRIPT.read_text()
    image = body.split('PG_IMAGE="')[1].split('"')[0]
    assert "@sha256:" in image, "pin the CI PostgreSQL by digest"
    compose = (WORKFLOWS.parent.parent / "docker-compose.yml").read_text()
    shipped = compose.split("image: postgres:")[1].split()[0]
    assert image.split("postgres:")[1].split("@")[0] == shipped, \
        f"CI tests against {image}, compose ships postgres:{shipped}"


def test_ci_smoke_does_not_require_a_session_for_now(build_deploy):
    """Card #205 made CI fail when the smoke cookie was unset or expired
    (SMOKE_REQUIRE_AUTH=1). A session cookie lives 24 hours, so that halted
    every deploy a day after the secret was pasted; it is backed out until
    card #417 settles how authenticated smoke signs in. This pins the
    interim state so it is not re-enabled by accident: the cookie still
    reaches the container when it is set, and nothing makes it mandatory."""
    step = next(s for s in build_deploy["jobs"]["smoke"]["steps"]
                if s.get("name") == "Run smoke tests")
    assert "SMOKE_REQUIRE_AUTH" not in step["env"]
    assert "SMOKE_REQUIRE_AUTH" not in step["run"]
    assert "-e SMOKE_SESSION_COOKIE" in step["run"], \
        "SMOKE_SESSION_COOKIE never reaches the smoke container"


# ── change detection on a fresh history ─────────────────────────────────────

def _detect_script(build_deploy):
    for job in build_deploy["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Detect changed service directories":
                return step["run"]
    raise AssertionError("no 'Detect changed service directories' step")


@pytest.mark.parametrize("before", ["0" * 40, "1234567" * 5 + "12345"])
def test_a_root_commit_builds_every_service_it_holds(build_deploy, tmp_path, before):
    """The public repository starts as one root commit, and it is force-pushed
    as one root commit, whose old base a shallow checkout cannot see. Plain
    `git diff-tree` prints nothing for a root commit, so detection found no
    changed services and nothing was built or deployed. Run the workflow's
    own script on a one-commit repo, as a new branch (zero base) and as a
    force-push (unknown base)."""
    import subprocess
    repo = tmp_path / "repo"
    for svc in ("editor", "mcp"):
        (repo / svc).mkdir(parents=True)
        (repo / svc / "main.py").write_text("x = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["-C", str(repo), "add", "-A"], check=True)
    subprocess.run(git + ["-C", str(repo), "commit", "-qm", "root"], check=True)
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                           capture_output=True, text=True).stdout.strip()
    out = tmp_path / "out"
    script = (_detect_script(build_deploy)
              .replace("${{ github.event_name }}", "push")
              .replace("${{ github.event.before }}", before)
              .replace("${{ github.event.after }}", after))
    subprocess.run(["bash", "-c", script], cwd=repo, check=True,
                   env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"})
    assert 'services=["editor","mcp"]' in out.read_text()


# ── anything a stranger can trigger stays off our machines ─────────────────
#
# The deploy-family guards above protect specific jobs. This is the general
# rule, for workflows that do not exist yet: an event an outside contributor
# can cause must never reach a self-hosted runner, a secret, an environment or
# a write permission. The known cases are pull_request (fork PRs) and the
# triggers that run with the base repo's token on untrusted input.

PR_REACHABLE = {"pull_request"}
NEVER_ALLOWED = {"pull_request_target", "workflow_run", "issue_comment",
                 "issues", "pull_request_review", "pull_request_review_comment",
                 "discussion", "discussion_comment", "repository_dispatch"}


def _triggers(workflow):
    on = workflow.get(True, workflow.get("on"))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set(on or {})


def test_no_workflow_uses_a_trigger_that_runs_our_token_on_a_strangers_input():
    for path in _workflow_files():
        bad = _triggers(yaml.safe_load(path.read_text())) & NEVER_ALLOWED
        assert not bad, (
            f"{path.name} uses {sorted(bad)}. These events run with this "
            "repository's token on input anyone can write; keep PR checks on "
            "plain pull_request and GitHub-hosted runners.")


def test_every_workflow_a_fork_pr_can_trigger_is_hosted_and_secretless():
    checked = 0
    for path in _workflow_files():
        wf = yaml.safe_load(path.read_text())
        if not _triggers(wf) & PR_REACHABLE:
            continue
        checked += 1
        text = path.read_text()
        assert "secrets." not in text, f"{path.name} reads a secret on a PR run"
        for name, job in wf["jobs"].items():
            where = f"{path.name}:{name}"
            assert _is_hosted(job.get("runs-on")), \
                f"{where} would run a stranger's PR on a self-hosted machine"
            assert "environment" not in job, f"{where} binds an environment on a PR run"
            assert "permissions" not in job, f"{where} widens the token on a PR run"
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, \
                        f"{where} leaves the token in .git/config for the PR's own scripts"
    assert checked >= 2, "pr-checks.yml and dco.yml should both be PR-triggered"


def test_only_the_fork_inert_jobs_hold_a_write_permission():
    for path in _workflow_files():
        wf = yaml.safe_load(path.read_text())
        for name, job in wf["jobs"].items():
            perms = job.get("permissions") or {}
            if "write" in perms.values():
                assert name in FORK_INERT.get(path.name, set()), (
                    f"{path.name}:{name} can write but is not guarded to this "
                    "repository; add it to FORK_INERT and give it the guard")
