# Running Elefant on Kubernetes

**Installing for the first time?** Start at
[`docs/install/kubernetes.md`](../../docs/install/kubernetes.md) — it is the
step-by-step guide, and it covers TLS, upgrades, backup and uninstall. This page
is the reference for the manifests themselves: what they assume, and why.

Two directories, and the split between them is the whole idea:

| Path | What it is |
|---|---|
| `base/` | Generic. No hostname, no storage class, no pull secret, no node names. This is what you apply, and what you build your own overlay on top of. |
| `overlays/example/` | A worked example of an overlay: ingress, storage class, pull secret and node affinity. Copy it and change the marked values. |
| `examples/` | Optional pieces the base leaves out: three Ingresses to copy (generic, ingress-nginx with cert-manager, Traefik). |

The base runs on amd64 and arm64, uses whatever storage class your cluster
defaults to, and assumes no ingress controller. If it needs something your
cluster does not have, that is a bug in the base, not in your cluster.

## Quick start

You need a cluster, `kubectl`, and a default StorageClass
(`kubectl get storageclass` should show one marked `(default)`).

```bash
# 1. The namespace. Separate from the kustomize build on purpose: a Namespace is
#    cluster-scoped, so a deploy credential scoped to one namespace cannot
#    create it.
kubectl apply -f deploy/k8s/base/namespace.yaml

# 2. The secrets. Created by hand, ONCE — see "Secrets" below before you rerun this.
kubectl -n elefant create secret generic elefant-secrets \
  --from-literal=ELEFANT_SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ELEFANT_INTERNAL_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)" \
  --from-literal=ELEFANT_S3_ACCESS_KEY="$(openssl rand -hex 16)" \
  --from-literal=ELEFANT_S3_SECRET_KEY="$(openssl rand -hex 32)"

# 3. The stack.
kubectl apply -k deploy/k8s/base

# 4. Watch it come up. The embedder is the slow one: the full image downloads
#    a sentence-transformers model on first boot, which can take a few minutes.
kubectl -n elefant get pods -w
```

Then reach it. With no ingress:

```bash
kubectl -n elefant port-forward svc/nginx 8080:80
# http://localhost:8080
```

With an ingress, copy the closest of the three in `examples/`, set the host to
the same value as `ELEFANT_PUBLIC_DOMAIN` (below), and add the file to your own
`resources`:

| File | For |
|---|---|
| `examples/ingress-generic.yaml` | Any controller. The minimum that works. |
| `examples/ingress-nginx-tls.yaml` | ingress-nginx with cert-manager issuing the certificate. |
| `examples/ingress-traefik.yaml` | Traefik, including what k3s ships. |

Every path goes through the `nginx` Service on port 80. Do not route `/editor`
or `/mcp` at those Services directly: nginx is what performs the `auth_request`
subrequest, so a rule that bypasses it bypasses authentication.

## Setting your domain

`ELEFANT_PUBLIC_DOMAIN` is both the host the Ingress serves and the host the
editor and the MCP server hand out as their own external URLs. It ships as
`elefant.example.com`, which will not work for you.

Do not edit `base/kustomization.yaml`. Write your own kustomization beside it
and merge over the value:

```yaml
# my-elefant/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: elefant

resources:
  - ../deploy/k8s/base
  - ingress.yaml          # your copy of examples/ingress-generic.yaml

configMapGenerator:
  - name: elefant-config
    behavior: merge       # overrides only the keys you name
    literals:
      - ELEFANT_PUBLIC_DOMAIN=elefant.yourdomain.com
```

`kubectl apply -k my-elefant`. Everything else in `elefant-config` — the deployment
mode, the embedding model, the S3 endpoint, the MCP rate limits — is overridden the
same way. `base/kustomization.yaml` lists them all with their defaults.

## Pinning a version

`base/kustomization.yaml` carries an `images:` block whose `newTag` is the
release the base belongs to: at the tag `vX.Y.Z`, every image says `vX.Y.Z`.
Fetch the base with `?ref=vX.Y.Z`, as the install guide does, and that is the
version you run. To pin it in your own kustomization instead:

```yaml
images:
  - name: ghcr.io/zennthic/elefant/editor
    newTag: v1.2.3
  # ... and the other five
```

The equivalent for the Docker Compose install is `ELEFANT_TAG=v1.2.3`.

## Secrets

One Secret, `elefant-secrets`, with five keys:

| Key | What it is |
|---|---|
| `ELEFANT_SESSION_SECRET` | Signs sessions. Rotating it logs everybody out. |
| `ELEFANT_INTERNAL_SERVICE_TOKEN` | Authenticates the containers to each other. Without it the internal flows fail closed. |
| `POSTGRES_PASSWORD` | The bundled Postgres DBA credential. |
| `ELEFANT_S3_ACCESS_KEY` | S3 credentials. With the bundled MinIO these are also its root credentials, which is why they are here and not in the ConfigMap. |
| `ELEFANT_S3_SECRET_KEY` | |

**It is deliberately not in kustomize.** `kubectl apply -k` would rewrite it on
every deploy, and two of these must not move once the stack has data:
`POSTGRES_PASSWORD` has to stay matched to the `postgres-data` volume, and a
fresh one against an existing volume locks the app out of its own database,
quietly. Create it once. If you must rotate, plan the data side first.

