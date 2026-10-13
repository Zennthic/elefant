# Installing Elefant on Kubernetes

A kustomize base you apply straight from this repository, plus one small file of
your own that carries your domain. No Helm chart, no operator, no cluster-wide
CRDs.

The quick start in the repository [`README.md`](../../README.md) gets you to a
login page on a greenfield cluster. This page is everything after that. For the
manifests themselves — what each Deployment assumes, why four probes are
`tcpSocket`, how the shared volumes constrain scheduling —
[`deploy/k8s/README.md`](../../deploy/k8s/README.md) is the reference and this
page does not repeat it.

If you do not already run a cluster, install with Docker Compose instead:
[`docker-compose.md`](docker-compose.md).

## What you need

| | |
|---|---|
| Cluster | Any conformant Kubernetes, 1.29 or newer. k3s, kubeadm, EKS, GKE and AKS all work; the base names nothing cluster-specific. |
| `kubectl` | 1.29+, with `git` on the same machine — kustomize shells out to it to fetch the remote base. |
| Nodes | `linux/amd64` or `linux/arm64`. Mixed clusters are fine: the images are manifest lists and the base carries no architecture selector. |
| Capacity | Requests total **730m CPU and 1.7 GiB memory**; limits total 8.2 CPU and 5.7 GiB. A single 4 GiB node fits it on requests and is tight at simultaneous peak. |
| Storage | A **default StorageClass** (`kubectl get storageclass` shows one marked `(default)`), `ReadWriteOnce`, **63 GiB** across seven claims. No RWX needed. |
| Ingress | Not required to start. Nothing in the base assumes a controller. |

Two things the base deliberately does **not** ship, and does not need:

- **A `storageClassName`.** Your cluster's default is used, whatever it is —
  k3s `local-path`, EBS, Longhorn.
- **An image pull secret.** The images are public. On a cluster running a secret
  replicator, adding your own copy is actively harmful: in pull mode the
  replicator replaces the good credential with your placeholder and every pull
  then fails as an "anonymous token" 401 that reads like an expired login.

## Install

### 1. The namespace

```bash
kubectl create namespace elefant
```

Separate from the kustomize build on purpose. A Namespace is cluster-scoped, so
a deploy credential that is admin *inside* one namespace cannot create it — and
that is the shape of credential a CI pipeline should be given.

### 2. The Secret

One Secret, five keys, created **once**:

```bash
kubectl -n elefant create secret generic elefant-secrets \
  --from-literal=ELEFANT_SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ELEFANT_INTERNAL_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=ELEFANT_S3_ACCESS_KEY="$(openssl rand -hex 12)" \
  --from-literal=ELEFANT_S3_SECRET_KEY="$(openssl rand -hex 24)"
```

| Key | What it is |
|---|---|
| `ELEFANT_SESSION_SECRET` | Signs sessions. Rotating it logs everybody out. |
| `ELEFANT_INTERNAL_SERVICE_TOKEN` | Authenticates the pods to each other. Without it the internal flows fail closed. |
| `POSTGRES_PASSWORD` | The bundled Postgres DBA credential. |
| `ELEFANT_S3_ACCESS_KEY` / `ELEFANT_S3_SECRET_KEY` | Object storage. With the bundled MinIO these are also its root credentials, which is why they are a Secret and not a ConfigMap. |

**It is outside kustomize on purpose.** `apply -k` would rewrite it on every
deploy, and `POSTGRES_PASSWORD` has to stay matched to the `postgres-data` volume
Postgres initialised with it. A fresh password against an existing volume locks
the app out of its own database, quietly. If you must rotate it, plan the data
side first.

### 3. Your kustomization

One file, in a directory of its own:

```yaml
# my-elefant/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: elefant

resources:
  - https://github.com/zennthic/elefant//deploy/k8s/base?ref=vX.Y.Z

configMapGenerator:
  - name: elefant-config
    behavior: merge
    literals:
      - ELEFANT_PUBLIC_DOMAIN=elefant.example.com
```

**Why this file has to exist.** A remote base on its own cannot take your
hostname, and `ELEFANT_PUBLIC_DOMAIN` is not cosmetic: it is the host an Ingress
serves and the host the editor and the MCP server hand out as their own external
URLs. The shipped value is `elefant.example.com`, which will not work for you.

