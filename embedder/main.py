"""
Embedder service
─────────────────
• Watches /docs for .md file changes via watchdog.
• Parses frontmatter (tags, title) and body with python-frontmatter.
• Chunks content by markdown headings.
• Embeds chunks with sentence-transformers (default) or OpenAI-compatible API.
• Upserts/deletes vectors in Qdrant, storing path + tags as payload
  so agents can filter by tag.
• Exposes a FastAPI semantic-search endpoint.
• Embedding backend is reloaded from the editor config at every full reindex
  so that Settings changes take effect without a container restart.
• Exposes Memory CRUD endpoints (/api/memories).
"""
import os

# ── CPU embedding thread pinning — MUST run before numpy/torch import ────────
# Unpinned, sentence-transformers/torch and the BLAS libraries fan every CPU
# encode across all cores and, on this ARM build, accumulate worker threads per
# call. A long org restore piled up ~3k threads, pegged every core, and slowed a
# ~150ms single-sentence encode to 5–11s while memory crept toward the cap. Cap
# the math-lib thread pools to a small fixed size — numpy/torch/BLAS read these
# at import time, so this block must precede those imports. Tunable via
# EMBED_CPU_THREADS; an explicit OMP_NUM_THREADS (etc.) already in the env wins.
_EMBED_CPU_THREADS = os.environ.setdefault("EMBED_CPU_THREADS", "2")
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, _EMBED_CPU_THREADS)

import collections
import concurrent.futures
import datetime
import hashlib
import hmac
import logging
import os
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import timezone
from pathlib import Path
from typing import Annotated, Callable, List, Optional

import frontmatter
import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


def _error_ref() -> str:
    """A short correlation id: shown to the caller, logged beside the detail."""
    return secrets.token_hex(4)


def _internal_http_error(status: int, what: str, exc: BaseException) -> HTTPException:
    """An HTTPException that says what failed without saying why.

    Raw exception text carries Qdrant collection names, service hostnames and
    backend internals. It goes to the log under a short ref; the response
    carries the ref, so an operator can find the detail from what a user saw.
    """
    ref = _error_ref()
    log.error("%s [ref %s]: %r", what, ref, exc)
    return HTTPException(status_code=status, detail=f"{what} (ref {ref})")

# ── In-memory log buffer + SSE subscriber registry ─────────────────────────
_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOG_BUFFER_LOCK = threading.Lock()
_LOG_SUBSCRIBERS: list = []          # list[queue.Queue]
_LOG_SUBS_LOCK   = threading.Lock()

class _BroadcastHandler(logging.Handler):
    """Captures every log record into the ring buffer and fans out to SSE subscribers."""
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(line)
        dead = []
        with _LOG_SUBS_LOCK:
            for q in _LOG_SUBSCRIBERS:
                try:
                    q.put_nowait(line)
                except Exception:
                    dead.append(q)
            for q in dead:
                _LOG_SUBSCRIBERS.remove(q)

_bcast = _BroadcastHandler()
_bcast.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logging.getLogger().addHandler(_bcast)

# ── Static config ──────────────────────────────────────────────────────────
DOCS_DIR            = Path(os.environ.get("DOCS_DIR", "/docs")).resolve()
QDRANT_HOST         = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT         = int(os.environ.get("QDRANT_PORT", "6333"))
COLLECTION_NAME     = os.environ.get("COLLECTION_NAME", "knowledge_base")
MEMORIES_COLLECTION = "memories"
# memory-api owns the hot-tier (Postgres) capture + lexical-search path.
# The embedder calls into it for short-term recall — captures become
# retrievable the moment they land, without waiting for the compactor.
MEMORY_API_URL      = os.environ.get("MEMORY_API_URL", "http://memory-api:8002")

# Git sources are lexical-indexed in Postgres (memory-api /git/index) by
# default; dense vector embedding of repo files is opt-in. Embedding code is
# expensive, churns on every vector-dim change, and naive line-window chunks
# make weak vectors — lexical + agentic retrieval (git_search/git_read) covers
# the dominant symbol/string lookups. Flip GIT_SOURCE_EMBED=true to also embed.
GIT_SOURCE_EMBED    = os.environ.get("GIT_SOURCE_EMBED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)


# ── S3 client (read-only, for tenant-scoped doc counts) ────────────────────
# The embedder still watches /docs for changes in Phase 2 (Phase 3 cuts it
# over to S3 entirely). Until then, the *count* of authoritative documents
# is sourced from S3 — the /docs filesystem is a write-only mirror and
# can lag behind for content the editor seeded directly (e.g. welcome.md).
_s3_client_singleton = None
_s3_client_lock = threading.Lock()


def _get_s3_client():
    global _s3_client_singleton
    if _s3_client_singleton is not None:
        return _s3_client_singleton
    access_key = os.environ.get("ELEFANT_S3_ACCESS_KEY", "")
    secret_key = os.environ.get("ELEFANT_S3_SECRET_KEY", "")
    if not access_key or not secret_key:
        return None
    with _s3_client_lock:
        if _s3_client_singleton is not None:
            return _s3_client_singleton
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            log.warning("boto3 not installed; S3 doc counts unavailable")
            return None
        kwargs: dict = {
            "aws_access_key_id":     access_key,
            "aws_secret_access_key": secret_key,
            "region_name":           os.environ.get("ELEFANT_S3_REGION", "us-east-1"),
            "config": Config(
                s3      = {"addressing_style": "path"},
                retries = {"max_attempts": 2, "mode": "standard"},
            ),
        }
        endpoint_url = os.environ.get("ELEFANT_S3_ENDPOINT_URL") or None
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        _s3_client_singleton = boto3.client("s3", **kwargs)
    return _s3_client_singleton


def _bucket_for(slug: str) -> str:
    """Mirror the editor's bucket naming: `{prefix}{slug}--default`."""
    prefix = os.environ.get("ELEFANT_S3_BUCKET_PREFIX", "")
    return f"{prefix}{slug}--default"


def _s3_doc_count(slug: str) -> int:
    """Count .md objects in the org's wiki bucket. Returns 0 on any
    error (bucket missing, MinIO down, etc.) — non-fatal for the
    status panel."""
    client = _get_s3_client()
    if client is None:
        return 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        return sum(
            1
            for page in paginator.paginate(Bucket=_bucket_for(slug))
            for obj in (page.get("Contents") or [])
            if (obj.get("Key") or "").endswith(".md")
        )
    except Exception:
        return 0


def _s3_aggregate_doc_count() -> int:
    """Sum .md counts across every `<prefix><slug>--default` bucket.
    Used by the platform-admin aggregate view."""
    client = _get_s3_client()
    if client is None:
        return 0
    prefix = os.environ.get("ELEFANT_S3_BUCKET_PREFIX", "")
    try:
        buckets = client.list_buckets().get("Buckets") or []
    except Exception:
        return 0
    total = 0
    for b in buckets:
        name = b.get("Name", "")
        if not name.endswith("--default"):
            continue
        if prefix and not name.startswith(prefix):
            continue
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=name):
                for obj in (page.get("Contents") or []):
                    if (obj.get("Key") or "").endswith(".md"):
                        total += 1
        except Exception:
            continue
    return total
# Set MULTI_TENANT_DOCS=true only when /docs is structured as /docs/{tenant-slug}/...
# In single-tenant deployments wiki content lives flat under /docs/ and all files
# belong to the default collection regardless of their subdirectory.
_MULTI_TENANT_DOCS  = os.environ.get("MULTI_TENANT_DOCS", "").lower() in ("1", "true", "yes")
EDITOR_URL          = os.environ.get("EDITOR_URL", "http://editor:8080")

# Shared secret used between Elefant's internal services (memory-api
# compactor → embedder, MCP server → memory-api, etc.) to prove that a
# request comes from a peer service rather than from an end user. End-user
# requests are authenticated by the nginx auth_request flow that sets the
# X-Auth-* headers; internal-service requests carry X-Internal-Service.
# Set to a high-entropy random value (e.g. `openssl rand -hex 32`) and
# share it across every Elefant container's environment.
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
if not INTERNAL_SERVICE_TOKEN:
    log.critical(
        "INTERNAL_SERVICE_TOKEN is not set — internal-service requests "
        "(compactor promotions, cross-service writes) will be rejected. "
        "Set INTERNAL_SERVICE_TOKEN in your .env (run: openssl rand -hex 32) "
        "and restart embedder + memory-api + mcp + editor."
    )


def _internal_service_headers() -> dict:
    """Headers proving this is a peer Elefant service when calling another
    internal service (memory-api). Empty dict when not configured — the
    peer will reject the call (correct fail-closed behaviour)."""
    if not INTERNAL_SERVICE_TOKEN:
        return {}
    return {"X-Internal-Service": INTERNAL_SERVICE_TOKEN}


def _request_is_internal_service(request: "Request") -> bool:
    """True iff the inbound request presented a valid X-Internal-Service
    token. Used to gate endpoints that can only legitimately be called by
    peer services (editor admin operations, memory-api compactor)."""
    if not INTERNAL_SERVICE_TOKEN:
        return False
    presented = (request.headers.get("x-internal-service") or "").strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, INTERNAL_SERVICE_TOKEN)

# The bundled containerized embedder is pinned to a single sentence-transformers
# model. Letting the user pick any model risks OOM on memory-constrained homelab
# hosts (a 768-d default like all-mpnet-base-v2 needs ~1.5 GB to encode and won't
# fit on a 4 GB ARM SBC running the full stack). External providers — cloud or
# self-hosted OpenAI-compatible — are unaffected.
LOCKED_LOCAL_MODEL  = "multi-qa-distilbert-cos-v1"
# The model is also pinned to one Hub commit (#422). Without a revision,
# huggingface_hub asked huggingface.co for a newer copy on every start, so a
# new upload by the publisher would silently change the weights behind the
# vectors. Bumping this is a model change: the revision is in the skip stamp
# and the sidecar, so the next startup re-embeds the wiki once.
LOCKED_LOCAL_MODEL_REPO     = f"sentence-transformers/{LOCKED_LOCAL_MODEL}"
LOCKED_LOCAL_MODEL_REVISION = "bc2339dca6eba2b116cc7c2cfa06f63e4ff87e3f"
# The revision the unpinned loader resolved to before #422 (Hub `main` has not
# moved since 2024-11-05). Chunks stamped without a revision were embedded by
# this commit, so they still count as current while the pin names it. Bumping
# LOCKED_LOCAL_MODEL_REVISION ends the alias by itself.
_UNPINNED_STAMP_REVISION    = "bc2339dca6eba2b116cc7c2cfa06f63e4ff87e3f"

# Sidecar that records {provider, model, vector_size, applied_at} per Qdrant
# collection. Lives in /backend_sync (mounted RW in editor + embedder) so the
# editor can read it for drift checks without round-tripping through the embedder.
EMBEDDING_STATE_FILE = Path(
    os.environ.get("BACKEND_READINESS_FILE", "/tmp/backend_ready")
).parent / "embedding_state.json"
EMBEDDING_STATE_SCHEMA_VERSION = 1


def _wiki_coll(tenant_slug: str = "default") -> str:
    """Return the Qdrant wiki collection name for the given tenant.

    Wiki content is only routed to per-tenant collections when /docs is
    physically structured as /docs/{slug}/... (i.e. MULTI_TENANT_DOCS=true).
    Otherwise all wiki content lives in the global default collection and
    queries must hit the same collection — per-tenant routing here would
    target an empty `{slug}_wiki` collection and break agent searches.
    """
    if not _MULTI_TENANT_DOCS:
        return COLLECTION_NAME
    if tenant_slug in ("default", "", None):
        return COLLECTION_NAME
    _ensure_tenant_collections_lazy(tenant_slug)
    return f"{tenant_slug}_wiki"


def _mem_coll(tenant_slug: str = "default") -> str:
    """Return the Qdrant memories collection name for the given tenant.

    Unlike wiki, memories are always per-tenant (one Qdrant collection per
    org) — there's no shared-with-ACL fallback because memories carry no
    space concept.
    """
    if tenant_slug in ("default", "", None):
        return MEMORIES_COLLECTION
    _ensure_tenant_collections_lazy(tenant_slug)
    return f"{tenant_slug}_memories"


# Cache of (collection_name -> stored_vector_size) so we can detect a
# dim-mismatch up front without hitting Qdrant on every search.
_coll_dim_cache: dict[str, int] = {}
_coll_dim_lock = threading.Lock()


def _memory_coll_dim_ok(coll_name: str) -> tuple[bool, Optional[int]]:
    """Return (ok, stored_dim). ok=False means current embedding dim differs
    from the collection's stored dim — Qdrant will 400 on every search until
    the operator either restores the prior model or forces a recreate via
    EMBEDDER_FORCE_COLLECTION_RECREATE=memories."""
    with _embed_lock:
        cur = _vector_size
    stored = _coll_dim_cache.get(coll_name)
    if stored is None:
        try:
            info = qdrant().get_collection(coll_name)
            stored = info.config.params.vectors.size
        except Exception:
            return True, None  # collection doesn't exist yet — let normal path handle it
        with _coll_dim_lock:
            _coll_dim_cache[coll_name] = stored
    return stored == cur, stored


def _qdrant_error_detail(exc: Exception) -> str:
    """Flatten an UnexpectedResponse into a single-line message including the
    response body, so it survives single-line log handlers."""
    body = getattr(exc, "content", None)
    status = getattr(exc, "status_code", None)
    reason = getattr(exc, "reason_phrase", "")
    if status is None:
        return str(exc)
    try:
        body_str = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    except Exception:
        body_str = repr(body)
    body_str = body_str.replace("\n", " ").strip()
    if len(body_str) > 500:
        body_str = body_str[:500] + "…"
    return f"{status} {reason}: {body_str}"


# Per-tenant collection ensure: cheap idempotent guard so the first request
# from a newly-provisioned org auto-creates `<slug>_wiki` and `<slug>_memories`
# instead of 500ing with "Collection doesn't exist". Memoised in-process so
# subsequent requests for the same slug skip the get_collections() round-trip.
_ensured_tenants:    set[str]      = set()
_ensured_tenants_lk: threading.Lock = threading.Lock()


def _ensure_tenant_collections_lazy(tenant_slug: str) -> None:
    if not tenant_slug or tenant_slug in ("default", ""):
        return
    if tenant_slug in _ensured_tenants:
        return
    with _ensured_tenants_lk:
        if tenant_slug in _ensured_tenants:
            return
        with _embed_lock:
            vsize = _vector_size
        try:
            # Only create the wiki collection in true multi-tenant-docs
            # deployments; otherwise wiki content lives in the global default
            # collection and a per-tenant wiki collection would be a phantom.
            names = [c.name for c in qdrant().get_collections().collections]
            mem_coll = f"{tenant_slug}_memories"
            if mem_coll not in names:
                qdrant().create_collection(
                    collection_name=mem_coll,
                    vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
                )
                log.info("Created Qdrant collection %r (vector_size=%d)", mem_coll, vsize)
                _record_collection_state(mem_coll)
            if _MULTI_TENANT_DOCS:
                wiki_coll = f"{tenant_slug}_wiki"
                if wiki_coll not in names:
                    qdrant().create_collection(
                        collection_name=wiki_coll,
                        vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
                    )
                    log.info("Created Qdrant collection %r (vector_size=%d)", wiki_coll, vsize)
                    _record_collection_state(wiki_coll)
            _ensured_tenants.add(tenant_slug)
        except Exception as exc:
            log.error(
                "Failed to ensure Qdrant collections for tenant %r: %s",
                tenant_slug, exc,
            )


def _tenant_from_path(path: Path) -> str:
    """
    Derive tenant slug from a file path.

    Only active when MULTI_TENANT_DOCS=true, which requires /docs to be
    structured as /docs/{tenant-slug}/....  In the default single-tenant layout
    all wiki content lives directly under /docs/ and everything belongs to the
    'default' collection — topic subdirectories must not be misread as tenant slugs.
    """
    if not _MULTI_TENANT_DOCS:
        return "default"
    try:
        parts = path.relative_to(DOCS_DIR).parts
        if len(parts) > 1:
            return parts[0]
    except ValueError:
        pass
    return "default"


def _tenant_docs_count(slug: str) -> int:
    """Number of .md files belonging to this tenant on disk.

    Single-tenant deployments (MULTI_TENANT_DOCS=false) have a flat
    /docs/ — every .md belongs to the default tenant; querying any
    other slug returns 0.

    Multi-tenant deployments route by first path component: a file at
    /docs/<X>/whatever.md belongs to tenant X. Files at the /docs root
    (no subdir prefix) belong to the default tenant, matching the
    convention in _tenant_from_path.
    """
    if not DOCS_DIR.exists():
        return 0
    if not _MULTI_TENANT_DOCS:
        return len(list(DOCS_DIR.rglob("*.md"))) if slug == "default" else 0
    return sum(
        1 for md in DOCS_DIR.rglob("*.md")
        if _tenant_from_path(md) == slug
    )


def _ensure_collections(wiki_coll: str, mem_coll: str, vsize: int):
    """Create Qdrant collections for a tenant if they don't exist."""
    names = [c.name for c in qdrant().get_collections().collections]
    for coll in (wiki_coll, mem_coll):
        if coll not in names:
            qdrant().create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
            )
            log.info("Created Qdrant collection '%s' (vector_size=%d)", coll, vsize)

# ── Deferred embedding ─────────────────────────────────────────────────────
# A memory written with embed=False carries this payload flag and a zero
# vector. It is real data — readable, listable, exportable — that simply
# cannot be reached by semantic search yet. The flag is the whole mechanism:
# it is how the pending work is counted, found again after a restart, and
# cleared exactly once the real vector lands.
PENDING_EMBED_KEY = "embedding_pending"

_pending_embed_state: dict = {
    "state":        "idle",        # idle | running | done | error
    "tenant_slug":  None,
    "started_at":   None,
    "finished_at":  None,
    "total":        0,
    "processed":    0,
    "failed":       0,
    "errors":       [],
}
_pending_embed_lock = threading.Lock()


def _pending_filter() -> Filter:
    return Filter(must=[FieldCondition(key=PENDING_EMBED_KEY, match=MatchValue(value=True))])


def count_pending_embeddings(tenant_slug: str) -> int:
    """How many memories in this tenant are stored but not yet embedded."""
    coll = _mem_coll(tenant_slug)
    try:
        return int(qdrant().count(collection_name=coll,
                                  count_filter=_pending_filter(),
                                  exact=True).count)
    except Exception:
        # A collection that doesn't exist has no pending work — not an error.
        return 0


def embed_pending_memories(tenant_slug: str) -> None:
    """Give every deferred memory in this tenant its real vector.

    Runs off-request because it is the slow half of a restore, deliberately
    postponed. Resumable by construction: the flag is cleared per point as
    that point succeeds, so an interruption leaves the rest still pending and
    a second run picks up exactly what is left — no bookkeeping to lose.
    """
    coll = _mem_coll(tenant_slug)
    started = datetime.datetime.utcnow()
    with _pending_embed_lock:
        _pending_embed_state.update({
            "state": "running", "tenant_slug": tenant_slug,
            "started_at": started.isoformat(), "finished_at": None,
            "total": count_pending_embeddings(tenant_slug),
            "processed": 0, "failed": 0, "errors": [],
        })
    try:
        # The loop re-queries the pending set each pass rather than paging a
        # cursor, because clearing the flag changes the set under it. That
        # makes termination depend on the set actually shrinking, so track the
        # ids already handled: a point that somehow stays flagged is dropped
        # out of the loop instead of being fetched forever.
        handled: set = set()
        while True:
            points, _next = qdrant().scroll(
                collection_name=coll, scroll_filter=_pending_filter(),
                limit=64, with_payload=True, with_vectors=False,
            )
            points = [p for p in points if p.id not in handled]
            if not points:
                break
            for pt in points:
                handled.add(pt.id)
                payload = dict(pt.payload or {})
                content = payload.get("content") or ""
                try:
                    vec = get_embedding(content)
                except Exception as exc:
                    # Leave the flag set: this one stays pending and a later
                    # run retries it. Bail out rather than spin through every
                    # remaining point when the backend is simply down.
                    ref = _error_ref()
                    with _pending_embed_lock:
                        _pending_embed_state["failed"] += 1
                        _pending_embed_state["errors"] = (
                            _pending_embed_state["errors"]
                            + [f"{payload.get('id')}: embedding failed (ref {ref})"])[-5:]
                    log.warning("embed_pending_memories: embedding failed for %s [ref %s]: %r",
                                payload.get("id"), ref, exc)
                    raise
                payload.pop(PENDING_EMBED_KEY, None)
                qdrant().upsert(collection_name=coll, points=[
                    PointStruct(id=pt.id, vector=vec, payload=payload)])
                with _pending_embed_lock:
                    _pending_embed_state["processed"] += 1
        with _pending_embed_lock:
            _pending_embed_state.update({
                "state": "done",
                "finished_at": datetime.datetime.utcnow().isoformat(),
            })
        log.info("embed_pending_memories(%s): finished, %d embedded",
                 tenant_slug, _pending_embed_state["processed"])
    except Exception:
        ref = _error_ref()
        log.exception("embed_pending_memories(%s) failed [ref %s]", tenant_slug, ref)
        with _pending_embed_lock:
            _pending_embed_state.update({
                "state": "error", "error": f"embedding run failed (ref {ref})",
                "finished_at": datetime.datetime.utcnow().isoformat(),
            })


# ── Re-embed memories state ────────────────────────────────────────────────
_reembed_state: dict = {
    "state":           "idle",     # idle | running | done | error
    "started_at":      None,
    "finished_at":     None,
    "duration_s":      None,
    "collections":     [],         # list of collections being processed
    "current_coll":    None,
    "total":           0,          # total memories across all colls
    "processed":       0,
    "errors":          [],
}
_reembed_lock = threading.Lock()


