"""The Kubernetes manifests, checked against the rules that are easy to break.

`deploy/k8s/base/` is the thing the README tells self-hosters to
`kubectl apply`. So it has to run on any cluster: no named storage class, no
Traefik, no pull secret, no node names, and on amd64 as readily as arm64.
Every assertion about the base below is really that one requirement, asked a
different way.

The example overlay is checked for the opposite: that it actually applies the
four cluster-specific things it exists to show, since a patch that silently
matches nothing renders perfectly and deploys wrong.

These parse the YAML rather than shelling out to `kubectl kustomize`, because
the test job runs in `python:3.11-slim` with no kubectl in it. The rendered
output is checked in CI by whoever runs the deploy; what is checked HERE is
every property that can be read off the sources.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
K8S = REPO / "deploy" / "k8s"
BASE = K8S / "base"
OVERLAY = K8S / "overlays" / "example"

# The five nginx.conf upstreams, as `service: port`. nginx.conf is SHARED with
# the Compose stack and is not templated, so a Service that renamed itself or
# moved port would 502 the front door with nothing else looking wrong.
NGINX_UPSTREAMS = {
    "editor": 8080,
    "embedder": 8001,
    "memory-api": 8002,
    "mcp": 8003,
    "memory-ui": 8080,
}

BUILT_SERVICES = ("editor", "embedder", "mcp", "memory-api", "memory-ui", "nginx",
                  "diagram-renderer", "diagram-validator")

pytestmark = pytest.mark.skipif(
    not BASE.exists(), reason="deploy/k8s is not checked out beside the editor"
)


def _load(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _ops(path):
    """A JSON6902 patch file is a LIST of operations, so it loads as a single
    document that happens to be a list — not as one document per operation."""
    doc = _load(path)
    assert len(doc) == 1 and isinstance(doc[0], list), f"{path.name} is not a patch list"
    return doc[0]


def _all_docs(directory):
    out = []
    for p in sorted(directory.glob("*.yaml")):
        if p.name == "kustomization.yaml":
            continue
        out.extend(_load(p))
    return out


@pytest.fixture(scope="module")
def base_docs():
    return _all_docs(BASE)


@pytest.fixture(scope="module")
def base_kustomization():
    return yaml.safe_load((BASE / "kustomization.yaml").read_text())


@pytest.fixture(scope="module")
def overlay_kustomization():
    return yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())


def _kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def _containers(deployment):
    return deployment["spec"]["template"]["spec"]["containers"]


# ── the base must run on somebody else's cluster ───────────────────────────

def test_no_claim_names_a_storage_class(base_docs):
    """Naming one pins the base to a cluster that has it. Unset means the
    cluster's default, which every cluster has and an overlay can override."""
    for pvc in _kind(base_docs, "PersistentVolumeClaim"):
        assert "storageClassName" not in pvc["spec"], (
            f"{pvc['metadata']['name']} pins a storage class; put that in an overlay"
        )


def test_no_claim_asks_for_rwx(base_docs):
    """RWX is a Longhorn share-manager or an NFS provisioner. Most clusters
    have neither, and the shared volumes are handled with pod affinity instead."""
    for pvc in _kind(base_docs, "PersistentVolumeClaim"):
        modes = pvc["spec"]["accessModes"]
        assert modes == ["ReadWriteOnce"], f"{pvc['metadata']['name']}: {modes}"


def test_the_base_ships_no_pull_secret(base_docs, base_kustomization):
    """Two failures in one. The images are public, so it is unnecessary — and
    on a cluster running a secret replicator, a shipped copy REPLACES the
    working one and every pull then fails as an 'anonymous token' 401."""
    assert not _kind(base_docs, "Secret"), "the base ships a Secret"
    for dep in _kind(base_docs, "Deployment"):
        pod = dep["spec"]["template"]["spec"]
        assert "imagePullSecrets" not in pod, (
            f"{dep['metadata']['name']} names a pull secret in the base"
        )
    assert "secretGenerator" not in base_kustomization


