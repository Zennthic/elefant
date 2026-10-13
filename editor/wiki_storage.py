"""
Where wiki pages live: path normalisation, the caller's org, S3 reads and
writes with the /docs mirror, the tree listing, and the embedder reindex
trigger.

Moved out of main.py by CQ-04 phase 2. The wiki router, backup,
import/export and the token-authed wiki endpoints all use these, so they
live here rather than in any one of them.
"""
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from acl import auth_context_from_request
from config_db import DEFAULT_TENANT_SLUG, Tenant, TenantSettings
from internal_service import EMBEDDER_URL, _internal_service_headers
from storage import S3Storage, get_s3_storage

log = logging.getLogger("editor")

DOCS_DIR      = Path(os.environ.get("DOCS_DIR", "/docs")).resolve()


# ── Path helpers ───────────────────────────────────────────────────────────

def _normalize_path(rel: str) -> str:
    """Validate and normalize a wiki-relative path.

    Rejects absolute paths, NUL bytes, parent-dir refs, and empty strings.
    Returns the normalized POSIX form (forward slashes, no leading "./",
    no doubled slashes). This is both the S3 object key and the suffix
    for the /docs mirror.

    Even though S3 keys can't escape buckets, the /docs mirror writes
    under /docs/<slug>/<path> on the local filesystem — without this
    check, a malicious ".." path could escape the per-org subtree.
    """
    if not rel or not isinstance(rel, str):
        raise HTTPException(status_code=400, detail="Empty path")
    if "\x00" in rel:
        raise HTTPException(status_code=400, detail="Invalid path")
    parts: List[str] = []
    for raw in rel.replace("\\", "/").split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise HTTPException(status_code=400, detail="Invalid path")
        parts.append(raw)
    if not parts:
        raise HTTPException(status_code=400, detail="Empty path")
    return "/".join(parts)


# Default top-level wiki folder AI agents may write into. Per-tenant override
# lives in TenantSettings.ai_wiki_folder (admin-configured only).
AI_WIKI_FOLDER_DEFAULT = "AI"


def _sanitize_ai_folder(value) -> Optional[str]:
    """Validate an admin-supplied AI-folder name. Must be a single path
    segment (no slashes, no '..'). Returns the cleaned name, or None to clear
    the override (revert to the global default). Raises 422 on a bad value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="ai_wiki_folder must be a string or null")
    name = value.strip().strip("/").strip()
    if name == "":
        return None  # clear → use default
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name or len(name) > 128:
        raise HTTPException(status_code=422,
                            detail="ai_wiki_folder must be a single folder name (no slashes)")
    return name


def _resolve_ai_wiki_folder(session: Session, tenant_slug: str) -> str:
    """The top-level folder AI agents may write into for this tenant — the
    per-tenant override if set, else the global default."""
    row = session.query(TenantSettings).filter_by(tenant_slug=tenant_slug).first()
    if row and row.ai_wiki_folder:
        return row.ai_wiki_folder
    return AI_WIKI_FOLDER_DEFAULT


def _is_within_ai_folder(rel: str, folder: str) -> bool:
    """True if normalized path `rel` is a page strictly under `folder/` (not
    the folder itself)."""
    return rel.startswith(folder + "/") and len(rel) > len(folder) + 1


def _active_org(request: Optional[Request], session: Session) -> Tenant:
    """Resolve the Tenant for the caller's authenticated org. Falls back
    to the default tenant for unauthenticated callers (single-tenant
    single-org installs) and pre-PR-3 deployments without an org_id."""
    if request is not None:
        ctx = auth_context_from_request(request)
        if ctx and ctx.get("org_id"):
            org = session.query(Tenant).filter_by(id=ctx["org_id"]).first()
            if org:
                return org
    org = session.query(Tenant).filter_by(slug=DEFAULT_TENANT_SLUG).first()
    if not org:
        raise HTTPException(status_code=500, detail="No active tenant")
    return org


def _docs_mirror_path(org: Tenant, rel: str) -> Path:
    """Where to mirror this write on the local filesystem so the embedder
    (Phase 2: still filesystem-bound) keeps seeing fresh content. Default
    tenant stays flat under /docs/ for backward-compat with single-tenant
    embedder setups; other orgs nest under /docs/<slug>/ to match the
    embedder's multi-tenant routing convention."""
    base = DOCS_DIR if org.slug == DEFAULT_TENANT_SLUG else DOCS_DIR / org.slug
    return base / rel


def _docs_mirror_root(org: Tenant) -> Path:
    return DOCS_DIR if org.slug == DEFAULT_TENANT_SLUG else DOCS_DIR / org.slug


# ── Storage helpers ────────────────────────────────────────────────────────