def reembed_memories() -> None:
    """
    Re-embed all memories across every collection that has a vector-size
    mismatch with the current embedding model.

    For each mismatched collection:
      1. Scroll all points (payload only — content is in the payload).
      2. Delete and recreate the collection at the current vector size.
      3. Re-embed each memory's content and upsert back.

    Data is preserved because it lives in the payload, not the vector.
    """
    with _embed_lock:
        cur_dim = _vector_size

    # Find every *_memories collection (and the default MEMORIES_COLLECTION)
    all_colls = [c.name for c in qdrant().get_collections().collections]
    target_colls = [c for c in all_colls
                    if c == MEMORIES_COLLECTION or c.endswith("_memories")]

    if not target_colls:
        log.info("reembed_memories: no memory collections found — nothing to do")
        with _reembed_lock:
            _reembed_state.update({"state": "done", "collections": [], "total": 0, "processed": 0})
        return

    # Determine which actually need re-embedding
    mismatch_colls = []
    for coll in target_colls:
        try:
            info = qdrant().get_collection(coll)
            stored = info.config.params.vectors.size
        except Exception:
            continue
        if stored != cur_dim:
            mismatch_colls.append((coll, stored))

    if not mismatch_colls:
        log.info("reembed_memories: all collections already at dim %d — nothing to do", cur_dim)
        with _reembed_lock:
            _reembed_state.update({
                "state": "done",
                "collections": target_colls,
                "total": 0,
                "processed": 0,
            })
        return

    log.info(
        "reembed_memories: will migrate %d collection(s) from dim %s → %d",
        len(mismatch_colls),
        "/".join(str(s) for _, s in mismatch_colls),
        cur_dim,
    )

    start_time = datetime.datetime.utcnow()
    with _reembed_lock:
        _reembed_state.update({
            "state":       "running",
            "started_at":  start_time.isoformat(),
            "finished_at": None,
            "duration_s":  None,
            "collections": [c for c, _ in mismatch_colls],
            "current_coll": None,
            "total":       0,
            "processed":   0,
            "errors":      [],
        })

    total_migrated = 0
    errors = []

    for coll_name, old_dim in mismatch_colls:
        with _reembed_lock:
            _reembed_state["current_coll"] = coll_name

        # ── Step 1: scroll all payloads ─────────────────────────────────────
        payloads = []
        offset = None
        try:
            while True:
                batch, offset = qdrant().scroll(
                    collection_name=coll_name,
                    limit=200,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                payloads.extend(pt.payload for pt in batch)
                if offset is None:
                    break
        except Exception as exc:
            msg = f"{coll_name}: scroll failed — {exc}"
            log.error("reembed_memories: %s", msg)
            errors.append(msg)
            continue

        log.info("reembed_memories: %s — %d memories to re-embed (dim %d → %d)",
                 coll_name, len(payloads), old_dim, cur_dim)

        with _reembed_lock:
            _reembed_state["total"] += len(payloads)

        # ── Step 2: delete + recreate ───────────────────────────────────────
        try:
            qdrant().delete_collection(coll_name)
            qdrant().create_collection(
                collection_name=coll_name,
                vectors_config=VectorParams(size=cur_dim, distance=Distance.COSINE),
            )
            # Bust the dim cache so searches immediately see the new size
            with _coll_dim_lock:
                _coll_dim_cache.pop(coll_name, None)
        except Exception as exc:
            msg = f"{coll_name}: recreate failed — {exc}"
            log.error("reembed_memories: %s", msg)
            errors.append(msg)
            continue

        # ── Step 3: re-embed and upsert ─────────────────────────────────────
        coll_errors = 0
        for payload in payloads:
            mem_id  = payload.get("id")
            content = payload.get("content") or ""
            try:
                vec = get_embedding(content)
                qdrant().upsert(
                    collection_name=coll_name,
                    points=[PointStruct(
                        id=memory_point_id(mem_id) if mem_id else str(uuid.uuid4()),
                        vector=vec,
                        payload=payload,
                    )],
                )
                total_migrated += 1
                with _reembed_lock:
                    _reembed_state["processed"] += 1
            except Exception as exc:
                coll_errors += 1
                msg = f"{coll_name}/{mem_id}: {exc}"
                if coll_errors <= 5:
                    errors.append(msg)
                log.warning("reembed_memories: failed to re-embed %s/%s: %s", coll_name, mem_id, exc)

        log.info("reembed_memories: %s — done (%d/%d migrated)", coll_name, total_migrated, len(payloads))
        if coll_errors == 0:
            _record_collection_state(coll_name)

    end_time = datetime.datetime.utcnow()
    duration = (end_time - start_time).total_seconds()
    final_state = "error" if errors else "done"

    with _reembed_lock:
        _reembed_state.update({
            "state":        final_state,
            "finished_at":  end_time.isoformat(),
            "duration_s":   round(duration, 2),
            "current_coll": None,
            "errors":       errors,
        })

    log.info(
        "reembed_memories: complete in %.1fs — %d memories migrated across %d collection(s)",
        duration, total_migrated, len(mismatch_colls),
    )


# ── Reindex state ─────────────────────────────────────────────────────────
_reindex_state: dict = {
    "state": "idle",
    "last_reindex_start": None,
    "last_reindex_end": None,
    "last_reindex_duration_s": None,
    "current_file": None,
    "files_processed": 0,
    "files_total": 0,
    "errors": [],
    # Issue #83 observability: make a partial reindex loud instead of green.
    "last_reindex_failed": [],        # rel paths that failed this run (bounded)
    "last_reindex_failed_count": 0,   # true count (unbounded)
    "last_reindex_docs_total": 0,     # docs walked this run
    "last_reindex_docs_indexed": 0,   # docs that produced >=1 chunk
    "last_reindex_skipped_unchanged": 0,  # of those, skipped as unchanged (#421)
    "last_reindex_reconciled": 0,     # docs pulled S3->/docs before this run
}
_state_lock = threading.Lock()

# ── Live embedding backend (replaced at every full reindex) ────────────────
# These are module-level refs overwritten by _activate_backend().
_embed_cfg:    dict              = {}
_embed_fn:     Callable[[str], List[float]] = lambda t: []
_vector_size:  int               = 768
_embed_lock    = threading.Lock()   # protect swaps while index_file may run


def _load_embedding_config() -> dict:
    """Fetch embedding config from editor API; fall back to env vars.

    Coerces the bundled local provider's model to LOCKED_LOCAL_MODEL —
    the containerized embedder is pinned to one model for stability on
    small hosts. External providers pass through untouched.
    """
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(f"{EDITOR_URL}/api/internal/config",
                                     headers=_internal_service_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read())
            cfg = data.get("embedding_config") or {}
            if cfg.get("provider"):
                log.info("Loaded embedding config from editor API: provider=%s", cfg["provider"])
                return _apply_local_lock(cfg)
    except Exception as exc:
        log.info("Could not load embedding config from editor API (%s), using env vars", exc)
    return _apply_local_lock({
        "provider": os.environ.get("EMBEDDING_PROVIDER", "local").lower(),
        "model":    os.environ.get("EMBEDDING_MODEL", LOCKED_LOCAL_MODEL),
        "api_key":  os.environ.get("OPENAI_API_KEY", ""),
    })


def _apply_local_lock(cfg: dict) -> dict:
    if (cfg.get("provider") or "").lower() != "local":
        return cfg
    requested = cfg.get("model")
    if requested and requested != LOCKED_LOCAL_MODEL:
        log.warning(
            "Local provider model coerced %r -> %r (bundled embedder is locked; "
            "use an external provider to run a different model)",
            requested, LOCKED_LOCAL_MODEL,
        )
    cfg = dict(cfg)
    cfg["model"] = LOCKED_LOCAL_MODEL
    return cfg


def _build_backend(cfg: dict):
    """
    Construct (embed_fn, vector_size) from a config dict.
    Returns (callable, int).  May raise on bad config.
    """
    provider = cfg.get("provider", "local").lower()

    if provider == "openai":
        from openai import OpenAI as _OpenAI
        key   = cfg.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        model = cfg.get("model", "text-embedding-3-small")
        client = _OpenAI(api_key=key)
        # ada-002 is 1536-d; the previous "3-small in name else 3072" check
        # silently misreported it as 3072. Map known models explicitly and
        # probe by API call for unknown names.
        _OPENAI_VSIZE_MAP = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }
        vsize = _OPENAI_VSIZE_MAP.get(model)
        if vsize is None:
            try:
                probe = client.embeddings.create(input=["probe"], model=model)
                vsize = len(probe.data[0].embedding)
                log.info("OpenAI vsize probe succeeded for model=%s: vector_size=%d", model, vsize)
            except Exception as exc:
                log.warning("OpenAI vsize probe failed for model=%s (%s), defaulting to 1536", model, exc)
                vsize = 1536

        def _fn(text: str) -> List[float]:
            resp = client.embeddings.create(input=[text[:8192]], model=model)
            return resp.data[0].embedding

        return _fn, vsize

    elif provider == "custom":
        # Use httpx directly — avoids OpenAI SDK quirks:
        #   • SDK forces encoding_format=base64 (newer versions); LM Studio needs float
        #   • SDK mangles API keys that contain colons (Basic-auth interpretation)
        import httpx as _httpx
        model    = cfg.get("model", "")
        key      = cfg.get("api_key", "")
        base_url = cfg.get("base_url", "").rstrip("/")

        headers: dict = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        def _raw_embed(text: str) -> List[float]:
            payload = {"model": model, "input": [text[:8192]], "encoding_format": "float"}
            resp = _httpx.post(
                f"{base_url}/embeddings",
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data["data"][0]["embedding"]
            if not isinstance(embedding, list):
                raise ValueError(f"Expected float list, got {type(embedding)}")
            return embedding

        # Probe to get real vector size
        try:
            probe_vec = _raw_embed("probe")
            vsize = len(probe_vec)
            log.info("Custom embedding probe succeeded: vector_size=%d model=%s", vsize, model)
        except Exception as exc:
            log.warning("Custom embedding probe failed (%s), defaulting vector_size=768", exc)
            vsize = 768

        return _raw_embed, vsize

    else:  # local / sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer as _ST
        except ModuleNotFoundError as exc:
            # The slim embedder image omits sentence-transformers/torch. If it's
            # asked to run the local backend, fail with an actionable message
            # instead of a bare ModuleNotFoundError so the operator knows the
            # image, not the code, is the mismatch.
            raise RuntimeError(
                "Local embedding backend requested (EMBEDDING_PROVIDER=local) but "
                "sentence-transformers is not installed. This is the slim embedder "
                "image, built for external/custom embedding backends only. Either "
                "set EMBEDDING_PROVIDER=openai|custom (with a base_url/api_key), or "
                "run the full embedder image (…/embedder:latest, or build with "
                "--build-arg INCLUDE_LOCAL_EMBEDDINGS=true)."
            ) from exc
        # Defense in depth: _load_embedding_config also coerces, but a direct
        # caller of _build_backend could bypass that. Keep both layers.
        model_obj  = _load_local_model(_ST)
        # Pin torch's intra-op pool to the same small cap as the BLAS libs: a
        # single-sentence CPU encode is fastest with a fixed handful of threads,
        # and this stops the per-call thread pile-up that pegged every core.
        try:
            import torch as _torch
            _torch.set_num_threads(int(os.environ.get("EMBED_CPU_THREADS", "2")))
        except Exception:
            pass
        # sentence-transformers 5.x+ renamed get_sentence_embedding_dimension;
        # the old name still works but warns on every boot.
        vsize      = model_obj.get_embedding_dimension()

        def _fn(text: str) -> List[float]:
            return model_obj.encode(text, normalize_embeddings=True).tolist()

        return _fn, vsize


def _load_local_model(st_cls):
    """Load the pinned local model, from the hf_cache volume when it is there.

    The cache-first load makes no request to the Hub, so a start works with
    no internet and logs no "unauthenticated requests" warning. Only when the
    pinned files are not cached (first boot, or an empty volume) does it load
    again with network access, still pinned, so the download happens once."""
    # Explicit device="cpu" prevents accelerate's meta-tensor placement path,
    # which fails on some ARM + torch version combinations with
    # "Cannot copy out of meta tensor".
    kwargs = {"revision": LOCKED_LOCAL_MODEL_REVISION, "device": "cpu"}
    try:
        return st_cls(LOCKED_LOCAL_MODEL_REPO, local_files_only=True, **kwargs)
    except OSError as exc:
        # huggingface_hub's LocalEntryNotFoundError and transformers' "not
        # cached" error are both OSErrors; anything else is a real fault.
        log.info(
            "Local embedding model %s@%s is not in the cache yet (%s); "
            "downloading it once into HF_HOME",
            LOCKED_LOCAL_MODEL_REPO, LOCKED_LOCAL_MODEL_REVISION[:7], exc,
        )
    return st_cls(LOCKED_LOCAL_MODEL_REPO, **kwargs)


def _backend_revision(provider: str) -> Optional[str]:
    """The model revision the backend is pinned to. Only the bundled local
    model has one; an external provider's model is whatever it serves."""
    return LOCKED_LOCAL_MODEL_REVISION if (provider or "").lower() == "local" else None


def _activate_backend(cfg: dict) -> bool:
    """
    Build and install a new embedding backend globally.
    Returns True if vector_size changed (collection must be recreated).
    """
    global _embed_cfg, _embed_fn, _vector_size

    try:
        new_fn, new_vsize = _build_backend(cfg)
    except Exception as exc:
        log.error("Failed to build embedding backend: %s", exc)
        raise

    with _embed_lock:
        old_vsize = _vector_size
        _embed_cfg   = cfg
        _embed_fn    = new_fn
        _vector_size = new_vsize

    with _coll_dim_lock:
        _coll_dim_cache.clear()

    log.info(
        "Embedding backend activated: provider=%s model=%s vector_size=%d",
        cfg.get("provider"), cfg.get("model"), new_vsize,
    )
    return new_vsize != old_vsize


# Transient-embed retry policy. A momentary backend fault (LM Studio
# restarting, a 503/timeout) must not silently drop a wiki page the way it did
# in issue #83 — but a permanent fault (wrong vector dimension, 4xx auth) must
# fail fast so a reindex can't loop.
_EMBED_MAX_ATTEMPTS = int(os.environ.get("EMBED_MAX_ATTEMPTS", "3"))
_EMBED_BACKOFF_BASE = float(os.environ.get("EMBED_BACKOFF_BASE_S", "0.5"))


def _is_transient_embed_error(exc: Exception) -> bool:
    """True for faults worth retrying (network timeout/reset, 429/5xx);
    False for permanent faults (wrong-dim ValueError, 4xx auth/not-found)."""
    import httpx as _httpx
    if isinstance(exc, (_httpx.TimeoutException, _httpx.TransportError)):
        return True
    if isinstance(exc, _httpx.HTTPStatusError):
        try:
            return exc.response.status_code in (429, 500, 502, 503, 504)
        except Exception:
            return True
    # OpenAI SDK backend raises its own exception classes (no httpx surface).
    if type(exc).__name__ in (
        "APITimeoutError", "APIConnectionError", "RateLimitError",
        "InternalServerError", "APIStatusError",
    ):
        return True
    return False


def get_embedding(text: str) -> List[float]:
    """Thread-safe call to the current embedding function, with bounded retry
    for TRANSIENT backend faults only (issue #83). Permanent errors re-raise
    immediately."""
    with _embed_lock:
        fn = _embed_fn
    for attempt in range(_EMBED_MAX_ATTEMPTS):
        try:
            return fn(text)
        except Exception as exc:
            if attempt == _EMBED_MAX_ATTEMPTS - 1 or not _is_transient_embed_error(exc):
                raise
            delay = _EMBED_BACKOFF_BASE * (2 ** attempt)
            log.warning(
                "Embedding transient error (attempt %d/%d) — retrying in %.1fs: %s",
                attempt + 1, _EMBED_MAX_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable: embedding retry loop exhausted")  # pragma: no cover


# ── Embedding state sidecar ────────────────────────────────────────────────
# We persist {provider, model, vector_size, applied_at} per Qdrant collection
# so the system can detect not just dim mismatches but same-dim-different-model
# drift (vectors that pass the dim check but live in incompatible embedding
# spaces, which silently degrades search recall).
_embedding_state_lock = threading.Lock()


def _load_embedding_state() -> dict:
    """Read the sidecar JSON; return an empty stub if absent or unreadable."""
    import json as _json
    try:
        with EMBEDDING_STATE_FILE.open("r") as f:
            data = _json.load(f)
        if not isinstance(data, dict) or "collections" not in data:
            return {"schema_version": EMBEDDING_STATE_SCHEMA_VERSION, "collections": {}}
        return data
    except FileNotFoundError:
        return {"schema_version": EMBEDDING_STATE_SCHEMA_VERSION, "collections": {}}
    except Exception as exc:
        log.warning("Could not read embedding state sidecar (%s); treating as empty", exc)
        return {"schema_version": EMBEDDING_STATE_SCHEMA_VERSION, "collections": {}}


def _write_embedding_state_unlocked(data: dict) -> None:
    """Atomic write: temp file + rename. Caller must hold _embedding_state_lock."""
    import json as _json
    EMBEDDING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EMBEDDING_STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        _json.dump(data, f, indent=2)
    tmp.replace(EMBEDDING_STATE_FILE)


def _write_embedding_state(coll: str, provider: str, model: str, vsize: int,
                           revision: Optional[str] = None) -> None:
    """Update one collection's entry in the sidecar. `revision` is kept apart
    from `model` because the admin UI compares `model` to the configured
    model name."""
    with _embedding_state_lock:
        data = _load_embedding_state()
        data["schema_version"] = EMBEDDING_STATE_SCHEMA_VERSION
        data.setdefault("collections", {})[coll] = {
            "provider":    provider,
            "model":       model,
            "revision":    revision,
            "vector_size": vsize,
            "applied_at":  datetime.datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_embedding_state_unlocked(data)
        except Exception as exc:
            log.warning("Could not write embedding state sidecar for %r: %s", coll, exc)


def _record_collection_state(coll: str) -> None:
    """Snapshot the currently-active backend's identity into the sidecar for
    the named collection. Use this right after every successful create / recreate
    / re-embed so the sidecar reflects what model produced the live vectors."""
    with _embed_lock:
        provider = (_embed_cfg or {}).get("provider", "unknown") or "unknown"
        model    = (_embed_cfg or {}).get("model",    "unknown") or "unknown"
        vsize    = _vector_size
    _write_embedding_state(coll, provider, model, vsize, _backend_revision(provider))


def _bootstrap_embedding_state_from_qdrant() -> None:
    """For any Qdrant collection without a sidecar entry, seed one with
    provider/model='unknown' and the actually-stored vector_size. Existing
    entries are not touched. Idempotent — safe to call repeatedly."""
    with _embedding_state_lock:
        data = _load_embedding_state()
        existing = data.setdefault("collections", {})
        try:
            qdrant_colls = qdrant().get_collections().collections
        except Exception as exc:
            log.warning("Could not enumerate collections for sidecar bootstrap: %s", exc)
            return
        added = 0
        for c in qdrant_colls:
            if c.name in existing:
                continue
            try:
                info = qdrant().get_collection(c.name)
                stored = info.config.params.vectors.size
            except Exception:
                continue
            existing[c.name] = {
                "provider":    "unknown",
                "model":       "unknown",
                "vector_size": stored,
                "applied_at":  None,
            }
            added += 1
        if added > 0:
            data["schema_version"] = EMBEDDING_STATE_SCHEMA_VERSION
            try:
                _write_embedding_state_unlocked(data)
                log.info(
                    "Embedding state sidecar bootstrapped: %d collection(s) seeded as 'unknown'",
                    added,
                )
            except Exception as exc:
                log.warning("Could not write bootstrapped embedding state: %s", exc)


# ── Qdrant client (lazy init) ─────────────────────────────────────────────
_qdrant: Optional[QdrantClient] = None


def qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        raise RuntimeError("Qdrant not ready")
    return _qdrant


def wait_for_qdrant() -> None:
    global _qdrant
    log.info("Waiting for Qdrant…")
    for _ in range(60):
        try:
            client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5)
            client.get_collections()
            _qdrant = client
            log.info("Qdrant connected.")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("Could not connect to Qdrant after 120 s")


def ensure_collection(recreate: bool = False) -> None:
    """
    Create (or recreate) the Qdrant collection for the current vector size.

    For knowledge_base: Auto-recreates on vector size mismatch (wiki content is
    regenerated from files anyway).

    For memories: PRESERVES existing collection even on vector size mismatch.
    See /api/stats for detailed status. To force recreation (DATA LOSS!), set
    EMBEDDER_FORCE_COLLECTION_RECREATE=memories or "all" and restart.
    """
    with _embed_lock:
        vsize = _vector_size

    # Seed the sidecar from existing Qdrant state on first run so the dim-check
    # endpoint can distinguish "we genuinely don't know what model produced these
    # vectors" from "we know and it matches".
    _bootstrap_embedding_state_from_qdrant()

    names = [c.name for c in qdrant().get_collections().collections]
    force_recreate = os.environ.get("EMBEDDER_FORCE_COLLECTION_RECREATE", "").lower()

    # ── Knowledge base collection (safe to recreate — wiki reindexing regenerates it)
    if COLLECTION_NAME in names and not recreate:
        try:
            info = qdrant().get_collection(COLLECTION_NAME)
            existing_size = info.config.params.vectors.size
            if existing_size != vsize:
                log.warning(
                    "Collection '%s' vector_size changed: %d → %d (recreating wiki index)",
                    COLLECTION_NAME, existing_size, vsize,
                )
                recreate = True
        except Exception as exc:
            log.warning("Could not inspect collection dimensions: %s", exc)

    if recreate and COLLECTION_NAME in names:
        log.info("Recreating Qdrant collection '%s' for vector_size=%d", COLLECTION_NAME, vsize)
        qdrant().delete_collection(COLLECTION_NAME)
        names = [n for n in names if n != COLLECTION_NAME]

    if COLLECTION_NAME not in names:
        qdrant().create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s' (vector_size=%d)", COLLECTION_NAME, vsize)
        _record_collection_state(COLLECTION_NAME)

    # ── Memories collection (PRESERVE on vector size mismatch to avoid data loss)
    # Memories are re-embedded on-demand when accessed, so a size change is safe
    # as long as we don't delete the PostgreSQL metadata.
    if MEMORIES_COLLECTION in names:
        try:
            mem_info = qdrant().get_collection(MEMORIES_COLLECTION)
            existing_mem_size = mem_info.config.params.vectors.size
            if existing_mem_size != vsize:
                if force_recreate in ("memories", "all"):
                    log.error(
                        "EMBEDDER_FORCE_COLLECTION_RECREATE=yes — deleting '%s' "
                        "(existing memories will be lost)",
                        MEMORIES_COLLECTION,
                    )
                    qdrant().delete_collection(MEMORIES_COLLECTION)
                    names = [n for n in names if n != MEMORIES_COLLECTION]
                else:
                    log.error(
                        "Collection '%s' vector_size mismatch: %d vs %d. "
                        "Memories are PRESERVED (not recreated) to prevent data loss. "
                        "To manually force recreation and lose all memories, restart with "
                        "EMBEDDER_FORCE_COLLECTION_RECREATE=memories",
                        MEMORIES_COLLECTION, existing_mem_size, vsize,
                    )
        except Exception as exc:
            log.warning("Could not inspect memories collection: %s", exc)

    if MEMORIES_COLLECTION not in names:
        qdrant().create_collection(
            collection_name=MEMORIES_COLLECTION,
            vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s' (vector_size=%d)", MEMORIES_COLLECTION, vsize)
        _record_collection_state(MEMORIES_COLLECTION)



# ── Markdown chunking ──────────────────────────────────────────────────────
def chunk_markdown(body: str) -> List[dict]:
    parts = re.split(r"\n(?=#{1,3} )", body)
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        heading = lines[0].lstrip("#").strip() if lines[0].startswith("#") else ""
        chunks.append({"title": heading, "content": part})
    return chunks or [{"title": "", "content": body.strip()}]


def point_id(path: str, idx: int) -> int:
    h = hashlib.sha1(f"{path}::{idx}".encode()).hexdigest()
    return int(h[:15], 16)


def memory_point_id(memory_id: str) -> int:
    """Convert a UUID string to a Qdrant-compatible uint64 point ID."""
    return int(uuid.UUID(memory_id).int % (2**63))


# ── Index one file ─────────────────────────────────────────────────────────
def index_file(file_path: Path, tenant_slug: str = "") -> int:
    """(Re)index one wiki file. Returns the number of chunks upserted.

    The page's existing vectors are replaced TRANSACTIONALLY: every chunk is
    embedded first, and only once they all succeed do we delete the old
    vectors and upsert the new ones. So a transient embed failure raises
    without having touched the index, leaving the previously-searchable page
    intact instead of destructively blanking it (issue #83).

    Every chunk is stamped with the page's `content_sha`, the `embed_model`
    that produced it and the page's `chunk_count`, so a startup reindex can
    tell an unchanged page from a stale one without re-embedding it (#421)."""
    slug = tenant_slug or _tenant_from_path(file_path)
    wiki_coll = _wiki_coll(slug)
    try:
        # Hash and parse the same bytes, so the stamp always describes the
        # text that was actually embedded.
        raw   = file_path.read_bytes()
        content_sha = hashlib.sha256(raw).hexdigest()
        model_tag   = _embed_model_tag()
        post  = frontmatter.loads(raw.decode("utf-8"))
        tags  = post.metadata.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tags  = [str(t) for t in tags]
        title = str(post.metadata.get("title", file_path.stem))
        body  = post.content
        rel   = str(file_path.relative_to(DOCS_DIR))

        chunks = chunk_markdown(body)
        points = []
        for i, chunk in enumerate(chunks):
            text_for_embed = "\n".join(filter(None, [title, chunk["title"], chunk["content"]]))
            vec = get_embedding(text_for_embed)
            points.append(PointStruct(
                id=point_id(rel, i),
                vector=vec,
                payload={
                    "path":          rel,
                    "title":         title,
                    "section_title": chunk["title"],
                    "tags":          tags,
                    "content":       chunk["content"][:3000],
                    "chunk_index":   i,
                    # Defence-in-depth: stamp the tenant on every wiki
                    # chunk so a post-retrieve check can flag a mismatched
                    # collection-routing bug. Collection naming
                    # ({slug}_wiki) is still the primary control.
                    "tenant_id":     slug,
                    # Startup skip test (#421): see _page_is_current.
                    "content_sha":   content_sha,
                    "embed_model":   model_tag,
                    "chunk_count":   len(chunks),
                },
            ))

        # All chunks embedded — now it's safe to swap old vectors for new.
        _remove_file(rel, slug)
        if points:
            qdrant().upsert(collection_name=wiki_coll, points=points)
            log.info("Indexed %d chunk(s) ← %s [tenant=%s]", len(points), rel, slug)
        return len(points)

    except Exception as exc:
        log.error("Failed to index %s: %s [tenant=%s]", file_path, exc, slug)
        raise


def _remove_file(rel_path: str, tenant_slug: str = "default") -> None:
    wiki_coll = _wiki_coll(tenant_slug)
    try:
        qdrant().delete(
            collection_name=wiki_coll,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="path", match=MatchValue(value=rel_path))]
                )
            ),
        )
    except Exception as exc:
        log.warning("Could not remove %s from index: %s [tenant=%s]",
                    rel_path, exc, tenant_slug)


