"""
S3-compatible storage providers for wiki content.

Elefant's wiki content lives in S3 buckets — there is no on-disk storage
option. The "local" option for self-hosted deployments is the bundled
MinIO container, which is itself S3-compatible; the "external" option is
any managed AWS S3 / R2 / B2 / Wasabi endpoint.

Bucket layout:
- One bucket per (org, wiki). Bucket name = `{prefix}{org_slug}--{wiki_name}`.
- Every org starts with a `default` wiki (provisioned eagerly at org
  create with a welcome page seed). Multi-wiki support is forthcoming;
  the bucket-per-wiki layout is already in place to receive it.
- `prefix` is empty by default for MinIO (where bucket names are scoped
  to the instance). For AWS S3, operators set ELEFANT_S3_BUCKET_PREFIX
  to a deployment-scoped value (e.g. `zennthic-`) since S3 bucket names
  are globally unique.

Bucket-level isolation is the audit-defensible form of multi-tenancy —
cross-org access becomes an IAM violation at the storage layer rather
than a path check we have to remember to write correctly everywhere in
the application.

Versioning is enabled on every provisioned bucket so we inherit S3's
point-in-time recovery for free. Lifecycle rules, cross-region
replication, and per-bucket IAM policies are operator concerns
configured outside Elefant (Terraform, MinIO admin, AWS console).
"""

import os
from abc import ABC, abstractmethod
from typing import Iterator, Optional


class StorageProvider(ABC):
    """Abstract surface for wiki content storage.

    Paths are always repo-relative POSIX strings — the provider owns the
    mapping from (org_slug, path) to whatever physical location it uses
    (S3 object key, filesystem path, etc.).
    """

    # ── Content CRUD ─────────────────────────────────────────────────────

    @abstractmethod
    def read(self, org_slug: str, path: str) -> bytes:
        ...

    @abstractmethod
    def write(self, org_slug: str, path: str, data: bytes) -> None:
        ...

    @abstractmethod
    def delete(self, org_slug: str, path: str) -> None:
        ...

    @abstractmethod
    def exists(self, org_slug: str, path: str) -> bool:
        ...

    @abstractmethod
    def list_dir(self, org_slug: str, path: str) -> Iterator[str]:
        """Yield immediate children of `path` (non-recursive). Returns
        paths relative to the org root. Empty `path` lists the root."""
        ...

    @abstractmethod
    def walk(self, org_slug: str, path: str = "") -> Iterator[str]:
        """Yield every file path under `path`, recursively. Returns paths
        relative to the org root. Empty `path` walks everything."""
        ...

    # ── Per-org lifecycle ───────────────────────────────────────────────
    # Called by org provisioning (PR 3). Idempotent so retrying a failed
    # org-create is safe.

    @abstractmethod
    def provision_org(self, org_slug: str) -> None:
        """Create whatever per-org container this provider needs (an S3
        bucket, a filesystem directory tree, etc.). Idempotent — re-runs
        on an already-provisioned org are no-ops."""
        ...

    @abstractmethod
    def deprovision_org(self, org_slug: str) -> None:
        """Tear down an org's storage. Used during hard-delete after the
        org has been offboarded long enough to be unrecoverable."""
        ...


