"""
Editor backend – file CRUD API + backend management.

Storage model:
  /docs is always the working copy on disk.
  - local  : files stay in /docs only.
  - gitlab / github : /docs is a git repo; every write is committed and pushed.

On startup, BackendManager.initialize() clones the configured repo into /docs
(if it isn't already a git repo) or does a git pull.
"""
import bcrypt
import hashlib
import json as _json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import quote as _urlquote

import httpx

from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from sqlalchemy.orm import Session

import restore_jobs
from config_db import (
    init_db, get_db_session, ConfigManager,
    SoftDeletedPage, WikiRevision,
    UserAccount, TeamMembership,
    MfaCredential,
    GitSource,
    Tenant, OidcState, SamlRequestId,
    OrgRestoreJob,
    DEFAULT_TENANT_SLUG,
    tenant_wiki_collection, tenant_mem_collection,
)
from storage import get_s3_storage

log = logging.getLogger("editor")
logging.basicConfig(level=logging.INFO)

# The embedder's and memory-api's URLs and the peer-service secret live in
# internal_service, and the wiki's storage and page helpers in wiki_storage
# and wiki_pages, so the routers can use them without importing this file.
from internal_service import (
    MEMORY_API_URL, _internal_service_headers, _require_internal_service,
)
from wiki_storage import (
    DOCS_DIR,
    _normalize_path, _resolve_ai_wiki_folder, _is_within_ai_folder,
    _s3, _s3_read, _s3_write, _s3_delete, _s3_exists,
    _org_spaces_from_s3, _trigger_embedder_reindex, _ensure_home_page,
)
from wiki_pages import (
    _heading_outline, _page_section, _apply_page_edit,
    _record_wiki_revision, _wiki_revisions_for,
    _wiki_lock_for, _assert_wiki_unlocked, _assert_wiki_prefix_unlocked,
)


# Public base URL, mount prefix, app name and the helpers the routers share
# with this file. They live in auth_helpers so a router never imports main.
from auth_helpers import (
    APP_NAME, _render, _STATIC,
    _effective_tenant_slug, _resolve_bearer_token, _board_stream_token,
    _live_session_user,
)
# Backup and restore machinery. The routes that serve it are in
# routers/backup.py; main.py only needs what its startup hooks use.
from org_backup import (
    _RESTORE_STAGING_DIR,
    _run_auto_backup_loop, _run_wiki_backup_loop, _run_org_backup_loop,
)
from routers import (
    admin_platform as admin_platform_router, backup as backup_router,
    boards as boards_router, git_sources as git_sources_router,
    local_auth as local_auth_router, oauth as oauth_router,
    org_admin as org_admin_router, sso as sso_router,
    tokens as tokens_router, wiki as wiki_router,
)

# Session constants are owned by auth.py — re-import them at module load
# time (further down) so the rest of this file can keep using the same names.
from auth import (
    SESSION_COOKIE, SESSION_SECRET,
    MFA_PENDING_COOKIE,
    parse_login,
    hash_password, verify_password,
    decode_session_token, bump_session_epoch,
    RequiresLogin, require_role,
    require_platform_admin, require_org_admin,
    get_user,
    password_reset_blocked, mfa_reset_blocked,
)

if not SESSION_SECRET:
    log.warning(
        "SESSION_SECRET is not set — local login disabled. In Compose it "
        "comes from ELEFANT_SESSION_SECRET in .env (openssl rand -hex 32); "
        "in Kubernetes from the elefant-secrets Secret."
    )

MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB

# Patch Starlette's upload size limits before any parsing
import starlette.formparsers
import starlette.datastructures
# Override Starlette's default limits
starlette.formparsers.UPLOAD_SIZE = MAX_UPLOAD_SIZE
starlette.datastructures.UPLOAD_SIZE = MAX_UPLOAD_SIZE

# ASGI middleware to allow large uploads by not rejecting based on content-length
class LargeUploadMiddleware:
    """Allow uploads up to 500MB by patching Starlette's body reading."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # For all POST requests (especially /api/admin/import), allow larger bodies
        # by wrapping receive to not enforce Starlette's default 16MB limit
        async def receive_with_large_size() -> Message:
            message = await receive()
            # Don't modify the message, just pass it through
            # The key is that we're not calling Starlette's built-in body size check
            return message

        # Patch the scope to increase max body size
        scope["max_body_size"] = MAX_UPLOAD_SIZE

        await self.app(scope, receive_with_large_size, send)

app = FastAPI(title="KB Editor API")

# Add middleware — order matters: LargeUpload must wrap everything; ProxyHeaders
# must run before route handlers so request.url.scheme is correct.
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(LargeUploadMiddleware)


# ── CSRF defence-in-depth ────────────────────────────────────────────────
#
# samesite=lax on the session cookie (see auth.cookie_kwargs) blocks the
# primary CSRF vector at the browser level. This middleware is the
# second gate: for any state-changing request that carries our session
# cookie, the Origin (or Referer if Origin is absent) must match our own
# host. A cross-site form submission that does manage to carry the
# cookie (older browser, lax-as-none quirks, header-stripping proxies)
# still hits a 403 here.
#
# Exempt path prefixes:
#   /auth/                 — external IdP callbacks (OIDC/SAML); designed
#                            for cross-site POST, doesn't depend on an
#                            existing session
#   /oauth/token           — RFC 6749 token endpoint; machine-to-machine
#                            POST, no browser session involved
#   /oauth/register        — RFC 7591 DCR; machine-to-machine
#   /oauth/revoke          — RFC 7009; machine-to-machine
#   /api/internal/         — peer-service-only paths; protected by the
#                            internal-service token check, not by session
#   /health                — Docker healthcheck

_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_CSRF_EXEMPT_PREFIXES = (
    "/auth/",
    "/oauth/token",
    "/oauth/register",
    "/oauth/revoke",
    "/api/internal/",
    "/health",
)


def _csrf_request_origin_host(request: Request) -> str:
    """Same-origin host the request must match. We rebuild from the
    proxy-forwarded headers rather than EDITOR_EXTERNAL_URL so the check
    works in any deployment topology (dev, single-host compose, traefik)
    without an explicit config knob."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").lower()
    host  = (request.headers.get("x-forwarded-host")
             or request.headers.get("host")
             or "")
    if not host:
        return ""
    # Strip a possible port to keep the compare tolerant of trailing :443/:80
    # in the Origin header.
    return f"{proto}://{host}".rstrip("/")


def _csrf_check_origin(request: Request) -> bool:
    """True when the request's Origin (or Referer) matches our own host.
    Lacking both headers, refuse — same-site browser POSTs always send
    one of them, so absence indicates either a non-browser caller (which
    should have been routed to an exempt path) or a stripped header."""
    expected = _csrf_request_origin_host(request)
    if not expected:
        # We can't compute our own origin — fail open rather than locking
        # everyone out of a misconfigured deployment.  The startup logs
        # already complain when X-Forwarded-* aren't being set.
        return True
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin:
        return origin == expected
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        # Compare scheme://host of the referer to our expected origin.
        from urllib.parse import urlparse as _urlparse
        p = _urlparse(referer)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}".rstrip("/") == expected
    return False


# Peer-only paths never answer a browser. nginx refuses /editor/api/internal/
# before it reaches us; this is the same rule from the inside, for a request
# that arrived through any proxy that forwards a signed-in identity. The
# peers (MCP server, embedder, memory-api) never send X-Auth-* headers here.
_PEER_ONLY_PREFIX = "/api/internal/"


@app.middleware("http")
async def peer_only_paths_refuse_a_browser_identity(request: Request, call_next):
    if request.url.path.startswith(_PEER_ONLY_PREFIX) and (
            request.headers.get("x-auth-user-id") or request.headers.get("x-auth-user")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    if request.method in _CSRF_SAFE_METHODS:
        return await call_next(request)
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _CSRF_EXEMPT_PREFIXES):
        return await call_next(request)
    # Bearer-token requests (OAuth-authenticated MCP clients hitting
    # /api/me/* or /api/admin/*) don't carry the session cookie, so a
    # cross-site forgery can't borrow their identity. Skip the CSRF gate
    # for them — they're authenticated by a header the browser can't
    # auto-attach.
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return await call_next(request)
    # No session cookie => nothing to forge; let the handler decide.
    if not request.cookies.get(SESSION_COOKIE):
        return await call_next(request)
    if not _csrf_check_origin(request):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"detail": "cross-origin request blocked"},
            status_code=403,
        )
    return await call_next(request)