def _wiki_collection_names() -> list[str]:
    """All Qdrant collections that hold wiki content.

    Single-tenant: just the global default collection. Multi-tenant: the
    default collection plus every per-tenant `<slug>_wiki` collection that
    currently exists.
    """
    names = {COLLECTION_NAME}
    if _MULTI_TENANT_DOCS:
        try:
            for c in qdrant().get_collections().collections:
                if c.name.endswith("_wiki"):
                    names.add(c.name)
        except Exception as exc:
            log.warning("Could not enumerate wiki collections for pruning: %s", exc)
    return sorted(names)


def _prune_orphaned_wiki_vectors(valid_by_coll: dict[str, set]) -> int:
    """Delete vectors for wiki pages whose source .md file no longer exists.

    A plain full reindex only *upserts* the files it finds on disk — it never
    removes vectors for pages that were deleted (recycled or purged) since the
    last index. That left deleted pages lingering in search results until the
    collection happened to be recreated. This walks every wiki collection and
    drops any wiki-doc point whose `path` isn't in the live set for that
    collection, so a rebuild becomes authoritative.

    Git-source content shares the wiki collection but is keyed by `source_id`
    and re-synced separately, so points carrying a `source_id` are left
    untouched.

    Returns the number of points deleted.
    """
    deleted_total = 0
    for coll in _wiki_collection_names():
        valid = valid_by_coll.get(coll, set())
        orphan_ids: list = []
        offset = None
        try:
            while True:
                batch, offset = qdrant().scroll(
                    collection_name=coll,
                    limit=500,
                    offset=offset,
                    with_payload=["path", "source_id"],
                    with_vectors=False,
                )
                for pt in batch:
                    payload = pt.payload or {}
                    if payload.get("source_id"):
                        continue  # git-source chunk — managed by its own sync
                    path = payload.get("path")
                    if path is not None and path not in valid:
                        orphan_ids.append(pt.id)
                if offset is None:
                    break
        except Exception as exc:
            log.warning("Prune scroll failed for %s: %s", coll, exc)
            continue

        if not orphan_ids:
            continue
        try:
            qdrant().delete(collection_name=coll, points_selector=orphan_ids)
            deleted_total += len(orphan_ids)
            log.info("Pruned %d orphaned wiki vector(s) from %s", len(orphan_ids), coll)
        except Exception as exc:
            log.warning("Prune delete failed for %s: %s", coll, exc)
    return deleted_total


# ── Startup skip test (#421) ──────────────────────────────────────────────
# A startup reindex used to re-embed every wiki page, although the vectors on
# the persistent Qdrant volume were still valid. Each wiki chunk now carries
# the sha256 of its page, the backend that embedded it and the page's chunk
# count, so full_reindex(force=False) re-embeds only what changed.
def _embed_model_tag() -> str:
    """`provider/model/vector_size` of the active backend, with the model as
    `model@rev7` when it is pinned to a revision (#422)."""
    with _embed_lock:
        provider = (_embed_cfg or {}).get("provider", "unknown") or "unknown"
        model    = (_embed_cfg or {}).get("model",    "unknown") or "unknown"
        vsize    = _vector_size
    rev = _backend_revision(provider)
    if rev:
        model = f"{model}@{rev[:7]}"
    return f"{provider}/{model}/{vsize}"


def _legacy_embed_model_tag() -> Optional[str]:
    """The stamp chunks carried before #422 pinned the revision, when they were
    embedded by the revision now pinned. Accepting it saves one full re-embed
    on upgrade (Stage's last full pass took over 3 hours)."""
    with _embed_lock:
        provider = (_embed_cfg or {}).get("provider", "unknown") or "unknown"
        model    = (_embed_cfg or {}).get("model",    "unknown") or "unknown"
        vsize    = _vector_size
    if _backend_revision(provider) != _UNPINNED_STAMP_REVISION:
        return None
    return f"{provider}/{model}/{vsize}"


def _page_content_sha(file_path: Path) -> str:
    """sha256 of the page file, the same value index_file stamps."""
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def _sidecar_backend_differs(coll: str) -> bool:
    """True when the sidecar names a backend for `coll` other than the active
    one. An absent entry, or one bootstrapped as 'unknown', says nothing, so
    the per-chunk stamps decide instead. So does an entry written before #422,
    which has no `revision` key."""
    entry = (_load_embedding_state().get("collections") or {}).get(coll) or {}
    provider = entry.get("provider") or "unknown"
    model    = entry.get("model") or "unknown"
    if provider == "unknown" or model == "unknown":
        return False
    with _embed_lock:
        active = (
            (_embed_cfg or {}).get("provider", "unknown") or "unknown",
            (_embed_cfg or {}).get("model",    "unknown") or "unknown",
            _vector_size,
        )
    if (provider, model, entry.get("vector_size")) != active:
        return True
    return "revision" in entry and entry["revision"] != _backend_revision(active[0])


def _scan_wiki_index_stamps(coll: str) -> Optional[dict]:
    """Scroll one wiki collection, payload only, into
    `path -> {"stamps": {(content_sha, embed_model, chunk_count)}, "points": n}`.

    Git-source chunks are skipped, as in the orphan prune. Returns None when
    the scroll fails, so the caller re-embeds that collection rather than
    trusting a partial picture."""
    index: dict = {}
    offset = None
    try:
        while True:
            batch, offset = qdrant().scroll(
                collection_name=coll,
                limit=500,
                offset=offset,
                with_payload=["path", "source_id", "content_sha", "embed_model", "chunk_count"],
                with_vectors=False,
            )
            for pt in batch:
                payload = pt.payload or {}
                if payload.get("source_id"):
                    continue
                path = payload.get("path")
                if path is None:
                    continue
                entry = index.setdefault(path, {"stamps": set(), "points": 0})
                entry["stamps"].add((
                    payload.get("content_sha"),
                    payload.get("embed_model"),
                    payload.get("chunk_count"),
                ))
                entry["points"] += 1
            if offset is None:
                break
    except Exception as exc:
        log.warning("Index stamp scan failed for %s; re-embedding it: %s", coll, exc)
        return None
    return index


def _page_is_current(entry: Optional[dict], content_sha: str, model_tag: str,
                     legacy_tag: Optional[str] = None) -> bool:
    """A page may be skipped only when every one of its points carries this
    content hash and this model, and none of its chunks is missing. A page
    with no points, or indexed before the stamps existed, is not current.
    `legacy_tag` is a stamp that also counts as this model (see
    _legacy_embed_model_tag)."""
    if not entry or not content_sha:
        return False
    stamps = entry["stamps"]
    if len(stamps) != 1:
        return False
    sha, model, count = next(iter(stamps))
    return (sha == content_sha and count == entry["points"]
            and (model == model_tag or (legacy_tag is not None and model == legacy_tag)))


# ── S3 → /docs reconcile ────────────────────────────────────────────────────
# S3 is the authoritative store and the source of the "docs on disk" count;
# /docs is a best-effort mirror the editor writes on each save and whose write
# failures it swallows (editor _s3_write). So a page can live in S3 — counted —
# yet be missing from the /docs volume this indexer walks, and thus never get
# embedded, with no error anywhere (issue #83). Pulling any missing/stale S3
# objects into /docs before a reindex makes the index authoritative against the
# source of truth and self-heals such gaps.
def _reconcile_docs_from_s3(slug: str) -> int:
    """Download wiki objects present in the org's S3 bucket but missing (or
    size-mismatched) on the /docs mirror. Returns the number of files pulled."""
    client = _get_s3_client()
    if client is None:
        return 0
    bucket = _bucket_for(slug)
    # Match the editor's mirror layout: default tenant flat under /docs/,
    # other orgs nested under /docs/<slug>/.
    base = DOCS_DIR if (not _MULTI_TENANT_DOCS or slug in ("default", "", None)) else DOCS_DIR / slug
    pulled = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in (page.get("Contents") or []):
                key = obj.get("Key") or ""
                if not key.endswith(".md"):
                    continue
                dest = base / key
                try:
                    if dest.exists() and dest.stat().st_size == (obj.get("Size") or -1):
                        continue  # already mirrored at the same size
                    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(body)
                    pulled += 1
                except Exception as exc:
                    log.warning("S3->/docs reconcile failed for %s/%s: %s", bucket, key, exc)
    except Exception as exc:
        log.warning("S3->/docs reconcile: could not list bucket %s: %s", bucket, exc)
    if pulled:
        log.info("Reconciled %d doc(s) from S3 into %s [tenant=%s]", pulled, base, slug)
    return pulled


def _reconcile_all_docs_from_s3() -> int:
    """Reconcile every org's wiki bucket into /docs (used by full_reindex)."""
    client = _get_s3_client()
    if client is None:
        return 0
    prefix = os.environ.get("ELEFANT_S3_BUCKET_PREFIX", "")
    try:
        buckets = client.list_buckets().get("Buckets") or []
    except Exception as exc:
        log.warning("S3->/docs reconcile: could not list buckets: %s", exc)
        return 0
    total = 0
    for b in buckets:
        name = b.get("Name", "")
        if not name.endswith("--default"):
            continue
        if prefix and not name.startswith(prefix):
            continue
        slug = name[len(prefix):] if prefix else name
        slug = slug[: -len("--default")]
        if slug:
            total += _reconcile_docs_from_s3(slug)
    return total


# ── Full reindex ───────────────────────────────────────────────────────────
def full_reindex(force: bool = True) -> None:
    """
    Reload embedding config from the editor, swap in the new backend if needed
    (recreating the Qdrant collection when vector size changes), then index all docs
    and re-sync every git source so their chunks are always at the current vector dim.

    `force=False` is the startup reconcile (#421): a page whose content hash
    and embedding model both match its stored points is skipped instead of
    re-embedded. The S3 reconcile and the orphan prune still run in full.
    Every user-triggered rebuild keeps the default, force=True.
    """
    # 1. Reload config and (re)activate backend
    new_cfg = _load_embedding_config()
    try:
        vsize_changed = _activate_backend(new_cfg)
    except Exception as exc:
        log.error("Cannot start reindex — backend failed to load: %s", exc)
        with _state_lock:
            _reindex_state["errors"].append(f"Backend init failed: {exc}")
            _reindex_state["errors"] = _reindex_state["errors"][-5:]
        return

    # 2. Ensure collection exists and has the right dimensions
    try:
        ensure_collection(recreate=vsize_changed)
    except Exception as exc:
        log.error("Collection ensure/recreate failed: %s", exc)
        with _state_lock:
            _reindex_state["errors"].append(f"Collection ensure failed: {exc}")
            _reindex_state["errors"] = _reindex_state["errors"][-5:]
        return

    # 2b. Reconcile the /docs mirror against S3 (the source of truth) so a
    #     swallowed editor mirror-write can't hide a page from the index (#83).
    try:
        reconciled = _reconcile_all_docs_from_s3()
        if reconciled:
            log.info("Pre-reindex reconcile pulled %d doc(s) from S3 into /docs", reconciled)
    except Exception as exc:
        reconciled = 0
        log.warning("Pre-reindex S3 reconcile failed: %s", exc)

    # 3. Index all wiki files
    all_files  = sorted(DOCS_DIR.rglob("*.md"))
    start_time = datetime.datetime.utcnow()
    log.info("Full reindex starting… (%d files, provider=%s, force=%s)",
             len(all_files), new_cfg.get("provider"), force)

    with _state_lock:
        _reindex_state["state"]              = "indexing"
        _reindex_state["last_reindex_start"] = start_time.isoformat()
        _reindex_state["last_reindex_end"]   = None
        _reindex_state["last_reindex_duration_s"] = None
        _reindex_state["files_processed"]    = 0
        _reindex_state["files_total"]        = len(all_files)
        _reindex_state["current_file"]       = None
        # Reset per-run failure tracking so status reflects THIS run, not a
        # stale accumulation (the old errors ring was never cleared).
        _reindex_state["errors"]                    = []
        _reindex_state["last_reindex_failed"]       = []
        _reindex_state["last_reindex_failed_count"] = 0

    # Track the live set of wiki paths per collection so we can prune vectors
    # for pages that were deleted since the last index (see _prune_orphaned…).
    valid_by_coll: dict[str, set] = {}
    # Without force, each collection is scanned once for its stamps; None
    # means "re-embed every page in it" (sidecar says the model changed, or
    # the scan failed).
    stamps_by_coll: dict[str, Optional[dict]] = {}
    model_tag = _embed_model_tag()
    legacy_tag = _legacy_embed_model_tag()
    failed_colls: set = set()
    indexed_docs = 0
    skipped_unchanged = 0
    failed: list = []
    for md in all_files:
        rel = str(md.relative_to(DOCS_DIR))
        slug = _tenant_from_path(md)
        coll = _wiki_coll(slug)
        valid_by_coll.setdefault(coll, set()).add(rel)
        with _state_lock:
            _reindex_state["current_file"] = rel
        if not force:
            if coll not in stamps_by_coll:
                if _sidecar_backend_differs(coll):
                    log.info("Embedding backend changed for %s; re-embedding all its pages", coll)
                    stamps_by_coll[coll] = None
                else:
                    stamps_by_coll[coll] = _scan_wiki_index_stamps(coll)
            stamps = stamps_by_coll[coll]
            if stamps is not None:
                try:
                    current = _page_is_current(
                        stamps.get(rel), _page_content_sha(md), model_tag, legacy_tag,
                    )
                except Exception:
                    current = False
                if current:
                    indexed_docs += 1
                    skipped_unchanged += 1
                    with _state_lock:
                        _reindex_state["files_processed"] += 1
                    continue
        try:
            n = index_file(md, slug)
            if n and n > 0:
                indexed_docs += 1
            else:
                failed.append(rel)   # loaded but produced no chunks — a miss
                failed_colls.add(coll)
        except Exception as exc:
            failed.append(rel)
            failed_colls.add(coll)
            with _state_lock:
                _reindex_state["errors"].append(f"{rel}: {exc}")
                _reindex_state["errors"] = _reindex_state["errors"][-5:]
        with _state_lock:
            _reindex_state["files_processed"] += 1

    # Drop vectors whose backing .md file is gone — makes a rebuild authoritative
    # rather than additive, so deleted/recycled pages stop showing up in search.
    try:
        _prune_orphaned_wiki_vectors(valid_by_coll)
    except Exception as exc:
        log.warning("Orphan prune pass failed: %s", exc)

    end_time = datetime.datetime.utcnow()
    duration = (end_time - start_time).total_seconds()

    with _state_lock:
        # A run that dropped pages is NOT healthy — surface it instead of the
        # bare "watching" that made #83 look green.
        _reindex_state["state"]                    = "watching_with_errors" if failed else "watching"
        _reindex_state["last_reindex_end"]         = end_time.isoformat()
        _reindex_state["last_reindex_duration_s"]  = round(duration, 2)
        _reindex_state["current_file"]             = None
        _reindex_state["files_processed"]          = 0
        _reindex_state["files_total"]              = 0
        _reindex_state["last_reindex_failed"]      = failed[-50:]
        _reindex_state["last_reindex_failed_count"] = len(failed)
        _reindex_state["last_reindex_docs_total"]  = len(all_files)
        _reindex_state["last_reindex_docs_indexed"] = indexed_docs
        _reindex_state["last_reindex_skipped_unchanged"] = skipped_unchanged
        _reindex_state["last_reindex_reconciled"]  = reconciled

    if failed:
        log.warning("Full reindex complete in %.1fs — %d/%d docs indexed (%d unchanged, skipped), "
                    "%d FAILED: %s", duration, indexed_docs, len(all_files), skipped_unchanged,
                    len(failed), ", ".join(failed[:10]))
    else:
        log.info("Full reindex complete in %.1fs — %d docs indexed (%d re-embedded, %d unchanged, skipped)",
                 duration, indexed_docs, indexed_docs - skipped_unchanged, skipped_unchanged)

    # Record the active backend's identity for the wiki collection. ensure_collection
    # only writes the sidecar on create/recreate; a no-dim-change reindex (same dim,
    # different model) wouldn't update the record otherwise — and that's exactly the
    # silent-drift case the sidecar exists to detect.
    _record_collection_state(COLLECTION_NAME)
    # Per-org wiki collections too, but only once every page in them is on the
    # active backend; otherwise the next startup would trust a stale record.
    for coll in valid_by_coll:
        if coll != COLLECTION_NAME and coll not in failed_colls:
            _record_collection_state(coll)

    # 4. Re-sync all git sources so their chunks are always current.
    #    Git source content lives in the same wiki collection — if the collection
    #    was just recreated (dim change), their chunks are gone and must be rebuilt.
    #    Even without a dim change, a full rebuild should include git source content.
    try:
        sources = _fetch_git_sources()
    except Exception as exc:
        log.warning("full_reindex: could not fetch git sources for re-sync: %s", exc)
        sources = []
    if sources:
        log.info("full_reindex: triggering re-sync of %d git source(s)", len(sources))
        for src in sources:
            threading.Thread(target=sync_one_source, args=(src,), daemon=True).start()


