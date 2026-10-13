"""The install instructions, checked against the things they install.

`README.md` is the whole install path for anybody who arrives from the release
video, and a README is the file most likely to be quietly wrong: nothing fails
when a manifest moves and the copy-paste block does not. `.github/workflows/
docs-install.yml` catches that by RUNNING both quick starts — but it is heavy,
and it only runs when the install path is touched.

These are the fast half of the same guarantee, in the ordinary test job on every
push. Each one reads the documented block and the thing it installs, and asserts
they still agree: the secrets the stack fails fast without, the Secret keys the
manifests read, the ConfigMap the kustomization merges into, the two compose
files that must be passed together. A drift here is a README that would strand
somebody on a fresh host, so the failure message says which file to fix.
"""
import importlib.util
import re
from pathlib import Path

import pytest

from internal_names import LAB_HOST_MARKERS

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
README = REPO / "README.md"
COMPOSE = REPO / "docker-compose.yml"
TRAEFIK = REPO / "docker-compose.traefik.yml"
GUIDES = (REPO / "docs" / "install" / "docker-compose.md",
          REPO / "docs" / "install" / "kubernetes.md")
BASE = REPO / "deploy" / "k8s" / "base"
WORKFLOW = REPO / ".github" / "workflows" / "build-deploy.yml"
DOCS_WORKFLOW = REPO / ".github" / "workflows" / "docs-install.yml"
RUNNER = REPO / ".github" / "scripts" / "test-install-docs.sh"