class S3Storage(StorageProvider):
    """S3-compatible storage with one bucket per (org, wiki).

    Bucket name = `{bucket_prefix}{org_slug}--{wiki_name}`. Each org starts
    with a `default` wiki (provisioned at org-create with a welcome page);
    additional wikis live in their own buckets following the same pattern.

    `bucket_prefix` is empty by default so self-hosted MinIO deployments
    get clean names like `acme--default`. For AWS S3 (globally unique
    bucket namespace), operators set ELEFANT_S3_BUCKET_PREFIX to a
    deployment-scoped prefix (e.g. `zennthic-`) to avoid collisions.
    """

    DEFAULT_WIKI = "default"

    def __init__(self,
                 endpoint_url:  Optional[str],
                 region:        str,
                 access_key:    str,
                 secret_key:    str,
                 bucket_prefix: str = ""):
        import boto3
        from botocore.config import Config

        self.endpoint_url  = endpoint_url   # None for AWS S3; http://minio:9000 for bundled
        self.region        = region
        self.bucket_prefix = bucket_prefix

        kwargs: dict = {
            "aws_access_key_id":     access_key,
            "aws_secret_access_key": secret_key,
            "region_name":           region,
            # Path-style addressing works for both MinIO and AWS S3.
            # Virtual-hosted-style fails on MinIO without DNS for
            # bucket.host.tld which operators don't typically configure.
            "config": Config(
                s3      = {"addressing_style": "path"},
                retries = {"max_attempts": 3, "mode": "standard"},
            ),
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._s3 = boto3.client("s3", **kwargs)

    def bucket_for(self, org_slug: str, wiki_name: str = DEFAULT_WIKI) -> str:
        return f"{self.bucket_prefix}{org_slug}--{wiki_name}"

    # ── Lifecycle ───────────────────────────────────────────────────────

    def provision_wiki(self, org_slug: str, wiki_name: str = DEFAULT_WIKI,
                       seed_welcome: bool = True) -> str:
        """Create the bucket for a wiki. Returns the bucket name.

        Idempotent: re-running against an existing bucket is a no-op.
        On first creation (and only then), seeds a home page (index.md) so
        new users see something other than an empty tree.
        """
        bucket = self.bucket_for(org_slug, wiki_name)
        # head_bucket is the canonical exists-and-accessible probe.
        try:
            self._s3.head_bucket(Bucket=bucket)
            return bucket  # already provisioned — don't re-seed
        except Exception:
            pass

        # AWS S3 quirk: us-east-1 must NOT pass CreateBucketConfiguration;
        # every other region must. MinIO accepts either form.
        if self.region and self.region != "us-east-1":
            self._s3.create_bucket(
                Bucket = bucket,
                CreateBucketConfiguration = {"LocationConstraint": self.region},
            )
        else:
            self._s3.create_bucket(Bucket=bucket)

        # Versioning gives PITR for free — every overwrite/delete leaves
        # a recoverable prior version until lifecycle rules expire it.
        self._s3.put_bucket_versioning(
            Bucket                  = bucket,
            VersioningConfiguration = {"Status": "Enabled"},
        )

        if seed_welcome:
            self._seed_home_page(bucket, org_slug, wiki_name)

        return bucket

    # Backward-compatible alias so older call sites keep working until
    # they migrate to provision_wiki(). Treated as "provision the org's
    # default wiki" for the eager-create flow added in PR 3.
    def provision_org(self, org_slug: str) -> None:
        self.provision_wiki(org_slug, self.DEFAULT_WIKI)

    def deprovision_wiki(self, org_slug: str, wiki_name: str = DEFAULT_WIKI) -> None:
        """Drain every object version + delete-marker (versioning is on)
        before dropping the bucket itself. Used by org/wiki hard-delete."""
        bucket = self.bucket_for(org_slug, wiki_name)
        paginator = self._s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            to_delete = []
            for v in (page.get("Versions") or []):
                to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for m in (page.get("DeleteMarkers") or []):
                to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
            if to_delete:
                self._s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
        self._s3.delete_bucket(Bucket=bucket)

    def deprovision_org(self, org_slug: str) -> None:
        """Tear down every wiki bucket belonging to an org. Discovers wikis
        by listing buckets whose name starts with `{prefix}{slug}--`."""
        prefix = f"{self.bucket_prefix}{org_slug}--"
        try:
            resp = self._s3.list_buckets()
        except Exception:
            return
        for b in resp.get("Buckets", []) or []:
            name = b.get("Name", "")
            if not name.startswith(prefix):
                continue
            wiki = name[len(prefix):]
            try:
                self.deprovision_wiki(org_slug, wiki)
            except Exception:
                # best-effort — leave remaining buckets in place rather than
                # bailing mid-teardown
                pass

    def _seed_home_page(self, bucket: str, org_slug: str, wiki_name: str) -> None:
        """Write a friendly home page (index.md) so a brand-new wiki isn't an
        empty tree. index.md is the wiki landing page the UI always opens, so
        seeding it (not welcome.md) guarantees the landing frame has content.
        The content references the org/wiki by name so the user has an
        obvious entrypoint."""
        content = (
            f"# Welcome to your wiki\n"
            f"\n"
            f"This is the **{wiki_name}** wiki for **{org_slug}**.\n"
            f"\n"
            f"This is the home page (`index.md`). Edit it or create new pages\n"
            f"from the tree on the left. Every change is versioned automatically\n"
            f"in object storage — prior revisions are recoverable until\n"
            f"lifecycle rules expire them.\n"
        ).encode("utf-8")
        try:
            self._s3.put_object(
                Bucket      = bucket,
                Key         = "index.md",
                Body        = content,
                ContentType = "text/markdown; charset=utf-8",
            )
        except Exception:
            # Best-effort — bucket exists either way, user can write their
            # own first page if seeding failed.
            pass

    # ── CRUD ────────────────────────────────────────────────────────────

    def read(self, org_slug: str, path: str) -> bytes:
        resp = self._s3.get_object(Bucket=self.bucket_for(org_slug), Key=path)
        return resp["Body"].read()

    def write(self, org_slug: str, path: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket_for(org_slug), Key=path, Body=data)

    def delete(self, org_slug: str, path: str) -> None:
        # With versioning on, this writes a delete-marker rather than
        # destroying history. deprovision_org drains all versions on
        # hard-delete; routine deletes leave recoverable history.
        self._s3.delete_object(Bucket=self.bucket_for(org_slug), Key=path)

    def exists(self, org_slug: str, path: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket_for(org_slug), Key=path)
            return True
        except Exception:
            # head_object raises ClientError(404) for not-found and
            # ClientError(403) for forbidden — both mean "not visible
            # to us" at this layer.
            return False

    def list_dir(self, org_slug: str, path: str) -> Iterator[str]:
        prefix = (path.rstrip("/") + "/") if path else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket    = self.bucket_for(org_slug),
            Prefix    = prefix,
            Delimiter = "/",
        ):
            for obj in (page.get("Contents") or []):
                yield obj["Key"]
            for p in (page.get("CommonPrefixes") or []):
                yield p["Prefix"]

    def walk(self, org_slug: str, path: str = "") -> Iterator[str]:
        prefix = (path.rstrip("/") + "/") if path else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_for(org_slug), Prefix=prefix):
            for obj in (page.get("Contents") or []):
                yield obj["Key"]