def reindex_org(slug: str) -> None:
    """Reindex wiki content for a single org, leaving other orgs untouched.

    In single-tenant deployments (MULTI_TENANT_DOCS=false) or when the slug
    resolves to the global default collection, there is only one collection so
    this falls back to full_reindex.  In multi-tenant mode the scope is:
      • only the /docs/<slug>/ subtree of files
      • only the <slug>_wiki Qdrant collection (prune + re-upsert)
      • only git sources whose org_slug matches
    This means one org's "Rebuild Now" click can no longer blank or stall
    another org's search index.
    """
    if not _MULTI_TENANT_DOCS or slug in ("default", "", None):
        full_reindex()
        return

    new_cfg = _load_embedding_config()
    try:
        vsize_changed = _activate_backend(new_cfg)
    except Exception as exc:
        log.error("reindex_org(%s): backend load failed: %s", slug, exc)
        with _state_lock:
            _reindex_state["errors"].append(f"Backend init failed: {exc}")
            _reindex_state["errors"] = _reindex_state["errors"][-5:]
        return

    wiki_coll = _wiki_coll(slug)
    try:
        with _embed_lock:
            vsize = _vector_size
        names = [c.name for c in qdrant().get_collections().collections]
        if vsize_changed and wiki_coll in names:
            log.info("Recreating Qdrant collection '%s' for vector_size=%d", wiki_coll, vsize)
            qdrant().delete_collection(wiki_coll)
            names = [n for n in names if n != wiki_coll]
        if wiki_coll not in names:
            qdrant().create_collection(
                collection_name=wiki_coll,
                vectors_config=VectorParams(size=vsize, distance=Distance.COSINE),
            )
            log.info("Created Qdrant collection '%s' (vector_size=%d)", wiki_coll, vsize)
            _record_collection_state(wiki_coll)
    except Exception as exc:
        log.error("reindex_org(%s): collection ensure failed: %s", slug, exc)
        with _state_lock:
            _reindex_state["errors"].append(f"Collection ensure failed: {exc}")
            _reindex_state["errors"] = _reindex_state["errors"][-5:]
        return

    # Reconcile this org's /docs subtree against its S3 bucket first (#83).
    try:
        reconciled = _reconcile_docs_from_s3(slug)
        if reconciled:
            log.info("reindex_org(%s): reconcile pulled %d doc(s) from S3", slug, reconciled)
    except Exception as exc:
        reconciled = 0
        log.warning("reindex_org(%s): S3 reconcile failed: %s", slug, exc)

    org_dir = DOCS_DIR / slug
    all_files = sorted(org_dir.rglob("*.md")) if org_dir.exists() else []
    start_time = datetime.datetime.utcnow()
    log.info("Org reindex starting… (%d files for org=%s)", len(all_files), slug)

    with _state_lock:
        _reindex_state["state"]                   = "indexing"
        _reindex_state["last_reindex_start"]      = start_time.isoformat()
        _reindex_state["last_reindex_end"]        = None
        _reindex_state["last_reindex_duration_s"] = None
        _reindex_state["files_processed"]         = 0
        _reindex_state["files_total"]             = len(all_files)
        _reindex_state["current_file"]            = None
        _reindex_state["errors"]                    = []
        _reindex_state["last_reindex_failed"]       = []
        _reindex_state["last_reindex_failed_count"] = 0

    valid_paths: set = set()
    indexed_docs = 0
    failed: list = []
    for md in all_files:
        rel = str(md.relative_to(DOCS_DIR))
        valid_paths.add(rel)
        with _state_lock:
            _reindex_state["current_file"] = rel
        try:
            n = index_file(md, slug)
            if n and n > 0:
                indexed_docs += 1
            else:
                failed.append(rel)
        except Exception as exc:
            failed.append(rel)
            with _state_lock:
                _reindex_state["errors"].append(f"{rel}: {exc}")
                _reindex_state["errors"] = _reindex_state["errors"][-5:]
        with _state_lock:
            _reindex_state["files_processed"] += 1

    try:
        _prune_orphaned_wiki_vectors({wiki_coll: valid_paths})
    except Exception as exc:
        log.warning("reindex_org(%s): orphan prune failed: %s", slug, exc)

    end_time = datetime.datetime.utcnow()
    duration = (end_time - start_time).total_seconds()

    with _state_lock:
        _reindex_state["state"]                    = "watching_with_errors" if failed else "watching"
        _reindex_state["last_reindex_end"]         = end_time.isoformat()
        _reindex_state["last_reindex_duration_s"]  = round(duration, 2)
        _reindex_state["current_file"]             = None
        _reindex_state["files_processed"]          = 0
        _reindex_state["files_total"]              = 0
        _reindex_state["last_reindex_failed"]      = failed[-50:]
        _reindex_state["last_reindex_failed_count"] = len(failed)
        _reindex_state["last_reindex_docs_total"]  = len(all_files)
        _reindex_state["last_reindex_docs_indexed"] = indexed_docs
        _reindex_state["last_reindex_skipped_unchanged"] = 0
        _reindex_state["last_reindex_reconciled"]  = reconciled

    if failed:
        log.warning("Org reindex for %s complete in %.1fs — %d/%d docs indexed, %d FAILED: %s",
                    slug, duration, indexed_docs, len(all_files), len(failed), ", ".join(failed[:10]))
    else:
        log.info("Org reindex for %s complete in %.1fs — %d docs indexed", slug, duration, indexed_docs)
    _record_collection_state(wiki_coll)

    try:
        sources = _fetch_git_sources()
        org_sources = [s for s in sources if (s.get("org_slug") or "default") == slug]
    except Exception as exc:
        log.warning("reindex_org(%s): could not fetch git sources: %s", slug, exc)
        org_sources = []
    if org_sources:
        log.info("reindex_org: triggering re-sync of %d git source(s) for %s", len(org_sources), slug)
        for src in org_sources:
            threading.Thread(target=sync_one_source, args=(src,), daemon=True).start()


# ── Watchdog handler ────────────────────────────────────────────────────────
class _Handler(FileSystemEventHandler):
    def _is_md(self, path: str) -> bool:
        return path.endswith(".md") and not os.path.basename(path).startswith(".")

    def on_modified(self, event):
        if not event.is_directory and self._is_md(event.src_path):
            p = Path(event.src_path)
            index_file(p, _tenant_from_path(p))

    def on_created(self, event):
        if not event.is_directory and self._is_md(event.src_path):
            p = Path(event.src_path)
            index_file(p, _tenant_from_path(p))

    def on_deleted(self, event):
        if not event.is_directory and self._is_md(event.src_path):
            p = Path(event.src_path)
            rel = str(p.relative_to(DOCS_DIR))
            slug = _tenant_from_path(p)
            _remove_file(rel, slug)
            log.info("Removed from index: %s [tenant=%s]", rel, slug)

    def on_moved(self, event):
        if not event.is_directory:
            if self._is_md(event.src_path):
                src = Path(event.src_path)
                _remove_file(str(src.relative_to(DOCS_DIR)), _tenant_from_path(src))
            if self._is_md(event.dest_path):
                dst = Path(event.dest_path)
                index_file(dst, _tenant_from_path(dst))


# ── Git external knowledge sources ────────────────────────────────────────────
GIT_SOURCES_DIR = Path(os.environ.get("GIT_SOURCES_DIR", "/git-sources"))
_git_source_states: dict = {}
_git_source_lock = threading.Lock()

# Index everything from a git source by default. We use a denylist (binary
# extensions, generated lockfiles, build-output directories) plus a size cap
# and a NUL-byte heuristic, so unknown text formats are admitted without
# having to keep expanding an allowlist. Markdown still gets frontmatter +
# heading chunking via _GIT_MARKDOWN_EXTENSIONS below; everything else gets
# fixed-line code chunking.
_GIT_BINARY_EXTENSIONS = {
    # Images / media
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".ico", ".heic",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".webm", ".flac", ".m4a",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    # Compiled artifacts
    ".pyc", ".pyo", ".o", ".a", ".so", ".dylib", ".dll", ".exe",
    ".class", ".wasm", ".obj", ".pdb",
    # Documents (binary formats — text-equivalents covered by markdown/rst/txt)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Databases / blobs
    ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    ".bin", ".dat", ".pkl", ".npy", ".npz", ".h5", ".hdf5",
}

# Generated / lockfile noise that is technically text but rarely worth indexing.
_GIT_SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "Pipfile.lock",
    "composer.lock", "Gemfile.lock", "go.sum",
}

# Path segments that mark generated, vendored, or IDE-state trees.
_GIT_SKIP_DIR_SEGMENTS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", "vendor",
    ".idea", ".vscode", ".terraform",
}

_GIT_MAX_FILE_BYTES = 1_000_000   # 1 MB raw bytes — skips minified bundles, big datasets

_GIT_MARKDOWN_EXTENSIONS = {".md", ".mdx"}

_CODE_CHUNK_LINES   = 80   # lines per code chunk
_CODE_CHUNK_OVERLAP = 10   # lines of overlap between adjacent chunks


def _is_indexable(path: Path) -> bool:
    name = path.name
    if name in _GIT_SKIP_FILENAMES:
        return False
    if path.suffix.lower() in _GIT_BINARY_EXTENSIONS:
        return False
    if any(seg in _GIT_SKIP_DIR_SEGMENTS for seg in path.parts):
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0 or size > _GIT_MAX_FILE_BYTES:
        return False
    # Final binary-sniff: a NUL byte in the first 8 KB is a reliable signal
    # the file isn't text, no matter what extension it claims.
    try:
        with open(path, "rb") as f:
            if b"\x00" in f.read(8192):
                return False
    except OSError:
        return False
    return True


def chunk_code(body: str, filename: str) -> List[dict]:
    """Split source code into overlapping fixed-line chunks."""
    lines = body.splitlines()
    if not lines:
        return [{"title": filename, "content": ""}]
    chunks = []
    i = 0
    while i < len(lines):
        end = min(i + _CODE_CHUNK_LINES, len(lines))
        content = "\n".join(lines[i:end])
        if content.strip():
            chunks.append({"title": f"{filename} lines {i+1}–{end}", "content": content})
        if end >= len(lines):
            break
        i += _CODE_CHUNK_LINES - _CODE_CHUNK_OVERLAP
    return chunks or [{"title": filename, "content": body.strip()}]


def _git_auth_args(token: str) -> list[str]:
    """Return git -c args supplying HTTP Basic creds via header instead of URL.

    Keeps the token out of .git/config (and .git/logs/HEAD) on disk, and is
    applied fresh on every git invocation so token rotation in the DB takes
    effect immediately — no re-clone or hand-rewrite of the origin URL
    needed when a PAT is updated in the admin UI.

    Username "oauth2" preserves the convention the URL-embedded form used.
    GitHub ignores the username for token-as-password basic auth; GitLab
    requires "oauth2" for OAuth tokens and accepts any user for PATs; that
    covers the providers Elefant is realistically syncing from.
    """
    if not token:
        return []
    import base64
    creds = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {creds}"]


# What a git source may name. The admin API validates these on the way in; this
# is the same check at the point of use, for rows written before it existed.
# A branch or URL that starts with `-` would otherwise be read by git as an
# option, and a URL scheme like `ext::` runs a command.
_GIT_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
_GIT_URL_RE = re.compile(
    r"(?:https|ssh)://(?:[^\s@/]+@)?[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?(?:/\S*)?"
    r"|[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*:\S+")


def _check_git_ref(branch: str, repo_url: str) -> None:
    """Raise ValueError unless branch and repo_url are safe to hand to git."""
    if (not branch or len(branch) > 255 or branch.startswith("-")
            or not _GIT_BRANCH_RE.fullmatch(branch)):
        raise ValueError("git source branch name is not allowed")
    if (not repo_url or len(repo_url) > 1024
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in repo_url)
            or not _GIT_URL_RE.fullmatch(repo_url)):
        raise ValueError("git source URL must be https://, ssh:// or user@host:path")


def _run_git(*args, cwd=None) -> str:
    import subprocess, os
    # With http.extraHeader supplying creds, git should never need to prompt,
    # but GIT_TERMINAL_PROMPT=0 guarantees we fail fast with a real error
    # instead of hanging if creds are missing/wrong in a TTY-less container.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _remove_git_source_file(rel_path: str, source_id: str) -> None:
    wiki_coll = _wiki_coll()
    try:
        qdrant().delete(
            collection_name=wiki_coll,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="path",      match=MatchValue(value=rel_path)),
                    FieldCondition(key="source_id", match=MatchValue(value=source_id)),
                ])
            ),
        )
    except Exception as exc:
        log.warning("Could not remove git source file %s: %s", rel_path, exc)


def _lang_for(rel: str) -> Optional[str]:
    ext = Path(rel).suffix.lower().lstrip(".")
    return ext or None