def test_the_base_assumes_no_ingress_controller(base_docs):
    """An Ingress in the base is a bet on a controller and an annotation
    dialect. It goes in an overlay; a generic one is an example."""
    assert not _kind(base_docs, "Ingress")
    assert (K8S / "examples" / "ingress-generic.yaml").exists(), (
        "a consumer with an ingress controller needs something to copy"
    )


def test_the_base_pins_no_architecture(base_docs):
    """Clusters run arm64 and amd64 alike. A nodeSelector on
    kubernetes.io/arch would leave pods Pending with no obvious cause."""
    for dep in _kind(base_docs, "Deployment"):
        pod = dep["spec"]["template"]["spec"]
        assert "kubernetes.io/arch" not in str(pod.get("nodeSelector", {}))
        assert "kubernetes.io/arch" not in str(pod.get("affinity", {}).get("nodeAffinity", {}))


def test_the_base_names_no_cluster_specific_value(base_docs, base_kustomization):
    """Any of these in the base ties it to one cluster rather than any."""
    leaks = ("storageClassName", "imagePullSecrets", "nodeAffinity", "longhorn",
             "slow-node-")
    # A comment in the base may well MENTION what an overlay does with these.
    # What must not appear is a value, so this reads the parsed documents
    # rather than the file text.
    rendered = str(base_docs)
    found = [leak for leak in leaks if leak in rendered]
    assert not found, f"the base carries {found} in a manifest, not just a comment"


def test_images_are_set_through_the_kustomize_images_block(base_kustomization):
    """`newTag` is the field a release bumps and the field the version check
    reads. Hardcoding a tag in each Deployment would make a pin a six-file edit."""
    images = {i["name"]: i for i in base_kustomization["images"]}
    for svc in BUILT_SERVICES:
        name = f"ghcr.io/zennthic/elefant/{svc}"
        assert name in images, f"{svc} is not in the images block"
        assert images[name].get("newTag"), f"{svc} has no newTag"


def test_deployments_carry_no_inline_tag_for_our_own_images(base_docs):
    """A tag written into the Deployment WINS over the images: block silently."""
    for dep in _kind(base_docs, "Deployment"):
        for c in _containers(dep):
            if c["image"].startswith("ghcr.io/zennthic/elefant/"):
                assert ":" not in c["image"].rsplit("/", 1)[-1], (
                    f"{dep['metadata']['name']} hardcodes a tag: {c['image']}"
                )


def test_third_party_images_are_pinned(base_docs):
    """Ours are pinned by the images block. Everybody else's must be pinned in
    place, or an upstream release moves the data store under a deploy."""
    for dep in _kind(base_docs, "Deployment"):
        for c in _containers(dep):
            if c["image"].startswith("ghcr.io/zennthic/elefant/"):
                continue
            tag = c["image"].rsplit(":", 1)[-1] if ":" in c["image"].rsplit("/", 1)[-1] else ""
            assert tag and tag != "latest", f"{c['image']} is unpinned"