# ── Module-level singleton, env-driven ──────────────────────────────────

_s3_storage: Optional[S3Storage] = None


def get_s3_storage() -> Optional[S3Storage]:
    """Return the configured S3Storage, or None if env vars aren't set.

    `None` means "no S3 backend is available in this deployment" — every
    org stays on the legacy filesystem path. Once PR 3 ships, the
    org.storage_backend flag decides per-org which path to take; this
    function just answers "is S3 even configured."

    Reads:
      ELEFANT_S3_ENDPOINT_URL    optional — empty means AWS S3 over public endpoints
      ELEFANT_S3_ACCESS_KEY      required to enable S3
      ELEFANT_S3_SECRET_KEY      required to enable S3
      ELEFANT_S3_REGION          default "us-east-1"
      ELEFANT_S3_BUCKET_PREFIX   default "elefant-org-"
    """
    global _s3_storage
    if _s3_storage is not None:
        return _s3_storage

    access_key = os.environ.get("ELEFANT_S3_ACCESS_KEY") or ""
    secret_key = os.environ.get("ELEFANT_S3_SECRET_KEY") or ""
    if not access_key or not secret_key:
        return None

    _s3_storage = S3Storage(
        endpoint_url  = os.environ.get("ELEFANT_S3_ENDPOINT_URL") or None,
        region        = os.environ.get("ELEFANT_S3_REGION") or "us-east-1",
        access_key    = access_key,
        secret_key    = secret_key,
        bucket_prefix = os.environ.get("ELEFANT_S3_BUCKET_PREFIX", ""),
    )
    return _s3_storage