def _memory_api_git_index(tenant_slug: str, source_id: str, source_name: str,
                          rel: str, content: str, lang: Optional[str]) -> None:
    """Push a git-source file into the memory-api lexical index (Postgres FTS).

    Best-effort and additive: a memory-api hiccup must never abort the sync.
    Failures are logged (not silent) so a misconfigured MEMORY_API_URL shows up
    in embedder logs rather than as silently-unsearchable files.
    """
    if not MEMORY_API_URL:
        return
    try:
        resp = httpx.post(f"{MEMORY_API_URL}/git/index", json={
            "tenant_slug": tenant_slug or "default",
            "source_id":   source_id,
            "source_name": source_name,
            "path":        rel,
            "content":     content,
            "lang":        lang,
        }, headers=_internal_service_headers(), timeout=15)
        if resp.status_code >= 300:
            log.warning("git lexical index non-2xx: source=%s path=%s status=%d body=%s",
                        source_id, rel, resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("git lexical index failed: source=%s path=%s err=%s", source_id, rel, exc)


def _memory_api_git_indexed_count(tenant_slug: str, source_id: str) -> Optional[int]:
    """How many files memory-api holds for this source, or None if it can't say.

    None and 0 mean different things and the caller acts on the difference:
    0 is "the index is genuinely empty, rebuild it", None is "I could not find
    out", and guessing 0 from a failed request would rebuild every source on
    every sync whenever memory-api hiccups.
    """
    if not MEMORY_API_URL:
        return None
    try:
        resp = httpx.get(f"{MEMORY_API_URL}/git/stats",
                         params={"tenant_slug": tenant_slug or "default",
                                 "source_id": source_id},
                         headers=_internal_service_headers(),
                         timeout=15)
        if resp.status_code >= 300:
            log.warning("git index count non-2xx: source=%s status=%d body=%s",
                        source_id, resp.status_code, resp.text[:200])
            return None
        return int(resp.json().get("files", 0))
    except Exception as exc:
        log.warning("git index count failed: source=%s err=%s", source_id, exc)
        return None


def _memory_api_git_manifest(tenant_slug: str, source_id: str) -> Optional[dict]:
    """{path: md5 of indexed content} for this source, or None if unknown.

    As with the count, None is "could not find out" and must never be read as
    an empty index: an empty manifest re-indexes the whole source.
    """
    if not MEMORY_API_URL:
        return None
    try:
        resp = httpx.get(f"{MEMORY_API_URL}/git/manifest",
                         params={"tenant_slug": tenant_slug or "default",
                                 "source_id": source_id},
                         headers=_internal_service_headers(),
                         timeout=30)
        if resp.status_code >= 300:
            log.warning("git index manifest non-2xx: source=%s status=%d body=%s",
                        source_id, resp.status_code, resp.text[:200])
            return None
        files = resp.json().get("files")
        return dict(files) if isinstance(files, dict) else None
    except Exception as exc:
        log.warning("git index manifest failed: source=%s err=%s", source_id, exc)
        return None


def _git_source_rel_in_scope(rel: str, source: dict) -> bool:
    """Same path_prefix rule index_git_source_file applies."""
    path_prefix = source.get("path_prefix", "").strip("/")
    return not path_prefix or rel.startswith(path_prefix)


def _git_source_indexable_tree(source_dir: Path, source: dict) -> dict:
    """{rel path: absolute path} for every file in the checkout the index should hold."""
    out = {}
    for p in source_dir.rglob("*"):
        if not p.is_file() or not _is_indexable(p):
            continue
        rel = str(p.relative_to(source_dir))
        if _git_source_rel_in_scope(rel, source):
            out[rel] = p
    return out


def _git_file_md5(abs_path: Path) -> Optional[str]:
    """md5 of the text index_git_source_file would send, matching Postgres md5(content)."""
    try:
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _memory_api_git_delete_file(tenant_slug: str, source_id: str, rel: str) -> None:
    if not MEMORY_API_URL:
        return
    try:
        httpx.delete(f"{MEMORY_API_URL}/git/file",
                     params={"tenant_slug": tenant_slug or "default",
                             "source_id": source_id, "path": rel},
                     headers=_internal_service_headers(),
                     timeout=10)
    except Exception as exc:
        log.warning("git lexical delete failed: source=%s path=%s err=%s", source_id, rel, exc)


def _memory_api_git_delete_source(tenant_slug: str, source_id: str) -> None:
    if not MEMORY_API_URL:
        return
    try:
        httpx.delete(f"{MEMORY_API_URL}/git/source",
                     params={"tenant_slug": tenant_slug or "default",
                             "source_id": source_id},
                     headers=_internal_service_headers(),
                     timeout=15)
    except Exception as exc:
        log.warning("git lexical delete-source failed: source=%s err=%s", source_id, exc)


def index_git_source_file(abs_path: Path, source_dir: Path, source: dict) -> None:
    source_id   = source["id"]
    source_name = source.get("name", source_id)
    org_slug    = (source.get("org_slug") or "default").strip() or "default"
    path_prefix = source.get("path_prefix", "").strip("/")
    rel         = str(abs_path.relative_to(source_dir))
    if path_prefix and not rel.startswith(path_prefix):
        return
    if not _is_indexable(abs_path):
        return

    try:
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.error("Failed to read git source file %s: %s", abs_path, exc)
        return

    # Lexical index (Postgres FTS via memory-api) — always on. Cheap, always
    # fresh, immune to vector-dim re-embeds, and the verbatim file backs
    # git_read. Indexes raw on-disk content (frontmatter included) so reads
    # match the real file.
    _memory_api_git_index(org_slug, source_id, source_name, rel, raw, _lang_for(rel))

    # Dense embedding into Qdrant — opt-in (GIT_SOURCE_EMBED). Skipped by default.
    if not GIT_SOURCE_EMBED:
        return

    wiki_coll = _wiki_coll()
    try:
        is_markdown = abs_path.suffix.lower() in _GIT_MARKDOWN_EXTENSIONS
        if is_markdown:
            post  = frontmatter.loads(raw)
            tags  = post.metadata.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            tags  = [str(t) for t in tags]
            title  = str(post.metadata.get("title", abs_path.stem))
            body   = post.content
            chunks = chunk_markdown(body)
        else:
            tags   = []
            title  = rel
            body   = raw
            chunks = chunk_code(body, rel)

        _remove_git_source_file(rel, source_id)
        points = []
        for i, chunk in enumerate(chunks):
            text_for_embed = "\n".join(filter(None, [title, chunk["title"], chunk["content"]]))
            vec = get_embedding(text_for_embed)
            points.append(PointStruct(
                id=point_id(f"gs:{source_id}:{rel}", i),
                vector=vec,
                payload={
                    "path":          rel,
                    "title":         title,
                    "section_title": chunk["title"],
                    "tags":          tags,
                    "content":       chunk["content"][:3000],
                    "chunk_index":   i,
                    "source_type":   "git_source",
                    "source_id":     source_id,
                    "source_name":   source_name,
                    # Same defence-in-depth as the wiki page index path.
                    "tenant_id":     org_slug,
                },
            ))
        if points:
            qdrant().upsert(collection_name=wiki_coll, points=points)
            log.info("Git source embedded %d chunk(s) ← %s [source=%s]", len(points), rel, source_name)
    except Exception as exc:
        log.error("Failed to embed git source file %s: %s", abs_path, exc)


def _git_source_reconcile(source_dir: Path, source: dict, org_slug: str,
                          old_head: str) -> tuple:
    """Work out what an incremental sync must (re)index and delete.

    Returns (files to index as absolute paths, repo-relative paths to delete).

    The index is compared with the checkout by content hash, so whatever the
    index is missing or holds at old content is re-indexed, and whatever it
    holds that the checkout no longer has is dropped. That repairs drift from
    any cause, including drift left by earlier syncs, which a diff between two
    HEADs never sees: the diff only says what changed in the repo this time
    (issue #269, where adds and edits since mid-August were never indexed).

    When memory-api cannot produce a manifest, fall back to that diff.
    """
    source_id = source["id"]
    tree = _git_source_indexable_tree(source_dir, source)
    manifest = _memory_api_git_manifest(org_slug, source_id)
    if manifest is not None:
        changed = [p for rel, p in tree.items() if manifest.get(rel) != _git_file_md5(p)]
        deleted = [rel for rel in manifest if rel not in tree]
        if changed or deleted:
            log.info("Git source %s: index differs from checkout, %d file(s) to index, "
                     "%d to drop", source.get("name", source_id), len(changed), len(deleted))
        return changed, deleted

    # --no-renames: a rename must show as a delete of the old path plus an add
    # of the new one, or the old path stays in the index forever.
    changed_raw = _run_git("diff", "--name-only", "--no-renames", "--diff-filter=d",
                           old_head, "HEAD", cwd=str(source_dir))
    deleted_raw = _run_git("diff", "--name-only", "--no-renames", "--diff-filter=D",
                           old_head, "HEAD", cwd=str(source_dir))
    # Diff paths are relative to the repo root. _is_indexable stats the file,
    # so it has to be given the path inside the checkout: handed the bare
    # relative path it stats the process's own cwd, finds nothing, and every
    # changed file was silently dropped (issue #269). A deleted path cannot be
    # statted at all, so it is deleted unconditionally; dropping a path the
    # index never held is a no-op.
    changed = [source_dir / f for f in changed_raw.splitlines()
               if f and _git_source_rel_in_scope(f, source) and _is_indexable(source_dir / f)]
    deleted = [f for f in deleted_raw.splitlines() if f]
    # A git diff answers "what changed in the repo", which is only the
    # right question when the index already holds everything that did
    # not. When the clone is current but the index has nothing for this
    # source (a restored volume, a rebuilt git_files table, a clone
    # older than the lexical index) the diff is empty, the sync
    # reports success, and the source stays unsearchable through every
    # future sync (issue #136). Ask what is actually indexed, and index
    # the whole checkout when the answer is nothing.
    if _memory_api_git_indexed_count(org_slug, source_id) == 0:
        changed = list(tree.values())
        if changed:
            log.info("Git source %s has a current clone but an empty index, "
                     "indexing all %d file(s)", source.get("name", source_id), len(changed))
    return changed, deleted


def sync_one_source(source: dict, force: bool = False) -> None:
    """
    Incremental sync of a git source. With force=True, behaves as a full
    reindex: every indexable file is re-embedded regardless of git diff,
    and any stale chunks from a prior indexing pass are cleared first.
    This is what the "Rebuild" button calls — useful after a vector-dim
    change rebuilt the wiki collection and dropped this source's chunks,
    or to recover from a partial/failed index.
    """
    source_id  = source["id"]
    branch     = source.get("branch", "main")
    repo_url   = source["repo_url"]
    # Creds are supplied via -c http.extraHeader on every git invocation that
    # hits the network (clone/fetch). The URL itself stays clean so .git/config
    # never holds a token on disk, and the *current* token from the DB is used
    # every time — picks up rotations without re-cloning.
    auth       = _git_auth_args(source.get("token", ""))
    # Clones live under /git-sources/<org_slug>/<source_id>/ — per-org subdir
    # for filesystem-level scoping. Older clones at /git-sources/<source_id>/
    # are migrated in place on first sync after upgrade.
    org_slug   = (source.get("org_slug") or "default").strip() or "default"
    source_dir = GIT_SOURCES_DIR / org_slug / source_id
    legacy_dir = GIT_SOURCES_DIR / source_id
    if legacy_dir.exists() and legacy_dir.is_dir() and not source_dir.exists():
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        legacy_dir.rename(source_dir)
        log.info("Migrated git source %s clone from %s to %s",
                 source_id, legacy_dir, source_dir)

    with _git_source_lock:
        _git_source_states[source_id] = {**_git_source_states.get(source_id, {}),
                                          "status": "syncing", "error": None}
    try:
        _check_git_ref(branch, repo_url)
        GIT_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        fresh_clone = not (source_dir / ".git").exists()
        if fresh_clone:
            # `--` ends option parsing: nothing after it is read as a flag.
            _run_git(*auth, "clone", "--depth", "1", f"--branch={branch}", "--",
                     repo_url, str(source_dir))
            changed_files = list(_git_source_indexable_tree(source_dir, source).values())
            deleted_files: list = []
        elif force:
            # Pull latest, then treat every indexable file as changed and wipe
            # all existing chunks for this source so the index is rebuilt clean.
            # set-url rewrites origin to the clean repo_url every sync — idempotent
            # no-op on already-clean clones, scrubs the token from clones made by
            # earlier versions that embedded creds in the URL.
            _run_git("remote", "set-url", "--", "origin", repo_url, cwd=str(source_dir))
            try:
                _run_git(*auth, "fetch", "--", "origin", branch, cwd=str(source_dir))
                _run_git("merge", "--ff-only", "--", f"origin/{branch}", cwd=str(source_dir))
            except Exception as exc:
                log.warning("force-rebuild: git pull failed for %s (continuing with local HEAD): %s",
                            source_id, exc)
            wiki_coll = _wiki_coll()
            try:
                qdrant().delete(
                    collection_name=wiki_coll,
                    points_selector=FilterSelector(
                        filter=Filter(must=[
                            FieldCondition(key="source_id", match=MatchValue(value=source_id)),
                        ])
                    ),
                )
            except Exception as exc:
                log.warning("force-rebuild: failed to clear chunks for %s: %s", source_id, exc)
            _memory_api_git_delete_source(org_slug, source_id)
            changed_files = list(_git_source_indexable_tree(source_dir, source).values())
            deleted_files = []
        else:
            # Scrub any embedded-cred origin URL from older versions before fetch.
            _run_git("remote", "set-url", "--", "origin", repo_url, cwd=str(source_dir))
            old_head = _run_git("rev-parse", "HEAD", cwd=str(source_dir)).strip()
            _run_git(*auth, "fetch", "--", "origin", branch, cwd=str(source_dir))
            _run_git("merge", "--ff-only", "--", f"origin/{branch}", cwd=str(source_dir))
            changed_files, deleted_files = _git_source_reconcile(
                source_dir, source, org_slug, old_head)

        for rel in deleted_files:
            _remove_git_source_file(rel, source_id)
            _memory_api_git_delete_file(org_slug, source_id, rel)

        file_count = 0
        for fp in changed_files:
            if fp.exists():
                index_git_source_file(fp, source_dir, source)
                file_count += 1

        # file_count is read by list_sources as "how big is this repo in the
        # index", so it has to be the source's total and not the delta this run
        # happened to touch. Reporting the delta made a healthy, fully indexed
        # repo advertise files: 0 after any no-change sync (issue #136).
        indexed_total = _memory_api_git_indexed_count(org_slug, source_id)
        # What the index should hold, so a reader can see when it does not.
        indexable_total = len(_git_source_indexable_tree(source_dir, source))
        now = datetime.datetime.utcnow()
        with _git_source_lock:
            _git_source_states[source_id] = {
                "status":       "idle",
                "error":        None,
                "last_sync":    now.isoformat() + "Z",
                "last_sync_dt": now,
                "file_count":   indexed_total if indexed_total is not None else file_count,
                "indexed_this_run": file_count,
                "indexable_files":  indexable_total,
            }
        log.info("Git source sync complete: %s (%d file(s) indexed this run, %s in index, "
                 "%d indexable in checkout)",
                 source.get("name", source_id), file_count,
                 indexed_total if indexed_total is not None else "unknown", indexable_total)
        if indexed_total is not None and indexed_total != indexable_total:
            log.warning("Git source %s: index holds %d file(s) but the checkout has %d "
                        "indexable", source.get("name", source_id), indexed_total,
                        indexable_total)

    except Exception as exc:
        log.error("Git source sync failed for %s: %s", source_id, exc)
        with _git_source_lock:
            _git_source_states[source_id] = {**_git_source_states.get(source_id, {}),
                                              "status": "error", "error": str(exc)}


def _fetch_git_sources() -> list:
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(f"{EDITOR_URL}/api/internal/config",
                                     headers=_internal_service_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read())
            return (data.get("git_sources") or {}).get("sources", [])
    except Exception:
        return []


def _git_source_poller_loop() -> None:
    while True:
        try:
            sources = _fetch_git_sources()
            min_interval = 3600
            now = datetime.datetime.utcnow()
            for src in sources:
                if not src.get("enabled", True):
                    continue
                interval_min = src.get("sync_interval_minutes", 60)
                # 0 / None = no recurring auto-sync (sync once on create or via
                # "Sync Now" only). Skip scheduling, and don't let a 0 collapse
                # the poller's sleep interval to zero.
                if not interval_min or interval_min <= 0:
                    continue
                interval_s  = interval_min * 60
                min_interval = min(min_interval, interval_s)
                state   = _git_source_states.get(src["id"], {})
                last_dt = state.get("last_sync_dt")
                if state.get("status") == "syncing":
                    continue
                if last_dt is None or (now - last_dt).total_seconds() >= interval_s:
                    threading.Thread(target=sync_one_source, args=(src,), daemon=True).start()
        except Exception as exc:
            log.error("Git source poller error: %s", exc)
        time.sleep(min(min_interval if sources else 3600, 300))


# ── Auth-context helpers ──────────────────────────────────────────────────
# nginx forwards X-Auth-* headers from the editor's auth_request.  These
# helpers read them and call back to the editor for ACL / memory-scope info.

def _auth_ctx(request: Request) -> Optional[dict]:
    """Returns {'user_id', 'org_id', 'org_slug', 'role'} or None when no auth headers are set."""
    if request is None:
        return None
    user_id = request.headers.get("x-auth-user-id") or ""
    if not user_id:
        return None
    return {
        "user_id":  user_id,
        "org_id":   request.headers.get("x-auth-org-id")   or "",
        "org_slug": request.headers.get("x-auth-org-slug") or "",
        "role":     request.headers.get("x-auth-role")     or "user",
        # The reader's token display name, forwarded by the MCP so a memory
        # owned only by an `owner:<name>` tag (compactor promotions carry no
        # author_id) is visible to the token that owns that name (issue #85).
        # nginx strips this on browser-facing routes so it can't be spoofed;
        # only the internal MCP call sets it.
        "owner_name": request.headers.get("x-auth-owner-name") or "",
    }


def _is_internal_service_request(request: Request) -> bool:
    """True iff the caller presented a valid X-Internal-Service token.

    Used by the compactor (memory-api) and other peer services that need
    to act on behalf of any tenant — they can't carry a user session.
    Fail-closed: if INTERNAL_SERVICE_TOKEN is unset, no caller can pass
    this check, so the slug-as-identity rule below applies universally.
    """
    if not INTERNAL_SERVICE_TOKEN:
        return False
    presented = (request.headers.get("x-internal-service") or "").strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, INTERNAL_SERVICE_TOKEN)


def _resolve_tenant_slug(request: Request, tenant_slug: str) -> str:
    """Resolve the tenant slug for this request, with identity as the source
    of truth.

    Two trust paths:
      1. End-user request via nginx: X-Auth-Org-Slug is set by the editor's
         auth_check from the session's authenticated DB org row. This slug
         is authoritative. If the caller also supplied a body/query
         tenant_slug that disagrees, raise 403 — that is an attempted
         cross-tenant access from inside an authenticated session.
      2. Internal-service request (e.g. memory-api compactor → embedder):
         the X-Internal-Service header proves peer identity, and the body
         tenant_slug names the tenant the operation targets. This is the
         only path that may legitimately specify an arbitrary slug.

    Absent both, refuse to accept a non-default tenant_slug. The legacy
    single-org "default" case still works (no auth headers, no
    tenant_slug -> "default").
    """
    body_or_query = (tenant_slug or "").strip()
    slug_hdr      = (request.headers.get("x-auth-org-slug") or "").strip()

    if _is_internal_service_request(request):
        # Peer service driving a write/read on behalf of a specific tenant
        # (compactor promotion, cross-org admin restore, etc).  Trust the
        # body slug; fall back to the auth-header slug, then default.
        return body_or_query or slug_hdr or "default"

    if slug_hdr:
        # Authenticated user. The header is the identity; any mismatch is a
        # cross-tenant attempt and must be refused.  "default" body value is
        # tolerated because it's the FastAPI Query() default — callers that
        # didn't supply a slug at all get coerced to the user's real org.
        if body_or_query and body_or_query != slug_hdr and body_or_query != "default":
            raise HTTPException(
                status_code=403,
                detail="tenant_slug does not match authenticated org",
            )
        return slug_hdr

    # No authenticated identity and no internal-service proof.  Reject any
    # attempt to pick a non-default tenant rather than silently routing to
    # the global default collection.
    if body_or_query and body_or_query != "default":
        raise HTTPException(
            status_code=403,
            detail="tenant_slug requires an authenticated session",
        )
    return "default"


def _is_admin(ctx: Optional[dict]) -> bool:
    return bool(ctx and ctx.get("role") in ("platform_admin", "org_admin", "team_admin"))


def _fetch_memory_scope(user_id: str, org_id: str) -> dict:
    """Ask the editor what memories this user is allowed to see."""
    import urllib.request, urllib.parse, json as _json
    qs = urllib.parse.urlencode({"user_id": user_id, "org_id": org_id})
    try:
        with urllib.request.urlopen(f"{EDITOR_URL}/api/internal/memory-scope?{qs}", timeout=5) as r:
            return _json.loads(r.read())
    except Exception as exc:
        log.warning("memory-scope lookup failed: %s — defaulting to individual scope", exc)
        return {"share_mode": "individual", "scope_user_ids": [user_id]}


def _fetch_git_source_access(user_id: str, org_id: str, source_ids: List[str]) -> set:
    """Ask the editor which git source IDs the user can see."""
    if not source_ids:
        return set()
    import urllib.request, urllib.parse, json as _json
    qs = urllib.parse.urlencode({
        "user_id": user_id, "org_id": org_id,
        "source_ids": ",".join(source_ids),
    })
    try:
        with urllib.request.urlopen(f"{EDITOR_URL}/api/internal/git-source-access?{qs}", timeout=5) as r:
            return set(_json.loads(r.read()).get("allowed", []))
    except Exception as exc:
        log.warning("git-source-access lookup failed: %s — denying all requested sources", exc)
        return set()


def _fetch_user_git_sources(user_id: str, org_id: str) -> list:
    """Return the git sources visible to the user (full metadata, secrets stripped)."""
    import urllib.request, urllib.parse, json as _json
    qs = urllib.parse.urlencode({"user_id": user_id, "org_id": org_id})
    try:
        with urllib.request.urlopen(f"{EDITOR_URL}/api/internal/git-sources?{qs}", timeout=5) as r:
            return _json.loads(r.read()).get("sources", []) or []
    except Exception as exc:
        log.warning("git-sources lookup failed: %s", exc)
        return []