def test_minio_is_the_same_image_as_compose(base_docs):
    """MinIO has had to move registry twice when upstream withdrew its public
    images. Each move edits two files, and the one left behind keeps pointing
    at a registry that answers `unauthorized`."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    images = [c["image"] for dep in _kind(base_docs, "Deployment")
              for c in _containers(dep) if c["name"] == "minio"]
    assert images == [compose["services"]["minio"]["image"]]


# ── the rules that make it actually run ────────────────────────────────────

def test_every_rwo_mounting_deployment_uses_recreate(base_docs):
    """RollingUpdate deadlocks on an RWO volume: the new pod waits for a volume
    the old pod holds, the old pod waits for the new one to go Ready, and the
    rollout sits there until it times out."""
    claims = {p["metadata"]["name"] for p in _kind(base_docs, "PersistentVolumeClaim")}
    for dep in _kind(base_docs, "Deployment"):
        vols = dep["spec"]["template"]["spec"].get("volumes", [])
        mounts_pvc = any(
            v.get("persistentVolumeClaim", {}).get("claimName") in claims for v in vols
        )
        if not mounts_pvc:
            continue
        strategy = dep["spec"].get("strategy", {}).get("type")
        assert strategy == "Recreate", (
            f"{dep['metadata']['name']} mounts an RWO claim but uses {strategy}"
        )


def test_every_container_has_requests_and_limits(base_docs):
    """Small nodes are common (4 GiB boards). One container without a limit
    can take a node down, and one without a request makes the scheduler's arithmetic a
    guess — which is what decides whether the editor and the embedder fit
    together."""
    for dep in _kind(base_docs, "Deployment"):
        for c in _containers(dep):
            res = c.get("resources", {})
            where = f"{dep['metadata']['name']}/{c['name']}"
            assert res.get("limits", {}).get("memory"), f"{where}: no memory limit"
            assert res.get("limits", {}).get("cpu"), f"{where}: no cpu limit"
            assert res.get("requests", {}).get("memory"), f"{where}: no memory request"
            assert res.get("requests", {}).get("cpu"), f"{where}: no cpu request"


def test_the_embedder_limit_matches_the_compose_mem_limit(base_docs):
    """`mem_limit: 1500m` in docker-compose.yml is a tuned number, not a guess.
    The two stacks should not quietly disagree about how much the model needs."""
    dep = next(d for d in _kind(base_docs, "Deployment") if d["metadata"]["name"] == "embedder")
    limit = _containers(dep)[0]["resources"]["limits"]["memory"]
    assert limit == "1500Mi", f"embedder memory limit is {limit}, compose says 1500m"


def test_the_shared_volumes_force_co_location(base_docs):
    """`docs` and `backend-sync` are RWO and mounted by BOTH the editor and the
    embedder. RWO attaches to one node, so without a REQUIRED affinity the
    embedder can be scheduled elsewhere and then hangs in ContainerCreating
    with a FailedAttachVolume — which reads like a storage fault."""
    deps = {d["metadata"]["name"]: d for d in _kind(base_docs, "Deployment")}

    def claims_of(name):
        return {
            v["persistentVolumeClaim"]["claimName"]
            for v in deps[name]["spec"]["template"]["spec"].get("volumes", [])
            if "persistentVolumeClaim" in v
        }

    shared = claims_of("editor") & claims_of("embedder")
    assert shared == {"docs", "backend-sync"}, f"shared claims changed: {shared}"

    affinity = deps["embedder"]["spec"]["template"]["spec"]["affinity"]["podAffinity"]
    required = affinity["requiredDuringSchedulingIgnoredDuringExecution"]
    assert required, "the embedder's affinity onto the editor is not required"
    assert required[0]["labelSelector"]["matchLabels"] == {"app": "editor"}
    assert required[0]["topologyKey"] == "kubernetes.io/hostname"


def test_postgres_keeps_pgdata_in_a_subdirectory(base_docs):
    """A fresh volume mounted at the data directory arrives with a lost+found
    in it, and initdb refuses to populate a non-empty directory."""
    dep = next(d for d in _kind(base_docs, "Deployment") if d["metadata"]["name"] == "postgres")
    c = _containers(dep)[0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    mount = next(m["mountPath"] for m in c["volumeMounts"])
    assert env["PGDATA"] == "/var/lib/postgresql/data/pgdata"
    assert env["PGDATA"].startswith(mount + "/"), "PGDATA is not under the mount"
    assert c["image"] == "postgres:16-alpine"


def test_the_model_cache_is_not_a_claim(base_docs):
    """It is a re-downloadable cache. Giving it a volume spends storage and a
    node attachment on something losing costs a few minutes."""
    dep = next(d for d in _kind(base_docs, "Deployment") if d["metadata"]["name"] == "embedder")
    vol = next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "hf-cache")
    assert "emptyDir" in vol


def test_every_container_has_both_probes(base_docs):
    """A pod with no readiness probe joins its Service the moment it starts,
    so a rollout sends traffic to a process that has not finished booting."""
    for dep in _kind(base_docs, "Deployment"):
        for c in _containers(dep):
            where = f"{dep['metadata']['name']}/{c['name']}"
            assert "livenessProbe" in c, f"{where}: no liveness probe"
            assert "readinessProbe" in c, f"{where}: no readiness probe"


def test_no_liveness_probe_depends_on_another_service(base_docs):
    """The rule the standard states: a slow dependency must not restart a
    healthy pod. memory-api's /health rolls up Postgres, Qdrant and the
    embedder, so it is specifically the path that must not appear here."""
    for dep in _kind(base_docs, "Deployment"):
        name = dep["metadata"]["name"]
        for c in _containers(dep):
            probe = c["livenessProbe"]
            if name in ("memory-api", "mcp", "nginx", "qdrant"):
                assert "tcpSocket" in probe, (
                    f"{name} has no dependency-free HTTP endpoint to probe; "
                    "see deploy/k8s/README.md before changing this"
                )


def test_the_embedder_gets_a_startup_budget(base_docs):
    """The full image downloads a sentence-transformers model on first boot.
    Compose allows 180s. Without a startupProbe that budget has to be bolted
    onto liveness, which then stays slack for the life of the pod."""
    dep = next(d for d in _kind(base_docs, "Deployment") if d["metadata"]["name"] == "embedder")
    probe = _containers(dep)[0]["startupProbe"]
    budget = probe["periodSeconds"] * probe["failureThreshold"]
    assert budget >= 180, f"only {budget}s to load the model; compose allows 180"


def test_service_links_are_off(base_docs):
    """Kubernetes otherwise injects an env var per Service into every pod.
    With nine Services those collide with the app's own names (`EDITOR_PORT`
    and friends) in ways that are very hard to trace."""
    for dep in _kind(base_docs, "Deployment"):
        pod = dep["spec"]["template"]["spec"]
        assert pod.get("enableServiceLinks") is False, dep["metadata"]["name"]


# ── nginx.conf is shared with the Compose stack ────────────────────────────

def test_service_names_and_ports_match_the_nginx_upstreams(base_docs):
    """nginx.conf is not templated and is the same file in both stacks. A
    renamed Service or a moved port 502s the front door."""
    services = {s["metadata"]["name"]: s for s in _kind(base_docs, "Service")}
    for name, port in NGINX_UPSTREAMS.items():
        assert name in services, f"nginx proxies to {name}, which has no Service"
        ports = [p["port"] for p in services[name]["spec"]["ports"]]
        assert port in ports, f"nginx wants {name}:{port}, the Service offers {ports}"


def test_the_upstreams_are_read_from_nginx_conf_not_just_remembered():
    """Guards the table above: if somebody edits nginx.conf, this fails rather
    than the manifests silently drifting from it."""
    conf = (REPO / "nginx" / "nginx.conf").read_text()
    for name, port in NGINX_UPSTREAMS.items():
        assert f"server {name}:{port};" in conf, (
            f"nginx.conf no longer has `server {name}:{port}` — update "
            "NGINX_UPSTREAMS here and the Services to match"
        )


def test_every_service_is_cluster_ip(base_docs):
    """A LoadBalancer or NodePort here would expose an internal service
    directly, bypassing the front door and its auth."""
    for svc in _kind(base_docs, "Service"):
        assert svc["spec"]["type"] == "ClusterIP", svc["metadata"]["name"]


# ── the overlay must actually apply what it exists to apply ────────────────

def test_the_overlay_builds_on_the_base(overlay_kustomization):
    assert "../../base" in overlay_kustomization["resources"]


def test_the_overlay_has_no_tls_block_on_its_ingress():
    """The example assumes Traefik already holds a wildcard certificate. A spec.tls block makes it
    look for a per-host Secret that does not exist, and serve its own
    self-signed default — which looks like a broken certificate, not a missing
    one."""
    ing = next(d for d in _load(OVERLAY / "ingress.yaml") if d["kind"] == "Ingress")
    assert "tls" not in ing["spec"]
    ann = ing["metadata"]["annotations"]
    assert ann["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
    assert ann["traefik.ingress.kubernetes.io/router.tls"] == "true"


def test_the_overlay_routes_to_the_front_door_not_a_backend():
    """Every path goes through nginx, which is what does the auth subrequest."""
    ing = next(d for d in _load(OVERLAY / "ingress.yaml") if d["kind"] == "Ingress")
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "nginx" and backend["port"]["number"] == 80


def _overlay_build_files(kustomization):
    """The files the overlay's kustomize build actually reads. A file merely
    SITTING in the directory (deployer-rbac.yaml) is not applied by the build,
    and the difference matters: that one holds a Secret on purpose."""
    named = set(kustomization.get("resources", []))
    named |= {p["path"] for p in kustomization.get("patches", []) if "path" in p}
    return [OVERLAY / n for n in sorted(named) if (OVERLAY / n).is_file()]


def test_the_overlay_references_the_pull_secret_and_never_ships_one(overlay_kustomization):
    """Reference is right; shipping is the trap. In pull mode the replicator
    replaces its own good copy with the placeholder, and every image pull then
    fails as an 'anonymous token' 401 that reads like an expired credential."""
    patch = _ops(OVERLAY / "pull-secret-patch.yaml")
    assert patch[0]["op"] == "add"
    assert [s["name"] for s in patch[0]["value"]], "the patch names no Secret"
    for path in _overlay_build_files(overlay_kustomization):
        shipped = [d for d in _load(path)
                   if isinstance(d, dict) and d.get("kind") == "Secret"]
        assert not shipped, f"{path.name} is in the build and ships a Secret"


# ── the deploy credential ──────────────────────────────────────────────────

def test_the_deployer_rbac_is_not_in_the_kustomize_build(overlay_kustomization):
    """`apply -k` runs AS this ServiceAccount, and a credential cannot be the
    thing that grants itself its own permissions. Its ClusterRole and
    ClusterRoleBinding are cluster-scoped besides, which is exactly what this
    account may not touch."""
    rbac = OVERLAY / "deployer-rbac.yaml"
    assert rbac.exists(), "the deploy credential has no manifest"
    assert rbac not in _overlay_build_files(overlay_kustomization)


def test_admin_is_bound_by_a_rolebinding_not_a_clusterrolebinding():
    """This is the point of the file. A ClusterRoleBinding to the
    built-in `admin` role is cluster-admin in all but name, and it would look
    like a one-word difference in review."""
    docs = _load(OVERLAY / "deployer-rbac.yaml")
    admin_bindings = [d for d in docs
                      if d["kind"].endswith("Binding")
                      and d["roleRef"]["name"] == "admin"]
    assert admin_bindings, "nothing binds the admin role"
    for b in admin_bindings:
        assert b["kind"] == "RoleBinding", (
            "admin is bound cluster-wide; that is cluster-admin with extra steps"
        )
        assert b["metadata"]["namespace"] == "elefant"


def test_the_only_cluster_scoped_grant_is_one_namespace():
    """A namespace-scoped account cannot relabel its own Namespace, which a
    pull-secret resync needs. This is the exception that buys that back, and it
    must stay the size of a keyhole."""
    docs = _load(OVERLAY / "deployer-rbac.yaml")
    cluster_roles = [d for d in docs if d["kind"] == "ClusterRole"]
    assert len(cluster_roles) == 1, f"{len(cluster_roles)} ClusterRoles defined"
    rules = cluster_roles[0]["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["resources"] == ["namespaces"]
    assert rule["resourceNames"] == ["elefant"], "the grant is not pinned to one namespace"
    # `list` and `watch` CANNOT be restricted by resourceNames — granting
    # either would let this account enumerate every namespace on the cluster.
    assert set(rule["verbs"]) <= {"get", "patch"}, f"verbs widen the grant: {rule['verbs']}"


def test_the_deployer_cannot_create_its_own_namespace():
    """Left to a cluster admin on purpose. If `create` appeared here the
    account could make namespaces anywhere, which resourceNames does not
    restrain for a create."""
    docs = _load(OVERLAY / "deployer-rbac.yaml")
    for d in docs:
        if d["kind"] != "ClusterRole":
            continue
        for rule in d["rules"]:
            assert "create" not in rule["verbs"]
            assert "delete" not in rule["verbs"]


def test_the_service_account_token_is_long_lived():
    """`kubectl create token` issues one that expires. This credential lives in
    a GitHub secret and is not rotated on a schedule, so an expiring token
    would break a deploy weeks later with nothing having changed."""
    docs = _load(OVERLAY / "deployer-rbac.yaml")
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert secret["type"] == "kubernetes.io/service-account-token"
    ann = secret["metadata"]["annotations"]["kubernetes.io/service-account.name"]
    sa = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert ann == sa["metadata"]["name"]


def test_the_deployer_secret_holds_no_literal_credential():
    """It is a request for the controller to mint a token, not a token. A
    `data:` or `stringData:` block here would be a credential in a public repo."""
    docs = _load(OVERLAY / "deployer-rbac.yaml")
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert "data" not in secret and "stringData" not in secret


def test_the_overlay_runbook_states_the_scope_checks():
    """The credential is only trustworthy if its scope is proved, and
    `auth can-i` is how the scope is proved rather than assumed."""
    readme = (OVERLAY / "README.md").read_text()
    assert "auth can-i" in readme
    assert "KUBECONFIG_B64" in readme


def test_the_overlay_pins_a_class_and_keeps_stores_off_named_nodes(overlay_kustomization):
    """A patch that matches nothing renders perfectly and deploys wrong, so
    check the targets, not only that the files exist."""
    targets = [(p.get("path"), p.get("target", {})) for p in overlay_kustomization["patches"]]
    by_path = {}
    for path, target in targets:
        by_path.setdefault(path, []).append(target)

    assert by_path["storageclass-patch.yaml"][0]["kind"] == "PersistentVolumeClaim"
    assert _ops(OVERLAY / "storageclass-patch.yaml")[0]["value"]

    affinity_targets = {t.get("name") for t in by_path["storage-node-affinity-patch.yaml"]}
    assert affinity_targets == {"postgres", "qdrant"}, (
        f"the node guard is applied to {affinity_targets}"
    )
    values = _ops(OVERLAY / "storage-node-affinity-patch.yaml")[0]["value"]
    term = values["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    expr = term["nodeSelectorTerms"][0]["matchExpressions"][0]
    assert expr["operator"] == "NotIn"
    assert expr["values"], "the node guard names no nodes"


def test_the_overlay_host_matches_its_public_domain(overlay_kustomization):
    """The Ingress host and ELEFANT_PUBLIC_DOMAIN are one value said twice. If
    they drift, the editor hands out URLs the Ingress does not serve."""
    ing = next(d for d in _load(OVERLAY / "ingress.yaml") if d["kind"] == "Ingress")
    literals = overlay_kustomization["configMapGenerator"][0]["literals"]
    assert f"ELEFANT_PUBLIC_DOMAIN={ing['spec']['rules'][0]['host']}" in literals


def test_the_docs_point_at_the_example_overlay():
    guide = (REPO / "docs" / "install" / "kubernetes.md").read_text()
    assert "deploy/k8s/overlays/example" in guide


def test_kustomize_is_kept_out_of_every_selector(base_kustomization, overlay_kustomization):
    """includeSelectors: true would put the label into spec.selector, and a
    Deployment's selector is immutable — so it applies once and then every
    later apply fails on a field conflict."""
    for k in (base_kustomization, overlay_kustomization):
        for entry in k["labels"]:
            assert entry["includeSelectors"] is False


def test_the_namespace_is_not_in_the_kustomize_build(base_kustomization):
    """A Namespace is cluster-scoped, so a credential that is admin inside one
    namespace cannot create it — and that is the credential CI deploys with."""
    assert "namespace.yaml" not in base_kustomization["resources"]
    assert (BASE / "namespace.yaml").exists(), "it still has to exist to be applied"


def test_there_is_a_readme_for_self_hosters():
    readme = (K8S / "README.md").read_text()
    assert "kubectl apply -k" in readme
    assert "elefant-secrets" in readme