**`behavior: merge` overrides only the keys you name**, so you get your domain
and keep every other default. `deploy/k8s/base/kustomization.yaml` lists them all
— the deployment mode, the embedding model and provider, the S3 endpoint and region, the MCP
rate limits — and each is overridden exactly the same way. Do not edit the base.
The [configuration reference](../configuration.md) says what each one does, and
lists the variables the base does not set.

**`?ref=vX.Y.Z` is the version pin, and it pins more than the YAML.** The base at
a release tag carries that release's `newTag` in its `images:` block, so the ref
fixes the manifests *and* the eight image tags together. Take the tag from the
[Releases](https://github.com/zennthic/elefant/releases) page. Omitting the ref
gives you the default branch, which moves.

**Need a storage class, a pull secret or node placement too?**
[`deploy/k8s/overlays/example/`](../../deploy/k8s/overlays/example/) is a
worked overlay that adds each of them, with the values to change marked.

### 4. Apply

```bash
kubectl apply -k my-elefant
kubectl -n elefant rollout status deploy/nginx --timeout=10m
kubectl -n elefant get pods
```

On a greenfield apply you may see nginx restart once or twice in the first
seconds, while its upstream Service names start resolving. It clears on its own.

The embedder is the slow one: the full image carries an ML runtime and downloads
a sentence-transformers model on first boot, with a `startupProbe` worth 300
seconds to cover it. Until it is Ready, semantic search returns nothing and the
rest of the stack works.

### 5. Reach it

With no ingress:

```bash
kubectl -n elefant port-forward svc/nginx 8321:80
# http://localhost:8321/setup
```

**A plain-http trial through `port-forward` works completely, login included.**
The session cookie is marked `Secure` only when something in front sends
`X-Forwarded-Proto: https`, and the editor derives its base URL and its OAuth
issuer from the address you actually reached it on — so over `port-forward` the
issuer is `http://localhost:8321` and OAuth discovery agrees with itself. That is
fine for a look around, and not fine on a network.

## Ingress and TLS

`deploy/k8s/examples/` holds three Ingresses to copy. Each is a plain manifest
rather than a kustomize component, because the host has to be edited either way
and indirection would not save you that.

| File | For |
|---|---|
| [`ingress-generic.yaml`](../../deploy/k8s/examples/ingress-generic.yaml) | Any controller. The minimum that works. |
| [`ingress-nginx-tls.yaml`](../../deploy/k8s/examples/ingress-nginx-tls.yaml) | ingress-nginx with cert-manager issuing the certificate. |
| [`ingress-traefik.yaml`](../../deploy/k8s/examples/ingress-traefik.yaml) | Traefik, both with cert-manager and with a wildcard Traefik already holds. |

Copy one next to your `kustomization.yaml`, set the host to the same value as
`ELEFANT_PUBLIC_DOMAIN`, and add it to `resources`:

```yaml
resources:
  - https://github.com/zennthic/elefant//deploy/k8s/base?ref=vX.Y.Z
  - ingress.yaml
```

Every path goes through the `nginx` Service on port 80, including `/editor`,
`/mcp` and the memory UI. Do not route around it: nginx is what performs the
`auth_request` subrequest, so a rule that reaches the editor directly skips
authentication.

Two things to get right whichever controller you run:

- **`spec.tls` only when your controller really issues a certificate for that
  host.** An unfulfilled `tls` block usually makes the controller serve its own
  self-signed default, which reads like a broken certificate rather than a
  missing one.
- **The MCP endpoint streams.** Raise the controller's proxy read timeout (300s
  is comfortable) or long tool calls are cut off with nothing the client can
  report.

## Upgrading

Change the ref, and the image tags come with it:

```bash
sed -i 's|?ref=v.*|?ref=vX.Y.Z|' my-elefant/kustomization.yaml
kubectl apply -k my-elefant
kubectl -n elefant rollout status deploy/editor --timeout=10m
```

No `restart nginx` step here, unlike the Compose install: a ClusterIP is stable
for the life of the Service, so the addresses stay correct when the pods behind
them are replaced.

Two properties of this base shape what an upgrade looks like:

- **Every Deployment that mounts a claim uses `strategy: Recreate`**, so it goes
  down before the new pod comes up. Expect a short gap, not a rolling upgrade.
  `RollingUpdate` on a `ReadWriteOnce` volume deadlocks — the new pod waits for a
  volume the old pod holds while the old pod waits for the new one to go Ready.
- **Database migrations run at editor startup** and are forward-only. Take a
  snapshot first if you might want to go back.
- **Every pod runs as non-root.** The Python services run as UID/GID `10001`
  and the two nginx images as `101`, set by each Deployment's
  `securityContext`. The editor and the embedder each have a `volume-perms`
  init container. It gives claims created by older (root) images to
  `10001:10001` and then exits. It is the only container that runs as root,
  and it keeps only `CHOWN`, `DAC_READ_SEARCH` and `FOWNER`. If your cluster
  enforces the `restricted` Pod Security Standard on the namespace, that init
  container is refused. Run the chown once by hand, then remove it with a
  patch in your overlay.

## Backup

Use the application's **org snapshot**, not a volume snapshot: it is consistent
across Postgres, Qdrant and object storage, which three separate volume copies
of a running stack are not. Take one from **Admin → Organisation → Snapshot** or
over the API; see
[`security/data-management.md`](../../security/data-management.md) for what it
contains and how to restore it.

Know where each store lives. The editor's own database (accounts, orgs, tokens,
settings) is **SQLite at `/data/config.db`** on the `editor-data` PVC, next to
`/data/crypto.key`; the base does not set `POSTGRES_EDITOR_DSN`, so it does not
use Postgres. The `postgres-data` volume holds memory-api's data: events, the
hot tier, revisions, the board and the git index. Wiki pages are in MinIO.

Volume-level backup (Velero, your CSI driver's snapshots) is worth having as a
disaster copy. Quiesce Postgres first, or accept that you are copying a database
mid-write.

## Uninstall

```bash
kubectl delete -k my-elefant
```

**Read this before you run it.** What happens to your data depends on one thing
the base does not control:

- The **PVCs are part of the kustomize build**, so `delete -k` deletes the seven
  claims along with the Deployments.
- Whether the *data* goes with them is decided by the StorageClass's
  `reclaimPolicy`. `Delete` — the default on most classes, including k3s
  `local-path` and most cloud CSI drivers — destroys the underlying volume with
  the claim, immediately and with no prompt. `Retain` leaves the PersistentVolume
  behind, holding your data, for you to reclaim or delete by hand.

Check before you delete:

```bash
kubectl get storageclass          # the RECLAIMPOLICY column
kubectl -n elefant get pvc
```

To take the workloads down and keep the data whatever your class does, delete the
Deployments and leave the claims:

```bash
kubectl -n elefant delete deploy --all
```

The Secret and the Namespace are outside the build, so neither is removed by
`delete -k`. Finish up with:

```bash
kubectl delete namespace elefant   # ALSO deletes the PVCs and the Secret in it
```

## When it does not come up

```bash
kubectl -n elefant get pods
kubectl -n elefant describe pod <name>      # events are at the bottom
kubectl -n elefant logs deploy/editor
```

| Symptom | What it usually is |
|---|---|
| Every pod `Pending`, PVCs `Pending` | No default StorageClass. `kubectl get storageclass`, and name one in your kustomization with a patch if the cluster has one but no default. |
| The embedder `Pending` with an unsatisfiable-affinity event | It carries a *required* affinity onto the editor's node, because they share two `ReadWriteOnce` claims. That node has no room. Give it headroom or lower the embedder's requests — do **not** split them onto two nodes, or the pod starts and then hangs in `ContainerCreating` with a `FailedAttachVolume`, which reads like a storage fault. |
| `ImagePullBackOff` | Check the tag your ref resolved to: `kubectl -n elefant get deploy editor -o jsonpath='{..image}'`. If the cluster runs a secret replicator, check that nothing has shipped an `imagePullSecret` into the namespace. |
| `502` from the Ingress | nginx is up before its upstreams resolve, or the Ingress routes past nginx to a service directly. Every path goes to `nginx:80`. |
| The editor `CrashLoopBackOff` at startup | Almost always the Secret: a missing key, or a `POSTGRES_PASSWORD` that no longer matches the existing `postgres-data` volume. `kubectl -n elefant logs deploy/editor` names it. |