def _s3() -> "S3Storage":
    """S3 is the only supported backend now. Refuse to operate if it's
    not configured — there's no /docs fallback to fall back to."""
    storage = get_s3_storage()
    if storage is None:
        raise HTTPException(
            status_code=503,
            detail="Object storage is not configured. Set ELEFANT_S3_* env vars.",
        )
    return storage


def _s3_read(org: Tenant, path: str) -> bytes:
    """Read an object from the org's default wiki bucket. 404 on miss."""
    try:
        return _s3().read(org.slug, path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


def _s3_write(org: Tenant, path: str, data: bytes) -> None:
    """Write to S3 (primary, authoritative) and mirror to /docs so the
    embedder keeps seeing fresh content. Mirror failures log but don't
    fail the request — S3 is the source of truth.

    Self-heals against a missing bucket: if the first PUT fails because
    the bucket isn't there (cold-start race against MinIO bringup, or an
    org whose Phase-1 provisioning silently failed), provision it and
    retry once."""
    storage = _s3()
    try:
        storage.write(org.slug, path, data)
    except Exception as exc:
        # Botocore raises ClientError("NoSuchBucket") — fall through to
        # the retry path on any exception so we cover MinIO-specific
        # error shapes too. If provisioning fails, the second write
        # propagates the real cause to the caller.
        log.warning("S3 write failed once, provisioning bucket and retrying: "
                    "org=%s path=%s err=%s", org.slug, path, exc)
        try:
            storage.provision_wiki(org.slug, seed_welcome=False)
        except Exception as prov_exc:
            log.error("Bucket provision-on-write failed: org=%s err=%s",
                      org.slug, prov_exc)
            raise
        storage.write(org.slug, path, data)
    # Mirror to /docs for the embedder. A swallowed one-shot failure here is
    # what desynced S3 from the search index in issue #83 (page in S3, counted,
    # but never on the volume the embedder walks), so retry a couple times
    # before giving up. The embedder also reconciles /docs from S3 at reindex,
    # which is the durable backstop; this just narrows the window.
    mp = _docs_mirror_path(org, path)
    for _attempt in range(3):
        try:
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_bytes(data)
            break
        except Exception as exc:
            if _attempt == 2:
                log.warning("Local mirror write failed after retries: org=%s path=%s err=%s",
                            org.slug, path, exc)
            else:
                time.sleep(0.05 * (_attempt + 1))


def _s3_delete(org: Tenant, path: str) -> None:
    _s3().delete(org.slug, path)
    try:
        mp = _docs_mirror_path(org, path)
        if mp.exists():
            mp.unlink()
        # Trim empty parents up to the org's mirror root so the embedder
        # doesn't keep stale empty dirs lying around.
        root = _docs_mirror_root(org)
        for parent in mp.parents:
            if parent == root or not str(parent).startswith(str(root)):
                break
            try:
                parent.rmdir()
            except OSError:
                break
    except Exception as exc:
        log.warning("Local mirror delete failed: org=%s path=%s err=%s",
                    org.slug, path, exc)


def _s3_exists(org: Tenant, path: str) -> bool:
    try:
        return _s3().exists(org.slug, path)
    except Exception:
        return False


# ── Tree building ──────────────────────────────────────────────────────────

def _build_tree(org: Tenant) -> dict:
    """Build the file-tree response by listing the org's S3 bucket.

    Returns the same JSON shape the UI has always consumed:
    {name, path, type, children: [...]}. Titles in Phase 2 are derived
    from filenames — pulling every file body to extract a heading would
    be N+1 GETs per tree call. A follow-up will stash titles in S3
    object metadata so the in-tree title can stay rich without the cost.
    """
    root_name = org.slug if org.slug != DEFAULT_TENANT_SLUG else DOCS_DIR.name
    root: dict = {"name": root_name, "path": "", "type": "directory", "children": []}
    try:
        keys = sorted(_s3().walk(org.slug))
    except Exception:
        return root
    dir_index: dict = {"": root}  # path → directory node
    for key in keys:
        if key.endswith("/.keep") or key == ".keep":
            # Directory placeholder — ensure the dir exists in the tree
            # but don't render the placeholder file itself.
            dir_path = key[:-len(".keep")].rstrip("/")
            if dir_path:
                _ensure_dir(dir_index, dir_path)
            continue
        if not key.endswith(".md"):
            continue
        parts = key.split("/")
        leaf = parts[-1]
        if leaf.startswith(".") or leaf.startswith("__"):
            continue
        dir_path = "/".join(parts[:-1])
        parent = _ensure_dir(dir_index, dir_path)
        parent["children"].append({
            "name": leaf,
            "path": key,
            "type": "file",
            "title": leaf[:-3].replace("-", " ").replace("_", " "),
        })
    _sort_tree(root)
    return root


def _ensure_dir(dir_index: dict, dir_path: str) -> dict:
    """Walk down dir_path, creating directory nodes as needed."""
    if dir_path in dir_index:
        return dir_index[dir_path]
    parts = dir_path.split("/") if dir_path else []
    cur_path = ""
    parent = dir_index[""]
    for part in parts:
        cur_path = f"{cur_path}/{part}" if cur_path else part
        if cur_path not in dir_index:
            node = {"name": part, "path": cur_path, "type": "directory", "children": []}
            parent["children"].append(node)
            dir_index[cur_path] = node
        parent = dir_index[cur_path]
    return parent


def _sort_tree(node: dict) -> None:
    if node.get("type") != "directory":
        return
    children = node.get("children") or []
    children.sort(key=lambda c: (c["type"] == "file", c["name"].lower()))
    for c in children:
        _sort_tree(c)


def _org_spaces_from_s3(org: Optional[Tenant]) -> set:
    """Top-level directories ("spaces") for ACL administration. Computed
    by inspecting the first path component of every key in the org's
    bucket — same answer as the old filesystem `list_org_spaces` but
    sourced from S3."""
    if org is None:
        return set()
    try:
        keys = _s3().walk(org.slug)
    except Exception:
        return set()
    spaces: set = set()
    for key in keys:
        if "/" not in key:
            continue  # top-level files don't count as spaces
        head = key.split("/", 1)[0]
        if head.startswith(".") or head.startswith("__"):
            continue
        spaces.add(head)
    return spaces



# ── Embedder trigger ───────────────────────────────────────────────────────

def _trigger_embedder_reindex(file_path: str = None, *,
                              org: "Tenant" = None,
                              deleted: bool = False) -> None:
    """Fire-and-forget POST to the embedder. Never blocks the caller.

    When called with a specific `file_path` and `org`, hits the targeted
    `/api/reindex/file` endpoint so a single page is (re)indexed or removed
    immediately — no full rebuild needed. This is what keeps a recycled or
    restored page in sync with search the instant the change happens, instead
    of relying on the next manual rebuild or a filesystem-watch event.

    Called without a path (e.g. multi-file moves/renames/dir deletes) it falls
    back to a full reindex, which now also prunes vectors for deleted pages.
    """
    import urllib.request as _urlreq
    import json as _json

    base_headers = _internal_service_headers()
    if file_path is not None and org is not None:
        # Send the DOCS_DIR-relative path the embedder indexes under, matching
        # the local mirror layout (default tenant flat, others nested by slug).
        rel = str(_docs_mirror_path(org, _normalize_path(file_path)).relative_to(DOCS_DIR))
        url  = f"{EMBEDDER_URL}/api/reindex/file"
        data = _json.dumps({"path": rel, "tenant": org.slug, "deleted": deleted}).encode()
        headers = {"Content-Type": "application/json", **base_headers}
    elif org is not None:
        # Multi-file bulk change: scope the rebuild to this org's collection so
        # other orgs' indexes are not affected.
        url, data, headers = f"{EMBEDDER_URL}/api/reindex?org_slug={org.slug}", b"", dict(base_headers)
    else:
        url, data, headers = f"{EMBEDDER_URL}/api/reindex", b"", dict(base_headers)

    def _do() -> None:
        try:
            req = _urlreq.Request(url, data=data, method="POST", headers=headers)
            with _urlreq.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            log.debug("Could not trigger embedder reindex: %s", exc)

    import threading as _t
    _t.Thread(target=_do, daemon=True).start()


def _ensure_home_page(org: Tenant) -> None:
    """Guarantee the org's wiki has a home page (index.md) — the landing page
    the UI always opens. Idempotent: only writes when index.md is absent, so
    it never clobbers user content. Writes via the normal editor path so S3
    and the /docs mirror agree from day one."""
    try:
        if _s3_exists(org, "index.md"):
            return
    except Exception:
        # If we can't even check, fall through and attempt the seed; a write
        # failure is logged below and is non-fatal.
        pass
    content = (
        f"# Welcome to your wiki\n"
        f"\n"
        f"This is the **default** wiki for **{org.slug}**.\n"
        f"\n"
        f"This is the home page (`index.md`). Edit it or create new pages from\n"
        f"the tree on the left. Every change is versioned automatically in\n"
        f"object storage — prior revisions are recoverable until lifecycle\n"
        f"rules expire them.\n"
    ).encode("utf-8")
    try:
        _s3_write(org, "index.md", content)
    except Exception as exc:
        log.warning("Home-page seed failed for %s: %s", org.slug, exc)