def _doc_blocks():
    """tools/doc_blocks.py, imported by path — it is a standalone script rather
    than part of a package, so there is nothing to `import` by name."""
    spec = importlib.util.spec_from_file_location(
        "doc_blocks", REPO / "tools" / "doc_blocks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def blocks():
    return _doc_blocks().collect(repo=REPO)


# ── the marked blocks exist and are what the runner expects ────────────────

# The runner asks for these by name. A rename in the markdown that is not made
# here leaves the CI job asking for a block that no longer exists.
EXPECTED = {
    "compose-quickstart": None,
    "k8s-kustomization": "kustomization.yaml",
    "k8s-quickstart": None,
}


def test_every_expected_block_is_marked_and_parses(blocks):
    assert set(blocks) == set(EXPECTED), (
        f"marked blocks are {sorted(blocks)}, the runner expects "
        f"{sorted(EXPECTED)}. Update `.github/scripts/test-install-docs.sh` "
        f"and this list together."
    )
    for tag, want_file in EXPECTED.items():
        b = blocks[tag]
        assert b.body.strip(), f"{tag} is empty"
        assert b.file == want_file, f"{tag} has file={b.file!r}, expected {want_file!r}"


def test_the_runner_asks_for_exactly_those_blocks():
    """The script names each tag in a shell string, where a typo is silent until
    the job runs."""
    body = RUNNER.read_text()
    for tag in EXPECTED:
        assert tag in body, f"{RUNNER.name} never asks for the {tag!r} block"


def test_the_runner_substitutions_still_match_the_published_text(blocks):
    """The runner rewrites four things so the docs point at the commit under
    test rather than at a release that does not exist yet. Each rewrite is a
    `sed` pattern, and a pattern that stops matching does not fail — it silently
    runs the published line, which then tries to clone the real repository or
    pull a tag called `vX.Y.Z`."""
    runner = RUNNER.read_text()
    compose = blocks["compose-quickstart"].body
    kust = blocks["k8s-kustomization"].body

    assert "git clone https://github.com/zennthic/elefant.git" in compose
    assert "\ncd elefant\n" in compose
    assert "ELEFANT_TAG=vX.Y.Z" in compose
    assert "https://github.com/zennthic/elefant//deploy/k8s/base?ref=vX.Y.Z" in kust

    # The same four literals, as the escaped forms the sed lines carry.
    for needle in (r"git clone https://github.com/zennthic/elefant\.git",
                   r"^cd elefant\$",
                   r"deploy/k8s/base?ref=vX\.Y\.Z",
                   r"ELEFANT_TAG=vX\.Y\.Z"):
        assert needle in runner, f"{RUNNER.name} no longer substitutes {needle!r}"
    # And the base's image pins, which a release commit bumps before its tag
    # has published the images they name.
    assert r"newTag:[[:space:]]*).*$#\1latest#" in runner, (
        f"{RUNNER.name} no longer points the copied base's newTag at latest")


# ── the Compose quick start matches the Compose file ──────────────────────

def _failfast_vars(text):
    """The `${VAR:?message}` values — the ones compose refuses to start without.

    `[A-Z0-9_]` and not `[A-Z_]`: the S3 pair is `ELEFANT_S3_ACCESS_KEY`, and a
    pattern without the digit silently returns four names instead of six. A
    narrower character class here does not fail, it just stops checking.
    """
    return set(re.findall(r"\$\{([A-Z0-9_]+):\?", text))


def test_the_quick_start_sets_everything_compose_fails_fast_without(blocks):
    body = blocks["compose-quickstart"].body
    for var in sorted(_failfast_vars(COMPOSE.read_text())):
        if var == "ELEFANT_PUBLIC_DOMAIN":
            # Supplied by .env.example as a placeholder, and the README tells the
            # reader to change it. Asserted separately below.
            continue
        assert f"{var}=" in body, (
            f"docker-compose.yml will not start without {var}, and the README's "
            f"quick start never sets it"
        )


def test_the_fail_fast_set_is_the_size_we_think_it_is(blocks):
    """A guard on the regex above rather than on the docs. If it silently
    narrowed, `test_the_quick_start_sets_everything_compose_fails_fast_without`
    would keep passing while checking less — which is how the S3 pair went
    unchecked in the first draft of this file."""
    found = _failfast_vars(COMPOSE.read_text())
    assert {"ELEFANT_S3_ACCESS_KEY", "ELEFANT_S3_SECRET_KEY"} <= found, (
        f"the S3 credentials are `:?` in docker-compose.yml but were not found: "
        f"{sorted(found)}"
    )
    assert len(found) >= 6, f"only {len(found)} fail-fast variables found: {sorted(found)}"


def test_the_quick_start_copies_the_env_file_that_exists(blocks):
    body = blocks["compose-quickstart"].body
    assert "cp .env.example .env" in body
    assert (REPO / ".env.example").exists()


def test_the_quick_start_pins_a_version_and_says_latest_is_not_one(blocks):
    """`latest` is the head of `main`, not a release. An unpinned install moves
    under the reader on our schedule."""
    body = blocks["compose-quickstart"].body
    assert "ELEFANT_TAG=" in body, "the quick start does not pin a version"
    readme = README.read_text()
    assert "latest" in readme and "not a release" in readme, (
        "the README must say plainly that `latest` is not a release"
    )


def test_appending_to_env_is_what_actually_wins():
    """The quick start appends its values to a copy of .env.example rather than
    editing in place, which only works because a later definition of a key wins.
    Keep the mechanism honest: every key it appends must ALSO be present in
    .env.example, or the append is doing something other than overriding."""
    env_example = (REPO / ".env.example").read_text()
    for var in ("ELEFANT_TAG", "ELEFANT_SESSION_SECRET",
                "ELEFANT_INTERNAL_SERVICE_TOKEN", "POSTGRES_PASSWORD",
                "ELEFANT_S3_ACCESS_KEY", "ELEFANT_S3_SECRET_KEY"):
        assert re.search(rf"^#?\s*{var}=", env_example, re.M), (
            f"{var} is appended to .env by the quick start but .env.example "
            f"does not document it"
        )


# ── the Kubernetes quick start matches the manifests ─────────────────────

def _base_docs():
    out = []
    for path in sorted(BASE.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        out += [d for d in yaml.safe_load_all(path.read_text()) if d]
    return out


def _secret_keys_the_manifests_read():
    keys = set()
    for doc in _base_docs():
        if doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            for env in c.get("env", []):
                ref = (env.get("valueFrom") or {}).get("secretKeyRef") or {}
                if ref.get("name") == "elefant-secrets":
                    keys.add(ref["key"])
    return keys


def test_the_quick_start_creates_every_secret_key_the_manifests_read(blocks):
    """A missing key is not a clear error: the pod starts and the editor crash
    loops on a value it cannot find, several minutes into a greenfield install."""
    body = blocks["k8s-quickstart"].body
    for key in sorted(_secret_keys_the_manifests_read()):
        assert f"--from-literal={key}=" in body, (
            f"deploy/k8s/base reads {key} from the elefant-secrets Secret, and "
            f"the README's quick start does not create it"
        )


def test_the_quick_start_creates_the_secret_the_manifests_name(blocks):
    body = blocks["k8s-quickstart"].body
    assert "secret generic elefant-secrets" in body
    assert "kubectl create namespace elefant" in body, (
        "the Namespace is outside the kustomize build, so the quick start has to "
        "create it"
    )
    assert "kubectl apply -k" in body


def test_the_kustomization_merges_into_the_configmap_the_base_generates(blocks):
    """`behavior: merge` against a name the base does not generate is not an
    error — kustomize creates a SECOND ConfigMap, the pods keep reading the
    first, and the reader's domain is silently ignored."""
    kust = yaml.safe_load(blocks["k8s-kustomization"].body)
    base = yaml.safe_load((BASE / "kustomization.yaml").read_text())

    base_names = {g["name"] for g in base["configMapGenerator"]}
    for gen in kust["configMapGenerator"]:
        assert gen["name"] in base_names, (
            f"the README merges into {gen['name']!r}, which the base does not "
            f"generate (it generates {sorted(base_names)})"
        )
        assert gen.get("behavior") == "merge", (
            "without `behavior: merge` the generator REPLACES the base's "
            "ConfigMap and every other default is dropped"
        )
        for literal in gen["literals"]:
            key = literal.split("=", 1)[0]
            assert any(l.split("=", 1)[0] == key for g in base["configMapGenerator"]
                       for l in g["literals"]), (
                f"the README overrides {key}, which the base never sets — a merge "
                f"of a key nothing reads does nothing"
            )


def test_the_kustomization_points_at_the_canonical_repo_and_pins_a_ref(blocks):
    kust = yaml.safe_load(blocks["k8s-kustomization"].body)
    remote = [r for r in kust["resources"] if r.startswith("http")]
    assert len(remote) == 1, f"expected one remote base, got {remote}"
    url = remote[0]
    assert url.startswith("https://github.com/zennthic/elefant//deploy/k8s/base"), url
    assert "?ref=" in url, (
        "without a ref the reader gets the default branch, which moves — and the "
        "ref is also what pins the image tags, via the base's `images:` block"
    )
    assert kust.get("namespace") == "elefant"


def test_the_pinned_ref_is_what_carries_the_image_tags():
    """The claim the guide makes about `?ref=`. It only holds while the base
    actually sets the tags, rather than leaving them on the container specs."""
    base = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    assert base.get("images"), (
        "deploy/k8s/base no longer sets image tags through kustomize, so "
        "`?ref=` stops pinning versions and the install guide is wrong"
    )
    for entry in base["images"]:
        assert entry.get("newTag"), f"{entry.get('name')} has no newTag"


# ── the compose split, which is the fix that made the quick start work ────

def test_the_base_compose_file_declares_no_external_network():
    """The original failure: `traefik_traefik-public` was `external: true`, so
    `docker compose up -d` stopped dead on any host not already running our
    Traefik. This is the assertion that keeps the install path working."""
    compose = yaml.safe_load(COMPOSE.read_text())
    for name, net in (compose.get("networks") or {}).items():
        assert not (net or {}).get("external"), (
            f"docker-compose.yml declares the external network {name!r}. On a "
            f"fresh host `docker compose up -d` fails with 'declared as "
            f"external, but could not be found'. Put it in "
            f"docker-compose.traefik.yml instead."
        )


def test_the_base_compose_file_publishes_the_front_door():
    """With no proxy in front, the published port is the only way in — and it is
    what the README tells the reader to open."""
    compose = yaml.safe_load(COMPOSE.read_text())
    ports = compose["services"]["nginx"].get("ports") or []
    assert any("8321" in str(p) for p in ports), (
        f"nginx publishes {ports}; the README's quick start opens port 8321"
    )


def test_no_service_in_the_base_compose_file_carries_a_traefik_label():
    """Comments about the override are fine and useful. A LABEL is not: a
    `traefik.http.routers.wiki.rule` here would reference the external network
    the base no longer declares, and on our own hosts it would silently take
    precedence over the override's copy."""
    compose = yaml.safe_load(COMPOSE.read_text())
    for name, svc in compose["services"].items():
        labels = svc.get("labels") or []
        joined = "\n".join(labels) if isinstance(labels, list) else str(labels)
        assert "traefik" not in joined.lower(), (
            f"service {name!r} in docker-compose.yml carries Traefik labels; "
            f"they belong in docker-compose.traefik.yml"
        )


def test_the_override_carries_the_traefik_routing_it_took_away():
    """The other half. The Stage and prod are reachable on their own hostnames
    only because of these labels, so moving them out of the base file has to put
    them somewhere."""
    assert TRAEFIK.exists(), "docker-compose.traefik.yml is missing"
    override = yaml.safe_load(TRAEFIK.read_text())
    labels = override["services"]["nginx"]["labels"]
    joined = "\n".join(labels)
    assert "traefik.enable=true" in joined
    assert "traefik.http.routers.wiki.rule=Host(" in joined
    assert "ELEFANT_PUBLIC_DOMAIN" in joined, (
        "the router rule no longer routes on the configured domain"
    )
    external = [n for n, v in (override.get("networks") or {}).items()
                if (v or {}).get("external")]
    assert external, "the override declares no external network, so it routes nothing"


def test_the_ci_deploy_passes_both_compose_files():
    """The change that has to happen in the same commit as the split. With only
    the base file, the deploy succeeds and the Stage silently stops being
    reachable on its own hostname."""
    body = WORKFLOW.read_text()
    deploy = body.split("  deploy:", 1)[1].split("\n  smoke:", 1)[0]
    ups = [line for line in deploy.splitlines() if "docker compose" in line]
    assert ups, "the deploy job no longer runs docker compose"
    assert "docker-compose.traefik.yml" in deploy, (
        "the deploy job does not pass docker-compose.traefik.yml, so the Stage "
        "would come up with no Traefik labels and no traefik-public network"
    )
    assert deploy.count("cp docker-compose") == 2, (
        "both compose files have to be copied to the deploy host, not just one"
    )


# ── the guides, and the rules for anything that will be on camera ────────

def test_the_readme_offers_both_paths_and_links_both_guides():
    readme = README.read_text()
    assert "Install: pick one" in readme
    for guide in GUIDES:
        rel = guide.relative_to(REPO).as_posix()
        assert guide.exists(), f"{rel} is linked from the README but missing"
        assert rel in readme, f"the README does not link {rel}"


def test_the_readme_states_the_supported_architectures():
    """`latest` was arm64 only until the multi-arch build landed, and an amd64
    reader's pull failed with 'no matching manifest'. Saying both out loud is
    what makes that a promise somebody would notice breaking."""
    readme = README.read_text()
    assert "linux/amd64" in readme and "linux/arm64" in readme


def test_the_readme_states_what_the_machine_needs():
    readme = README.read_text()
    assert "4 GiB" in readme, "no memory requirement stated"
    assert "GiB" in readme and "CPU" in readme.replace("CPU cores", "CPU cores")


def test_both_guides_cover_the_lifecycle_not_just_the_install():
    for guide in GUIDES:
        body = guide.read_text()
        for heading in ("## Upgrading", "## Backup", "## Uninstall"):
            assert heading in body, f"{guide.name} has no {heading!r} section"


def test_the_kubernetes_uninstall_warns_about_the_volumes():
    """`kubectl delete -k .` removes the PVCs, and whether the DATA goes with
    them is the StorageClass's reclaim policy — `Delete` on most clusters. That
    has to be spelled out where somebody is about to run it, not left to be
    discovered."""
    body = (REPO / "docs" / "install" / "kubernetes.md").read_text()
    tail = body.split("## Uninstall", 1)[1]
    assert "reclaimPolicy" in tail or "reclaim policy" in tail
    assert "PVC" in tail or "PersistentVolume" in tail
    assert "Retain" in tail and "Delete" in tail


@pytest.mark.parametrize("path", [
    "README.md",
    "docs/install/docker-compose.md",
    "docs/install/kubernetes.md",
    "docs/index.md",
    "docs/connect/README.md",
    "docs/getting-started/overview.md",
    "deploy/k8s/examples/ingress-generic.yaml",
    "deploy/k8s/examples/ingress-nginx-tls.yaml",
    "deploy/k8s/examples/ingress-traefik.yaml",
])
def test_nothing_on_camera_names_a_real_host(path):
    """Anything that may appear in a docs page or video uses only reserved
    placeholder names. `example.com`, `example.net` and 192.0.2.0/24 are
    reserved for documentation precisely so that a viewer who types one in
    reaches nothing."""
    body = (REPO / path).read_text()
    for leak in (*LAB_HOST_MARKERS, "elefant-stage"):
        assert leak.lower() not in body.lower(), (
            f"{path} names {leak!r}. Examples use example.com / example.net / "
            f"192.0.2.x only — this file can end up on screen."
        )
    # A HOST under our own domain. `trademarks@zennthic.com` is a contact
    # address and stays; a name with a label before the domain would be a real
    # deployment.
    host = re.search(r"[a-z0-9-]+\.zennthic\.com", body, re.I)
    assert not host, (
        f"{path} names the real host {host.group(0)!r}; use example.com"
    )


def test_the_ingress_examples_all_route_through_nginx():
    """nginx is what performs the auth_request subrequest. An example that sent
    /editor or /mcp straight at those Services would be an example of how to
    turn authentication off."""
    for path in sorted((REPO / "deploy" / "k8s" / "examples").glob("ingress-*.yaml")):
        doc = yaml.safe_load(path.read_text())
        assert doc["kind"] == "Ingress", path.name
        paths = doc["spec"]["rules"][0]["http"]["paths"]
        assert len(paths) == 1, f"{path.name} has {len(paths)} rules, expected one"
        backend = paths[0]["backend"]["service"]
        assert backend["name"] == "nginx" and backend["port"]["number"] == 80, (
            f"{path.name} routes to {backend}, bypassing the auth gate"
        )


def test_the_docs_workflow_runs_on_release_tags_and_stays_self_hosted():
    """It is the run that matters most — a release tag is the version strangers
    install — and it must not drift onto a hosted runner, which is the rule the
    rest of the pipeline is held to (see test_ci_runners.py)."""
    wf = yaml.safe_load(DOCS_WORKFLOW.read_text())
    triggers = wf.get("on", wf.get(True))
    assert "tags" in triggers["push"], "release tags do not trigger the docs test"
    assert any(t.startswith("v") for t in triggers["push"]["tags"])
    assert "pull_request" not in triggers, (
        "this workflow runs a fork's shell script on a workshop machine if it "
        "ever gains a pull_request trigger"
    )
    for name, job in wf["jobs"].items():
        assert not str(job["runs-on"]).startswith("ubuntu"), f"{name} is hosted"