## Storage

Seven ReadWriteOnce claims, no storage class named — your cluster's default is
used.

| Claim | Size | Used by |
|---|---|---|
| `docs` | 10Gi | editor **and** embedder |
| `backend-sync` | 1Gi | editor **and** embedder |
| `editor-data` | 2Gi | editor |
| `git-sources` | 10Gi | embedder |
| `qdrant-data` | 10Gi | qdrant |
| `postgres-data` | 10Gi | postgres |
| `minio-data` | 20Gi | minio |

**Two claims are shared, and that shapes the scheduling.** RWO attaches a volume
to one node, though any number of pods on that node may mount it. So the
embedder carries a *required* pod affinity onto the editor's node. Both
Deployments use `strategy: Recreate`, because with `RollingUpdate` the new pod
waits for a volume the old pod still holds while the old pod waits for the new
one to go Ready, and the rollout deadlocks until it times out.

If the embedder sits in `Pending` with an unsatisfiable-affinity event, the
editor's node has no room for it. Give that node more headroom, or lower the
embedder's requests. Do not split them onto two nodes: the pod will start and
then hang in `ContainerCreating` with a `FailedAttachVolume`, which reads like
a storage fault rather than a scheduling one.

Using an RWX class instead would remove the constraint. It is not the default
because most clusters do not offer one.

The embedder's model cache is an `emptyDir`, not a claim. Losing it on a restart
costs a refetch and nothing else.

## Health probes, and what they do not yet check

Probes follow the Compose healthchecks, which means they are honest about what
each service actually exposes:

| Service | Probe | Why |
|---|---|---|
| editor, embedder | `httpGet /health` | These handlers touch no dependency, so the same path is safe for liveness and readiness. The embedder also gets a `startupProbe` worth 300s for its first model load. |
| memory-ui | `httpGet /` | Static files; serving the index is the check. |
| postgres | `exec pg_isready` | |
| minio | `httpGet /minio/health/live` | |
| qdrant, memory-api, mcp, nginx | `tcpSocket` | See below. |

The four TCP probes are each a deliberate choice, not an oversight:

- **qdrant** ships no HTTP client in its image, so an exec probe would fail on
  the missing binary rather than on the service.
- **memory-api**'s `/health` is a dependency roll-up — it calls Postgres, Qdrant
  and the embedder, and answers `200` either way with the verdict in the body.
  That cannot fail as an HTTP probe, and would be actively wrong as liveness: a
  slow Postgres would restart a healthy pod.
- **mcp** has no unauthenticated HTTP route to probe. Everything but the OAuth
  discovery documents sits behind Bearer auth.
- **nginx** proxies every location somewhere; `/` goes to the editor. An httpGet
  probe would restart the front door whenever a backend was unwell.

**Known gap.** Three of those would be better as real endpoints: a `/livez` and
`/healthz` split on `memory-api`, an unauthenticated `/livez` on `mcp`, and a
self-served health location in `nginx.conf`. All three are application changes
rather than manifest ones — and adding an unauthenticated route to an
auth-protected MCP surface is a security decision that deserves its own
review — so they are filed separately rather than bolted on here. Until then
these pods report "the process is accepting connections", which is true, and
never less than the Compose stack checks.

## What is deliberately absent

- **An image pull secret.** The images are public. On a cluster running a
  secret replicator, shipping your own copy is worse than useless: in pull mode
  the replicator replaces the good copy with the placeholder, and every image
  pull then fails as an "anonymous token" 401 that reads like an expired
  credential.
- **`spec.tls` on the example Ingress.** It assumes Traefik already holds a wildcard certificate.
  A `spec.tls` block makes it look for a per-host Secret that does not exist and
  serve its own self-signed default instead. If your controller does issue
  per-host certificates, the example Ingress shows where to turn it back on.
- **The Namespace, inside the kustomize build.** See step 1.
- **`app.kubernetes.io/*` labels in any selector.** `includeSelectors: false` is
  set on purpose: letting kustomize into `spec.selector` makes every Deployment
  fail the *next* apply on an immutable-field conflict.

## The example overlay

`overlays/example/` shows the four things an overlay usually has to say:

- pin every claim to a named storage class;
- reference an image pull secret that already exists (reference, never ship);
- keep Postgres and Qdrant off named nodes with a node affinity;
- a Traefik Ingress on your own host.

None of that belongs in the base, and your cluster will want its own version
of each. Its README also covers a scoped ServiceAccount for deploying from CI.

## Differences from the Docker Compose stack

- **No `restart nginx` after a deploy.** Compose needs it because container IPs
  change and nginx resolves its upstreams once, at startup. A ClusterIP is
  stable for the life of the Service, so the addresses stay correct when the
  pods behind them are replaced.
- **Service names and ports are the Compose ones.** `editor:8080`,
  `embedder:8001`, `memory-api:8002`, `mcp:8003`, `memory-ui:8080` — `nginx.conf`
  is shared between the two stacks and is not templated, so the Services have to
  answer exactly what its upstreams ask for.
- **No `depends_on`.** On a greenfield apply you may see nginx restart once or
  twice in the first seconds, while it waits for its upstream Service names to
  resolve. It clears on its own.