init_db()


# ── App startup ────────────────────────────────────────────────────────────

# DOCS_DIR is kept as a *write-only mirror sink* for the duration of
# Phase 2: the embedder still watches the filesystem, so every editor
# write also lands under /docs/<slug>/<path> (or /docs/<path> for the
# default tenant) to keep search results fresh. Editor *reads* go to S3
# only. Phase 3 cuts the embedder over to S3 polling and removes the
# /docs volume entirely.

@app.on_event("startup")
def startup():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # Every active tenant — default and otherwise — needs its wiki bucket
    # provisioned. Pre-Phase-1 orgs had buckets named `elefant-org-<slug>`;
    # those are now orphaned and the new `<slug>--default` buckets won't
    # exist until we create them here. Idempotent: provision_wiki is a
    # no-op when the bucket already exists.
    try:
        s3 = get_s3_storage()
        if s3 is not None:
            _provision_all_active_org_buckets(s3)
            _migrate_local_docs_to_s3_once()
    except Exception as exc:
        log.warning("S3 bootstrap skipped: %s", exc)
    try:
        readiness_file = os.environ.get("BACKEND_READINESS_FILE", "/tmp/backend_ready")
        import pathlib
        pathlib.Path(readiness_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(readiness_file).touch()
        log.info("Backend ready — wrote marker: %s", readiness_file)
    except Exception as sig_err:
        log.warning("Could not write readiness marker: %s", sig_err)


def _provision_all_active_org_buckets(s3) -> None:
    """Ensure every active tenant has its default wiki bucket. We pass
    seed_welcome=False to storage.provision_wiki and seed via _s3_write
    instead — that way the home page lands in both S3 *and* the /docs
    mirror, so the embedder (still filesystem-bound in Phase 2) actually
    sees it during indexing. A direct-to-S3 seed would leave /docs blank
    and the indexed-vs-on-disk counts would diverge."""
    from config_db import SessionLocal as _SL
    with _SL() as session:
        orgs = list(session.query(Tenant).filter_by(is_active=True).all())
    for org in orgs:
        try:
            s3.provision_wiki(org.slug, seed_welcome=False)
        except Exception as exc:
            log.warning("Bucket provision skipped for %s: %s", org.slug, exc)
            continue
        # Guarantee a home page (index.md) exists — the UI always opens it as
        # the landing page, so a missing index.md shows a 404 in the wiki
        # frame. _ensure_home_page is idempotent (only writes when absent), so
        # this backfills existing wikis without clobbering user content.
        try:
            _ensure_home_page(org)
        except Exception as exc:
            log.warning("Home-seed check skipped for %s: %s", org.slug, exc)


def _migrate_local_docs_to_s3_once() -> None:
    """One-shot upgrade migration: walk /docs and upload every file to the
    matching org's S3 bucket. Idempotent — skips any org whose bucket
    already has content. Covers the upgrade path from pre-Phase-2
    deployments where wiki content lived only on the filesystem.

    Assumes _provision_all_active_org_buckets has already created every
    target bucket, so a walk that comes back empty means "migrate this".
    A walk that *fails* is treated as "give up on this org" — we won't
    write into a bucket we can't even list.

    File-to-org routing matches the embedder's multi-tenant convention:
    /docs/<slug>/* routes to <slug> when <slug> is a known active org;
    everything else goes to the default tenant.
    """
    s3 = get_s3_storage()
    if s3 is None or not DOCS_DIR.exists():
        return
    from config_db import SessionLocal as _SL
    with _SL() as session:
        active_slugs = {
            t.slug for t in session.query(Tenant).filter_by(is_active=True).all()
        }
    empty_cache: dict = {}  # slug → bool (True if bucket is empty and ready to receive content)
    uploaded = 0
    for fp in DOCS_DIR.rglob("*"):
        if not fp.is_file():
            continue
        rel_parts = fp.relative_to(DOCS_DIR).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        if rel_parts[0] in active_slugs and rel_parts[0] != DEFAULT_TENANT_SLUG:
            org_slug = rel_parts[0]
            org_rel = "/".join(rel_parts[1:])
        else:
            org_slug = DEFAULT_TENANT_SLUG
            org_rel = "/".join(rel_parts)
        if not org_rel:
            continue
        if org_slug not in empty_cache:
            try:
                # An empty bucket → migration target. Any real object means the
                # org has been touched already; don't clobber. A bucket holding
                # nothing but the seed home page (index.md, or the legacy
                # welcome.md) is still a target — the startup home-page backfill
                # runs before this migration, so we must not let that seed mask
                # un-migrated /docs content.
                keys = list(s3.walk(org_slug))
                only_seed = keys in (["index.md"], ["welcome.md"])
                empty_cache[org_slug] = (not keys) or only_seed
            except Exception as exc:
                # Walk failed (bucket missing despite provision attempt,
                # or transient MinIO blip). Skip rather than risk writing
                # to a bucket whose state we don't know.
                log.warning("Migration: cannot list bucket for %s, skipping: %s",
                            org_slug, exc)
                empty_cache[org_slug] = False
        if not empty_cache[org_slug]:
            continue  # bucket already populated, leave alone
        try:
            s3.write(org_slug, org_rel, fp.read_bytes())
            uploaded += 1
        except Exception as exc:
            log.warning("Migration upload failed: slug=%s path=%s err=%s",
                        org_slug, org_rel, exc)
    if uploaded:
        log.info("Migrated %d files from /docs to S3", uploaded)


# ── Models ─────────────────────────────────────────────────────────────────

class AdminConfigRequest(BaseModel):
    storage_backend: Optional[Dict] = None
    embedding_config: Optional[Dict] = None
    general_settings: Optional[Dict] = None


class AdminConfigResponse(BaseModel):
    storage_backend: Optional[Dict] = None
    embedding_config: Optional[Dict] = None
    general_settings: Optional[Dict] = None
    tested_backends: Dict = {}


class TestBackendRequest(BaseModel):
    backend_type: str
    config: Dict


class TestBackendResponse(BaseModel):
    success: bool
    message: str
    details: dict = {}


# ── Health check ──────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "ok", "backend": "s3"}


# ── ACL helpers ────────────────────────────────────────────────────────────

from acl import (
    filter_readable_spaces, memory_visibility_filter,
)


# ── Wiki: file CRUD, revisions, locks, sharing, recycle bin, directories ──
# Lives in routers/wiki.py. Included here, where its routes used to be
# declared, so the route order (and with it which route matches) is unchanged.
app.include_router(wiki_router.router)
app.router.add_event_handler("startup", wiki_router.purge_old_recycle_bin)


# ── Admin config ────────────────────────────────────────────────────────────

@app.get("/api/admin/config", response_model=AdminConfigResponse)
def get_admin_config(
    _claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    all_configs  = ConfigManager.get_all_configs(session)  # masked — safe for browser
    test_results = ConfigManager.get_raw_config(session, "test_results") or {}
    return AdminConfigResponse(
        storage_backend=all_configs.get("storage_backend"),
        embedding_config=all_configs.get("embedding_config"),
        general_settings=all_configs.get("general_settings"),
        tested_backends=test_results.get("backends", {}),
    )


@app.get("/api/internal/config")
def get_internal_config(request: Request, session: Session = Depends(get_db_session)):
    """Unmasked config for internal service use only (not exposed through nginx)."""
    _require_internal_service(request)
    embedding_config = ConfigManager.get_raw_config(session, "embedding_config") or {}
    storage_backend  = ConfigManager.get_raw_config(session, "storage_backend") or {}

    # Build git_sources list with per-source org_slug so the embedder can
    # route on-disk clones to /git-sources/<org_slug>/<source_id>/.
    slug_by_org = {t.id: t.slug for t in session.query(Tenant).all()}
    git_source_rows = session.query(GitSource).all()
    git_sources = {
        "sources": [
            {
                **r.to_dict(include_token=True, include_org_id=True),
                "org_slug": slug_by_org.get(r.org_id) or DEFAULT_TENANT_SLUG,
            }
            for r in git_source_rows
        ]
    }
    return {
        "embedding_config": embedding_config,
        "storage_backend":  storage_backend,
        "git_sources":      git_sources,
    }


@app.post("/api/admin/config")
def save_admin_config(
    body: AdminConfigRequest,
    _claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    saved = []
    if body.storage_backend:
        ConfigManager.set_config(session, "storage_backend", body.storage_backend)
        saved.append("storage_backend")
    if body.embedding_config:
        embed_cfg = dict(body.embedding_config)
        # Blank api_key means "keep stored key" — merge in the raw (unmasked) stored value.
        if not embed_cfg.get("api_key"):
            stored = ConfigManager.get_raw_config(session, "embedding_config") or {}
            if stored.get("api_key"):
                embed_cfg["api_key"] = stored["api_key"]
        # The bundled containerized embedder is locked to a single model for
        # stability on small homelab hosts. Coerce rather than reject so older
        # clients that still POST a model field don't get a 4xx.
        if (embed_cfg.get("provider") or "").lower() == "local":
            requested = embed_cfg.get("model")
            if requested and requested != "multi-qa-distilbert-cos-v1":
                log.warning(
                    "Coerced local-provider model %r -> 'multi-qa-distilbert-cos-v1' "
                    "(bundled embedder is locked; use an external provider for other models)",
                    requested,
                )
            embed_cfg["model"] = "multi-qa-distilbert-cos-v1"
        ConfigManager.set_config(session, "embedding_config", embed_cfg)
        saved.append("embedding_config")
    if body.general_settings:
        ConfigManager.set_config(session, "general_settings", body.general_settings)
        saved.append("general_settings")
    return {"status": "saved" if saved else "no_changes", "saved": saved}


@app.get("/api/admin/embedding-state")
def embedding_state(_claims: dict = Depends(require_org_admin)):
    """Return the embedder's per-collection sidecar — what provider+model+dim
    was used to populate each Qdrant collection's vectors. The admin UI reads
    this before a config save to warn when the new config would orphan the
    existing index.

    The file lives on the shared backend_sync volume (written by the embedder
    after every successful build) so the editor can read it directly without
    a round-trip through the embedder.
    """
    readiness_file = Path(os.environ.get("BACKEND_READINESS_FILE", "/tmp/backend_ready"))
    state_file = readiness_file.parent / "embedding_state.json"
    try:
        with state_file.open("r") as f:
            data = _json.load(f)
    except FileNotFoundError:
        return {"schema_version": 1, "collections": {}}
    except Exception as exc:
        log.warning("Could not read embedding state sidecar (%s): %s", state_file, exc)
        return {"schema_version": 1, "collections": {}}
    if not isinstance(data, dict):
        return {"schema_version": 1, "collections": {}}
    data.setdefault("schema_version", 1)
    data.setdefault("collections", {})
    return data


# ── Hot tier admin proxies (PR C) ────────────────────────────────────────────
# Thin pass-throughs to memory-api so the admin UI talks to one origin and
# auth stays on the editor (require_org_admin). tenant_slug is resolved
# server-side so an org admin can't peek into another org's hot tier even
# by tampering with a query param.

@app.get("/api/admin/memory/compact/config")
def memory_compact_config_admin(claims: dict = Depends(require_org_admin)):
    try:
        resp = httpx.get(f"{MEMORY_API_URL}/memory/compact/config",
                         headers=_internal_service_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.get("/api/admin/memory/compact/stats")
def memory_compact_stats_admin(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Hot-tier health snapshot for the Memory Admin panel — queue
    depth, totals, oldest pending capture age, and the last few
    compactor passes. Tenant scope is resolved server-side from
    claims so an org admin can't peek into another org's metrics."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        resp = httpx.get(
            f"{MEMORY_API_URL}/memory/compact/stats",
            params={"tenant_slug": slug},
            headers=_internal_service_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.get("/api/admin/memory/hot/diag")
def memory_hot_diag_admin(claims: dict = Depends(require_platform_admin)):
    """Cross-tenant diagnostic view of memory_hot — total rows,
    per-tenant breakdown, pg_policies state, and whether SET LOCAL is
    actually persisting the GUC inside memory-api's transaction.
    Platform-admin only because it intentionally bypasses tenant
    isolation to expose state across all orgs."""
    try:
        resp = httpx.get(f"{MEMORY_API_URL}/memory/hot/diag",
                         headers=_internal_service_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.post("/api/admin/memory/compact/run")
def memory_compact_run_admin(claims: dict = Depends(require_org_admin)):
    try:
        resp = httpx.post(f"{MEMORY_API_URL}/memory/compact/run",
                          headers=_internal_service_headers(), timeout=120)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.get("/api/admin/memory/hot/recent")
def memory_hot_recent_admin(
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("all", regex="^(all|pending|promoted|dropped)$"),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        resp = httpx.get(
            f"{MEMORY_API_URL}/memory/hot/recent",
            params={"tenant_slug": slug, "limit": limit, "status": status},
            headers=_internal_service_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.post("/api/admin/memory/hot/{row_id}/promote")
def memory_hot_promote_admin(
    row_id: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        resp = httpx.post(
            f"{MEMORY_API_URL}/memory/hot/{row_id}/promote",
            params={"tenant_slug": slug},
            headers=_internal_service_headers(),
            timeout=60,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code,
                                detail=resp.json().get("detail", resp.text))
        return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


@app.post("/api/admin/memory/hot/{row_id}/drop")
def memory_hot_drop_admin(
    row_id: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        resp = httpx.post(
            f"{MEMORY_API_URL}/memory/hot/{row_id}/drop",
            params={"tenant_slug": slug},
            headers=_internal_service_headers(),
            timeout=15,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code,
                                detail=resp.json().get("detail", resp.text))
        return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")


# ── Board: columns, cards, comments, people, the live stream ─────────────────
# Lives in routers/boards.py. Included here, where its routes used to be
# declared, so the route order (and with it which route matches) is unchanged.
app.include_router(boards_router.router)


@app.post("/api/admin/test-backend", response_model=TestBackendResponse)
def test_backend(
    body: TestBackendRequest,
    _claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """S3 is the only supported backend — older github/gitlab/local cases
    have been removed along with their helper functions. The endpoint
    stays here because the admin UI still POSTs to it for credential
    verification."""
    backend_type = body.backend_type.lower()
    config = body.config or {}
    if backend_type != "s3":
        return TestBackendResponse(
            success=False,
            message=f"Unsupported backend: {backend_type}. S3 is the only supported option.",
        )
    try:
        result = _test_s3_backend(config)
        test_results = ConfigManager.get_raw_config(session, "test_results") or {"backends": {}}
        if result["success"]:
            test_results["backends"]["s3"] = True
        ConfigManager.set_config(session, "test_results", test_results)
        return TestBackendResponse(success=result["success"], message=result["message"], details=result.get("details", {}))
    except Exception as e:
        return TestBackendResponse(success=False, message=f"Test failed: {str(e)}")


@app.post("/api/admin/test-embedding", response_model=TestBackendResponse)
def test_embedding(
    body: TestBackendRequest,
    _claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    provider = body.backend_type.lower()
    config   = body.config or {}
    # If api_key is absent/null (blank field = "use stored key"), substitute the
    # raw (unmasked) stored value so the test uses the real credential.
    if not config.get("api_key"):
        stored = ConfigManager.get_raw_config(session, "embedding_config") or {}
        if stored.get("api_key"):
            config = {**config, "api_key": stored["api_key"]}
    try:
        result = _test_local_embedding(config) if provider == "local" else \
                 _test_openai_embedding(config) if provider == "openai" else \
                 _test_custom_embedding(config) if provider == "custom" else \
                 {"success": False, "message": f"Unknown provider: {provider}"}
        return TestBackendResponse(success=result["success"], message=result["message"], details=result.get("details", {}))
    except Exception as e:
        return TestBackendResponse(success=False, message=f"Test failed: {str(e)}")


# ── Backend connectivity helpers ────────────────────────────────────────────

def _test_s3_backend(config: dict) -> dict:
    try:
        import boto3
        from botocore.exceptions import ClientError
        access_key = config.get("s3_access_key", "").strip()
        secret_key = config.get("s3_secret_key", "").strip()
        bucket     = config.get("s3_bucket", "").strip()
        region     = config.get("s3_region", "us-east-1").strip()
        if not access_key or not secret_key or not bucket:
            return {"success": False, "message": "Missing S3 credentials or bucket name"}
        s3 = boto3.client("s3", aws_access_key_id=access_key,
                          aws_secret_access_key=secret_key, region_name=region)
        s3.head_bucket(Bucket=bucket)
        return {"success": True, "message": f"S3 bucket '{bucket}' accessible"}
    except ImportError:
        return {"success": False, "message": "boto3 not installed in this image"}
    except Exception as e:
        return {"success": False, "message": f"S3 test failed: {e}"}


def _test_local_embedding(config: dict) -> dict:
    return {"success": True, "message": "Local embedding model available (sentence-transformers)"}


def _test_openai_embedding(config: dict) -> dict:
    try:
        api_key = config.get("openai_api_key", "").strip()
        if not api_key:
            return {"success": False, "message": "OpenAI API key not provided"}
        resp = httpx.get("https://api.openai.com/v1/models",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=8)
        if resp.status_code == 200:
            return {"success": True, "message": "OpenAI API key authenticated"}
        return {"success": False, "message": f"OpenAI API returned {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"OpenAI test failed: {e}"}


def _test_custom_embedding(config: dict) -> dict:
    try:
        base_url = config.get("base_url", "").strip().rstrip("/")
        model    = config.get("model", "").strip()
        api_key  = config.get("api_key", "").strip()
        if not base_url:
            return {"success": False, "message": "Custom API endpoint (base_url) is required"}
        if not model:
            return {"success": False, "message": "Model name is required"}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model, "input": ["connection test"], "encoding_format": "float"}
        resp = httpx.post(f"{base_url}/embeddings", json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {"success": False, "message": f"Endpoint returned HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
        except Exception:
            return {"success": False, "message": f"Endpoint returned non-JSON response: {resp.text[:200]}"}
        # Validate the response actually contains embeddings (not just an HTTP 200 stub)
        embedding = data.get("data", [{}])[0].get("embedding", [])
        if not embedding:
            # Surface any error message from the body to help diagnose
            err_msg = data.get("error", {}).get("message") or data.get("message") or ""
            hint = (
                " Make sure the embedding model is loaded and started in LM Studio's Developer tab."
                if not err_msg else ""
            )
            return {
                "success": False,
                "message": f"Endpoint replied but returned no embedding data. {err_msg}{hint}".strip(),
            }
        dims = len(embedding)
        return {"success": True, "message": f"Custom endpoint reachable \u2014 got {dims}-dim embedding"}
    except httpx.ConnectError as e:
        return {"success": False, "message": f"Could not connect to custom endpoint: {e}"}
    except Exception as e:
        return {"success": False, "message": f"Custom endpoint test failed: {e}"}


# ── Import / export, memory and wiki backup/restore ────────────────────────
# Lives in routers/backup.py, the machinery behind it in org_backup.py.
# Included here, where its routes used to be declared, so the route order
# (and with it which route matches) is unchanged.
app.include_router(backup_router.router)


# ── API Token management ────────────────────────────────────────────────────
# Lives in routers/tokens.py. Included here, where its routes used to be
# declared, so the route order (and with it which route matches) is unchanged.
app.include_router(tokens_router.router)


def _internal_wiki_writer(session: Session, body: dict) -> tuple:
    """Resolve an agent's write context: (token info, tenant, AI folder).

    The security boundary lives here rather than in the MCP layer, which is
    just a client — re-validate the bearer token, require the wiki_write
    scope, and let the caller check every path against the returned folder.
    """
    info, tenant = _resolve_bearer_token(session, (body or {}).get("token_hash", ""))
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_write"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_write scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")
    return info, tenant, _resolve_ai_wiki_folder(session, tenant.slug)


def _guard_ai_path(rel: str, folder: str, what: str = "path") -> str:
    """A promoted page is out of an agent's reach, permanently.

    Moving a page out of the AI folder would be self-promotion — the agent
    marking its own work human-reviewed — so this is checked on BOTH ends of a
    move, not just the source.
    """
    if not _is_within_ai_folder(rel, folder):
        raise HTTPException(
            status_code=403,
            detail=f"AI agents may only touch content under '{folder}/' "
                   f"({what}: '{rel}'). A page moved out of that folder has been "
                   f"promoted by a human and is no longer agent-writable.")
    return rel


def _repoint_revisions(session: Session, tenant, src: str, dst: str) -> None:
    """Carry a page's edit history across a move.

    Revisions are keyed by path, so without this a rename silently orphans the
    history — the page looks brand new at its destination and the old trail is
    unreachable. Best-effort: never fail the move over bookkeeping.
    """
    try:
        (session.query(WikiRevision)
         .filter_by(tenant_id=tenant.id, path=src)
         .update({WikiRevision.path: dst}, synchronize_session=False))
    except Exception:
        log.warning("could not repoint revisions %s → %s", src, dst, exc_info=True)


def _prepare_page_edit(tenant, rel: str, body: dict):
    """Compute a bounded edit without saving it: the stored bytes and the page
    as the edit leaves it. Shared by /edit and /edit/preview, so a preview is
    exactly what /edit would save."""
    if not _s3_exists(tenant, rel):
        raise HTTPException(
            status_code=404,
            detail=f"No page at '{rel}'. Use wiki_write to create it first.")

    raw = _s3_read(tenant, rel)
    after = _apply_page_edit(
        raw.decode("utf-8", errors="replace"),
        (body.get("mode") or "").strip(),
        heading=body.get("heading") or "",
        content=body.get("content") or "",
        old=body.get("old") or "",
    )
    return raw, after


@app.post("/api/internal/wiki/edit/preview")
async def internal_wiki_edit_preview(request: Request, session: Session = Depends(get_db_session)):
    """What /edit would save, without saving it.

    The MCP layer validates any Mermaid diagram an edit adds or changes before
    the edit lands, so it needs the page as the edit leaves it. It then sends
    the real /edit with `if_match` set to `sha256`, so a page that moved on in
    between is refused rather than saved unchecked. Same checks as /edit.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    _, tenant, folder = _internal_wiki_writer(session, body)
    rel = _guard_ai_path(_normalize_path((body or {}).get("path", "")), folder)
    _assert_wiki_unlocked(session, tenant.id, rel)
    raw, after = _prepare_page_edit(tenant, rel, body)
    before = raw.decode("utf-8", errors="replace")
    return {"path": rel, "changed": after != before, "before": before, "content": after,
            "sha256": hashlib.sha256(raw).hexdigest()}


@app.post("/api/internal/wiki/edit")
async def internal_wiki_edit(request: Request, session: Session = Depends(get_db_session)):
    """Apply a BOUNDED change to a page, instead of replacing it wholesale.

    The counterpart to /write. An agent changing one line of a long page had to
    regenerate the whole document, and a regenerated document quietly loses
    content. Here the untouched parts of the page are the same bytes afterwards
    — and because revisions store snapshots, the History diff then shows the
    real change rather than a whole-page replacement.

    Same boundary as /write: AI folder only, wiki_write scope required.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant, folder = _internal_wiki_writer(session, body)
    rel = _guard_ai_path(_normalize_path((body or {}).get("path", "")), folder)

    _assert_wiki_unlocked(session, tenant.id, rel)
    raw, after = _prepare_page_edit(tenant, rel, body)
    before = raw.decode("utf-8", errors="replace")
    want = body.get("if_match")
    if want is not None:
        if not isinstance(want, str) or not want:
            raise HTTPException(status_code=422, detail="if_match must be a sha256 hex string")
        if hashlib.sha256(raw).hexdigest() != want.lower():
            raise HTTPException(
                status_code=409,
                detail=f"'{rel}' changed since the edit was checked (version conflict); "
                       "try the edit again")
    if after == before:
        return {"status": "unchanged", "path": rel, "changed": False}

    _s3_write(tenant, rel, after.encode("utf-8"))
    _trigger_embedder_reindex(rel, org=tenant)
    _record_wiki_revision(session, tenant, rel, after.encode("utf-8"),
                          edited_by_id=info.get("user_id"),
                          edited_by_name=info.get("name"),
                          source=info.get("source") or "mcp")
    return {
        "status": "edited", "path": rel, "changed": True,
        "bytes_before": len(before), "bytes_after": len(after),
    }


@app.post("/api/internal/wiki/move")
async def internal_wiki_move(request: Request, session: Session = Depends(get_db_session)):
    """Move or rename a page or a whole folder, recursively.

    Without this, relocating anything meant writing it at the new path and
    abandoning the original — so the hierarchy an agent created was effectively
    immutable, and every reorganisation left strays for a human to sweep up.

    Both ends are checked against the AI folder: an agent can rearrange its own
    workspace and cannot promote its work out of it.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant, folder = _internal_wiki_writer(session, body)
    src = _guard_ai_path(_normalize_path((body or {}).get("from_path", "")), folder, "source")
    dst = _guard_ai_path(_normalize_path((body or {}).get("to_path", "")), folder, "destination")
    if src == dst:
        raise HTTPException(status_code=422, detail="Source and destination are the same")
    if dst.startswith(src + "/"):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot move '{src}' into itself ('{dst}')")

    # A single page, or a folder prefix? Ask the store rather than guess from
    # the presence of an extension — a page may legitimately have none.
    if _s3_exists(tenant, src):
        _assert_wiki_unlocked(session, tenant.id, src, "moved")
        _assert_wiki_unlocked(session, tenant.id, dst, "moved onto")
        if _s3_exists(tenant, dst):
            raise HTTPException(status_code=409, detail=f"'{dst}' already exists")
        _s3_write(tenant, dst, _s3_read(tenant, src))
        _s3_delete(tenant, src)
        _repoint_revisions(session, tenant, src, dst)
        session.commit()
        _trigger_embedder_reindex(src, org=tenant, deleted=True)
        _trigger_embedder_reindex(dst, org=tenant)
        return {"status": "moved", "kind": "page", "from": src, "to": dst, "moved": 1}

    src_prefix, dst_prefix = src + "/", dst + "/"
    _assert_wiki_prefix_unlocked(session, tenant.id, src, "moved")
    all_keys = list(_s3().walk(tenant.slug))
    src_keys = [k for k in all_keys if k.startswith(src_prefix)]
    if not src_keys:
        raise HTTPException(status_code=404, detail=f"No page or folder at '{src}'")
    if any(k.startswith(dst_prefix) for k in all_keys):
        raise HTTPException(status_code=409, detail=f"'{dst}' already exists")

    # S3 has no atomic rename: copy-then-delete per key. On a partial failure
    # the already-copied keys are gone from the source, so the operation is
    # replayable rather than lossy.
    moved = []
    for k in sorted(src_keys):
        new_key = dst_prefix + k[len(src_prefix):]
        _s3_write(tenant, new_key, _s3().read(tenant.slug, k))
        _s3_delete(tenant, k)
        _repoint_revisions(session, tenant, k, new_key)
        moved.append(new_key)
    session.commit()
    _trigger_embedder_reindex(org=tenant)
    return {"status": "moved", "kind": "folder", "from": src, "to": dst,
            "moved": len(moved), "paths": moved[:50]}


@app.post("/api/internal/wiki/delete")
async def internal_wiki_delete(request: Request, session: Session = Depends(get_db_session)):
    """Delete a page, or a folder and everything under it.

    Markdown pages go to the same recycle bin a human deletion uses, so an
    agent's mistake is recoverable for 30 days rather than final. Deleting a
    folder needs recursive=true — an agent should have to say it meant to take
    the contents with it.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant, folder = _internal_wiki_writer(session, body)
    rel = _guard_ai_path(_normalize_path((body or {}).get("path", "")), folder)
    recursive = bool((body or {}).get("recursive"))

    def _recycle(key: str) -> None:
        try:
            content = _s3().read(tenant.slug, key).decode("utf-8", errors="replace")
        except Exception:
            content = ""
        session.add(SoftDeletedPage(original_path=key, name=Path(key).name,
                                    content=content, tenant_id=tenant.id))

    if _s3_exists(tenant, rel):
        _assert_wiki_unlocked(session, tenant.id, rel, "deleted")
        if rel.lower().endswith((".md", ".markdown")):
            _recycle(rel)
        _s3_delete(tenant, rel)
        session.commit()
        _trigger_embedder_reindex(rel, org=tenant, deleted=True)
        return {"status": "deleted", "kind": "page", "path": rel, "deleted": 1,
                "recoverable": rel.lower().endswith((".md", ".markdown"))}

    prefix = rel + "/"
    _assert_wiki_prefix_unlocked(session, tenant.id, rel, "deleted")
    keys = [k for k in _s3().walk(tenant.slug) if k.startswith(prefix)]
    if not keys:
        raise HTTPException(status_code=404, detail=f"No page or folder at '{rel}'")
    if not recursive:
        raise HTTPException(
            status_code=409,
            detail=f"'{rel}' is a folder holding {len(keys)} item(s). Pass "
                   f"recursive=true to delete it and everything under it.")

    recycled = []
    for k in sorted(keys):
        if k.lower().endswith((".md", ".markdown")):
            _recycle(k)
            recycled.append(k)
        _s3_delete(tenant, k)
    session.commit()
    _trigger_embedder_reindex(org=tenant)
    return {"status": "deleted", "kind": "folder", "path": rel,
            "deleted": len(keys), "recycled": recycled[:50]}


@app.post("/api/internal/wiki/write")
async def internal_wiki_write(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint used by the MCP `wiki_write` tool to let an AI agent
    create/update a wiki page — constrained to the tenant's designated AI
    folder. Internal network only (never exposed through nginx).

    Security boundary is enforced HERE, not trusted from the MCP layer:
    re-validates the bearer token, requires the `wiki_write` scope, and rejects
    any path that isn't strictly under the tenant's AI folder. Path traversal
    is blocked by _normalize_path.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    token_hash = (body or {}).get("token_hash", "")
    raw_path   = (body or {}).get("path", "")
    content    = (body or {}).get("content")
    if content is None or not isinstance(content, str):
        raise HTTPException(status_code=422, detail="content (string) is required")

    info, tenant = _resolve_bearer_token(session, token_hash)
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_write"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_write scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    folder = _resolve_ai_wiki_folder(session, tenant.slug)
    rel = _normalize_path(raw_path)  # rejects .., absolute, NUL
    if not _is_within_ai_folder(rel, folder):
        raise HTTPException(
            status_code=403,
            detail=f"AI agents may only write under '{folder}/' (got '{rel}')",
        )

    _assert_wiki_unlocked(session, tenant.id, rel, "written")
    existed = _s3_exists(tenant, rel)
    _check_write_preconditions(tenant, rel, existed, body or {})
    _s3_write(tenant, rel, content.encode("utf-8"))
    _trigger_embedder_reindex(rel, org=tenant)
    _record_wiki_revision(session, tenant, rel, content.encode("utf-8"),
                          edited_by_id=info.get("user_id"),
                          edited_by_name=info.get("name"),
                          source=info.get("source") or "mcp",
                          created=not existed)
    return {"status": "saved", "path": rel, "ai_folder": folder, "created": not existed,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


def _check_write_preconditions(tenant, rel: str, existed: bool, body: dict) -> None:
    """Optional optimistic-concurrency checks on /write, for callers that
    revise a page they read earlier (the MCP diagram tools).

      if_none_match: true   the page must not exist yet (create, never clobber)
      if_match: <sha256>    the page must exist and still hold exactly the
                            bytes with this hash (patch the version you read)

    Both are refused with 409 rather than written, so a second writer is told
    the page moved on instead of having its change silently overwritten.
    Callers that send neither get the old last-write-wins behaviour.
    """
    if body.get("if_none_match") and existed:
        raise HTTPException(status_code=409, detail=f"'{rel}' already exists")
    want = body.get("if_match")
    if want is None:
        return
    if not isinstance(want, str) or not want:
        raise HTTPException(status_code=422, detail="if_match must be a sha256 hex string")
    if not existed:
        raise HTTPException(status_code=409, detail=f"'{rel}' does not exist any more")
    have = hashlib.sha256(_s3_read(tenant, rel)).hexdigest()
    if have != want.lower():
        raise HTTPException(
            status_code=409,
            detail=f"'{rel}' changed since you read it (version conflict); read it again")


# Leading bytes of each image type an agent may upload. Checked against the
# declared content type so a mislabelled file is refused at upload rather than
# stored and then rendered as a broken image (or served under the wrong type).
_IMAGE_MAGIC = {
    "image/png":  (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif":  (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


def _image_bytes_match(mime: str, data: bytes) -> bool:
    if not any(data.startswith(m) for m in _IMAGE_MAGIC.get(mime, ())):
        return False
    return mime != "image/webp" or data[8:12] == b"WEBP"


@app.post("/api/internal/wiki/upload-image")
async def internal_wiki_upload_image(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint for the MCP `wiki_upload_image` tool: store an image
    an agent supplies, so a page under the AI folder can embed it. Internal
    network only (never exposed through nginx).

    Same boundary as /api/internal/wiki/write: the token is re-validated, the
    `wiki_write` scope is required, and the path must sit under the tenant's
    AI folder. The types are the ones the UI's /api/upload-image accepts, and
    for the same reason SVG is not one of them: /api/image serves it from our
    own origin as a script-capable document.
    """
    import base64
    import binascii
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant, folder = _internal_wiki_writer(session, body)
    rel = _guard_ai_path(_normalize_path((body or {}).get("path", "")), folder)

    mime = str((body or {}).get("content_type") or "").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in wiki_router._IMAGE_EXT_BY_MIME:
        raise HTTPException(
            status_code=415,
            detail=(f"'{mime or '(none)'}' is not an image type the wiki accepts. "
                    "Use image/png, image/jpeg, image/gif or image/webp. SVG is "
                    "refused because it can carry script."))
    ext = Path(rel).suffix.lower()
    allowed = {wiki_router._IMAGE_EXT_BY_MIME[mime]} | ({".jpeg"} if mime == "image/jpeg" else set())
    if ext not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(f"'{rel}' does not end in {' or '.join(sorted(allowed))}, which "
                    f"{mime} needs. The extension decides the type the image is "
                    "served as, so the two have to agree."))

    raw = (body or {}).get("image_base64")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422, detail="image_base64 (string) is required")
    raw = raw.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]   # tolerate a data URI as well as bare base64
    try:
        data = base64.b64decode("".join(raw.split()), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64")
    if not data:
        raise HTTPException(status_code=422, detail="image_base64 decodes to nothing")
    limit = wiki_router.MAX_IMAGE_BYTES
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=(f"That image is {len(data) // (1024 * 1024)}MB. The limit is "
                    f"{limit // (1024 * 1024)}MB. Scale it down, or link to it."))
    if not _image_bytes_match(mime, data):
        raise HTTPException(
            status_code=422,
            detail=f"Those bytes are not a {mime} image. Check content_type.")

    _assert_wiki_unlocked(session, tenant.id, rel, "replaced")
    existed = _s3_exists(tenant, rel)
    _s3_write(tenant, rel, data)
    return {"status": "saved", "path": rel, "ai_folder": folder,
            "created": not existed, "bytes": len(data), "content_type": mime}


@app.post("/api/internal/wiki/read")
async def internal_wiki_read(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint used by the MCP `get_wiki_page` tool to fetch a wiki
    page in the tenant's space — any path in the tree, not just AI/. Internal
    network only (never exposed through nginx).

    Mirrors the write side: re-validates the bearer token, requires the
    `wiki_read` scope, resolves the tenant from the token, and reads from
    that tenant's S3 prefix. The path is space-relative; tenant resolution
    is the token's job, not the caller's.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    token_hash = (body or {}).get("token_hash", "")
    raw_path   = (body or {}).get("path", "")

    info, tenant = _resolve_bearer_token(session, token_hash)
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_read"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_read scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    # `page.md#heading` names one section. The fragment can come on the path
    # (how links are written) or as `anchor`; either way the page is read whole
    # and the section cut from it, so a renamed heading fails soft to the page.
    anchor = str((body or {}).get("anchor") or "")
    if "#" in (raw_path or ""):
        raw_path, _, frag = raw_path.partition("#")
        anchor = anchor or frag

    rel = _normalize_path(raw_path)  # rejects .., absolute, NUL
    data = _s3_read(tenant, rel)     # raises HTTPException(404) on miss
    lock = _wiki_lock_for(session, tenant.id, rel)
    content = data.decode("utf-8", errors="replace")
    out = {"path": rel, "content": content,
           # The hash of the stored bytes: what /write's `if_match` compares
           # against, so a caller can revise exactly the version it read.
           "sha256": hashlib.sha256(data).hexdigest(),
           # Reading a locked page is fine — say so, so an agent learns the
           # page is frozen from the read rather than from a 423 on its next write.
           "lock": lock.to_dict() if lock else {"locked": False},
           "headings": [{"level": lvl, "text": t, "anchor": a}
                        for _i, lvl, t, a in _heading_outline(content.splitlines())]}
    if anchor.strip().lstrip("#").strip():
        out["anchor"] = anchor
        out["section"] = _page_section(content, anchor)
    return out


@app.post("/api/internal/wiki/history")
async def internal_wiki_history(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint for the MCP `wiki_history` tool: list a wiki page's
    edit history in the token's tenant. Requires `wiki_read`; any path (not
    just AI/). Content is omitted from the list — use /wiki/revision to fetch
    a specific version. Internal network only (never exposed through nginx)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant = _resolve_bearer_token(session, (body or {}).get("token_hash", ""))
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_read"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_read scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    rel = _normalize_path((body or {}).get("path", ""))
    rows = _wiki_revisions_for(session, tenant.id, rel, (body or {}).get("limit", 50))
    return {
        "path": rel,
        "count": len(rows),
        "revisions": [r.to_dict(include_content=False) for r in rows],
    }


@app.post("/api/internal/wiki/revision")
async def internal_wiki_revision(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint for the MCP `wiki_revision` / `wiki_diff` tools: fetch
    the full content of one historical revision. Requires `wiki_read`. The
    revision is scoped to the token's tenant and the given path, so an id from
    another tenant/page can't be read. Internal network only."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, tenant = _resolve_bearer_token(session, (body or {}).get("token_hash", ""))
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_read"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_read scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    rel = _normalize_path((body or {}).get("path", ""))
    row = (session.query(WikiRevision)
           .filter_by(id=(body or {}).get("revision_id"), tenant_id=tenant.id, path=rel)
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return {"path": rel, "revision": row.to_dict(include_content=True)}


@app.post("/api/internal/wiki/revert")
async def internal_wiki_revert(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint for the MCP `wiki_revert` tool: restore a wiki page to
    a previous revision's content. Requires `wiki_write` AND — like
    wiki_write — the page must sit inside the tenant's AI folder (agents can't
    rewrite human-promoted pages). Non-destructive: the restored content is
    written forward as a NEW revision, so history stays append-only. Internal
    network only."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    revision_id = (body or {}).get("revision_id")
    info, tenant = _resolve_bearer_token(session, (body or {}).get("token_hash", ""))
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_write"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_write scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    folder = _resolve_ai_wiki_folder(session, tenant.slug)
    rel = _normalize_path((body or {}).get("path", ""))
    if not _is_within_ai_folder(rel, folder):
        raise HTTPException(
            status_code=403,
            detail=f"AI agents may only revert pages under '{folder}/' (got '{rel}')",
        )

    _assert_wiki_unlocked(session, tenant.id, rel, "reverted")
    row = (session.query(WikiRevision)
           .filter_by(id=revision_id, tenant_id=tenant.id, path=rel)
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="Revision not found")

    content_bytes = (row.content or "").encode("utf-8")
    _s3_write(tenant, rel, content_bytes)
    _trigger_embedder_reindex(rel, org=tenant)
    _record_wiki_revision(
        session, tenant, rel, content_bytes,
        edited_by_id=info.get("user_id"),
        edited_by_name=info.get("name"),
        source=info.get("source") or "mcp",
        message=f"Reverted to revision {revision_id}",
        created=False,
    )
    return {"status": "reverted", "path": rel, "revision_id": revision_id,
            "restored_from": row.edited_at.isoformat() if row.edited_at else None}


@app.post("/api/internal/wiki/tree")
async def internal_wiki_tree(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint used by the MCP `wiki_tree` tool to enumerate every
    wiki page in the token's tenant, optionally scoped to a path prefix.
    Internal network only.

    Mirrors the read/write internal endpoints: re-validates the bearer token,
    requires the `wiki_read` scope, resolves the tenant from the token, and
    lists from that tenant's S3 prefix. Paths returned are space-relative.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    token_hash = (body or {}).get("token_hash", "")
    raw_prefix = ((body or {}).get("prefix") or "").strip()

    info, tenant = _resolve_bearer_token(session, token_hash)
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_read"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_read scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    # Empty prefix = walk the whole tenant. Non-empty goes through the same
    # traversal-rejecting normaliser as read/write.
    prefix = _normalize_path(raw_prefix.rstrip("/")) if raw_prefix else ""
    paths = sorted(_s3().walk(tenant.slug, prefix))
    return {"paths": paths, "count": len(paths)}


@app.post("/api/internal/wiki/image")
async def internal_wiki_image(request: Request, session: Session = Depends(get_db_session)):
    """Internal endpoint used by the MCP `get_wiki_page` tool to fetch a binary
    asset (image) referenced from a wiki page in the token's tenant. Internal
    network only.

    Mirrors /api/internal/wiki/read: re-validates the bearer token, requires
    the `wiki_read` scope, resolves the tenant from the token, and serves the
    asset from that tenant's S3 prefix.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    token_hash = (body or {}).get("token_hash", "")
    raw_path   = (body or {}).get("path", "")

    info, tenant = _resolve_bearer_token(session, token_hash)
    if not info or not info.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not info.get("wiki_read"):
        raise HTTPException(status_code=403, detail="Token lacks the wiki_read scope")
    if tenant is None:
        raise HTTPException(status_code=400, detail="Token has no tenant context")

    rel  = _normalize_path(raw_path)
    data = _s3_read(tenant, rel)
    # Same table the browser routes use: the MCP side puts this type in a
    # data: URI, so a webp must say image/webp here too.
    return Response(content=data, media_type=wiki_router.asset_mime(rel))


# ── Local auth (session-based login) ───────────────────────────────────────

# Wire auth's RequiresLogin into FastAPI's exception handler
@app.exception_handler(RequiresLogin)
async def _login_redirect_handler(request: Request, exc: RequiresLogin):
    return RedirectResponse(
        url=f"/login?next={_urlquote(exc.next_url, safe='')}",
        status_code=302,
    )

# Legacy alias — many existing routes use Depends(require_admin).  org_admin
# is the new canonical role with the same intent.
require_admin = require_org_admin


# ── Git knowledge sources ───────────────────────────────────────────────────
# Lives in routers/git_sources.py, the helpers behind it in git_sources.py.
# Included here, where its routes used to be declared, so the route order
# (and with it which route matches) is unchanged.
app.include_router(git_sources_router.router)


# Paths a bearer token may unlock through the nginx auth_request gateway.
# Deliberately narrow: the session cookie stays the only way to reach every
# other web route, so a leaked graph-visualization token can't read the wiki
# or mutate memories through the browser-facing API. The board stream is here
# so a service can hear card moves live; it carries no card content.
_BEARER_AUTH_PATHS = ("/api/memories/graph", "/api/board/stream")


def _bearer_auth_check(request: Request, session: Session) -> Response:
    """Fallback for auth_check when no session cookie is present: accept a
    graph-scoped bearer token, but only for _BEARER_AUTH_PATHS. Lets the
    standalone /graph page (and anything embedding it) call
    GET /api/memories/graph with `Authorization: Bearer <token>`.

    Identity headers are derived from the token's owning user so the embedder
    applies exactly the same memory-visibility ACL the user gets in the UI;
    tokens without a linked user are rejected because there is no scope to
    evaluate against."""
    deny = Response(status_code=401, headers={"Cache-Control": "no-store"})

    original_path = (request.headers.get("X-Original-URI") or "").split("?", 1)[0]
    if original_path not in _BEARER_AUTH_PATHS:
        return deny

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return deny
    raw = auth_header[7:].strip()
    if not raw:
        return deny

    if original_path == "/api/board/stream":
        # The route re-checks the token itself; this only decides whether
        # nginx lets the request reach it. A 403 passes through auth_request
        # as a 403, so a token without board_read is told why.
        status, info = _board_stream_token(session, raw)
        if status != 200:
            return Response(status_code=status, headers={"Cache-Control": "no-store"})
        return Response(status_code=200, headers={
            "X-Auth-User":     info.get("name") or "",
            "X-Auth-User-Id":  info.get("user_id") or "",
            "X-Auth-Org-Id":   info.get("tenant_id") or "",
            "X-Auth-Org-Slug": info.get("tenant_slug") or "",
            "X-Auth-Role":     "user",
            "Cache-Control":   "no-store",
        })

    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    info, _tenant = _resolve_bearer_token(session, token_hash)
    if not info or not info.get("valid") or not info.get("graph_read"):
        return deny

    user = None
    if info.get("user_id"):
        user = session.query(UserAccount).filter_by(id=info["user_id"]).first()
    if not user or not user.is_active:
        return deny

    return Response(status_code=200, headers={
        "X-Auth-User":     user.username or "",
        "X-Auth-User-Id":  user.id,
        "X-Auth-Org-Id":   info.get("tenant_id") or "",
        "X-Auth-Org-Slug": info.get("tenant_slug") or "",
        "X-Auth-Role":     user.role or "user",
        "Cache-Control":   "no-store",
    })


@app.get("/api/auth/check")
def auth_check(request: Request, session: Session = Depends(get_db_session)):
    """
    nginx auth_request target. Returns 200 + identity headers if the session
    cookie is valid AND the user's org is active (or the user is a platform
    admin), 401 otherwise.  Never redirects.

    Forwards X-Auth-Org-Slug downstream so the embedder can route memory /
    wiki / search requests to the correct per-tenant Qdrant collection.
    Without this header, UI reads land in the global "default" collection
    while restore writes (which carry an explicit tenant_slug) land in the
    per-org collection — the user-visible symptom is empty Memories.

    Re-checks the UserAccount row on every request — the session JWT is
    stateless and lives for 24h, but role demotions, org reassignments,
    and offboarding all need to take effect immediately, not the next
    morning. Previously a demoted platform_admin kept X-Auth-Role=
    platform_admin until their cookie expired, giving them a full day of
    leftover privilege.
    """
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        return _bearer_auth_check(request, session)

    user, org = _live_session_user(claims, session)
    if user is None:
        return Response(status_code=401, headers={"Cache-Control": "no-store"})

    return Response(status_code=200, headers={
        # Identity is sourced from the DB row, not the cookie's claims, so
        # any role/org change since the cookie was minted takes effect on
        # the very next request.
        "X-Auth-User":     user.full_username or user.username or "",
        "X-Auth-User-Id":  user.id,
        "X-Auth-Org-Id":   user.org_id or "",
        "X-Auth-Org-Slug": (org.slug if org else "") or "",
        "X-Auth-Role":     user.role or "user",
        "Cache-Control":   "no-store",
    })


@app.get("/api/admin/whoami")
def whoami(request: Request, session: Session = Depends(get_db_session)):
    """Return the current session user's identity and role."""
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        return {"authenticated": False}
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    # Team_admin callers need to know which teams they administer so the UI
    # can scope team pickers and visibility filters client-side.  We compute
    # this for every authenticated user (cheap, single indexed query).
    admin_team_ids = []
    if claims.get("uid"):
        admin_team_ids = [
            m.team_id for m in session.query(TeamMembership).filter_by(
                user_id=claims["uid"], role="team_admin"
            ).all()
        ]
    return {
        "authenticated":          True,
        "user_id":                claims["uid"],
        "username":               claims.get("sub"),
        "display_name":           user.display_name if user else None,
        "email":                  user.email if user else None,
        "org_id":                 claims.get("oid"),
        "role":                   claims.get("role"),
        "team_admin_team_ids":    admin_team_ids,
        "mfa_enabled":            bool(session.query(MfaCredential).filter_by(
                                     user_id=claims["uid"], is_enabled=True,
                                 ).first()) if claims.get("uid") else False,
        # UI colour scheme, so the SPA can adopt the account's saved preference
        # on load without a second round trip.
        "theme":                  (user.theme if user else None) or "dark",
        "palette":                (user.palette if user else None) or "indigo",
    }


# ── Local auth: setup, login, logout, password change, MFA, invites ───────
# Lives in routers/local_auth.py. Included here, where its routes used to be
# declared, so the route order (and with it which route matches) is unchanged.
app.include_router(local_auth_router.router)


# ── OAuth 2.0 Authorization Server ─────────────────────────────────────────
# Lives in routers/oauth.py. Included here, where its routes used to be
# declared, so the app's route order is unchanged.
app.include_router(oauth_router.router)


# ── Legacy /api/admin/tenants — gone, answers 410 ─────────────────────────
# Lives in routers/admin_platform.py, included where the route used to be
# declared.
app.include_router(admin_platform_router.legacy_router)


# ── OIDC / SAML: provider management and login flows ──────────────────────
# Lives in routers/sso.py, included where its routes used to be declared.
app.include_router(sso_router.router)


# ── Org admin: users, teams, groups, memory sharing, wiki and git ACLs ────
# Lives in routers/org_admin.py. Included here, where its routes used to be
# declared, so the route order (and with it which route matches) is unchanged.
app.include_router(org_admin_router.router)


# ── Internal endpoints used by the embedder to enforce ACLs ────────────────

@app.get("/api/internal/wiki-spaces")
def internal_wiki_spaces(
    user_id: str = Query(...),
    org_id: str  = Query(...),
    session: Session = Depends(get_db_session),
):
    """Returns the set of wiki space names the user can read in their org."""
    org = session.query(Tenant).filter_by(id=org_id).first()
    spaces = _org_spaces_from_s3(org) if org else set()
    user = session.query(UserAccount).filter_by(id=user_id).first()
    if user and user.role in ("platform_admin", "org_admin"):
        return {"spaces": sorted(spaces)}
    allowed = filter_readable_spaces(session, user_id, org_id, spaces)
    return {"spaces": sorted(allowed)}


# The git source ACL and listing endpoints live in routers/git_sources.py,
# on a second router so they can be included here, where they used to be.
app.include_router(git_sources_router.internal_router)


@app.get("/api/internal/memory-scope")
def internal_memory_scope(
    user_id: str = Query(...),
    org_id: str  = Query(...),
    session: Session = Depends(get_db_session),
):
    """Returns the user's effective memory visibility (individual or shared)."""
    scope = memory_visibility_filter(session, user_id, org_id)
    return {
        "share_mode":     scope["share_mode"],
        "scope_user_ids": sorted(scope["scope_user_ids"]),
    }


# ── Tenant settings, memory edit notifications, org setup wizard ──────────
# Lives in routers/org_admin.py too, on a second router so it can be
# included here, where these routes used to be declared.
app.include_router(org_admin_router.settings_router)


# ── User self-service: profile, password, MFA, my tokens ──────────────────

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, session: Session = Depends(get_db_session)):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        return RedirectResponse("/login?next=/profile", status_code=302)
    return HTMLResponse(_render(_STATIC / "profile.html", {
        "<!-- APP_NAME -->": APP_NAME,
    }))


@app.get("/api/me")
def me(request: Request, session: Session = Depends(get_db_session)):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    mfa = session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first()
    return {**user.to_dict(), "mfa_enabled": bool(mfa)}


@app.patch("/api/me")
def update_me(
    body: dict,
    request: Request,
    session: Session = Depends(get_db_session),
):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if "display_name" in body:
        user.display_name = (body["display_name"] or "").strip() or user.username
    if "email" in body:
        user.email = (body["email"] or "").strip() or None
    for field in ("firstname", "lastname", "title", "department", "phone"):
        if field in body:
            setattr(user, field, (body[field] or "").strip() or None)
    if "theme" in body:
        theme = (body["theme"] or "").strip().lower()
        if theme not in ("dark", "light", "auto"):
            raise HTTPException(status_code=400,
                                detail="theme must be one of: dark, light, auto")
        user.theme = theme
    if "palette" in body:
        from appearance import PALETTE_IDS
        palette = (body["palette"] or "").strip().lower()
        if palette not in PALETTE_IDS:
            raise HTTPException(status_code=400,
                                detail="palette must be one of: " + ", ".join(sorted(PALETTE_IDS)))
        user.palette = palette
    session.commit()
    return user.to_dict()


@app.post("/api/me/password")
def change_my_password(
    body: dict,
    request: Request,
    session: Session = Depends(get_db_session),
):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if not user or user.auth_method != "local":
        raise HTTPException(status_code=400, detail="Local-auth users only")
    current = body.get("current_password") or ""
    new_pw  = body.get("new_password") or ""
    if not verify_password(current, user.password_hash or ""):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    if password_reset_blocked(user, session):
        raise HTTPException(status_code=409,
            detail="Cannot reset password within 1 hour of an MFA reset")
    user.password_hash = hash_password(new_pw)
    user.last_password_reset_at = datetime.utcnow()
    bump_session_epoch(user)   # SEC-04: a password change logs out other sessions
    session.commit()
    return {"ok": True}


@app.post("/api/me/logout-all")
def logout_all_my_sessions(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """End every session for this account, on every device, right now.

    Ordinary logout only clears the cookie in front of it (see `/logout`), which
    is the right default but can't help you when a laptop is stolen or you left
    yourself signed in somewhere you can't reach. Bumping the session epoch
    makes every session JWT issued so far fail `/api/auth/check` on its next
    request — including this one, so the caller is signed out here too and the
    UI sends them to the login page.

    Deliberately not gated on re-entering the password: someone who has walked
    away from an unlocked machine needs this to be one click, and the worst a
    passer-by can do with it is sign the owner out.
    """
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    bump_session_epoch(user)
    session.commit()
    log.info("logout-all: every session revoked for user=%s", user.username)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


@app.post("/api/me/mfa/disable")
def disable_my_mfa(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """User-initiated MFA disable.  Only allowed when org policy is 'optional'."""
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.role in ("platform_admin", "org_admin", "team_admin"):
        raise HTTPException(status_code=403, detail="Admins cannot disable their own MFA")
    org = session.query(Tenant).filter_by(id=user.org_id).first() if user.org_id else None
    if org and org.mfa_policy == "enforced":
        raise HTTPException(status_code=403,
            detail="Org policy requires MFA — contact your org admin")
    if mfa_reset_blocked(user):
        raise HTTPException(status_code=409,
            detail="Cannot reset MFA within 1 hour of a password reset")
    cred = session.query(MfaCredential).filter_by(user_id=user.id).first()
    if cred:
        cred.is_enabled = False
        cred.last_reset_at = datetime.utcnow()
        session.commit()
    return {"ok": True}


# The user's own API tokens and OAuth sessions live in routers/tokens.py too,
# on a second router so they can be included here, where they used to be
# declared.
app.include_router(tokens_router.me_router)


# ── Platform admin: orgs, org lifecycle, platform domains, admins ─────────
# Lives in routers/admin_platform.py, with its two startup hooks
# (bootstrap_platform_admin, _auto_purge_expired_recycle_bin). Included
# here, where they used to be declared, so the route order and the startup
# hook order are unchanged. The hooks are registered on the app, not the
# router: FastAPI 0.142 runs an included router's own hooks twice.
app.include_router(admin_platform_router.router)
app.router.add_event_handler("startup", admin_platform_router.bootstrap_platform_admin)
app.router.add_event_handler("startup", admin_platform_router._auto_purge_expired_recycle_bin)


# ── Platform org backup / restore, supervised org-restore jobs ─────────────
# Lives in routers/backup.py too, on a second router so it can be included
# here, where these routes used to be declared.
app.include_router(backup_router.platform_router)


# ── Org-scoped domains: CRUD, CNAME verification, legacy endpoints ────────
# Lives in routers/admin_platform.py; the helpers behind it are in orgs.py.
app.include_router(admin_platform_router.domains_router)


# ── Auth state cleanup (startup) ─────────────────────────────────────────────

@app.on_event("startup")
def purge_expired_auth_states():
    """Remove expired OIDC states and SAML request IDs on startup."""
    try:
        from config_db import SessionLocal as _SL, _init_engine
        _init_engine()
        with _SL() as session:
            now = datetime.utcnow()
            session.query(OidcState).filter(OidcState.expires_at < now).delete()
            session.query(SamlRequestId).filter(SamlRequestId.expires_at < now).delete()
            session.commit()
    except Exception as e:
        log.warning("Could not purge expired auth states: %s", e)


@app.on_event("startup")
def settle_interrupted_restores():
    """A restore thread does not survive a restart. Close out anything the DB
    still calls active, and bin bundles staged for a confirmation that never
    came — a full org backup is the most sensitive file this platform holds."""
    try:
        from config_db import _init_engine
        _init_engine()
        restore_jobs.registry.reap_orphans()
        swept = restore_jobs.sweep_staging(_RESTORE_STAGING_DIR)
        if swept:
            log.info("Swept %d abandoned staged restore bundle(s)", swept)
    except Exception as exc:
        log.warning("Could not settle interrupted restore jobs: %s", exc)


@app.on_event("startup")
def start_auto_backup_scheduler():
    """Start the auto-backup scheduler threads (memory + wiki)."""
    import threading
    threading.Thread(target=_run_auto_backup_loop, daemon=True).start()
    threading.Thread(target=_run_wiki_backup_loop, daemon=True).start()
    threading.Thread(target=_run_org_backup_loop, daemon=True).start()
    log.info("Memory, wiki, and org auto-backup schedulers started")


# ── Static (editor UI) ─────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