def _post_memory_edit_notification(memory_id: str, original_author_id: str, edited_by_id: str) -> None:
    """Tell the editor an admin edited someone else's memory."""
    import urllib.request, json as _json
    payload = _json.dumps({
        "memory_id": memory_id,
        "original_author_id": original_author_id,
        "edited_by_id": edited_by_id,
    }).encode()
    req = urllib.request.Request(
        f"{EDITOR_URL}/api/internal/memory-edit-notify",
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:
        log.warning("memory-edit-notify failed: %s", exc)


def _owner_tag(user_id: str) -> str:
    return f"owner:{user_id}"


def _payload_owner(payload: dict) -> Optional[str]:
    """Extract owner user_id from a memory payload."""
    if payload.get("author_id"):
        return payload["author_id"]
    for tag in (payload.get("tags") or []):
        if isinstance(tag, str) and tag.startswith("owner:"):
            return tag.split(":", 1)[1]
    return None


def _owner_name_matches(payload: dict, ctx: Optional[dict]) -> bool:
    """True iff the memory carries an `owner:<name>` tag equal to the reader's
    token name (issue #85).

    Agent-captured memories are attributed only by this tag — the compactor
    promotion carries no author_id — so the token name is the only owner
    signal for them. Used for read visibility AND edit/delete authorization so
    a token can manage the memories it owns by name. The name is forwarded by
    the MCP and stripped by nginx on browser routes, so it can't be spoofed by
    a browser client."""
    reader_name = (ctx or {}).get("owner_name")
    if not reader_name:
        return False
    for t in (payload.get("tags") or []):
        if isinstance(t, str) and t.startswith("owner:") and t.split(":", 1)[1] == reader_name:
            return True
    return False


# ── FastAPI app ────────────────────────────────────────────────────────────
app = FastAPI(title="KB Embedder / Search")
# Embedder is reached same-origin through nginx (browser) or service-to-
# service on the internal network. No cross-origin browser access is
# legitimate, so allow_origins is empty (was ["*"], a defence-in-depth
# gap if the port were ever published).
app.add_middleware(CORSMiddleware, allow_origins=[],
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    # Wait for editor backend readiness marker
    backend_ready_file = os.environ.get("BACKEND_READINESS_FILE", "/tmp/backend_ready")
    timeout = int(os.environ.get("BACKEND_INIT_TIMEOUT", "300"))
    elapsed = 0
    log.info("Waiting for backend to be initialized...")
    while elapsed < timeout:
        if os.path.exists(backend_ready_file):
            log.info("Backend is ready")
            break
        time.sleep(1)
        elapsed += 1
    if not os.path.exists(backend_ready_file):
        log.warning("Backend readiness timeout after %ds, proceeding anyway", timeout)

    wait_for_qdrant()

    # Load initial embedding backend from config (best-effort; reindex will also reload)
    initial_cfg = _load_embedding_config()
    try:
        _activate_backend(initial_cfg)
    except Exception as exc:
        log.warning("Initial embedding backend load failed (%s); will retry at reindex", exc)

    # Log current vector size for debugging (helps diagnose vector mismatch issues)
    with _embed_lock:
        vsize = _vector_size
    log.info(
        "Embedding backend ready: provider=%s model=%s vector_size=%d",
        initial_cfg.get("provider", "unknown"),
        initial_cfg.get("model", "unknown"),
        vsize,
    )

    ensure_collection()

    observer = Observer()
    observer.schedule(_Handler(), str(DOCS_DIR), recursive=True)
    observer.daemon = True
    observer.start()
    log.info("Watching %s for changes", DOCS_DIR)

    with _state_lock:
        _reindex_state["state"] = "watching"

    # Initial reindex in background. Not forced: the vectors on the persistent
    # volume are still valid, so only changed or missing pages are re-embedded
    # (#421). A model change or a missing point still re-embeds that page.
    threading.Thread(target=full_reindex, kwargs={"force": False}, daemon=True).start()

    # Git external sources poller
    threading.Thread(target=_git_source_poller_loop, daemon=True).start()


@app.get("/health")
def health():
    with _embed_lock:
        provider = _embed_cfg.get("provider", "unknown")
        model    = _embed_cfg.get("model", "unknown")
        vsize    = _vector_size
    return {
        "status":         "ok",
        "collection":     COLLECTION_NAME,
        "vector_size":    vsize,
        "provider":       provider,
        "model":          model,
        "model_revision": _backend_revision(provider),
    }


def _wiki_hit_source(payload: dict) -> dict:
    """Source attribution for a wiki/git-source hit. Lets agents tell
    a synced-repo file apart from a hand-written wiki page."""
    if payload.get("source_type") == "git_source":
        return {
            "type": "git_source",
            "id":   payload.get("source_id"),
            "name": payload.get("source_name"),
        }
    return {"type": "wiki"}


def _wiki_hit_visible(payload: dict, ctx: Optional[dict],
                      allowed_git_sources: Optional[set],
                      tenant_slug: Optional[str] = None) -> bool:
    """Apply WikiSpaceACL and GitSourceACL filtering to a wiki search hit."""
    # Cross-tenant defence-in-depth: a wiki chunk whose stamped tenant_id
    # disagrees with the resolved slug is never visible, even on the
    # ctx-less path. Legacy chunks (tenant_id=None) are tolerated.
    if tenant_slug is not None:
        pt = (payload or {}).get("tenant_id")
        if pt is not None and pt != tenant_slug:
            return False
    # Git-sourced result → must be in the allowed source set
    if payload.get("source_type") == "git_source":
        if allowed_git_sources is None:
            return True   # no auth context → permissive
        return payload.get("source_id") in allowed_git_sources
    # Native wiki result → top-level dir = space; admins always allowed
    if ctx is None or _is_admin(ctx):
        return True
    path = payload.get("path") or ""
    space = path.strip("/").split("/", 1)[0] if "/" in path.strip("/") else ""
    if not space:
        return True
    # The editor returns the user's accessible spaces lazily — for search we
    # rely on the WikiSpaceACL DB the editor manages.  Defer to a single
    # editor RPC per request so we don't N+1.
    return space in (ctx.get("_allowed_spaces") or set())


def _resolve_allowed_spaces(ctx: dict) -> set:
    """Cache-once helper: list of wiki spaces the user can read in their org."""
    if "_allowed_spaces" in ctx:
        return ctx["_allowed_spaces"]
    import urllib.request, urllib.parse, json as _json
    qs = urllib.parse.urlencode({"user_id": ctx["user_id"], "org_id": ctx["org_id"]})
    try:
        with urllib.request.urlopen(f"{EDITOR_URL}/api/internal/wiki-spaces?{qs}", timeout=5) as r:
            data = _json.loads(r.read())
            ctx["_allowed_spaces"] = set(data.get("spaces") or [])
    except Exception as exc:
        log.warning("wiki-spaces lookup failed: %s — defaulting to permissive", exc)
        ctx["_allowed_spaces"] = None  # None = bypass space filtering
    return ctx["_allowed_spaces"] or set()


def _collect_git_source_ids(hits) -> List[str]:
    return [h.payload.get("source_id") for h in hits
            if h.payload.get("source_type") == "git_source" and h.payload.get("source_id")]


@app.get("/api/search")
def search(
    request: Request,
    q: str = Query(..., description="Natural-language query"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter by (AND logic)"),
    limit: int = Query(10, ge=1, le=50),
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
):
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    vec = get_embedding(q)

    query_filter = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            query_filter = Filter(
                must=[FieldCondition(key="tags", match=MatchAny(any=tag_list))]
            )

    fetch = max(limit * 4, 50) if ctx and not _is_admin(ctx) else limit
    hits = qdrant().search(
        collection_name=_wiki_coll(tenant_slug),
        query_vector=vec,
        query_filter=query_filter,
        limit=fetch,
        with_payload=True,
    )

    allowed_sources = None
    if ctx and not _is_admin(ctx):
        _resolve_allowed_spaces(ctx)
        gs_ids = _collect_git_source_ids(hits)
        allowed_sources = _fetch_git_source_access(ctx["user_id"], ctx["org_id"], gs_ids) if gs_ids else set()

    visible = [h for h in hits if _wiki_hit_visible(h.payload, ctx, allowed_sources, tenant_slug)]
    return {
        "query":      q,
        "tags_filter": tags,
        "results": [
            {
                "score":         round(h.score, 4),
                "path":          h.payload.get("path"),
                "title":         h.payload.get("title"),
                "section_title": h.payload.get("section_title"),
                "tags":          h.payload.get("tags", []),
                "excerpt":       h.payload.get("content", "")[:400],
                "source":        _wiki_hit_source(h.payload),
            }
            for h in visible[:limit]
        ],
    }


@app.get("/api/search/unified")
def search_unified(
    request: Request,
    q: str = Query(..., description="Natural-language query"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter (applied to both sources)"),
    limit: int = Query(20, ge=1, le=50),
    wiki_boost: float = Query(1.15, ge=1.0, le=2.0, description="Score multiplier applied to wiki results"),
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
):
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    scope = None
    if ctx and not _is_admin(ctx):
        scope = _fetch_memory_scope(ctx["user_id"], ctx["org_id"])
        scope["scope_user_ids"] = set(scope.get("scope_user_ids") or [])
        _resolve_allowed_spaces(ctx)
    """
    Unified semantic search across both the wiki (knowledge_base) and memories collections.
    Wiki results are boosted by `wiki_boost` (default ×1.15) so that when scores are
    close, factual wiki content ranks higher.  High-confidence memories still beat weak
    wiki matches.  Results are tagged with `source: "wiki"` or `source: "memory"`.
    """
    vec = get_embedding(q)
    wiki_collection   = _wiki_coll(tenant_slug)
    memory_collection = _mem_coll(tenant_slug)

    query_filter = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            query_filter = Filter(
                must=[FieldCondition(key="tags", match=MatchAny(any=tag_list))]
            )

    # Query both collections in parallel
    per_source = max(limit, 10)

    def _wiki_hits():
        return qdrant().search(
            collection_name=wiki_collection,
            query_vector=vec,
            query_filter=query_filter,
            limit=per_source,
            with_payload=True,
        )

    def _memory_hits():
        return qdrant().search(
            collection_name=memory_collection,
            query_vector=vec,
            query_filter=query_filter,
            limit=per_source,
            with_payload=True,
        )

    mem_ok, mem_stored_dim = _memory_coll_dim_ok(memory_collection)
    if not mem_ok:
        with _embed_lock:
            cur_dim = _vector_size
        log.error(
            "Memory collection %r vector_size mismatch (stored=%s, current=%s) — "
            "memory search disabled until operator restores the prior embedding model "
            "or restarts with EMBEDDER_FORCE_COLLECTION_RECREATE=memories (DATA LOSS).",
            memory_collection, mem_stored_dim, cur_dim,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        wiki_future   = pool.submit(_wiki_hits)
        memory_future = pool.submit(_memory_hits) if mem_ok else None
        wiki_hits_res = wiki_future.result()
        if memory_future is None:
            memory_hits_res = []
        else:
            try:
                memory_hits_res = memory_future.result()
            except Exception as exc:
                log.warning(
                    "Memory collection search failed (returning wiki-only results): %s",
                    _qdrant_error_detail(exc),
                )
                memory_hits_res = []

    # Resolve git-source access in one batch
    allowed_sources = None
    if ctx and not _is_admin(ctx):
        gs_ids = _collect_git_source_ids(wiki_hits_res)
        allowed_sources = _fetch_git_source_access(ctx["user_id"], ctx["org_id"], gs_ids) if gs_ids else set()

    results = []

    for h in wiki_hits_res:
        if not _wiki_hit_visible(h.payload, ctx, allowed_sources, tenant_slug):
            continue
        raw   = round(h.score, 4)
        adj   = round(h.score * wiki_boost, 4)
        results.append({
            "source":        "wiki",
            "origin":        _wiki_hit_source(h.payload),
            "score":         adj,
            "raw_score":     raw,
            "path":          h.payload.get("path"),
            "title":         h.payload.get("title"),
            "section_title": h.payload.get("section_title"),
            "tags":          h.payload.get("tags", []),
            "excerpt":       h.payload.get("content", "")[:400],
            # memory-specific fields absent for wiki hits
            "id":            None,
            "created_at":    None,
        })

    for h in memory_hits_res:
        if not _memory_in_scope(h.payload, ctx, scope, tenant_slug):
            continue
        raw = round(h.score, 4)
        mem = _memory_payload_to_dict(h.payload)
        results.append({
            "source":        "memory",
            "score":         raw,
            "raw_score":     raw,
            "path":          None,
            "title":         mem.get("content", "")[:80],
            "section_title": None,
            "tags":          mem.get("tags", []),
            "excerpt":       mem.get("content", "")[:400],
            "id":            mem.get("id"),
            "created_at":    mem.get("created_at"),
            "tier":          "durable",
        })

    # Hot-tier lexical hits — captures from the last few days that haven't
    # made it through the compactor yet. Surfaces fresh content for the
    # short-term-recall path. Failures here are silent — purely additive
    # to the semantic search above.
    for hot in _fetch_hot_tier_hits(tenant_slug, q, tags, limit):
        results.append({
            "source":        "memory",
            "score":         round(float(hot.get("score") or 0.0), 4),
            "raw_score":     round(float(hot.get("score") or 0.0), 4),
            "path":          None,
            "title":         (hot.get("content") or "")[:80],
            "section_title": None,
            "tags":          hot.get("tags") or [],
            "excerpt":       (hot.get("content") or "")[:400],
            "id":            hot.get("id"),
            "created_at":    hot.get("created_at"),
            "tier":          "hot",
        })

    # Hot rows tie-break ahead of durable when scores collide, since the
    # blended hot score already accounts for recency — a freshly captured
    # match should beat a stale durable one of equal lexical/semantic
    # confidence.
    results.sort(key=lambda r: (r["score"], 1 if r.get("tier") == "hot" else 0),
                 reverse=True)
    results = results[:limit]

    return {
        "query":      q,
        "tags_filter": tags,
        "wiki_boost": wiki_boost,
        "results":    results,
    }


@app.post("/api/reindex")
def trigger_reindex(
    request: Request,
    scope: str = Query(default=""),
    org_slug: str = Query(default=""),
):
    """Trigger a wiki reindex.

    Web users (routed through nginx, which injects X-Auth-Org-Slug) rebuild
    only their own org's collection so a single-org rebuild can't blank or
    stall another org's search. Platform admins may add ?scope=all to force
    a full all-orgs rebuild (e.g. after a global embedding-model change).

    Internal editor calls (no auth headers on the docker network) must
    present X-Internal-Service to pick a specific ?org_slug=<slug>.
    Previously this branch accepted any caller, which was a free
    cross-tenant DOS / reindex-of-anybody primitive.
    """
    ctx = _auth_ctx(request)
    if ctx:
        slug = ctx.get("org_slug") or "default"
        role = ctx.get("role") or ""
        if scope == "all" and role == "platform_admin":
            threading.Thread(target=full_reindex, kwargs={"force": True}, daemon=True).start()
        else:
            threading.Thread(target=reindex_org, args=(slug,), daemon=True).start()
        return {"status": "reindex started"}
    if _request_is_internal_service(request):
        slug = (org_slug or "default").strip() or "default"
        threading.Thread(target=reindex_org, args=(slug,), daemon=True).start()
        return {"status": "reindex started"}
    raise HTTPException(status_code=401, detail="Authentication required")


class FileReindexBody(BaseModel):
    path: str            # path relative to DOCS_DIR (e.g. "acme/notes.md")
    tenant: str = ""     # tenant slug for collection routing
    deleted: bool = False  # True ⇒ remove from index, False ⇒ (re)index


@app.post("/api/reindex/file")
def trigger_file_reindex(body: FileReindexBody, request: Request):
    """Apply a single-page index change immediately, without a full rebuild.

    The editor calls this the moment a page is recycled, deleted, restored, or
    saved so the search index stays in sync in real time instead of waiting for
    the next full reindex (or a flaky filesystem watch event).
    """
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")

    raw = body.path.strip().lstrip("/")
    if not raw:
        raise HTTPException(status_code=422, detail="path is required")

    # Reject path traversal: previously body.path was passed straight through
    # to DOCS_DIR / rel. With no auth check on this route, an attacker could
    # read /etc/passwd by sending {"path": "../../../etc/passwd", "tenant":
    # "..."} — index_file would load and embed it into the named tenant's
    # wiki collection. Lock everything to paths that resolve inside DOCS_DIR.
    if ".." in raw.split("/") or raw.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be DOCS_DIR-relative (no '..', no leading '/')")
    target = (DOCS_DIR / raw).resolve()
    try:
        target.relative_to(DOCS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes DOCS_DIR")

    rel  = raw
    slug = (body.tenant or "").strip()
    if not slug:
        slug = _tenant_from_path(target)
    if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise HTTPException(status_code=400, detail="tenant slug must match [a-z0-9-]")

    if body.deleted:
        _remove_file(rel, slug)
        log.info("Removed from index on request: %s [tenant=%s]", rel, slug)
        return {"status": "removed", "path": rel}

    if not target.exists():
        # File isn't on the mirror (yet) — nothing to index. Treat as a no-op
        # rather than an error so callers don't have to race the mirror write.
        return {"status": "skipped", "path": rel}
    try:
        index_file(target, slug)
    except Exception as exc:
        raise _internal_http_error(500, "index failed", exc)
    return {"status": "indexed", "path": rel}


@app.post("/api/admin/memories/reembed")
def trigger_reembed_memories(request: Request):
    """Re-embed all memories whose Qdrant collection has a vector-size mismatch.

    Reads each memory's content from the existing payload, recreates the
    collection at the current embedding dim, and re-inserts everything with
    fresh vectors.  No data is lost — memories are fully recoverable from
    their payload content.
    """
    ctx = _auth_ctx(request)
    if not _is_admin(ctx):
        raise HTTPException(status_code=403, detail="Admin role required")
    with _reembed_lock:
        if _reembed_state["state"] == "running":
            return {"status": "already running", **_reembed_state}
    threading.Thread(target=reembed_memories, daemon=True).start()
    return {"status": "started"}


@app.get("/api/admin/memories/reembed/status")
def reembed_status(request: Request):
    """Current status of a running or completed memory re-embed job."""
    ctx = _auth_ctx(request)
    if not _is_admin(ctx):
        raise HTTPException(status_code=403, detail="Admin role required")
    with _reembed_lock:
        return dict(_reembed_state)


@app.get("/api/admin/memories/dim-check")
def memory_dim_check(request: Request):
    """Report vector-size and model-identity status for every memories collection.

    `ok` is the dim-match (a `false` here means search is broken until re-embed).
    `model_ok` is the model-match (a `false` here means the vectors live in a
    different embedding space than the current backend — search recall degrades
    silently). Both flags are surfaced so the UI can show distinct banners.
    """
    ctx = _auth_ctx(request)
    # Previously accepted any admin (org_admin, team_admin, platform_admin),
    # but this endpoint enumerates EVERY memories collection across all
    # tenants — an org_admin in tenant A could observe tenant B's
    # existence, the embedding model history each org was on, and infer
    # rough activity from collection presence. Restrict to platform_admin
    # so cross-tenant enumeration requires a cross-tenant role.
    if not ctx or ctx.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform admin role required")
    with _embed_lock:
        cur_dim      = _vector_size
        cur_provider = (_embed_cfg or {}).get("provider", "unknown") or "unknown"
        cur_model    = (_embed_cfg or {}).get("model",    "unknown") or "unknown"

    state = _load_embedding_state().get("collections", {})

    all_colls = [c.name for c in qdrant().get_collections().collections]
    mem_colls = [c for c in all_colls
                 if c == MEMORIES_COLLECTION or c.endswith("_memories")]
    results = []
    for coll in mem_colls:
        try:
            info = qdrant().get_collection(coll)
            stored = info.config.params.vectors.size
        except Exception as exc:
            results.append({"collection": coll, "ok": False, "error": str(exc)})
            continue
        rec = state.get(coll, {})
        stored_provider = rec.get("provider", "unknown")
        stored_model    = rec.get("model",    "unknown")
        results.append({
            "collection":      coll,
            "stored_dim":      stored,
            "current_dim":     cur_dim,
            "ok":              stored == cur_dim,
            "stored_provider": stored_provider,
            "stored_model":    stored_model,
            # An "unknown" stored_model means we genuinely don't know — treat as
            # mismatched so the user can re-embed and clear the warning.
            "model_ok":        stored_model != "unknown" and stored_model == cur_model,
        })
    return {
        "current_embedding_dim":      cur_dim,
        "current_embedding_provider": cur_provider,
        "current_embedding_model":    cur_model,
        "collections":                results,
    }


# ── Git source endpoints ───────────────────────────────────────────────────

@app.get("/api/git-sources/status")
def git_sources_status(request: Request):
    """Cross-org git-source sync state — exposed only to peer services
    (the editor's admin UI fetches it server-side and filters per org).
    Previously open: leaked every org's source ids, last_sync timestamps,
    and error strings (which can include repo URLs / auth failures with
    token snippets in stderr) to any caller on the internal network."""
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    with _git_source_lock:
        return dict(_git_source_states)


def _git_source_clone_dir(source: dict) -> Optional[Path]:
    """Where this source's checkout actually lives, or None if it has none.

    Two layouts are in play: the current per-org
    `GIT_SOURCES_DIR/<org_slug>/<source_id>/`, and the older unslugged
    `GIT_SOURCES_DIR/<source_id>/` that clones sit at until the next sync
    migrates them. Every reader has to try both, and when two readers each
    wrote their own version of "try both" they disagreed about what the slug
    was: one defaulted to "default", the other to "", so the same checkout
    was found by one and declared missing by the other (issue #136). One
    resolver, one answer.
    """
    sid = source.get("id")
    if not sid:
        return None
    slug = (source.get("org_slug") or source.get("tenant_slug") or "").strip()
    candidates = [GIT_SOURCES_DIR / slug / sid] if slug else []
    candidates.append(GIT_SOURCES_DIR / sid)
    return next((c for c in candidates if (c / ".git").exists()), None)


def _git_source_disk_state(source: dict) -> dict:
    """Recover a source's sync state from the checkout when memory has none.

    The in-process state dict is lost on every restart. The clone is not: it
    lives on a mounted volume, so the checkout itself is the durable record of
    whether — and roughly when — this source last synced.

      last_sync   .git/FETCH_HEAD's mtime, written by every fetch. Falls back
                  to HEAD's own mtime for a clone that has never re-fetched.
      file_count  tracked files, straight from the index.

    Best-effort by design: a source whose clone is missing genuinely has not
    synced on this host, and reports exactly that.
    """
    if not source.get("id"):
        return {}
    d = _git_source_clone_dir(source)
    if d is None:
        return {"status": "not cloned"}

    out: dict = {"status": "idle (from disk)"}
    for marker in (d / ".git" / "FETCH_HEAD", d / ".git" / "HEAD"):
        try:
            if marker.exists():
                out["last_sync"] = (datetime.datetime.utcfromtimestamp(
                    marker.stat().st_mtime).isoformat() + "Z")
                break
        except OSError:
            pass
    try:
        import subprocess as _sp
        r = _sp.run(["git", "-C", str(d), "ls-files"],
                    capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            out["file_count"] = sum(1 for line in r.stdout.splitlines() if line)
    except Exception:
        pass
    return out


@app.get("/api/git-sources")
def git_sources_for_user(request: Request):
    """List the git sources visible to the calling user, enriched with sync state.
    Used by the MCP `list_sources` tool so agents can see the perimeter of
    synced repos before searching."""
    ctx = _auth_ctx(request)
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")
    sources = _fetch_user_git_sources(ctx["user_id"], ctx["org_id"])
    with _git_source_lock:
        states = dict(_git_source_states)
    out = []
    for s in sources:
        st = states.get(s.get("id")) or {}
        # _git_source_states is process memory, so a restart empties it while
        # the clone and its index both survive on disk. Reporting that as
        # "never synced, no files" was simply wrong — and confusingly so,
        # because git_search and git_read kept working against the same repo
        # (issue #93). Fall back to what the checkout itself can tell us.
        disk = _git_source_disk_state(s) if not st.get("last_sync") else {}
        out.append({
            **s,
            "last_synced_at": st.get("last_sync") or disk.get("last_sync"),
            "file_count":     st.get("file_count") if st.get("file_count") is not None
                              else disk.get("file_count"),
            # Set by a sync in this process only. The disk fallback counts
            # tracked files, not indexable ones, so it cannot stand in here.
            "indexable_files": st.get("indexable_files"),
            "status":         st.get("status") or disk.get("status"),
        })
    return {"sources": out}


@app.get("/api/git-sources/{source_id}/tree")
def git_source_tree(source_id: str, request: Request):
    """Return the file tree of a synced git source so agents can see the
    repository's structure and boundaries."""
    ctx = _auth_ctx(request)
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _is_admin(ctx):
        allowed = _fetch_git_source_access(ctx["user_id"], ctx["org_id"], [source_id])
        if source_id not in allowed:
            raise HTTPException(status_code=403, detail="Access denied to this source")
    import subprocess
    # Clones live at GIT_SOURCES_DIR/<org_slug>/<source_id> (current layout) but
    # older clones may still sit at GIT_SOURCES_DIR/<source_id> until they're
    # migrated on next sync. Check both, matching the cleanup logic in
    # git_source_delete.
    sources = _fetch_user_git_sources(ctx["user_id"], ctx["org_id"])
    src_meta = next((s for s in sources if s.get("id") == source_id), {"id": source_id})
    source_dir = _git_source_clone_dir(src_meta)
    if source_dir is None:
        raise HTTPException(status_code=404, detail="Source has not been synced yet")
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(source_dir), capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="git ls-tree timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"git ls-tree failed: {result.stderr.strip()}")
    files = [line for line in result.stdout.splitlines() if line]
    return {
        "source":     src_meta,
        "file_count": len(files),
        "files":      files,
    }


@app.post("/api/git-sources/{source_id}/sync")
def git_source_sync(source_id: str, request: Request):
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    sources = _fetch_git_sources()
    src = next((s for s in sources if s["id"] == source_id), None)
    if src is None:
        raise HTTPException(status_code=404, detail="Git source not found")
    state = _git_source_states.get(source_id, {})
    if state.get("status") == "syncing":
        return {"status": "already syncing"}
    threading.Thread(target=sync_one_source, args=(src,), daemon=True).start()
    return {"status": "sync started"}


@app.post("/api/git-sources/{source_id}/reindex")
def git_source_reindex(source_id: str, request: Request):
    """Force a full re-index of one git source: wipes all existing chunks for
    this source from the wiki collection and re-embeds every indexable file.
    Use this to rebuild a single repo's index without re-indexing everything."""
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    sources = _fetch_git_sources()
    src = next((s for s in sources if s["id"] == source_id), None)
    if src is None:
        raise HTTPException(status_code=404, detail="Git source not found")
    state = _git_source_states.get(source_id, {})
    if state.get("status") == "syncing":
        return {"status": "already syncing"}
    threading.Thread(target=sync_one_source, args=(src, True), daemon=True).start()
    return {"status": "reindex started"}


@app.delete("/api/git-sources/{source_id}")
def git_source_delete(source_id: str, request: Request, org_slug: str = ""):
    """Drop chunks + on-disk clone for a deleted source.

    The editor has already removed the GitSource row by the time we run,
    so it passes org_slug as a query param — we can't look it up ourselves.
    Both the new per-org path and the legacy flat path are removed if they
    exist (handles cleanup of clones from before the per-org migration).
    """
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    # Validate org_slug to prevent shutil.rmtree from walking outside
    # GIT_SOURCES_DIR via a crafted slug like "../other-org".
    slug_in = (org_slug or "").strip()
    if slug_in and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug_in):
        raise HTTPException(status_code=400, detail="org_slug must match [a-z0-9-]")
    import shutil
    wiki_coll = _wiki_coll()
    try:
        qdrant().delete(
            collection_name=wiki_coll,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="source_id", match=MatchValue(value=source_id)),
                ])
            ),
        )
    except Exception as exc:
        log.warning("Could not remove git source points for %s: %s", source_id, exc)

    slug = (org_slug or "").strip() or "default"
    candidates = [GIT_SOURCES_DIR / slug / source_id, GIT_SOURCES_DIR / source_id]
    for d in candidates:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # Purge the lexical index (Postgres git_files) too, else a deleted source's
    # files linger in git_search results.
    _memory_api_git_delete_source(slug, source_id)

    with _git_source_lock:
        _git_source_states.pop(source_id, None)

    return {"status": "deleted"}


@app.post("/api/embed")
def embed_text(body: dict, request: Request):
    """
    Single-text embedding endpoint for internal service use (e.g. memory-api).
    Request: {"text": "..."}  →  Response: {"embedding": [...], "dimensions": N}

    Previously open with no auth — any caller on the internal network
    (and, if the embedder port were ever published, the public internet)
    could call this for free, exhausting CPU on the local sentence-
    transformers model or burning OPENAI_API_KEY billing on the cloud
    backend. Now requires the internal-service token; the editor or
    memory-api can call it but random clients cannot.
    """
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    text_input = body.get("text", "")
    if not text_input:
        raise HTTPException(status_code=400, detail="'text' field required")
    vec = get_embedding(text_input)
    return {"embedding": vec, "dimensions": len(vec)}


@app.get("/api/status")
def get_status(
    request: Request,
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
    aggregate: bool = Query(False, description="Platform-admin only: return cross-org totals"),
):
    """Status panel for the editor's admin UI. Defaults to the caller's
    org scope (resolved via auth headers). Platform admins can pass
    aggregate=true to see global totals — used by the Platform Admin
    dashboard for a cross-org view of indexing health."""
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    role = (request.headers.get("x-auth-role") or "user").strip()
    aggregate = aggregate and role == "platform_admin"

    wiki_collection = _wiki_coll(tenant_slug)
    with _state_lock:
        state_copy = dict(_reindex_state)
        state_copy["errors"] = list(state_copy["errors"])

    with _embed_lock:
        provider = _embed_cfg.get("provider", "unknown")
        model    = _embed_cfg.get("model", "unknown")
        vsize    = _vector_size

    if aggregate:
        # Authoritative: count .md objects across every wiki bucket in S3.
        # Falls back to disk if S3 isn't configured (e.g. dev environments).
        total_docs = _s3_aggregate_doc_count() or (
            len(list(DOCS_DIR.rglob("*.md"))) if DOCS_DIR.exists() else 0
        )
        indexed_docs = _aggregate_wiki_points()
        collection_label = "(all orgs)"
    else:
        # S3 is the source of truth post-Phase-2. /docs is a write-only
        # mirror that lags behind content seeded directly (welcome.md).
        total_docs = _s3_doc_count(tenant_slug)
        indexed_docs = 0
        try:
            info = qdrant().get_collection(wiki_collection)
            indexed_docs = info.points_count or info.vectors_count or 0
        except Exception:
            pass
        collection_label = wiki_collection

    return {
        "state":                   state_copy["state"],
        "total_docs":              total_docs,
        "indexed_docs":            indexed_docs,
        "last_reindex_start":      state_copy["last_reindex_start"],
        "last_reindex_end":        state_copy["last_reindex_end"],
        "last_reindex_duration_s": state_copy["last_reindex_duration_s"],
        "current_file":            state_copy["current_file"],
        "files_processed":         state_copy["files_processed"],
        "files_total":             state_copy["files_total"],
        "errors":                  state_copy["errors"],
        # Issue #83 coverage signal: distinguish "everything indexed" from
        # "reindex finished but silently dropped pages".
        "last_reindex_failed":       state_copy.get("last_reindex_failed", []),
        "last_reindex_failed_count": state_copy.get("last_reindex_failed_count", 0),
        "last_reindex_docs_total":   state_copy.get("last_reindex_docs_total", 0),
        "last_reindex_docs_indexed": state_copy.get("last_reindex_docs_indexed", 0),
        "last_reindex_skipped_unchanged": state_copy.get("last_reindex_skipped_unchanged", 0),
        "last_reindex_reconciled":   state_copy.get("last_reindex_reconciled", 0),
        "provider":                provider,
        "model":                   model,
        "vector_size":             vsize,
        "collection":              collection_label,
        "scope":                   "aggregate" if aggregate else tenant_slug,
    }


def _aggregate_wiki_points() -> int:
    """Sum point counts across every wiki collection. Used by the
    platform-admin aggregate view; tolerates per-collection failures
    so a single broken tenant doesn't poison the total."""
    total = 0
    try:
        collections = qdrant().get_collections().collections
    except Exception:
        return 0
    for c in collections:
        if not (c.name == "knowledge_base" or c.name.endswith("_wiki")):
            continue
        try:
            info = qdrant().get_collection(c.name)
            total += info.points_count or info.vectors_count or 0
        except Exception:
            continue
    return total


# ── Detailed composition stats ────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(
    request: Request,
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
    aggregate: bool = Query(False, description="Platform-admin only: return cross-org totals"),
):
    """Full composition breakdown: wiki chunks, memory counts, type split,
    top tags. Defaults to the caller's org scope. Platform admins can
    pass aggregate=true for a cross-org rollup."""
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    role = (request.headers.get("x-auth-role") or "user").strip()
    aggregate = aggregate and role == "platform_admin"

    if aggregate:
        total_docs = _s3_aggregate_doc_count() or (
            len(list(DOCS_DIR.rglob("*.md"))) if DOCS_DIR.exists() else 0
        )
        # Sum every wiki + memory collection across orgs
        wiki_chunks    = _aggregate_wiki_points()
        total_memories, memory_types, tag_counts = _aggregate_memory_stats()
    else:
        total_docs = _s3_doc_count(tenant_slug)
        wiki_collection = _wiki_coll(tenant_slug)
        mem_collection  = _mem_coll(tenant_slug)

        wiki_chunks = 0
        try:
            info = qdrant().get_collection(wiki_collection)
            wiki_chunks = info.points_count or 0
        except Exception:
            pass

        total_memories = 0
        memory_types = {}
        tag_counts   = {}
        try:
            mem_info = qdrant().get_collection(mem_collection)
            total_memories = mem_info.points_count or 0
            offset = None
            while True:
                points, offset = qdrant().scroll(
                    collection_name=mem_collection,
                    limit=250,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for pt in points:
                    ptype = pt.payload.get("type", "unknown")
                    memory_types[ptype] = memory_types.get(ptype, 0) + 1
                    for tag in pt.payload.get("tags", []):
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                if offset is None:
                    break
        except Exception:
            pass

    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:25]

    return {
        "wiki":     {"total_docs": total_docs, "indexed_chunks": wiki_chunks},
        "memories": {
            "total":    total_memories,
            "by_type":  memory_types,
            "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        },
        "totals":   {"vectors": wiki_chunks + total_memories},
        "scope":    "aggregate" if aggregate else tenant_slug,
    }


def _aggregate_memory_stats() -> tuple:
    """Sum memory totals + type/tag breakdowns across every tenant
    collection. Returns (total, by_type, tag_counts). Tolerates
    per-collection failures."""
    total = 0
    by_type: dict = {}
    tag_counts: dict = {}
    try:
        collections = qdrant().get_collections().collections
    except Exception:
        return total, by_type, tag_counts
    for c in collections:
        if not (c.name == "memories" or c.name.endswith("_memories")):
            continue
        try:
            info = qdrant().get_collection(c.name)
            total += info.points_count or 0
        except Exception:
            continue
        try:
            offset = None
            while True:
                points, offset = qdrant().scroll(
                    collection_name=c.name,
                    limit=250,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for pt in points:
                    ptype = pt.payload.get("type", "unknown")
                    by_type[ptype] = by_type.get(ptype, 0) + 1
                    for tag in pt.payload.get("tags", []):
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                if offset is None:
                    break
        except Exception:
            continue
    return total, by_type, tag_counts


# ── Log endpoints ─────────────────────────────────────────────────────────

_TENANT_TAG_RE = re.compile(
    r"tenant(?:_slug)?\s*[:=]\s*['\"]?([\w\-]+)['\"]?",
    re.IGNORECASE,
)


def _line_visible_to(line: str, viewer_slug: str, viewer_role: str,
                     aggregate: bool) -> bool:
    """Decide whether a log line should be emitted to this viewer.

    Rules:
      - Platform admins with aggregate=true see everything.
      - For everyone else, a line that names a tenant explicitly
        (`tenant=<slug>` or `tenant: <slug>`) is shown only when the
        slug matches the viewer's org.
      - Lines without any tenant tag (service-level events: startup,
        config changes, generic errors) are shown to every viewer —
        they're not org-specific noise.

    This is a heuristic — robustness depends on tenant-scoped log
    sites consistently tagging their output. The noisy log lines that
    motivated this fix (indexing chatter from `index_file`) already do.
    """
    if aggregate and viewer_role == "platform_admin":
        return True
    m = _TENANT_TAG_RE.search(line)
    if not m:
        return True  # untagged → service-level, visible to all
    line_slug = m.group(1)
    viewer_slug = viewer_slug or "default"
    return line_slug == viewer_slug


@app.get("/api/logs")
def get_logs(
    request: Request,
    limit: int = 200,
    aggregate: bool = Query(False),
):
    """Return recent buffered log lines (newest last). Filtered to the
    caller's org unless they're a platform_admin with aggregate=true."""
    viewer_slug = (request.headers.get("x-auth-org-slug") or "default").strip() or "default"
    viewer_role = (request.headers.get("x-auth-role") or "user").strip()
    with _LOG_BUFFER_LOCK:
        all_lines = list(_LOG_BUFFER)
    lines = [
        ln for ln in all_lines
        if _line_visible_to(ln, viewer_slug, viewer_role, aggregate)
    ][-limit:]
    return {"lines": lines}


@app.get("/api/logs/stream")
def stream_logs(
    request: Request,
    aggregate: bool = Query(False, description="Platform-admin only: stream all orgs' logs"),
):
    """SSE stream of live log output. Filtered to the caller's org by
    default; platform admins can pass aggregate=true for a cross-org
    firehose."""
    viewer_slug = (request.headers.get("x-auth-org-slug") or "default").strip() or "default"
    viewer_role = (request.headers.get("x-auth-role") or "user").strip()
    aggregate = aggregate and viewer_role == "platform_admin"

    q: queue.Queue = queue.Queue(maxsize=1000)
    with _LOG_SUBS_LOCK:
        _LOG_SUBSCRIBERS.append(q)

    def generate():
        # Flush recent history so the browser has context immediately
        with _LOG_BUFFER_LOCK:
            recent = list(_LOG_BUFFER)[-150:]
        for line in recent:
            if _line_visible_to(line, viewer_slug, viewer_role, aggregate):
                yield f"data: {line}\n\n"
        # Then stream live lines
        while True:
            try:
                line = q.get(timeout=20)
                if _line_visible_to(line, viewer_slug, viewer_role, aggregate):
                    yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"   # SSE comment keeps connection open

    def cleanup():
        with _LOG_SUBS_LOCK:
            try:
                _LOG_SUBSCRIBERS.remove(q)
            except ValueError:
                pass

    class _CleanupStream:
        """Wrap generator so we remove the queue when the client disconnects."""
        def __init__(self, gen):
            self._gen = gen
        def __iter__(self):
            try:
                yield from self._gen
            finally:
                cleanup()
        def __next__(self):
            return next(self._gen)

    return StreamingResponse(
        _CleanupStream(generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Memory API ─────────────────────────────────────────────────────────────

MEMORY_TYPES = {"fact", "event", "procedure", "conversation", "derived"}

# Input size caps (SEC-08). Generous — a memory that runs past them belongs in
# the wiki. Content and tag length match what memory-api accepts into the hot
# tier, so a capture accepted there can always be promoted here. The tag COUNT
# is higher than memory-api's: a compactor promotion carries the union of a
# whole cluster's tags.
MAX_CONTENT_CHARS = 200_000
MAX_TAG_CHARS = 200
MAX_TAGS = 1_000
Tag = Annotated[str, Field(max_length=MAX_TAG_CHARS)]


class MemoryCreate(BaseModel):
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    tags: List[Tag] = Field(default_factory=list, max_length=MAX_TAGS)
    type: str = Field(default="fact", max_length=MAX_TAG_CHARS)
    # Structural attribution for every memory: identifies the host/agent that
    # produced it (claude_code, claude_desktop, chatgpt, cursor, open_webui,
    # mcp_direct, automation, editor, …). Populated from the calling token's
    # source field by the MCP server. "unknown" / "legacy" are transitional
    # values that pre-date PR A1.
    source: str = Field(default="unknown", max_length=MAX_TAG_CHARS)
    tenant_slug: str = "default"
    # ── Provenance (populated by the compactor when this memory is promoted
    # from the hot tier; null for direct memory_add or legacy writes). Lets
    # downstream tools answer "where did this memory come from?" without
    # reverse-engineering it from the hot tier afterward.
    originated_from_hot_ids: Optional[List[str]] = None
    originated_session_ids:  Optional[List[str]] = None
    compactor_score:         Optional[float]     = None
    promoted_at:             Optional[str]       = None
    oldest_captured_at:      Optional[str]       = None
    # Restoring an org means embedding every memory again, which on a small
    # box with the bundled model is most of the restore's wall-clock time.
    # embed=False stores the memory now and leaves the vector for later: the
    # content lives in the Qdrant payload, so nothing is lost, but the memory
    # is NOT semantically searchable until /api/memories/embed-pending runs.
    # Get the data back in minutes, then embed on your own schedule — or after
    # pointing the deployment at a faster external embedding backend.
    embed: bool = True
    # ── Restore mode (BCDR): when a backup is restored, the caller supplies the
    # original identity/timestamps/attribution so the memory comes back
    # unchanged — same id (so the graph + any references hold), same created_at
    # (so recency/session structure survives), same author. Omitted for normal
    # writes, which mint fresh values.
    id:                      Optional[str]       = None
    created_at:              Optional[str]       = None
    updated_at:              Optional[str]       = None
    author_id:               Optional[str]       = None


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, max_length=MAX_CONTENT_CHARS)
    tags: Optional[List[Tag]] = Field(default=None, max_length=MAX_TAGS)
    type: Optional[str] = Field(default=None, max_length=MAX_TAG_CHARS)
    tenant_slug: str = "default"
    # Caller-supplied attribution for the revision row. Used only when the
    # request carries no real user session (the MCP path is the canonical
    # example — its bearer token isn't a UI session, so _auth_ctx returns
    # None and there's no user_id to record on the audit trail). When a
    # session IS present, ctx["user_id"] wins and this hint is ignored —
    # we never let the body override an authenticated identity.
    edited_by_hint: Optional[str] = None


def _memory_payload_to_dict(payload: dict) -> dict:
    out = {
        "id":         payload.get("id"),
        "content":    payload.get("content"),
        "tags":       payload.get("tags", []),
        "type":       payload.get("type", "fact"),
        # Memories written before the source-attribution column existed have
        # no source key in their Qdrant payload — surface them as "legacy"
        # rather than NULL so downstream filters/scoring have a stable value.
        "source":     payload.get("source") or "legacy",
        "author_id":  payload.get("author_id"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "tier":       "durable",
    }
    # Surface provenance fields when they're present (compactor-promoted
    # memories); leave them out entirely for legacy/direct writes so the
    # consumer can treat "key absent" as "no provenance recorded".
    for field in ("originated_from_hot_ids", "originated_session_ids",
                  "compactor_score", "promoted_at", "oldest_captured_at"):
        if field in payload:
            out[field] = payload[field]
    return out


def _fetch_hot_tier_hits(tenant_slug: str, q: str,
                         tags: Optional[str], limit: int) -> list[dict]:
    """Lexical hits from the hot tier (Postgres FTS via memory-api).

    Returns an empty list on any failure — this path is purely additive
    to the durable semantic search and must not break it. Timeout is
    aggressive (1.5s) because the hot path needs to feel snappy; the
    GIN index keeps queries O(log n) so 1.5s is generous.

    Failures are *logged* (not silent) so a misconfigured MEMORY_API_URL
    or a memory-api outage shows up in embedder logs instead of looking
    like "no hot captures match" forever.
    """
    if not q or not q.strip() or not MEMORY_API_URL:
        return []
    params: dict = {
        "tenant_slug": tenant_slug or "default",
        "q":           q,
        "limit":       limit,
    }
    if tags:
        params["tags"] = tags
    try:
        resp = httpx.get(
            f"{MEMORY_API_URL}/memory/hot/search",
            params=params,
            headers=_internal_service_headers(),
            timeout=1.5,
        )
    except Exception as exc:
        log.warning("hot-tier fetch failed: url=%s tenant=%s err=%s [tenant=%s]",
                    MEMORY_API_URL, tenant_slug, exc, tenant_slug)
        return []
    if resp.status_code != 200:
        log.warning("hot-tier fetch non-200: url=%s tenant=%s status=%d body=%s [tenant=%s]",
                    MEMORY_API_URL, tenant_slug, resp.status_code, resp.text[:200], tenant_slug)
        return []
    try:
        data = resp.json() or {}
    except Exception as exc:
        log.warning("hot-tier fetch bad JSON: err=%s [tenant=%s]", exc, tenant_slug)
        return []
    out: list[dict] = []
    for row in data.get("results", []):
        out.append({
            "id":         row.get("id"),
            "content":    row.get("content"),
            "tags":       row.get("tags") or [],
            "type":       row.get("type_hint") or "capture",
            "source":     row.get("source") or "legacy",
            "created_at": row.get("captured_at"),
            "updated_at": row.get("captured_at"),
            "score":      round(float(row.get("score") or 0.0), 4),
            "tier":       "hot",
        })
    if out:
        log.info("hot-tier returned %d hit(s) for q=%r [tenant=%s]",
                 len(out), q[:60], tenant_slug)
    return out


def _merge_hot_and_durable(hot: list[dict], durable: list[dict],
                           limit: int) -> list[dict]:
    """Combine hot-tier (FTS+recency) hits with durable (semantic) hits.

    Hot scores are FTS ranks blended with recency, durable scores are
    Qdrant cosine similarities — different scales. We don't try to
    unify them mathematically; instead hot hits get an interleave bias
    so a strong fresh capture surfaces above a mid-confidence semantic
    match, but a barely-matching hot row can't displace a high-score
    durable result. Concretely: hot rows with lex_score > a small floor
    sort to the front in lex_score order; the rest of the slate is
    durable hits in semantic-score order. Deduplication is by id so a
    capture that's already been promoted doesn't appear twice.
    """
    seen = set()
    out: list[dict] = []
    # Hot first (already sorted by blended score), then durable
    for r in hot:
        rid = r.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        out.append(r)
        if len(out) >= limit:
            return out
    for r in durable:
        rid = r.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        out.append(r)
        if len(out) >= limit:
            return out
    return out


def _memory_in_scope(payload: dict, ctx: Optional[dict], scope: Optional[dict],
                     tenant_slug: Optional[str] = None) -> bool:
    """True iff the calling user may see a memory with this payload.

    When tenant_slug is provided, cross-tenant defence-in-depth runs FIRST
    and unconditionally: if the payload carries a tenant_id that doesn't
    match, the memory is hidden regardless of any per-user scope decision
    below — and this gate applies even on the ctx-less MCP path. Legacy
    points with tenant_id=None are tolerated (collection naming already
    isolates them).
    """
    if tenant_slug is not None:
        pt = (payload or {}).get("tenant_id")
        if pt is not None and pt != tenant_slug:
            return False
    if ctx is None:
        return True   # no auth headers → permissive (legacy / MCP path)
    if _is_admin(ctx):
        return True   # admins see everything in their org
    # Owner-by-name (issue #85): agent-captured memories are attributed by an
    # `owner:<token-name>` tag, and the compactor's promotion write carries no
    # author_id — so a UUID-only scope check hides every promoted memory. Admit
    # a memory whose owner name-tag matches the reader's token name.
    if _owner_name_matches(payload, ctx):
        return True
    owner = _payload_owner(payload)
    if not owner:
        # Untagged legacy memory — only visible to admins
        return False
    if scope is None:
        return owner == ctx["user_id"]
    return owner in scope.get("scope_user_ids", set())


def _assert_create_may_replace(mem_id: str, tenant_slug: str, ctx: Optional[dict]) -> None:
    """A create that names an id replaces any point already stored under it,
    so it must pass the same author/admin check as update_memory. Callers
    without an identity may not replace anything."""
    try:
        existing = qdrant().retrieve(
            collection_name=_mem_coll(tenant_slug),
            ids=[memory_point_id(mem_id)],
            with_payload=True,
        )
    except Exception as exc:
        log.exception("create_memory: existing-id lookup failed (tenant=%s)", tenant_slug)
        raise _internal_http_error(500, "existing-id lookup failed", exc)
    if not existing:
        return
    payload = dict(existing[0].payload or {})
    _assert_payload_tenant(payload, tenant_slug, mem_id)
    if ctx is None:
        raise HTTPException(status_code=403,
                            detail="A memory with this id already exists")
    owner = _payload_owner(payload)
    if (not _is_admin(ctx) and owner not in (None, ctx["user_id"])
            and not _owner_name_matches(payload, ctx)):
        raise HTTPException(status_code=403,
                            detail="Only the original author or an admin can edit this memory")


@app.post("/api/memories", status_code=201)
def create_memory(body: MemoryCreate, request: Request):
    """Create a new memory — embeds content and upserts to Qdrant."""
    ctx = _auth_ctx(request)
    body.tenant_slug = _resolve_tenant_slug(request, body.tenant_slug)
    # Restore mode: honour a caller-supplied id/created_at (a valid UUID and a
    # parseable timestamp), else mint fresh. Keeps a restored memory identical
    # to the original so the graph and any references survive.
    mem_id = (body.id or "").strip() or str(uuid.uuid4())
    try:
        uuid.UUID(mem_id)
    except (ValueError, TypeError):
        mem_id = str(uuid.uuid4())
    internal = _is_internal_service_request(request)
    # Admins and peer services (compactor promotion, org restore) may set
    # attribution on the caller's behalf; everyone else writes as themselves.
    trusted  = internal or _is_admin(ctx)
    if (body.id or "").strip() and not internal:
        _assert_create_may_replace(mem_id, body.tenant_slug, ctx)
    now      = datetime.datetime.now(timezone.utc).isoformat()
    created  = (body.created_at or "").strip() or now
    updated  = (body.updated_at or "").strip() or created
    coll    = _mem_coll(body.tenant_slug)

    if body.embed:
        try:
            vec = get_embedding(body.content)
        except Exception as exc:
            log.exception("create_memory: embedding failed (tenant=%s)", body.tenant_slug)
            raise _internal_http_error(500, "embedding failed", exc)
    else:
        # Deferred: park a placeholder of the right width. Qdrant needs a
        # vector of the collection's dimension for the point to exist at all,
        # and a zero vector scores 0 against everything under cosine — so the
        # memory is readable, listable and exportable straight away, and
        # simply never surfaces in semantic search until it is embedded.
        # That is the honest trade, and PENDING_EMBED_KEY is what makes it
        # findable and fixable rather than a silent hole in the corpus.
        with _embed_lock:
            vec = [0.0] * _vector_size

    mem_type = body.type if body.type in MEMORY_TYPES else "fact"

    # Auto-inject owner tag and author_id
    tags = list(body.tags or [])
    if ctx and not trusted:
        own = {ctx["user_id"], ctx.get("owner_name") or ""} - {""}
        tags = [t for t in tags
                if not (isinstance(t, str) and t.startswith("owner:"))
                or t.split(":", 1)[1] in own]
    if ctx and not any(t.startswith("owner:") for t in tags):
        tags.append(_owner_tag(ctx["user_id"]))
    if ctx and not trusted:
        author_id = ctx["user_id"]
    else:
        author_id = body.author_id or (ctx["user_id"] if ctx else None)

    payload = {
        "id":         mem_id,
        "content":    body.content,
        "tags":       tags,
        "type":       mem_type,
        "source":     (body.source or "unknown").strip().lower() or "unknown",
        "author_id":  author_id,
        "created_at": created,
        "updated_at": updated,
        # Stamp the tenant on every memory point so a post-retrieve
        # tenant_id check can catch a future code bug that routed an
        # operation to the wrong collection. Collection naming
        # ({slug}_memories) is still the primary control; this is the
        # defence-in-depth promised in security/tenant-isolation.md.
        "tenant_id":  body.tenant_slug,
    }
    if not body.embed:
        payload[PENDING_EMBED_KEY] = True
    # Only carry provenance fields when the caller (the compactor) actually
    # supplied them — direct /api/memories writes (memory_add etc.) leave
    # them null, and we'd rather omit the key than store null clutter.
    for field in ("originated_from_hot_ids", "originated_session_ids",
                  "compactor_score", "promoted_at", "oldest_captured_at"):
        val = getattr(body, field, None)
        if val is not None:
            payload[field] = val
    try:
        qdrant().upsert(
            collection_name=coll,
            points=[PointStruct(id=memory_point_id(mem_id), vector=vec, payload=payload)],
        )
    except Exception as exc:
        # The response names a ref rather than the Qdrant error, so the
        # editor's /restore can still point an operator at the log line
        # without relaying the collection name and backend detail.
        log.exception("create_memory: qdrant upsert failed (coll=%s, vec_dim=%d)",
                      coll, len(vec) if vec is not None else -1)
        raise _internal_http_error(500, "storing the memory failed", exc)
    log.info("Created memory %s (tenant: %s, source: %s, author: %s)",
             mem_id, body.tenant_slug, payload["source"],
             ctx["user_id"] if ctx else "anon")
    return payload


@app.get("/api/memories")
def list_memories(
    request: Request,
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter by"),
    source: Optional[str] = Query(None, description="Source slug to filter by (exact match)"),
    session_id: Optional[str] = Query(None, description="Restrict to memories that originated (in part) from this hot-tier session"),
    limit: int = Query(0, ge=0, description="Max memories to return (0 = all)"),
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
):
    """List memories visible to the caller, optionally filtered by tags,
    source, and/or originating hot-tier session."""
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    scope = None
    if ctx and not _is_admin(ctx):
        scope = _fetch_memory_scope(ctx["user_id"], ctx["org_id"])
        scope["scope_user_ids"] = set(scope.get("scope_user_ids") or [])

    coll = _mem_coll(tenant_slug)
    conditions = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tag_list)))
    if source:
        src = source.strip().lower()
        if src:
            conditions.append(FieldCondition(key="source", match=MatchValue(value=src)))
    if session_id:
        sid = session_id.strip()
        if sid:
            # originated_session_ids is a string array on memories promoted
            # after Tier 1.1 — MatchAny tests "array contains any of these".
            # Legacy memories without the field simply won't match, which
            # is the correct semantic (they have no session association).
            conditions.append(FieldCondition(
                key="originated_session_ids", match=MatchAny(any=[sid]),
            ))
    query_filter = Filter(must=conditions) if conditions else None

    results = []
    offset = None
    while True:
        batch, next_offset = qdrant().scroll(
            collection_name=coll,
            scroll_filter=query_filter,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in batch:
            if _memory_in_scope(point.payload, ctx, scope, tenant_slug):
                results.append(_memory_payload_to_dict(point.payload))
        if next_offset is None:
            break
        offset = next_offset

    results.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    if limit > 0:
        results = results[:limit]
    return {"memories": results}


@app.get("/api/memories/search")
def search_memories(
    request: Request,
    q: str = Query(..., description="Semantic search query"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter by"),
    source: Optional[str] = Query(None, description="Source slug to filter by (exact match)"),
    limit: int = Query(10, ge=1, le=50),
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
):
    """Semantic search over memories visible to the caller."""
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    scope = None
    if ctx and not _is_admin(ctx):
        scope = _fetch_memory_scope(ctx["user_id"], ctx["org_id"])
        scope["scope_user_ids"] = set(scope.get("scope_user_ids") or [])

    coll = _mem_coll(tenant_slug)
    mem_ok, mem_stored_dim = _memory_coll_dim_ok(coll)
    if not mem_ok:
        with _embed_lock:
            cur_dim = _vector_size
        log.error(
            "Memory collection %r vector_size mismatch (stored=%s, current=%s) — "
            "returning empty result set.",
            coll, mem_stored_dim, cur_dim,
        )
        return {"query": q, "results": []}

    vec = get_embedding(q)

    conditions = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tag_list)))
    if source:
        src = source.strip().lower()
        if src:
            conditions.append(FieldCondition(key="source", match=MatchValue(value=src)))
    query_filter = Filter(must=conditions) if conditions else None

    # Over-fetch and post-filter, since Qdrant's filter doesn't easily express
    # "tag starts with owner: AND that tag's suffix is in this set"
    fetch_limit = max(limit * 4, 50)
    try:
        hits = qdrant().search(
            collection_name=coll,
            query_vector=vec,
            query_filter=query_filter,
            limit=fetch_limit,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("Memory search failed on %r: %s", coll, _qdrant_error_detail(exc))
        return {"query": q, "results": []}

    visible = [h for h in hits if _memory_in_scope(h.payload, ctx, scope, tenant_slug)]
    durable_results = [
        {"score": round(h.score, 4), **_memory_payload_to_dict(h.payload)}
        for h in visible[:limit]
    ]
    # Hot-tier lexical hits — captures that haven't been promoted yet.
    # Fetched additively; failures here don't affect the durable path.
    hot_results = _fetch_hot_tier_hits(tenant_slug, q, tags, limit)
    merged = _merge_hot_and_durable(hot_results, durable_results, limit)
    return {"query": q, "results": merged}


@app.get("/api/memories/graph")
def memory_graph(
    request: Request,
    tags: Optional[str] = Query(None, description="Comma-separated tags to restrict the graph"),
    source: Optional[str] = Query(None, description="Source slug to restrict the graph"),
    since: Optional[str] = Query(None, description="ISO date/timestamp; only memories created on/after this"),
    max_nodes: int = Query(2000, ge=1, le=5000,
                           description="Hard cap on nodes returned. O(N^2) similarity in-process."),
    top_k: int = Query(8, ge=1, le=20,
                       description="Neighbours per node to consider for edges."),
    min_similarity: float = Query(0.30, ge=0.0, le=1.0,
                                  description="Drop edges weaker than this cosine score."),
    strategy: str = Query("recent",
                          description="Window strategy. Currently only 'recent' (most-recent first by created_at)."),
    tenant_slug: str = Query("default", description="Tenant slug for collection routing"),
):
    """Return a tenant-scoped relationship graph of memories.

    Nodes are memories the caller is allowed to see (same ACL as
    /api/memories/search). Edges are the top-K nearest neighbours per node
    above `min_similarity` (cosine on the stored embeddings), deduplicated
    so the graph is undirected.

    The graph is windowed by `strategy` (currently 'recent') and hard-capped
    at `max_nodes`. `total_memories` reports the user's full visible count so
    the UI can show "showing X of Y" when the cap kicks in. `strategy` is
    forward-compatible — other windowing strategies (age buckets, tag
    clusters, etc.) can be added without breaking the response shape.
    """
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    scope = None
    if ctx and not _is_admin(ctx):
        scope = _fetch_memory_scope(ctx["user_id"], ctx["org_id"])
        scope["scope_user_ids"] = set(scope.get("scope_user_ids") or [])

    coll = _mem_coll(tenant_slug)

    conditions = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=tag_list)))
    if source:
        src = source.strip().lower()
        if src:
            conditions.append(FieldCondition(key="source", match=MatchValue(value=src)))
    query_filter = Filter(must=conditions) if conditions else None

    # Scroll everything visible to the caller, with vectors. Date filter is
    # applied in-process because payload date storage varies across deploys.
    visible: list[tuple[dict, list[float], str]] = []  # (payload, vector, point_id)
    offset = None
    while True:
        batch, next_offset = qdrant().scroll(
            collection_name=coll,
            scroll_filter=query_filter,
            limit=200,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in batch:
            if not _memory_in_scope(point.payload, ctx, scope, tenant_slug):
                continue
            if point.vector is None:
                continue
            visible.append((point.payload, point.vector, str(point.id)))
        if next_offset is None:
            break
        offset = next_offset

    if since:
        visible = [m for m in visible if (m[0].get("created_at") or "") >= since]

    # Window: most-recent-first, cap at max_nodes. 'strategy' is a placeholder
    # for future windowing schemes; only 'recent' is implemented today.
    visible.sort(key=lambda m: m[0].get("created_at") or "", reverse=True)
    total_visible = len(visible)
    if total_visible > max_nodes:
        visible = visible[:max_nodes]

    params = {
        "max_nodes": max_nodes, "top_k": top_k,
        "min_similarity": min_similarity, "since": since,
        "tags": tags, "source": source,
    }

    if not visible:
        return {"nodes": [], "edges": [],
                "total_memories": total_visible, "shown": 0,
                "strategy": strategy, "params": params}

    # Cosine similarity via normalised dot product over the windowed set.
    vectors = np.asarray([m[1] for m in visible], dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms
    sim = vectors @ vectors.T
    np.fill_diagonal(sim, -1.0)

    # Top-K neighbours per row above the similarity threshold; dedupe to
    # produce undirected edges.
    n = len(visible)
    k = min(top_k, n - 1) if n > 1 else 0
    edges: list[dict] = []
    if k > 0:
        top_idx = np.argpartition(-sim, k, axis=1)[:, :k]
        seen: set[tuple[int, int]] = set()
        for i in range(n):
            for raw_j in top_idx[i]:
                j = int(raw_j)
                if j == i:
                    continue
                w = float(sim[i, j])
                if w < min_similarity:
                    continue
                a, b = (i, j) if i < j else (j, i)
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                edges.append({"source": visible[a][2], "target": visible[b][2],
                              "weight": w})

    def _snippet(content: str, limit: int = 140) -> str:
        s = (content or "").strip().replace("\n", " ")
        return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"

    nodes = []
    for payload, _vec, point_id in visible:
        d = _memory_payload_to_dict(payload)
        nodes.append({
            "id": point_id,
            "snippet": _snippet(d.get("content", "")),
            "type": d.get("type", "fact"),
            "tags": d.get("tags", []),
            "created_at": d.get("created_at"),
        })

    return {"nodes": nodes, "edges": edges,
            "total_memories": total_visible, "shown": len(nodes),
            "strategy": strategy, "params": params}


def _assert_payload_tenant(payload, expected_slug: str, memory_id: str) -> None:
    """Defence-in-depth: verify the retrieved memory point's payload
    `tenant_id` matches the resolved slug. Collection naming
    ({slug}_memories) already keeps tenants separate; this is the
    second gate that catches a future code bug where routing picks the
    wrong collection, or an id-bypass attempt gets past
    _resolve_tenant_slug. Returns 404 to avoid leaking existence.

    Legacy memories written before the tenant_id stamp (~ June 2026)
    have payload['tenant_id'] = None and are tolerated — they live in
    the right collection by virtue of collection naming, so the
    primary control already protects them.
    """
    pt = (payload or {}).get("tenant_id")
    if pt is None:
        return
    if pt != expected_slug:
        log.error(
            "tenant mismatch on memory %s: payload.tenant_id=%r expected=%r",
            memory_id, pt, expected_slug,
        )
        raise HTTPException(status_code=404, detail="Memory not found")


@app.get("/api/memories/embed-pending")
def get_pending_embeddings(request: Request, tenant_slug: Optional[str] = Query(None)):
    """How much of this tenant's memory corpus is stored but not yet embedded.

    Worth checking after a deferred restore: until this reaches zero those
    memories exist and can be read, but semantic search cannot reach them.
    """
    slug = _resolve_tenant_slug(request, tenant_slug or "")
    with _pending_embed_lock:
        run = dict(_pending_embed_state)
    return {"tenant_slug": slug, "pending": count_pending_embeddings(slug), "run": run}


@app.post("/api/memories/embed-pending", status_code=202)
def start_pending_embeddings(request: Request, tenant_slug: Optional[str] = Query(None)):
    """Embed everything a deferred restore left behind. Returns immediately;
    poll the GET. Safe to call again — it only ever picks up what is still
    flagged, so a retry after a failure resumes rather than redoing."""
    slug = _resolve_tenant_slug(request, tenant_slug or "")
    with _pending_embed_lock:
        if _pending_embed_state.get("state") == "running":
            return {"status": "already_running", "tenant_slug": _pending_embed_state["tenant_slug"]}
    pending = count_pending_embeddings(slug)
    if not pending:
        return {"status": "nothing_pending", "tenant_slug": slug, "pending": 0}
    threading.Thread(target=embed_pending_memories, args=(slug,), daemon=True).start()
    return {"status": "started", "tenant_slug": slug, "pending": pending}


@app.get("/api/memories/{memory_id}")
def get_memory(memory_id: str, request: Request,
               tenant_slug: str = Query("default", description="Tenant slug for collection routing")):
    """Get a single memory by ID (visibility-checked)."""
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    scope = None
    if ctx and not _is_admin(ctx):
        scope = _fetch_memory_scope(ctx["user_id"], ctx["org_id"])
        scope["scope_user_ids"] = set(scope.get("scope_user_ids") or [])

    coll = _mem_coll(tenant_slug)
    results = qdrant().retrieve(
        collection_name=coll,
        ids=[memory_point_id(memory_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not results:
        raise HTTPException(status_code=404, detail="Memory not found")
    payload = results[0].payload
    _assert_payload_tenant(payload, tenant_slug, memory_id)
    if not _memory_in_scope(payload, ctx, scope):
        raise HTTPException(status_code=404, detail="Memory not found")
    return _memory_payload_to_dict(payload)


@app.put("/api/memories/{memory_id}")
def update_memory(memory_id: str, body: MemoryUpdate, request: Request):
    """Update a memory.  Only the original author or an admin may edit."""
    ctx = _auth_ctx(request)
    body.tenant_slug = _resolve_tenant_slug(request, body.tenant_slug)
    coll = _mem_coll(body.tenant_slug)
    existing = qdrant().retrieve(
        collection_name=coll,
        ids=[memory_point_id(memory_id)],
        with_payload=True,
        with_vectors=True,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")

    point   = existing[0]
    payload = dict(point.payload)
    _assert_payload_tenant(payload, body.tenant_slug, memory_id)
    owner   = _payload_owner(payload)

    if ctx is not None:
        if (not _is_admin(ctx) and owner not in (None, ctx["user_id"])
                and not _owner_name_matches(payload, ctx)):
            raise HTTPException(status_code=403,
                                detail="Only the original author or an admin can edit this memory")
        # Admin editing someone else's memory → notify the original author
        if _is_admin(ctx) and owner and owner != ctx["user_id"]:
            _post_memory_edit_notification(memory_id, owner, ctx["user_id"])

    # ── Detect what will actually change ─────────────────────────────────
    # Snapshot the PRE-update state to memory_revisions iff the update
    # genuinely modifies a field — no-op PUTs don't bloat the audit log.
    old_content = payload.get("content")
    old_type    = payload.get("type")
    old_tags    = list(payload.get("tags") or [])
    old_source  = payload.get("source")

    changed: List[str] = []
    if body.content is not None and body.content != old_content:
        changed.append("content")
    if body.tags is not None:
        new_tags_proposed = list(body.tags)
        if owner and not any(t.startswith("owner:") for t in new_tags_proposed):
            new_tags_proposed.append(f"owner:{owner}")
        if new_tags_proposed != old_tags:
            changed.append("tags")
    if body.type is not None:
        proposed_type = body.type if body.type in MEMORY_TYPES else "fact"
        if proposed_type != old_type:
            changed.append("type")

    # ── Record the pre-update snapshot (strict) ──────────────────────────
    # Policy: corrections preserve history, never silently overwrite. If
    # recording fails we refuse the update — the audit trail must be
    # durable before mutation. This is the single chokepoint for durable
    # edits, so the policy can't be bypassed from any client (MCP,
    # memory-ui, internal tooling).
    if changed:
        try:
            resp = httpx.post(
                f"{MEMORY_API_URL}/memory/revisions",
                headers=_internal_service_headers(),
                json={
                    "tenant_slug": body.tenant_slug,
                    "durable_id":  memory_id,
                    "snapshot": {
                        "content":    old_content,
                        "type":       old_type,
                        "tags":       old_tags,
                        "source":     old_source,
                        "created_at": payload.get("created_at"),
                        "updated_at": payload.get("updated_at"),
                    },
                    # Prefer a real session's user_id. Fall back to the
                    # caller's edited_by_hint (MCP path), but never let the
                    # hint override an authenticated identity.
                    "edited_by":      (ctx["user_id"] if ctx
                                       else (body.edited_by_hint or None)),
                    "changed_fields": changed,
                },
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.exception(
                "update_memory: revision recording failed; refusing update (memory=%s)",
                memory_id,
            )
            raise _internal_http_error(
                503,
                "Cannot record edit history, so the update was refused: the "
                "audit trail must be durable before mutation",
                exc,
            )

    now = datetime.datetime.now(timezone.utc).isoformat()
    if body.content is not None:
        payload["content"] = body.content
        vec = get_embedding(body.content)
    else:
        vec = point.vector
    if body.tags is not None:
        # Preserve the owner: tag if the caller drops it
        new_tags = list(body.tags)
        if owner and not any(t.startswith("owner:") for t in new_tags):
            new_tags.append(f"owner:{owner}")
        payload["tags"] = new_tags
    if body.type is not None:
        payload["type"] = body.type if body.type in MEMORY_TYPES else "fact"
    payload["updated_at"] = now

    qdrant().upsert(
        collection_name=coll,
        points=[PointStruct(id=memory_point_id(memory_id), vector=vec, payload=payload)],
    )
    log.info("Updated memory %s by %s", memory_id, ctx["user_id"] if ctx else "anon")
    return _memory_payload_to_dict(payload)


@app.delete("/api/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: str, request: Request,
                  tenant_slug: str = Query("default", description="Tenant slug for collection routing")):
    """Delete a memory from Qdrant.  Only the original author or an admin may delete."""
    ctx = _auth_ctx(request)
    tenant_slug = _resolve_tenant_slug(request, tenant_slug)
    coll = _mem_coll(tenant_slug)
    existing = qdrant().retrieve(
        collection_name=coll,
        ids=[memory_point_id(memory_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")

    _assert_payload_tenant(existing[0].payload, tenant_slug, memory_id)

    if ctx is not None and not _is_admin(ctx):
        owner = _payload_owner(existing[0].payload)
        if owner not in (None, ctx["user_id"]) and not _owner_name_matches(existing[0].payload, ctx):
            raise HTTPException(status_code=403,
                                detail="Only the original author or an admin can delete this memory")

    qdrant().delete(
        collection_name=coll,
        points_selector=[memory_point_id(memory_id)],
    )
    log.info("Deleted memory %s", memory_id)


@app.post("/api/internal/memories/clear")
def clear_tenant_memories(request: Request, tenant_slug: str = Query(...)):
    """Delete every durable memory for a tenant, leaving the collection in
    place. The destructive half of a point-in-time memory restore: the caller
    asked for the corpus to BE the backup, which means what is not in the
    backup has to go.

    Internal-service only, and deliberately not reachable with a user token —
    it is the same data-destruction primitive as the org purge and belongs
    behind the same door.
    """
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    slug = (tenant_slug or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise HTTPException(status_code=400, detail="slug must match [a-z0-9-]")
    coll = _mem_coll(slug)
    try:
        before = int(qdrant().count(collection_name=coll, exact=True).count)
    except Exception:
        return {"tenant_slug": slug, "deleted": 0, "note": "no memory collection"}
    # Match on the tenant stamp rather than clearing the whole collection:
    # the collection name is the primary isolation control, and filtering on
    # the payload too means a mis-routed call deletes nothing instead of
    # someone else's corpus.
    qdrant().delete(
        collection_name=coll,
        points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=slug))])),
    )
    log.info("clear_tenant_memories(%s): cleared %d memories", slug, before)
    return {"tenant_slug": slug, "deleted": before}


@app.delete("/api/tenant/{slug}/collections")
def delete_tenant_collections(slug: str, request: Request):
    """
    Drop the Qdrant wiki and memories collections for the given tenant slug.
    Called by the editor's org-purge endpoint.  Best-effort — returns 200 even
    if a collection didn't exist.

    Previously unauthenticated, which made this a free data-destruction
    primitive for anyone reachable on the internal network. Now requires
    the internal-service token; only the editor's purge_org flow can
    legitimately call this.
    """
    if not _request_is_internal_service(request):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
        raise HTTPException(status_code=400, detail="slug must match [a-z0-9-]")
    dropped = []
    errors  = []
    for coll_name in (_wiki_coll(slug), _mem_coll(slug)):
        try:
            names = [c.name for c in qdrant().get_collections().collections]
            if coll_name in names:
                qdrant().delete_collection(coll_name)
                dropped.append(coll_name)
        except Exception as exc:
            log.warning("delete_tenant_collections(%s): could not drop %s: %s", slug, coll_name, exc)
            errors.append(str(exc))
    return {"dropped": dropped, "errors": errors}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
