# An example overlay

The base in `../../base` is generic: no hostname, no storage class, no pull
secret, no node names. Most real clusters need to say at least one of those.
This directory shows how, one file for each:

| File | What it adds | Change |
|---|---|---|
| `ingress.yaml` | A Traefik Ingress in front of the `nginx` Service. For other controllers, copy one from `../../examples/` instead. | The host |
| `storageclass-patch.yaml` | Pins every claim to one storage class. | The class name |
| `pull-secret-patch.yaml` | Makes every Deployment reference an image pull secret. Only needed for a private registry or mirror. | The Secret name |
| `storage-node-affinity-patch.yaml` | Keeps Postgres and Qdrant off named nodes, for example nodes with slow or fragile storage. | The node names |
| `kustomization.yaml` | Ties them together and sets `ELEFANT_PUBLIC_DOMAIN`. | The domain |

Values to change are marked `CHANGE THIS`. Delete any patch you do not need
from `kustomization.yaml`; the base works without all of them.

## Using it

```bash
cp -r deploy/k8s/overlays/example my-overlay
# edit the CHANGE THIS values, and fix the `../../base` path to point at the base
kubectl apply -k my-overlay
```

Create the namespace and the `elefant-secrets` Secret first, as
[`docs/install/kubernetes.md`](../../../../docs/install/kubernetes.md) describes.

## A scoped deploy credential for CI (optional)

`deployer-rbac.yaml` is for when CI deploys the stack. It is **not** in
`kustomization.yaml`, because `kubectl apply -k` runs as this account, and a
credential cannot grant itself its own permissions. A cluster admin applies it
once, by hand.

### 1. Create the namespace

```bash
kubectl apply -f ../../base/namespace.yaml
```

By hand, and once. The deploy credential below is admin *inside* `elefant`, and
a Namespace is a cluster-scoped object, so it cannot create its own namespace.
That is deliberate.

### 2. Create the deploy credential

```bash
kubectl apply -f deployer-rbac.yaml
```

That is a `github-deployer` ServiceAccount, the built-in `admin` ClusterRole
bound through a **RoleBinding** so it applies only inside `elefant`, a
long-lived token Secret, and one deliberately tiny cluster-scoped grant:
`get` and `patch` on the single namespace `elefant`, for a deploy job that
relabels its namespace.

### 3. Prove the scope, before trusting it

Build a kubeconfig from the token and check what it can and cannot do.

```bash
API=https://192.0.2.10:6443          # CHANGE THIS: your cluster's API endpoint
TOKEN=$(kubectl -n elefant get secret github-deployer-token \
          -o jsonpath='{.data.token}' | base64 -d)
CA=$(kubectl -n elefant get secret github-deployer-token \
          -o jsonpath='{.data.ca\.crt}')

cat > /tmp/elefant-deployer.kubeconfig <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: cluster
    cluster:
      server: ${API}
      certificate-authority-data: ${CA}
contexts:
  - name: elefant
    context: {cluster: cluster, namespace: elefant, user: github-deployer}
current-context: elefant
users:
  - name: github-deployer
    user: {token: ${TOKEN}}
KUBECONFIG

K="kubectl --kubeconfig /tmp/elefant-deployer.kubeconfig"

# Must answer yes
$K -n elefant auth can-i create deployments
$K auth can-i patch namespace/elefant

# Must answer no
$K auth can-i list nodes
$K -n kube-system get secrets
$K auth can-i patch namespace/default
```

All five answers must come out as written. A `yes` in the bottom group means the
RoleBinding was applied as a ClusterRoleBinding somewhere, which is
cluster-admin with a different name.

### 4. Store it as a CI secret

```bash
base64 -w0 /tmp/elefant-deployer.kubeconfig    # -b0 on macOS
rm /tmp/elefant-deployer.kubeconfig
```

Store the output as `KUBECONFIG_B64` in a GitHub Environment restricted to your
deploy branch, not as a repository-level secret. Keep the cluster address and
hostnames in that Environment's variables as well, so they stay out of the
workflow file.
