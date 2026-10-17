"""
Memory API
──────────
Central interface for all memory operations.

  POST /memory/store   — removed (410 Gone); use the embedder's /api/memories/*
  POST /memory/search  — removed (410 Gone); use the embedder's /api/memories/*
  GET  /events         — paginated event log from PostgreSQL
  GET  /health         — component health check

Reranking:
  If RERANKER_ENDPOINT is set, POSTs pairs to that HTTP endpoint.
  Otherwise results are sorted by hybrid score (vector 70% + keyword 30%).
  No local ML model required — keeps the Docker image small and ARM64-friendly.
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import uuid
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
from dateutil import parser as dateutil_parser
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
QDRANT_HOST       = os.getenv("QDRANT_HOST", "qdrant")
QDRANT_PORT       = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME   = os.getenv("COLLECTION_NAME", "knowledge_base")
EMBEDDER_URL      = os.getenv("EMBEDDER_URL", "http://embedder:8001")
EDITOR_URL        = os.getenv("EDITOR_URL",   "http://editor:8080")
POSTGRES_HOST     = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT     = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB       = os.getenv("POSTGRES_DB", "memory")
POSTGRES_USER     = os.getenv("POSTGRES_USER", "memory")
# No default: a guessable fallback password is how a forgotten .env ends up
# shipping a database anyone can log in to. Refuse to start instead.
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
if not POSTGRES_PASSWORD:
    raise RuntimeError(
        "POSTGRES_PASSWORD is not set. Add it to your .env "
        "(use a high-entropy string, e.g. openssl rand -hex 32) and restart."
    )
# Optional: HTTP reranker endpoint (e.g. LM Studio reranker or jina-reranker)
# Expected: POST with {"pairs": [["query", "doc"], ...]} → {"scores": [...]}
RERANKER_ENDPOINT = os.getenv("RERANKER_ENDPOINT", "")

# Shared secret used to prove this service is a peer of the embedder / MCP
# / editor when posting cross-service requests (compactor promotions,
# cross-org admin restores). Sent as X-Internal-Service on every outbound
# call to an internal endpoint that needs to operate on a tenant other
# than the caller's own session.  Must match the value set in every other
# Elefant container's environment.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
if not INTERNAL_SERVICE_TOKEN:
    log.critical(
        "INTERNAL_SERVICE_TOKEN is not set — compactor promotions and "
        "other cross-service writes will be REJECTED by peers. Set "
        "INTERNAL_SERVICE_TOKEN in your .env (run: openssl rand -hex 32) "
        "and restart embedder + memory-api + mcp + editor."
    )


def _internal_service_headers() -> dict:
    """Headers that identify this request as coming from a peer Elefant
    service. Empty dict when the token isn't configured — peer will reject
    the request, which is the correct fail-closed posture."""
    if not INTERNAL_SERVICE_TOKEN:
        return {}
    return {"X-Internal-Service": INTERNAL_SERVICE_TOKEN}

DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Memory API", version="1.0.0")

# CORS is restricted because memory-api is an internal API — no browser
# origin should ever talk to it directly. The wide-open allow_origins=["*"]
# that lived here previously did not block real callers (memory-api isn't
# published on a host port) but is a defence-in-depth gap if the port ever
# does get exposed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],     # no cross-origin browser access permitted
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ── Internal-service auth middleware ───────────────────────────────────────
#
# memory-api used to accept any request from any caller on the internal
# Docker network and trust tenant_slug as a body/query parameter. Anyone
# inside the network — including a compromised sidecar — could read,
# write, mutate or delete any tenant's data simply by naming the slug.
# This middleware closes that gap: every request must present a valid
# X-Internal-Service token, except /health which the Docker healthcheck
# probes anonymously.
#
# This is identity authentication, NOT per-tenant authorization. Once a
# peer service is authenticated, the existing tenant_slug parameter still
# decides which tenant the operation targets — the assumption is that
# every peer (MCP, embedder, editor) is responsible for binding the slug
# to a real user identity before forwarding it here. A follow-up commit
# will tighten that contract further (signed assertion of the caller's
# verified slug rather than a free-form parameter).

_AUTH_EXEMPT_PATHS = ("/health",)


@app.middleware("http")
async def require_internal_service(request, call_next):
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    if not INTERNAL_SERVICE_TOKEN:
        # Fail-closed: refuse every non-health request until the operator
        # configures the shared secret.  The startup log above tells them
        # what to set.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"detail": "memory-api is mis-configured: "
                       "INTERNAL_SERVICE_TOKEN is not set"},
            status_code=503,
        )
    presented = (request.headers.get("x-internal-service") or "").strip()
    if not presented or not hmac.compare_digest(presented, INTERNAL_SERVICE_TOKEN):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"detail": "internal-service authentication required"},
            status_code=401,
        )
    return await call_next(request)

# ── Singletons ────────────────────────────────────────────────────────────────
_qdrant: Optional[QdrantClient] = None
_engine = None


def get_qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    return _qdrant


def engine_url(url):
    """Name the PostgreSQL driver the image actually installs (psycopg2).

    SQLAlchemy 2.1 made psycopg 3 the default for a bare `postgresql://` URL,
    which this service does not ship, so the same DSN connected on 2.0 and
    failed on 2.1. An explicit driver means the same thing on both. Any other
    URL (sqlite, or a DSN that already names its driver) is left as given.
    """
    u = make_url(url)
    if u.drivername == "postgresql":
        u = u.set(drivername="postgresql+psycopg2")
    return u


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(engine_url(DATABASE_URL), pool_pre_ping=True)
        _ensure_schema(_engine)
    return _engine


def _ensure_index(engine, label: str, *statements: str) -> None:
    """Run optional, performance-only DDL (a GIN index, an extension) in its
    own transaction, logging and swallowing any failure. Keeps a single bad
    index expression or a missing extension from rolling back the critical
    table/RLS schema (which all shares one transaction in _ensure_schema)."""
    try:
        with engine.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
            conn.commit()
    except Exception as exc:
        log.warning("schema: optional object %r skipped: %s", label, exc)


def _ensure_schema(engine):
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS events (
                id        SERIAL PRIMARY KEY,
                type      TEXT NOT NULL,
                content   TEXT NOT NULL,
                source    TEXT NOT NULL DEFAULT 'api',
                tags      TEXT[],
                metadata  JSONB,
                created   TIMESTAMPTZ NOT NULL DEFAULT now(),
                tenant_id TEXT NOT NULL DEFAULT 'default'
            )
        """))
        # Idempotent: add tenant_id to pre-existing tables that lack it
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'events' AND column_name = 'tenant_id'
                ) THEN
                    ALTER TABLE events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
                END IF;
            END$$;
        """))
        # Row-Level Security: ensure tenant rows are invisible across tenants
        conn.execute(text("ALTER TABLE events ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE events FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'events' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON events
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # ── Hot tier (PR A2) ────────────────────────────────────────────
        # Cheap append-only capture target for agents. The compactor (PR B)
        # batch-embeds these rows, clusters by cosine similarity, scores by
        # recurrence × source diversity × recency, and promotes survivors
        # into the durable Qdrant tier via the embedder. Same RLS pattern
        # as `events` so a tenant only sees its own captures.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_hot (
                id                 UUID PRIMARY KEY,
                tenant_id          TEXT NOT NULL,
                content            TEXT NOT NULL,
                type_hint          TEXT,
                tags               TEXT[] NOT NULL DEFAULT '{}',
                scope_hint         JSONB NOT NULL DEFAULT '{}'::jsonb,
                source             TEXT NOT NULL DEFAULT 'unknown',
                source_session_id  TEXT,
                captured_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                dedup_hash         TEXT NOT NULL,
                promoted_to        UUID,
                promoted_at        TIMESTAMPTZ,
                dropped_at         TIMESTAMPTZ,
                score              REAL
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS memory_hot_tenant_captured_idx "
            "ON memory_hot (tenant_id, captured_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS memory_hot_tenant_dedup_idx "
            "ON memory_hot (tenant_id, dedup_hash)"
        ))
        # Compactor's pending-queue lookup: skip rows already promoted/dropped.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS memory_hot_pending_idx "
            "ON memory_hot (tenant_id, captured_at) "
            "WHERE promoted_to IS NULL AND dropped_at IS NULL"
        ))
        # NOTE: there is intentionally no memory_hot_fts_idx here. Its historical
        # definition indexed to_tsvector('english', content || array_to_string(
        # tags,' ')), but array_to_string() is STABLE, not IMMUTABLE — so the
        # index could never actually be built. Every _ensure_schema run threw
        # "functions in index expression must be marked IMMUTABLE" at this line
        # and rolled the entire schema back (which is why git_files, defined
        # below, never got created). /memory/hot/search builds its tsvector at
        # query time and works fine on a seq scan. If the hot tier ever needs
        # the index, add it best-effort with an IMMUTABLE expression (e.g.
        # to_tsvector('english', content) || array_to_tsvector(tags)) and make
        # the query use the same expression so the planner can pick it up.
        conn.execute(text("ALTER TABLE memory_hot ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE memory_hot FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'memory_hot' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON memory_hot
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # Permissive second policy for the compactor (PR B): when the caller
        # sets app.tenant_id = '__compactor__' all rows are visible. Postgres
        # OR-combines policies, so this doesn't weaken tenant_isolation — both
        # are evaluated and a row is visible if either matches. The compactor
        # uses this only for enumeration; per-tenant work sets the real slug.
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'memory_hot' AND policyname = 'compactor_bypass'
                ) THEN
                    CREATE POLICY compactor_bypass ON memory_hot
                        USING (current_setting('app.tenant_id', true) = '__compactor__');
                END IF;
            END$$;
        """))

        # ── memory_revisions: append-only edit history for durable memories ──
        # Every PUT to embedder /api/memories/{id} snapshots the PRE-update
        # state here before mutating. The policy "corrections preserve
        # history, never silently overwrite" lives at this data layer — the
        # embedder refuses to overwrite if the snapshot can't be recorded,
        # so bypassing the audit trail is impossible from any client.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id                  BIGSERIAL PRIMARY KEY,
                tenant_id           TEXT NOT NULL,
                durable_id          UUID NOT NULL,
                content             TEXT NOT NULL,
                type                TEXT,
                tags                TEXT[] NOT NULL DEFAULT '{}',
                source              TEXT,
                snapshot_created_at TIMESTAMPTZ,
                snapshot_updated_at TIMESTAMPTZ,
                edited_by           TEXT,
                edited_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                changed_fields      TEXT[] NOT NULL DEFAULT '{}'
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS memory_revisions_tenant_durable_idx "
            "ON memory_revisions (tenant_id, durable_id, edited_at DESC)"
        ))
        conn.execute(text("ALTER TABLE memory_revisions ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE memory_revisions FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'memory_revisions' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON memory_revisions
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))

        # ── git_files: lexical index for synced git sources ──────────────────
        # The code-search counterpart to the durable Qdrant tier. Repo files
        # land here verbatim (one row per file) and are retrieved by exact
        # keyword/symbol match — Postgres FTS + trigram — instead of dense
        # embeddings. Rationale: code search is dominated by exact-token
        # lookups (function names, error strings, routes) where lexical beats
        # vectors, it's always-fresh and immune to the re-embed-on-vector-dim
        # tax, and an agent supplies the semantics at query time. Same RLS
        # pattern as memory_hot.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS git_files (
                tenant_id    TEXT NOT NULL,
                source_id    TEXT NOT NULL,
                source_name  TEXT NOT NULL DEFAULT '',
                path         TEXT NOT NULL,
                lang         TEXT,
                size_bytes   INTEGER NOT NULL DEFAULT 0,
                line_count   INTEGER NOT NULL DEFAULT 0,
                content      TEXT NOT NULL,
                indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (source_id, path)
            )
        """))
        # The FTS ('simple' config) and trigram GIN indexes for git_files are
        # created best-effort after this transaction (see below) so a failure
        # (e.g. pg_trgm unavailable) can't roll back the table + RLS. Only the
        # cheap, always-safe btree index lives in the critical transaction.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS git_files_tenant_source_idx "
            "ON git_files (tenant_id, source_id)"
        ))
        conn.execute(text("ALTER TABLE git_files ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE git_files FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'git_files' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON git_files
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))

        # ── Board: board_columns + cards (post-it Kanban) ─────────────────────
        # A third durable surface alongside memory and the wiki. Cards are thin
        # post-its; the deep links (memories, wiki, repo) are DERIVED from the
        # slug at read time, never stored. Same RLS pattern as memory_hot.
        #
        # Stage vs. column: `stage` is the architected, fixed vocabulary that
        # evidence/dispatch bind to; `board_columns.name` is the user's own
        # label. A column maps to exactly one stage; many columns may share a
        # stage. A card holds only column_id — its stage is derived through the
        # column and never stored on the card, so the two can't desync.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS board_columns (
                id          UUID PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                name        TEXT NOT NULL,
                stage       TEXT NOT NULL,
                position    DOUBLE PRECISION NOT NULL,
                on_enter    TEXT,
                color       TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                archived_at TIMESTAMPTZ
            )
        """))
        # Upgrade path for boards created before `color` existed.
        conn.execute(text("ALTER TABLE board_columns ADD COLUMN IF NOT EXISTS color TEXT"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS board_columns_tenant_position_idx "
            "ON board_columns (tenant_id, position)"
        ))
        conn.execute(text("ALTER TABLE board_columns ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE board_columns FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'board_columns' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON board_columns
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # position is DOUBLE PRECISION (float8), NOT REAL: fractional ordering
        # inserts a moved card at the midpoint of its neighbours; float4's ~7
        # significant digits exhaust after ~20 drops into the same gap
        # (reachable by hand within a month), float8 gives ~50 plus a cheap
        # column rebalance. Same reasoning on board_columns.position.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cards (
                id           UUID PRIMARY KEY,
                tenant_id    TEXT NOT NULL,
                column_id    UUID NOT NULL REFERENCES board_columns(id),
                slug         TEXT NOT NULL,
                title        TEXT NOT NULL,
                body         TEXT NOT NULL DEFAULT '',
                aliases      TEXT[] NOT NULL DEFAULT '{}',
                labels       TEXT[] NOT NULL DEFAULT '{}',
                repo         TEXT NOT NULL DEFAULT '',
                wiki_links   TEXT[] NOT NULL DEFAULT '{}',
                position     DOUBLE PRECISION NOT NULL,
                source       TEXT NOT NULL DEFAULT 'unknown',
                dispatch_ref TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                archived_at  TIMESTAMPTZ
            )
        """))
        # Upgrade path for boards created before `labels` / `wiki_links` existed.
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS labels TEXT[] NOT NULL DEFAULT '{}'"))
        # `labels` was TWO columns until the merge below: `tags` for SYSTEM
        # markers the board set itself (`note-to-self`), `labels` for the user's
        # and the agent's free-form classification. They are one field now —
        # two free-form string arrays on one card was a distinction people had
        # to be taught rather than one they could see on the card.
        #
        # What the split was protecting is still protected, one layer down. An
        # agent replacing a card's labels could not reach a marker in the other
        # column; now it cannot drop one because `card_update` re-attaches
        # RESERVED_CARD_LABELS. Same guarantee, enforced at the write path
        # instead of by the schema — and a deliberate removal is still sayable,
        # which it never was while the column was out of reach.
        _merge_card_tags_into_labels(conn)
        # `repo` is scalar, not an array: one card belongs to one repo, matching
        # one dispatch routine per repo. It replaces the `repo: github.com/o/n`
        # BODY CONVENTION as the machine-readable source — the convention was
        # prose, so reading it meant grepping and hoping. Ask before adding:
        # is the column already there? ADD COLUMN IF NOT EXISTS cannot say, and
        # the backfill below must run exactly once, on the board that has never
        # had the field, or it would fight later deliberate clears.
        repo_existed = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cards' AND column_name = 'repo'"
        )).first() is not None
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS repo TEXT NOT NULL DEFAULT ''"))
        if not repo_existed:
            _backfill_card_repos(conn)
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS wiki_links TEXT[] NOT NULL DEFAULT '{}'"))
        # People on the card: who RAISED it (`reporter`, one) and who is ON it
        # (`participants`, several). Both hold canonical principal references —
        # `user:<id>` or `agent:<id>` — never a name. A name copied onto a card
        # rots the moment somebody is renamed, and two spellings of one person
        # split every filter in two, which is the same argument that made
        # `repo` a canonicalized field rather than prose.
        #
        # Scalar vs. array mirrors what each one MEANS. Exactly one account
        # raised a card, and provenance that could name two is not provenance;
        # any number of people can be on it, added and removed independently.
        #
        # No backfill, deliberately. `source` is the only pre-existing signal
        # ("ui", "claude") and it names a KIND of client, not an account —
        # deriving a reporter from it would invent an identity and attribute
        # every agent-written card to one fictional person. A blank field is
        # visibly missing; a wrong one looks answered.
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS reporter TEXT NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS participants TEXT[] NOT NULL DEFAULT '{}'"))
        # And who is DOING it. Reporter and participants between them still do
        # not answer the question a board is mostly asked: a participant list of
        # three does not say which one has it.
        #
        # Scalar, like `reporter`, for the same reason read the other way round:
        # two assignees is no assignee. Nullable-by-blank, where blank means
        # unassigned rather than unknown. Agents are assignable — this board is
        # majority agent-worked, and a field that could only name humans could
        # not express what is true of most of its cards.
        #
        # No backfill here either, and for a stronger reason than reporter had:
        # nothing existing can be derived into an assignee without inventing one.
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS assignee TEXT NOT NULL DEFAULT ''"))
        # When the card is DUE. A calendar date, not a timestamp: "due on the
        # 27th" is a day, and a time of day would invent a precision nobody
        # gave and make the answer depend on whose timezone read it. NULL is
        # "no due date", which is most cards.
        #
        # Before this field a time-bound card carried its date as a
        # `due-YYYY-MM-DD` label, which nothing could sort, compare or warn on.
        # Those are folded in once, on the board that has never had the
        # column, by the same ask-first pattern `repo` uses.
        due_existed = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cards' AND column_name = 'due'"
        )).first() is not None
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS due DATE"))
        if not due_existed:
            _backfill_card_due_from_labels(conn)
        # Object KEYS for files attached to the card, in the org's own bucket.
        # The key carries the original filename, so there is no second place to
        # remember what a file was called and no way for the two to disagree.
        # Keys rather than URLs: serving is an authenticated route's job, and a
        # stored URL would outlive whatever route shape we have today.
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS attachments TEXT[] NOT NULL DEFAULT '{}'"))
        # The EVALUATION LOCK. An automated reviewer takes this before
        # it evaluates a card, so a second run and a human reading the board can
        # both see that the card is in somebody's hands.
        #
        # Two columns, not a label and not a structured comment, which were the
        # obvious alternatives. Neither can
        # work: a label write is a read-modify-write over an array and a comment
        # is an append, so neither can be the atomic compare-and-set the lock
        # needs. A lock that is not atomic does not prevent double evaluation,
        # which is the only thing it exists to do. Two plain columns can be set
        # by one conditional UPDATE, which is why they are columns.
        #
        # Blank-as-unlocked, like `assignee`, so there is ONE fact here rather
        # than a held flag and a holder that can disagree about whether the card
        # is locked. `eval_lock_at` is NULL exactly when `eval_lock_run` is ''.
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS eval_lock_run TEXT NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS eval_lock_at TIMESTAMPTZ"))
        # `number` is the card's short, immutable, human-quotable handle — "#33"
        # in conversation, and what a permalink is built from. It is not the
        # position, is not derived from one, and never changes once allocated.
        #
        # The counter is its own table rather than MAX(number)+1, and that is the
        # whole point rather than an optimisation. Archive is the terminal state
        # on this board (there is no card deletion), so cards leave the live view
        # but their numbers stay spent. MAX+1 would hand the highest archived
        # card's number straight to the next card created, and one tenant's
        # history would then contain two different #33s — with the older one
        # already quoted in a wiki page or a GitHub issue. A forward-only counter
        # cannot do that, no matter what the cards table currently holds.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS card_number_seq (
                tenant_id   TEXT PRIMARY KEY,
                last_number BIGINT NOT NULL DEFAULT 0
            )
        """))
        # Same ask-first pattern as `repo`: ADD COLUMN IF NOT EXISTS cannot
        # report whether it did anything, and the backfill must run exactly
        # once, on the board that has never had numbers.
        number_existed = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cards' AND column_name = 'number'"
        )).first() is not None
        conn.execute(text("ALTER TABLE cards ADD COLUMN IF NOT EXISTS number BIGINT"))
        if not number_existed:
            _backfill_card_numbers(conn)
        # Numbers are global WITHIN a tenant and each tenant counts separately,
        # so uniqueness is on the pair. This is the structural guarantee behind
        # "never reused": a bug in the allocator fails the insert instead of
        # quietly minting a duplicate.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS cards_tenant_number_idx "
            "ON cards (tenant_id, number)"
        ))
        conn.execute(text("ALTER TABLE card_number_seq ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE card_number_seq FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'card_number_seq' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON card_number_seq
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # As with cards: deliberately NO compactor bypass. The counter is board
        # state, and the compactor has no business reading or advancing it.
        # Slug is the card's identity and the join key every derived pointer
        # resolves on — two cards sharing one would poison every join. Uniqueness
        # is per-tenant, case-insensitive. lower() is IMMUTABLE so this index
        # builds safely inside the critical transaction.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS cards_tenant_slug_idx "
            "ON cards (tenant_id, lower(slug))"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS cards_column_position_idx "
            "ON cards (column_id, position)"
        ))
        conn.execute(text("ALTER TABLE cards ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE cards FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'cards' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON cards
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # ── Relationships between cards ───────────────────────────────────────
        # Its own table rather than an array on the card, and the reason is
        # mirroring. A relationship is one fact about two cards, and both cards
        # have to be able to state it: "A blocks B" read from A, "B is blocked by
        # A" read from B. Held as two arrays that is two writes, and every way of
        # writing twice has a way of writing once — a failure between them, a
        # restore of one card, a rename — and the board is then left asserting a
        # dependency from one side that the other side denies.
        #
        # One row, read from both ends, cannot disagree with itself. So the edge
        # is stored ONCE, canonically, and the two directions are views of it:
        #   blocks   directed, stored as (blocker → blocked)
        #   related  symmetric, stored with the lower id first so A-B and B-A are
        #            the same row rather than two rows that could be deleted apart
        #
        # Card IDs, not slugs. A slug can be renamed (the old one lands in
        # aliases), and a dependency that quietly stopped pointing anywhere after
        # a rename is exactly the invisible failure the field replaces.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS card_links (
                id         UUID PRIMARY KEY,
                tenant_id  TEXT NOT NULL,
                src_id     UUID NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                dst_id     UUID NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                kind       TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'unknown',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """))
        # One edge of one kind between one pair. Linking twice is idempotent
        # rather than a second row that has to be deleted twice to go away.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS card_links_edge_idx "
            "ON card_links (tenant_id, kind, src_id, dst_id)"
        ))
        # Both directions are read constantly (a card's blockers, and what it
        # blocks), so both ends are indexed.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS card_links_src_idx ON card_links (tenant_id, src_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS card_links_dst_idx ON card_links (tenant_id, dst_id)"
        ))
        conn.execute(text("ALTER TABLE card_links ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE card_links FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'card_links' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON card_links
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # ── Comments ────────────────────────────────────────────────────────
        # A card records what the work IS. The conversation ABOUT it had
        # nowhere to live, so it happened in a chat window and was gone by the
        # next session — this very table exists because a page of considered
        # feedback about the board arrived somewhere the board could not keep.
        #
        # The agent case is the same shape: a dispatch run that decides
        # something, or gets stuck on a judgement call, otherwise writes it
        # into a wiki page nobody asked for or a card body that stops being a
        # post-it. "Here is what I found, here is what I need" is a comment.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS card_comments (
                id         UUID PRIMARY KEY,
                tenant_id  TEXT NOT NULL,
                -- The card's ID, never its slug: a rename must not orphan a
                -- thread, and the slug is the one thing about a card that
                -- changes.
                card_id    UUID NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                -- A principal reference, like every other person on a card, so
                -- an author is named at read time and agents post as
                -- themselves rather than as "the agent".
                author     TEXT NOT NULL DEFAULT '',
                body       TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'unknown',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                -- SOFT delete. A removed comment leaves "deleted" behind
                -- rather than a hole in a conversation somebody is reading,
                -- and a thread that silently loses a message reads as one that
                -- never had it.
                deleted_at TIMESTAMPTZ
            )
        """))
        # Read one way only: the whole thread for one card, oldest first.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS card_comments_card_idx "
            "ON card_comments (tenant_id, card_id, created_at)"
        ))
        conn.execute(text("ALTER TABLE card_comments ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE card_comments FORCE ROW LEVEL SECURITY"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'card_comments' AND policyname = 'tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON card_comments
                        USING (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END$$;
        """))
        # No compactor_bypass here either, for the same reason as cards.

        # DELIBERATELY NO compactor_bypass policy on cards (or board_columns).
        # The compactor sets app.tenant_id = '__compactor__' and can only see
        # rows a policy admits; with no bypass here it can select nothing, so
        # "cards are never compacted" is enforced by Postgres, not convention.
        # Do not add one.

        conn.commit()

    # Performance-only GIN indexes + extensions, each in its own transaction so
    # one failure can't abort the table/RLS setup above. The git_files search
    # predicates (@@ and ILIKE) both work without these — just slower.
    _ensure_index(
        engine, "git_files_fts_idx",
        "CREATE INDEX IF NOT EXISTS git_files_fts_idx "
        "ON git_files USING GIN (to_tsvector('simple', content))",
    )
    _ensure_index(
        engine, "git_files_trgm_idx (pg_trgm)",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS git_files_trgm_idx "
        "ON git_files USING GIN (content gin_trgm_ops)",
    )
    log.info("PostgreSQL schema ready (RLS enabled on events, memory_hot, memory_revisions, git_files, board_columns, cards).")


# ── Embedding ─────────────────────────────────────────────────────────────────
async def embed(text_input: str) -> list[float]:
    """
    Get embedding from the embedder service.
    Uses /api/embed (single-text convenience endpoint added to embedder).
    Falls back to reading the collection's vector dimensions if the endpoint
    is unavailable (shouldn't happen in normal ops).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{EMBEDDER_URL}/api/embed",
            json={"text": text_input},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


# ── Reranking ─────────────────────────────────────────────────────────────────
async def rerank(query: str, texts: list[str]) -> list[float]:
    """
    Rerank texts against query using an external HTTP reranker.
    Returns a score list parallel to texts[].
    If RERANKER_ENDPOINT is not set, raises — caller falls back to hybrid score.
    """
    if not RERANKER_ENDPOINT:
        raise RuntimeError("No RERANKER_ENDPOINT configured")
    pairs = [[query, t] for t in texts]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(RERANKER_ENDPOINT, json={"pairs": pairs})
        resp.raise_for_status()
        return resp.json()["scores"]


def _keyword_score(text_lower: str, query_terms: list[str]) -> float:
    if not query_terms:
        return 0.0
    matched = sum(1 for t in query_terms if t in text_lower)
    if matched == len(query_terms):
        return 1.0
    if matched > 0:
        return 0.5
    return 0.0


# ── Input size caps (SEC-08) ─────────────────────────────────────────────────
# Without a cap a single token can write gigabytes of text a day. The limits
# are generous: a memory or a card that runs past them belongs in the wiki.
# Over-long input is refused with a 422 before anything is stored.
MAX_CONTENT_CHARS = 200_000     # a memory's text
MAX_QUERY_CHARS = 10_000
MAX_TITLE_CHARS = 1_000
MAX_CARD_BODY_CHARS = 200_000
MAX_NAME_CHARS = 200            # a tag, label, column, slug, source, reporter …
MAX_REF_CHARS = 1_024           # a wiki path or repo reference
MAX_LIST_ITEMS = 200            # tags, labels, links, participants …

ShortStr = Annotated[str, Field(max_length=MAX_NAME_CHARS)]
RefStr = Annotated[str, Field(max_length=MAX_REF_CHARS)]


def _short_list():
    return Field(default_factory=list, max_length=MAX_LIST_ITEMS)


# ── Schemas ───────────────────────────────────────────────────────────────────
class StoreRequest(BaseModel):
    text: str = Field(max_length=MAX_CONTENT_CHARS)
    type: ShortStr = "fact"
    tags: list[ShortStr] = _short_list()
    metadata: dict = {}


class SearchRequest(BaseModel):
    query: str = Field(max_length=MAX_QUERY_CHARS)
    limit: int = 5
    type_filter: Optional[ShortStr] = None
    tags: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)


class CaptureRequest(BaseModel):
    """Hot-tier capture body. No embedding cost; just a Postgres INSERT.
    Compactor (PR B) batch-embeds, clusters, and promotes survivors."""
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    type_hint: Optional[ShortStr] = None
    tags: list[ShortStr] = _short_list()
    scope_hint: dict = {}
    source: ShortStr = "unknown"
    source_session_id: Optional[ShortStr] = None
    tenant_slug: str = "default"


class MemorySnapshot(BaseModel):
    """Pre-update state of a durable memory, captured before the embedder
    overwrites it. Each field is the value as it stood just before the
    update — what the audit trail should show as "the previous version"."""
    content: str
    type: Optional[str] = None
    tags: list[str] = []
    source: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RevisionRecord(BaseModel):
    """Body for POST /memory/revisions — the embedder posts this before
    mutating a durable Qdrant payload, so refusing the insert blocks the
    overwrite."""
    tenant_slug: str = "default"
    durable_id: str
    snapshot: MemorySnapshot
    edited_by: Optional[str] = None
    changed_fields: list[str] = []


def _normalize_for_dedup(s: str) -> str:
    """Cheap canonical form: lower-case, trim, collapse internal whitespace.
    Used only for the short-window dedup hash — real semantic dedup is the
    compactor's job at cluster time."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _dedup_hash(s: str) -> str:
    return hashlib.sha256(_normalize_for_dedup(s).encode("utf-8")).hexdigest()


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # The board stream fans out from worker threads back onto this loop, so it
    # needs the loop that is actually serving requests.
    _board_hub.bind_loop(asyncio.get_running_loop())
    # Wait for PostgreSQL
    # Off the loop, and an async sleep between tries: this can take up to a
    # minute, and the board hub bound above is already serving on the loop.
    for attempt in range(30):
        try:
            await asyncio.to_thread(get_engine)
            log.info("PostgreSQL connected.")
            break
        except Exception as exc:
            log.info("Waiting for PostgreSQL... (%s)", exc)
            await asyncio.sleep(2)
    # Qdrant connection is lazy — tested in /health
    log.info("Memory API ready. Reranker: %s", RERANKER_ENDPOINT or "disabled (hybrid score only)")

    # Start the compactor loop unless explicitly disabled. Kill-switch is
    # the env var COMPACTOR_ENABLED=0 — useful when chasing a misbehaving
    # promote/drop policy without redeploying.
    global _compactor_task
    if COMPACTOR_ENABLED:
        _compactor_task = asyncio.create_task(_compactor_loop())
    else:
        log.info("compactor: disabled via COMPACTOR_ENABLED=0")


@app.on_event("shutdown")
async def shutdown():
    global _compactor_task
    if _compactor_task and not _compactor_task.done():
        _compactor_task.cancel()
        try:
            await _compactor_task
        except (asyncio.CancelledError, Exception):
            pass


# ── POST /memory/capture (hot tier) ──────────────────────────────────────────
@app.post("/memory/capture", status_code=201)
def memory_capture(req: CaptureRequest):
    """Cheap append-only capture into the hot tier.

    Designed so agents can stream memories aggressively without embedding
    cost or worrying about bloat — the compactor (PR B) scans this table
    periodically, clusters by cosine similarity, scores recurrence ×
    source diversity × recency, and promotes survivors to the durable
    Qdrant tier via the embedder.

    Tenant isolation is enforced via Postgres RLS on the `tenant_id`
    column (same pattern as `events`).
    """
    if not req.content or not req.content.strip():
        raise HTTPException(status_code=400, detail="content is required")

    slug = (req.tenant_slug or "default").strip() or "default"
    dh = _dedup_hash(req.content)
    source_norm = (req.source or "unknown").strip().lower() or "unknown"
    type_hint = (req.type_hint or "").strip().lower() or None

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        # 60-second short-window dedup: a retried tool call shouldn't write
        # two identical pending rows. Real semantic dedup happens in the
        # compactor at cluster time; this is just spam control.
        existing = conn.execute(
            text("""
                SELECT id, captured_at FROM memory_hot
                WHERE tenant_id = :tenant_id
                  AND dedup_hash = :dh
                  AND promoted_to IS NULL
                  AND dropped_at IS NULL
                  AND captured_at > (now() - INTERVAL '60 seconds')
                ORDER BY captured_at DESC LIMIT 1
            """),
            {"tenant_id": slug, "dh": dh},
        ).fetchone()
        if existing:
            return {
                "id":          str(existing[0]),
                "captured_at": existing[1].isoformat() if existing[1] else None,
                "deduped":     True,
            }
        row_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO memory_hot
                    (id, tenant_id, content, type_hint, tags, scope_hint,
                     source, source_session_id, dedup_hash)
                VALUES
                    (:id, :tenant_id, :content, :type_hint, :tags,
                     CAST(:scope_hint AS JSONB),
                     :source, :session, :dedup_hash)
            """),
            {
                "id":         row_id,
                "tenant_id":  slug,
                "content":    req.content,
                "type_hint":  type_hint,
                "tags":       req.tags or [],
                # JSONB column: pass JSON string + cast for portability across
                # psycopg2 versions (some setups auto-convert dict, some don't).
                "scope_hint": json.dumps(req.scope_hint or {}),
                "source":     source_norm,
                "session":    req.source_session_id,
                "dedup_hash": dh,
            },
        )
        conn.commit()
    log.info("memory_capture id=%s tenant=%s source=%s",
             row_id, slug, source_norm)
    return {
        "id":          row_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "deduped":     False,
    }


# ── Board (post-it Kanban) ────────────────────────────────────────────────────
# Thin cards on a per-tenant board. `stage` is the architected vocabulary that
# evidence/dispatch bind to; a column maps to exactly one stage and carries the
# user's own name. Cards hold column_id only — stage is derived through the
# column, never stored on the card, so the two can't desync. Every limit here
# is advisory (returned in warnings[]); the single hard rule is slug uniqueness,
# which is identity, not a limit.

# The architected stage vocabulary. A column maps to exactly
# one of these; its NAME is the user's own.
BOARD_STAGES = [
    "backlog", "selected-for-development", "in-progress",
    "scheduled", "blocked", "review", "done",
]

# The vocabulary this replaced. Every board that existed before the change is
# mapped to it, and the change set new defaults for a NEW board — not a migration
# of the boards people are working on. So these stay VALID: a column carrying
# one still saves, still filters, and is still offered in the picker for the
# board that uses it. They are simply not offered to a board that has never
# seen them.
#
# Not a translation table on purpose. Remapping `shaping` to something in the
# new list would move somebody's column out from under them, and the two
# vocabularies do not line up cleanly anyway (`shaping` and `dropped` have no
# counterpart). The only place the two are reconciled is TERMINAL_STAGES, where
# the meaning — "this card is finished, it blocks nothing" — is the same fact
# under either name and a blocker that failed to resolve would stall a column.
LEGACY_BOARD_STAGES = [
    "spark", "shaping", "designed", "ready",
    "building", "shipped", "dropped",
]

# What a column may be set to: the current vocabulary, plus the old one so an
# existing column can still be edited without being silently remapped first.
VALID_BOARD_STAGES = BOARD_STAGES + LEGACY_BOARD_STAGES
# Off by default. Cards started as post-its and became
# fully-formed issue tracking; a description warned about on length was the
# product arguing with what it had become. `_nudge_warning` already reads 0 as
# "no nudge", and `board_nudge_chars` remains settable, so a tenant that wants
# one can still ask for it — it is just no longer imposed.
BOARD_NUDGE_DEFAULT = 0

# How long an evaluation lock may sit before a new acquire is allowed to break
# it. A run can die mid-evaluation and leave a card locked forever, and Elefant
# must not depend on the reviewer always releasing cleanly.
#
# Deliberately read from Elefant's own environment rather than taken as a
# request field: Elefant owns the lock, so the staleness ceiling is Elefant
# policy. A caller that could name its own ceiling could pass zero and walk
# through every lock on the board, which is the one thing the lock prevents.
#
# 30 minutes is a small multiple of the longest check a reviewer is expected to
# run (a full render, a print check). Note this is NOT refreshed by re-acquiring
# — see card_eval_lock — so it has to be longer than a whole evaluation, not
# longer than one step of one.
CARD_EVAL_LOCK_STALE_SECONDS = int(os.getenv("CARD_EVAL_LOCK_STALE_SECONDS", "1800"))

# Seeded once per tenant on first touch: one column per stage, in board order.
# The user renames / adds / remaps freely afterwards. A board seeded under the
# legacy vocabulary keeps the five columns it was given — this is what a NEW board
# starts with, not a migration.
_SEED_COLUMNS = [
    ("Backlog",     "backlog"),
    ("Up Next",     "selected-for-development"),
    ("In Progress", "in-progress"),
    ("Scheduled",   "scheduled"),
    ("Blocked",     "blocked"),
    ("Review",      "review"),
    ("Done",        "done"),
]

_BOARD_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _board_slugify(title: str) -> str:
    """lowercase, strip to [a-z0-9-], collapse runs of '-', trim."""
    return _BOARD_SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")


def _set_tenant(conn, slug: str) -> None:
    conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})


def _ensure_seed_columns(conn, slug: str) -> None:
    """Insert the default columns if this tenant has none yet. Idempotent;
    runs inside the caller's transaction so the seeded rows are visible to the
    same request's subsequent SELECTs before commit.

    Serialised per tenant by an advisory lock. This is a check-then-insert with
    no unique constraint behind it, and it is reached from nearly every board
    endpoint — so an agent touching a fresh board with two calls at once had
    both transactions read count=0 and both insert the five rows, leaving every
    column duplicated (issue #93). The lock is transaction-scoped, released on
    commit or rollback, and keyed on the tenant so boards don't block each
    other.
    """
    # hashtext() is stable within a Postgres major version, and a collision
    # between two tenants would only mean one waits briefly for the other.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:t))"), {"t": slug})

    n = conn.execute(
        text("SELECT count(*) FROM board_columns WHERE tenant_id = :t"),
        {"t": slug},
    ).scalar()
    if n and n > 0:
        _heal_duplicate_seed_columns(conn, slug)
        return
    for i, (name, stage) in enumerate(_SEED_COLUMNS, start=1):
        conn.execute(
            text("""
                INSERT INTO board_columns (id, tenant_id, name, stage, position)
                VALUES (:id, :t, :name, :stage, :pos)
            """),
            {"id": str(uuid.uuid4()), "t": slug, "name": name,
             "stage": stage, "pos": float(i)},
        )


def _heal_duplicate_seed_columns(conn, slug: str) -> None:
    """Remove columns duplicated by the pre-lock race, on next touch.

    The lock above stops new duplicates; boards that already raced still show
    every column twice. A duplicate from that race is by construction EMPTY —
    it was created at first touch, before any card could reference it — so
    dropping the empty one of a same-named pair is safe and loses nothing.
    A duplicate holding cards is left alone: two real columns that happen to
    share a name are the user's business, not ours to merge.
    """
    conn.execute(
        text("""
            DELETE FROM board_columns bc
            WHERE bc.tenant_id = :t
              AND bc.archived_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.column_id = bc.id)
              AND EXISTS (
                    SELECT 1 FROM board_columns keep
                    WHERE keep.tenant_id = bc.tenant_id
                      AND keep.archived_at IS NULL
                      AND lower(keep.name) = lower(bc.name)
                      AND (keep.position, keep.id) < (bc.position, bc.id)
              )
        """),
        {"t": slug},
    )


def _resolve_column(conn, slug: str, ref: str):
    """Resolve a column reference (id or case-insensitive name) to a mapping row.
    An exact id match wins (even if archived); name matches consider only
    non-archived columns. Returns the row mapping or None."""
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        uuid.UUID(ref)                       # id path only for a well-formed UUID
        row = conn.execute(
            text("SELECT * FROM board_columns WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": ref},
        ).mappings().fetchone()
        if row:
            return row
    except ValueError:
        pass
    return conn.execute(
        text("""
            SELECT * FROM board_columns
            WHERE tenant_id = :t AND archived_at IS NULL AND lower(name) = lower(:name)
            ORDER BY position LIMIT 1
        """),
        {"t": slug, "name": ref},
    ).mappings().fetchone()


def _fetch_card(conn, slug: str, ref: str):
    """Fetch a card by its slug OR any of its aliases (case-insensitive), so
    history keeps resolving after a rename. Prefers the live card when an alias
    collides. Returns the mapping row or None."""
    return conn.execute(
        text("""
            SELECT * FROM cards
            WHERE tenant_id = :t
              AND (lower(slug) = lower(:s)
                   OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = lower(:s)))
            ORDER BY archived_at NULLS FIRST
            LIMIT 1
        """),
        {"t": slug, "s": ref},
    ).mappings().fetchone()


def _eval_lock_dict(row) -> Optional[dict]:
    """A card's evaluation lock as the API reports it, or None when unlocked.

    ONE field rather than a `locked` flag beside a holder: two fields can
    disagree about whether a card is locked, and a caller then has to know
    which of them wins. `None` or an object, and the object always names who
    holds it and since when.

    `stale` is computed here rather than left to the caller to work out from
    `since`, because the staleness ceiling is Elefant's (see
    CARD_EVAL_LOCK_STALE_SECONDS) and a caller doing that arithmetic itself
    would be applying its own policy to Elefant's lock. It says "a new acquire
    would break this one", which is what anybody reading the field wants to
    know.
    """
    run = (row["eval_lock_run"] or "") if "eval_lock_run" in row else ""
    if not run:
        return None
    at = row["eval_lock_at"] if "eval_lock_at" in row else None
    out = {"run_id": run, "since": at.isoformat() if at else None, "stale": False}
    if at is not None:
        # The column is TIMESTAMPTZ so the driver hands back an aware datetime,
        # but this runs on EVERY card read: a naive value arriving from anywhere
        # (a restored row, a hand-written fixture) would raise here and take the
        # whole board down with it rather than one field. Treated as UTC, which
        # is what the database stores.
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - at).total_seconds()
        out["stale"] = age > CARD_EVAL_LOCK_STALE_SECONDS
    return out


def _card_dict(row, column_name: str, stage: str) -> dict:
    return {
        "id":           str(row["id"]),
        # Nullable in the payload on purpose: a card written by an older image
        # before the backfill ran has no number, and the UI must render nothing
        # rather than "#null".
        "number":       int(row["number"]) if row["number"] is not None else None,
        "slug":         row["slug"],
        "title":        row["title"],
        "body":         row["body"],
        "aliases":      list(row["aliases"] or []),
        "labels":       list(row["labels"] or []),
        "repo":         row["repo"] or "",
        # References, never names. The display name is resolved at read time by
        # whoever is rendering (the editor for the browser, the editor via MCP
        # for an agent), so a person renamed once is renamed everywhere.
        "reporter":     row["reporter"] or "",
        "assignee":     row["assignee"] or "",
        # ISO `YYYY-MM-DD`, or None. Read defensively: a row handed over by an
        # older query shape without the column must not take the board down.
        "due":          (row["due"].isoformat() if row["due"] else None) if "due" in row else None,
        "participants": list(row["participants"] or []),
        "wiki_links":   list(row["wiki_links"] or []),
        "attachments":  list(row["attachments"] or []),
        "column":       column_name,
        "column_id":    str(row["column_id"]),
        "stage":        stage,
        "position":     row["position"],
        "source":       row["source"],
        "dispatch_ref": row["dispatch_ref"],
        # Closed, not archived. Taking a card off the board is closing it —
        # the word the rest of issue tracking uses, and the one a card's
        # "when it was closed" needs. The stored column is still archived_at
        # (it is the same instant, and renaming a column buys nothing), and
        # `archived` stays on the payload as an alias so an older UI or an
        # external caller keeps working across a rolling deploy.
        "closed":       row["archived_at"] is not None,
        "closed_at":    row["archived_at"].isoformat() if row["archived_at"] else None,
        "archived":     row["archived_at"] is not None,   # deprecated alias of `closed`
        # The evaluation lock rides on the ORDINARY card payload, so any caller
        # sees it by reading the card and none of them needs a second call to
        # find out whether a card is in somebody's hands. That is most of the
        # reason the lock lives on the card in Elefant at all.
        "eval_lock":    _eval_lock_dict(row),
        "created_at":   row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at":   row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _card_joined(conn, slug: str, card_id: str):
    """Re-read a card joined to its column (for a response), before commit."""
    return conn.execute(
        text("""
            SELECT c.*, bc.name AS column_name, bc.stage AS stage
            FROM cards c JOIN board_columns bc ON c.column_id = bc.id
            WHERE c.tenant_id = :t AND c.id = :id
        """),
        {"t": slug, "id": str(card_id)},
    ).mappings().fetchone()


def _nudge_warning(body: str, nudge_chars: int) -> list:
    """Advisory only, and off unless a tenant asks for it (see
    BOARD_NUDGE_DEFAULT). There is no maximum description length — this never
    refused a write and does not now."""
    if body and nudge_chars > 0 and len(body) > nudge_chars:
        return [
            f"description is {len(body)} chars, past the {nudge_chars}-char nudge "
            f"this board asked for — consider writing it up as a wiki page under "
            f"AI/projects/<slug>/ and linking it from the card."
        ]
    return []


class CardAddRequest(BaseModel):
    tenant_slug: str = "default"
    title: str = Field(max_length=MAX_TITLE_CHARS)
    slug: Optional[ShortStr] = None
    body: str = Field(default="", max_length=MAX_CARD_BODY_CHARS)
    column: Optional[ShortStr] = None     # column id or name; default = leftmost backlog column
    # Pre-merge spelling of `labels`, folded into it on write. See card_add.
    tags: list[ShortStr] = _short_list()
    labels: list[ShortStr] = _short_list()
    repo: RefStr = ""
    wiki_links: list[RefStr] = _short_list()
    # Who this card is ABOUT (reporter/participants) and who is MAKING the call
    # (actor). They are usually the same and deliberately separate: an agent
    # filing a card on a person's behalf reports it as that person, and is
    # itself put on the card as a participant. Both accept anything the
    # directory can resolve — a reference, a username, a display name.
    reporter: ShortStr = ""
    assignee: ShortStr = ""
    # `YYYY-MM-DD`; blank is no due date.
    due: ShortStr = ""
    participants: list[ShortStr] = _short_list()
    actor: ShortStr = ""
    source: ShortStr = "unknown"
    nudge_chars: int = BOARD_NUDGE_DEFAULT


class CardMoveRequest(BaseModel):
    tenant_slug: str = "default"
    column: str                            # target column id or name
    position: Optional[float] = None
    source: str = "unknown"


class CardUpdateRequest(BaseModel):
    tenant_slug: str = "default"
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_CHARS)
    body: Optional[str] = Field(default=None, max_length=MAX_CARD_BODY_CHARS)
    aliases: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    # Accepted, folded into `labels`, and kept only for the pre-merge clients
    # the published skill still has in the field.
    tags: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    labels: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    # Opt out of reserved-label preservation, for a caller that means to take
    # one off. Default False so the protection is what you get by not thinking
    # about it, which is the state every routine label rewrite is in.
    allow_reserved: bool = False
    # None leaves the repo alone; "" is a deliberate clear. A scalar needs the
    # distinction the list fields get for free, or a card could never lose a
    # repo it was given by mistake.
    repo: Optional[RefStr] = None
    wiki_links: Optional[list[RefStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    attachments: Optional[list[RefStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    # Same None-vs-"" distinction as `repo`, for the same reason: editing a
    # card's title must not be able to strip who raised it. Participants are a
    # list, so None leaves them alone and [] is a deliberate clear.
    reporter: Optional[ShortStr] = None
    assignee: Optional[ShortStr] = None
    # None leaves the due date alone; "" is a deliberate clear, as for
    # `assignee` and `repo`.
    due: Optional[ShortStr] = None
    participants: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    new_slug: Optional[ShortStr] = None
    nudge_chars: int = BOARD_NUDGE_DEFAULT


class CardArchiveRequest(BaseModel):
    tenant_slug: str = "default"


def _slugify_or_400(raw: str) -> str:
    s = _board_slugify(raw)
    if not s:
        raise HTTPException(
            status_code=400,
            detail="Could not derive a slug (need at least one [a-z0-9] character); "
                   "pass an explicit slug.",
        )
    return s


# Labels the BOARD sets itself, as opposed to classification a person or an
# agent chose. `note-to-self` is written at capture time by the note_to_self
# tool and is how the user finds what they filed with that phrase, so losing it
# loses the record of how the card was captured.
#
# A replace must not drop one of these. Removing one stays possible — that is
# what `allow_reserved` on a card update is for, and it is what the
# single-label path sets when it is asked to take a label off by name. The rule
# is only that it cannot happen as a SIDE EFFECT of rewriting the whole list,
# which is the routine operation an agent performs without thinking about it.
RESERVED_CARD_LABELS = {"note-to-self"}


def _keep_reserved(before, after: list) -> list:
    """Re-attach reserved labels that replacing the list would silently drop."""
    have = {lb.lower() for lb in after}
    return after + [lb for lb in (before or [])
                    if lb.lower() in RESERVED_CARD_LABELS and lb.lower() not in have]


def _merge_card_tags_into_labels(conn) -> None:
    """One-time collapse of `cards.tags` into `cards.labels`, then drop it.

    Guarded on the column still being there, so it runs exactly once and is a
    no-op on every later start — the same ask-first pattern `repo` and `number`
    use, for the same reason: ADD/DROP COLUMN IF EXISTS cannot report whether it
    did anything.

    Merge and drop run in the SAME transaction as the rest of the schema work.
    A half-applied migration would leave values in a column nothing reads any
    more, which from the board's point of view is indistinguishable from having
    lost them.
    """
    exists = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'cards' AND column_name = 'tags'"
    )).first() is not None
    if not exists:
        return
    # RLS comes OFF for the length of the migration, and this is the line the
    # whole function turns on. `cards` is FORCE ROW LEVEL SECURITY with a
    # policy of `tenant_id = current_setting('app.tenant_id', true)`, and
    # schema startup is the one path that never sets a tenant — it is not
    # serving a request, it is fixing up every tenant at once. Left on, the
    # policy evaluates against NULL, the UPDATE matches ZERO rows, and the DROP
    # below then takes every tag on the board with it. Silently, and with the
    # backup being the board it just emptied.
    #
    # FORCE is what makes this reachable: it applies the policy to the table's
    # OWNER too, so "we own the table" is not the escape it looks like. Owning
    # it is what lets us lift the policy explicitly instead. A superuser role
    # would bypass RLS and never see this, which is exactly why it must not be
    # left to depend on how the operator provisioned their database.
    #
    # Re-enabled immediately below, and again by the unconditional ENABLE/FORCE
    # further down _ensure_schema — and the whole function is one transaction,
    # so a failure in between rolls the table back protected rather than bare.
    conn.execute(text("ALTER TABLE cards DISABLE ROW LEVEL SECURITY"))
    # Labels first, then tags, de-duped case-insensitively with the first
    # spelling winning and the original order kept — _clean_tags' hygiene
    # written in SQL so it applies to every tenant's rows in one statement.
    # DISTINCT ON keeps the first row per lowercased value; the outer ORDER BY
    # puts the survivors back in the order they appeared, rather than the
    # alphabetical order DISTINCT ON had to sort by to choose them.
    conn.execute(text("""
        UPDATE cards SET labels = ARRAY(
            SELECT d.val FROM (
                SELECT DISTINCT ON (lower(btrim(u.v)))
                       btrim(u.v) AS val, u.ord
                FROM unnest(labels || tags) WITH ORDINALITY AS u(v, ord)
                WHERE btrim(u.v) <> ''
                ORDER BY lower(btrim(u.v)), u.ord
            ) d ORDER BY d.ord
        )
        WHERE tags <> '{}'
    """))
    conn.execute(text("ALTER TABLE cards ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE cards DROP COLUMN tags"))


def _clean_tags(tags) -> list:
    """Strip, drop empties, de-dup (case-insensitive, first-casing wins)."""
    seen, out = set(), []
    for t in (tags or []):
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# One canonical form for a repo reference: `owner/name`. Everything a person
# might paste normalizes into it — the browser URL, the clone URL, the SSH
# remote, the bare pair — because the value's job is to be COMPARED (filter the
# board to one repo) and two spellings of one repo would split that filter in
# two. GitHub's own rule for both segments: ASCII letters, digits, dot, dash,
# underscore.
_REPO_SEGMENT = r"[A-Za-z0-9._-]+"
_REPO_RE = re.compile(rf"^({_REPO_SEGMENT})/({_REPO_SEGMENT})$")
# Only github.com is stripped as a host. A different host is left in place so
# it fails validation loudly rather than being silently filed as GitHub.
_REPO_PREFIXES = re.compile(
    r"^(?:https?://)?(?:git@)?(?:www\.)?github\.com[/:]", re.IGNORECASE)


def _clean_repo(value) -> str:
    """Normalize a repo reference to `owner/name`, or '' for no repo.

    Accepts the forms people actually paste: https://github.com/o/n(.git),
    github.com/o/n, git@github.com:o/n.git, or a bare o/n. Raises 400 on
    anything else rather than storing junk that no filter will ever match —
    a card claiming a repo nobody can resolve is worse than a card with none.
    """
    s = (value or "").strip()
    if not s:
        return ""
    s = _REPO_PREFIXES.sub("", s).strip().strip("/")
    # A dot is legal INSIDE a repo name (`some_repo.js`), but GitHub rejects a
    # name that ENDS in one — so a trailing dot is punctuation, not part of the
    # name. It matters most for the backfill, where the convention is written
    # mid-sentence: "repo: github.com/acme/widgets. See wiki." would
    # otherwise store `widgets.` and match nothing forever.
    s = s.rstrip(".")
    if s.lower().endswith(".git"):
        s = s[:-4].rstrip(".")
    m = _REPO_RE.match(s)
    if not m:
        raise HTTPException(
            status_code=400,
            detail=(f"'{value}' is not a GitHub repo reference. Expected "
                    "owner/name, or a github.com URL that resolves to one "
                    "(https://github.com/owner/name, git@github.com:owner/name.git)."),
        )
    return f"{m.group(1)}/{m.group(2)}"


def _match_repo(value) -> str:
    """Normalize a repo FILTER value, leniently.

    Same canonicalization as `_clean_repo`, but a value that does not parse is
    passed through instead of raising: a filter that matches nothing is a fine
    answer to a malformed query, whereas a 400 would break a board that merely
    has a stale filter in its URL.
    """
    try:
        return _clean_repo(value)
    except HTTPException:
        return (value or "").strip()


# The body convention this field replaces, e.g. "repo: github.com/acme/widgets".
_REPO_BODY_RE = re.compile(
    rf"repo:\s*((?:https?://)?(?:www\.)?github\.com/{_REPO_SEGMENT}/{_REPO_SEGMENT})",
    re.IGNORECASE)


def _backfill_card_repos(conn) -> int:
    """Populate `repo` from the `repo:` body convention, once, at migration.

    Deliberately narrow: it reads only the explicit `repo: <github.com URL>`
    form the standard prescribes. A bare `owner/name` after `repo:` is NOT
    matched, because ordinary prose contains slashed pairs and a wrong repo on
    a card is worse than a blank one — a blank field is visibly missing, a
    wrong one sends a routine at the wrong repository. Bodies it cannot parse
    keep their prose and get a field a human or agent sets deliberately.
    """
    rows = conn.execute(text(
        "SELECT id, body FROM cards WHERE repo = '' AND body ILIKE '%repo:%'"
    )).mappings().all()
    filled = 0
    for r in rows:
        m = _REPO_BODY_RE.search(r["body"] or "")
        if not m:
            continue
        try:
            repo = _clean_repo(m.group(1))
        except HTTPException:
            continue
        if not repo:
            continue
        conn.execute(text("UPDATE cards SET repo = :repo WHERE id = :id"),
                     {"repo": repo, "id": r["id"]})
        filled += 1
    if filled:
        log.info("board: backfilled repo on %d card(s) from the body convention", filled)
    return filled


# The label convention the `due` field replaces, e.g. `due-2026-11-27`.
_DUE_LABEL_RE = re.compile(r"^due-(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)


def _backfill_card_due_from_labels(conn) -> int:
    """Move `due-YYYY-MM-DD` labels into the `due` field, once, at migration.

    The label is removed once its date is in the field, so the card does not
    say the same thing twice and the two can never disagree. A label whose date
    is not a real date (`due-2026-02-30`) is left exactly where it is: a wrong
    due date is worse than a label a person can still read. Where a card has
    more than one, the first wins and the rest stay as labels, visibly.

    RLS comes off for the length of the backfill for the reason spelled out in
    _merge_card_tags_into_labels: schema startup never sets a tenant, so under
    FORCE ROW LEVEL SECURITY the SELECT would see no rows and the backfill
    would silently do nothing. It is re-enabled here and again, with FORCE, by
    the unconditional statements further down _ensure_schema.
    """
    conn.execute(text("ALTER TABLE cards DISABLE ROW LEVEL SECURITY"))
    rows = conn.execute(text(
        "SELECT id, labels FROM cards WHERE due IS NULL AND EXISTS "
        "(SELECT 1 FROM unnest(labels) l WHERE l ~* '^due-[0-9]{4}-[0-9]{2}-[0-9]{2}$')"
    )).mappings().all()
    filled = 0
    for r in rows:
        labels = list(r["labels"] or [])
        for i, lb in enumerate(labels):
            m = _DUE_LABEL_RE.match((lb or "").strip())
            if not m:
                continue
            try:
                due = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            conn.execute(
                text("UPDATE cards SET due = :due, labels = :labels WHERE id = :id"),
                {"due": due, "labels": labels[:i] + labels[i + 1:], "id": r["id"]})
            filled += 1
            break
    conn.execute(text("ALTER TABLE cards ENABLE ROW LEVEL SECURITY"))
    if filled:
        log.info("board: moved a due-YYYY-MM-DD label into `due` on %d card(s)", filled)
    return filled


def _clean_due(value) -> Optional[date]:
    """Parse a due date as given on the wire: `YYYY-MM-DD`, or '' for none.

    Strict, and 400 on anything else, for the reason `_clean_repo` is: a value
    that half-parses would be stored as a date nobody meant. A full ISO
    timestamp is accepted and cut to its date, because that is what a caller
    holding a datetime naturally sends, and the day is the whole of the field.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = (value or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10] if len(s) > 10 and s[10] in "T " else s)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"'{value}' is not a date. Give a due date as YYYY-MM-DD, e.g. 2026-11-27.",
        )


def _match_due(value) -> Optional[date]:
    """A due-date FILTER value, leniently: unparseable means no filter would
    match, so it is reported as a warning by the caller rather than a 400."""
    try:
        return _clean_due(value)
    except HTTPException:
        return None


def _today_utc() -> date:
    """Today, as the board decides "overdue". UTC, because the server has no
    one viewer's timezone to prefer; the UI shades its own chips by the
    viewer's local day."""
    return datetime.now(timezone.utc).date()


def _backfill_card_numbers(conn) -> int:
    """Give every existing card a number, once, at migration.

    Ordered by creation so the numbers read as the history they describe: the
    oldest card on a board becomes #1. Deliberately cross-tenant in one pass,
    grouped by tenant_id, because the sequence is per-tenant — two tenants both
    get a #1, which is the specified behaviour rather than a collision.

    Archived cards are numbered too. They are still cards, they are still
    quotable, and skipping them would leave gaps that look like the reuse this
    design exists to prevent.

    Seeds each tenant's counter to the highest number it just handed out, so
    the next live allocation continues rather than restarting at 1.
    """
    counters: dict = {}
    # Any numbers already present win, so a partial or re-run migration cannot
    # hand out a number twice.
    for r in conn.execute(text(
        "SELECT tenant_id, COALESCE(MAX(number), 0) AS m FROM cards "
        "WHERE number IS NOT NULL GROUP BY tenant_id"
    )).mappings().all():
        counters[r["tenant_id"]] = int(r["m"])

    rows = conn.execute(text(
        "SELECT id, tenant_id FROM cards WHERE number IS NULL "
        "ORDER BY tenant_id, created_at, id"
    )).mappings().all()
    for r in rows:
        t = r["tenant_id"]
        counters[t] = counters.get(t, 0) + 1
        conn.execute(text("UPDATE cards SET number = :n WHERE id = :id"),
                     {"n": counters[t], "id": r["id"]})

    for t, last in counters.items():
        conn.execute(text("""
            INSERT INTO card_number_seq (tenant_id, last_number) VALUES (:t, :n)
            ON CONFLICT (tenant_id) DO UPDATE
                SET last_number = GREATEST(card_number_seq.last_number, EXCLUDED.last_number)
        """), {"t": t, "n": last})

    if rows:
        log.info("board: backfilled number on %d card(s) across %d tenant(s)",
                 len(rows), len(counters))
    return len(rows)


def _next_card_number(conn, tenant: str) -> int:
    """Allocate this tenant's next card number.

    One statement on purpose. The ON CONFLICT DO UPDATE takes a row lock on the
    tenant's counter, so two cards created at the same moment serialise here and
    come out with different numbers — a read-then-write would race and issue the
    same number twice, which the unique index would then reject as a 500 on an
    ordinary card creation.

    Advances even if the surrounding transaction later rolls back. That is
    correct: a burnt number is a gap, and gaps are fine. Reuse is not.
    """
    return int(conn.execute(text("""
        INSERT INTO card_number_seq (tenant_id, last_number) VALUES (:t, 1)
        ON CONFLICT (tenant_id)
        DO UPDATE SET last_number = card_number_seq.last_number + 1
        RETURNING last_number
    """), {"t": tenant}).scalar())


# A principal reference: `user:<id>` or `agent:<id>`. The shape is checked
# here, but EXISTENCE is not knowable here — identity lives in the editor —
# so anything written to a card goes through _resolve_people() first.
_PRINCIPAL_RE = re.compile(r"^(user|agent):([A-Za-z0-9][A-Za-z0-9._-]{0,63})$", re.IGNORECASE)


def _is_principal_ref(value) -> bool:
    return bool(_PRINCIPAL_RE.match((value or "").strip()))


def _dedup_refs(refs) -> list:
    """Strip, drop empties, de-dup case-insensitively (first spelling wins).

    Participants are a SET of people, so the same person named twice is one
    participant, not two.
    """
    seen, out = set(), []
    for r in (refs or []):
        r = (r or "").strip()
        if r and r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def _resolve_people(tenant: str, values: list) -> dict:
    """Turn what a caller passed into canonical principal references.

    Identity lives in the editor, so this is the one place a card write leaves
    the database to ask a question, and the answer is the promise the whole
    attribute rests on: a card's people are REAL accounts. Accepts the same
    inputs a person would type — a username, an email, a display name, a bare
    id, or a reference read back off another card.

    Fails CLOSED. An unresolvable name is a 400 naming the accounts that do
    exist, and an editor that cannot be reached is a 502 — not a card quietly
    stored with a person on it that nothing can resolve. That would be the
    free-text failure this field exists to remove, wearing a colon.
    """
    wanted = _dedup_refs(values)
    if not wanted:
        return {}
    try:
        resp = httpx.post(
            f"{EDITOR_URL}/api/internal/board/people",
            json={"tenant_slug": tenant, "resolve": wanted},
            headers=_internal_service_headers(), timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        log.warning("board: people resolution failed for tenant=%s: %s", tenant, exc)
        raise HTTPException(
            status_code=502,
            detail="Could not reach the identity directory to resolve the people "
                   "on this card, so the card was not written. A card's reporter "
                   "and participants are real accounts, and storing one that was "
                   "never checked would make that untrue.",
        )
    errors = data.get("errors") or {}
    if errors:
        raise HTTPException(
            status_code=400,
            detail=" ".join(str(msg) for msg in errors.values()),
        )
    resolved = data.get("resolved") or {}
    missing = [v for v in wanted if v not in resolved]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {missing[0]!r} to an account on this board.")
    return resolved


def _match_person(tenant: str, value) -> str:
    """Normalize a people FILTER value, leniently.

    A canonical reference is used as-is (the board UI's chips carry one, so
    the polling path never leaves the database). Anything else is resolved
    best-effort, and a value that resolves to nobody is passed through to
    match nothing — same call as `_match_repo`: a filter that finds nothing is
    a fine answer to a stale filter in a URL, whereas a 400 would break the
    board that merely has one.
    """
    v = (value or "").strip()
    if not v or _is_principal_ref(v):
        return v
    try:
        return _resolve_people(tenant, [v]).get(v, v)
    except HTTPException:
        return v


_ATTACHMENT_KEY_RE = re.compile(r"^cards/[a-z0-9][a-z0-9._-]{0,100}/[A-Za-z0-9._-]{1,160}$")


def _clean_attachments(vals) -> list:
    """Object keys, de-duplicated, order kept.

    Shape-checked rather than trusted: these come back from a client, and the
    only keys a card has any business naming are the ones under its own card
    prefix. A key pointing anywhere else in the org's bucket would turn a card
    edit into a way to read whatever else is in there through the download
    route.
    """
    out, seen = [], set()
    for v in vals or []:
        k = (v or "").strip().lstrip("/")
        if not k or k in seen:
            continue
        if ".." in k.split("/") or not _ATTACHMENT_KEY_RE.match(k):
            continue
        seen.add(k)
        out.append(k)
    return out[:50]


def _people_for_new_card(tenant: str, req) -> tuple:
    """Decide a new card's people. Returns (reporter, assignee, participants).

    Two rules, both from the specification rather than invented here:

    REPORTER DEFAULTS TO WHOEVER CREATED THE CARD. An explicit reporter wins,
    because the common agent case is a conversation with a known person: the
    agent holds the credential but the human raised the thing, and the card
    should say the human.

    AN ACTOR WHO IS NOT THE REPORTER GOES ON THE CARD AS A PARTICIPANT. Stated
    for agents ("set themselves as a Participant if they are not the
    Reporter") and applied to everyone, because the reasoning does not depend
    on being a machine: filing a card on somebody's behalf is being involved
    in it, and a card that records neither who raised it nor who acted has
    lost the more useful half.

    ASSIGNEE HAS NO SUCH DEFAULT. It is taken only when it is given. Whoever
    files a card is very often not whoever will do it, so defaulting it to the
    actor the way `reporter` does would assign most cards to whoever happened
    to type them — and an assignee that is merely probable is worse than a
    blank one, because blank is visibly unanswered and a guess looks decided.
    Being assigned does not put somebody on the participant list either: that
    would be this function inventing a second fact from the one it was told.

    One resolution round-trip for all of them, so a card with a reporter, an
    assignee and three participants asks the directory once rather than five
    times.
    """
    inputs = [v for v in [req.actor, req.reporter, getattr(req, "assignee", ""),
                          *(req.participants or [])] if (v or "").strip()]
    resolved = _resolve_people(tenant, inputs)
    actor_ref = resolved.get((req.actor or "").strip(), "")
    reporter = resolved.get((req.reporter or "").strip(), "") or actor_ref
    assignee = resolved.get((getattr(req, "assignee", "") or "").strip(), "")
    participants = _dedup_refs(
        [resolved.get((p or "").strip(), "") for p in (req.participants or [])])
    if actor_ref and actor_ref != reporter and actor_ref not in participants:
        participants.append(actor_ref)
    return reporter, assignee, participants


# ── Relationships between cards ───────────────────────────────────────────────
#
# Two relationship types, as the design asks for:
#   related      a loose link, no ordering implied
#   blocks       a directed dependency; the blocker has to finish first
# `blocked-by` is the same edge as `blocks` seen from the other side, so it is
# accepted as input and normalised away rather than stored — which is what makes
# "setting one reflects the other" true by construction instead of by a second
# write that could fail on its own.
LINK_KINDS = ("related", "blocks", "blocked-by")
LINK_KINDS_STORED = ("related", "blocks")

# A blocker stops blocking when it is DONE, and done is a property of the column
# the card sits in rather than of the card. `done` is the board's terminal stage
# and `dropped` is the other way work ends — a card nobody is going to do blocks
# nothing, and treating it as a live blocker would stall the column behind a
# decision that has already been made.
#
# `shipped` is `done` under the legacy vocabulary and stays here. This is the
# one place the two vocabularies are reconciled, because it is the one place
# where failing to recognise the old name has a consequence rather than a
# cosmetic difference: a finished blocker that did not resolve would stall
# everything waiting on it.
TERMINAL_STAGES = ("done", "shipped", "dropped")

# A hard bound on how far the blocker traversal will descend. Never reached in
# practice — a chain cannot be longer than the board, and the traversal's own
# cycle check stops it first. It exists for the case where that check is WRONG,
# because the failure then is not a wrong answer but a request that never
# returns, holding a database connection, with nothing in the log to say why.
BOARD_BLOCKER_MAX_DEPTH = 500


def _normalize_link(kind: str, a_id: str, b_id: str) -> tuple:
    """(stored_kind, src, dst) for a link of `kind` from card a to card b.

    `blocked-by` flips into a `blocks` edge pointing the other way, and
    `related` is stored with the lower id first, so A-B and B-A are one row
    rather than two that could be removed apart and leave the board half
    asserting a link.
    """
    k = (kind or "").strip().lower().replace("_", "-")
    if k in ("blockedby", "blocked-by", "blocked by"):
        return "blocks", str(b_id), str(a_id)
    if k == "blocks":
        return "blocks", str(a_id), str(b_id)
    if k in ("related", "related-to", "relates-to"):
        lo, hi = sorted([str(a_id), str(b_id)])
        return "related", lo, hi
    raise HTTPException(
        status_code=400,
        detail=f"unknown relationship {kind!r}; must be one of: {', '.join(LINK_KINDS)}")


def _link_rows(conn, tenant: str, card_ids: list) -> list:
    """Every link touching any of these cards, with the OTHER card's details.

    One query for the whole set. The board renders a blocked chip per card, so
    doing this per card would be a query per sticky on every poll and every
    stream event.
    """
    if not card_ids:
        return []
    return conn.execute(
        text("""
            SELECT l.id, l.kind, l.src_id, l.dst_id, l.created_at,
                   sc.slug AS src_slug, sc.title AS src_title, sc.number AS src_number,
                   sbc.stage AS src_stage, sc.archived_at AS src_archived,
                   dc.slug AS dst_slug, dc.title AS dst_title, dc.number AS dst_number,
                   dbc.stage AS dst_stage, dc.archived_at AS dst_archived
            FROM card_links l
            JOIN cards sc  ON sc.id = l.src_id
            JOIN cards dc  ON dc.id = l.dst_id
            JOIN board_columns sbc ON sbc.id = sc.column_id
            JOIN board_columns dbc ON dbc.id = dc.column_id
            WHERE l.tenant_id = :t
              AND (l.src_id = ANY(CAST(:ids AS uuid[])) OR l.dst_id = ANY(CAST(:ids AS uuid[])))
        """),
        {"t": tenant, "ids": [str(i) for i in card_ids]},
    ).mappings().all()


def _link_resolved(stage, archived_at) -> bool:
    """Whether a card counts as done for the purpose of blocking."""
    return archived_at is not None or (stage in TERMINAL_STAGES)


def _relationships_by_card(conn, tenant: str, card_ids: list) -> dict:
    """card id → {"blocks": [...], "blocked_by": [...], "related": [...]}.

    Each entry names the other card the way a human refers to one — number,
    slug, title — plus whether it is resolved, so a caller can render a chip or
    decide readiness without a second round trip.
    """
    out = {str(cid): {"blocks": [], "blocked_by": [], "related": []} for cid in card_ids}
    wanted = set(out)

    def entry(row, side: str) -> dict:
        return {
            "slug":     row[f"{side}_slug"],
            "title":    row[f"{side}_title"],
            "number":   int(row[f"{side}_number"]) if row[f"{side}_number"] is not None else None,
            "stage":    row[f"{side}_stage"],
            "closed":   row[f"{side}_archived"] is not None,
            "archived": row[f"{side}_archived"] is not None,   # deprecated alias
            "resolved": _link_resolved(row[f"{side}_stage"], row[f"{side}_archived"]),
            "created":  row["created_at"].isoformat() if row["created_at"] else None,
        }

    for row in _link_rows(conn, tenant, card_ids):
        src, dst = str(row["src_id"]), str(row["dst_id"])
        if row["kind"] == "blocks":
            if src in wanted:
                out[src]["blocks"].append(entry(row, "dst"))
            if dst in wanted:
                out[dst]["blocked_by"].append(entry(row, "src"))
        else:                                   # related: symmetric, one row
            if src in wanted:
                out[src]["related"].append(entry(row, "dst"))
            if dst in wanted:
                out[dst]["related"].append(entry(row, "src"))
    return out


def _unresolved_blockers(conn, tenant: str, card_id: str) -> list:
    """The cards standing in this one's way, most important first.

    "Most important" is the board's own order — leftmost column, then highest in
    it — because that is the order its owner already arranged the work in. An
    agent asked to clear a blocker should reach for the one nearest the top of
    the board, not the one that happens to sort first by id.

    A blocker in a terminal stage, or archived, is not a blocker: the point of
    the edge is "this has to be done first", and it is done.
    """
    return conn.execute(
        text(f"""
            SELECT c.id, c.slug, c.title, c.number, bc.stage AS stage
            FROM card_links l
            JOIN cards c ON c.id = l.src_id
            JOIN board_columns bc ON bc.id = c.column_id
            WHERE l.tenant_id = :t AND l.kind = 'blocks'
              AND l.dst_id = CAST(:id AS uuid)
              AND c.archived_at IS NULL
              AND bc.stage NOT IN ({', '.join(repr(s) for s in TERMINAL_STAGES)})
            ORDER BY bc.position, c.position
        """),
        {"t": tenant, "id": str(card_id)},
    ).mappings().all()


def _blocking_cycle(conn, tenant: str, start_id: str) -> Optional[list]:
    """The slugs of a blocking cycle reachable from this card, or None.

    Walks the whole blocker graph rather than one chain, because a cycle need
    not sit on the path the traversal happens to take: A's first blocker can be
    clean while its second loops. Depth-first with the current path tracked, so
    the answer names the loop rather than only reporting that one exists.

    Deliberately ignores whether a card is resolved. This answers "is what the
    board asserts satisfiable", and a chain that only works because one of its
    links is already shipped is still a chain nobody should have drawn.
    """
    path: list = []
    on_path: set = set()
    done: set = set()

    def walk(node_id: str, node_slug: str) -> Optional[list]:
        if node_id in on_path:                 # closed the loop
            return path[path.index(node_slug):] + [node_slug]
        if node_id in done:
            return None
        on_path.add(node_id)
        path.append(node_slug)
        rows = conn.execute(
            text("""
                SELECT c.id, c.slug FROM card_links l
                JOIN cards c ON c.id = l.src_id
                WHERE l.tenant_id = :t AND l.kind = 'blocks' AND l.dst_id = CAST(:id AS uuid)
            """),
            {"t": tenant, "id": str(node_id)},
        ).mappings().all()
        for r in rows:
            found = walk(str(r["id"]), r["slug"])
            if found:
                return found
        on_path.discard(node_id)
        path.pop()
        done.add(node_id)
        return None

    start = conn.execute(
        text("SELECT slug FROM cards WHERE tenant_id = :t AND id = CAST(:id AS uuid)"),
        {"t": tenant, "id": str(start_id)},
    ).scalar()
    if start is None:
        return None
    return walk(str(start_id), start)


def _blocked_summary(rel: dict) -> dict:
    """The one thing every surface needs from a card's relationships: is it
    blocked right now, and by what. Small enough to ride on a card in a list."""
    unresolved = [b for b in (rel or {}).get("blocked_by", []) if not b["resolved"]]
    return {
        "blocked": bool(unresolved),
        "blocked_by": [{"slug": b["slug"], "number": b["number"], "title": b["title"]}
                       for b in unresolved],
    }


# ── Board live stream (the hub) ───────────────────────────────────────────────
#
# Every board mutation funnels through this service no matter who made it: the
# board UI goes through the editor, an agent goes through the MCP server, and
# both land on the handlers below. That makes this the one place a change can be
# announced from, so an open board can learn about an agent's edit the moment it
# lands instead of on the next poll.
#
# The hub is in-process on purpose. memory-api runs as a single uvicorn worker
# and a subscriber is one browser holding one connection, so a set of asyncio
# queues is the right size for the job — no Redis, no LISTEN/NOTIFY, nothing to
# operate.
#
# Nothing is persisted and nothing is replayed. An event says only THAT the
# board changed; the client re-reads the board itself and diffs it. That is the
# property worth having: a dropped or duplicated event costs a late render or a
# wasted one, never a wrong one, and the client keeps a slow poll running as a
# safety net for exactly that reason.
BOARD_STREAM_QUEUE_MAX = 64          # per subscriber; a hint backlog, not a log
BOARD_STREAM_KEEPALIVE_SECS = 20     # under any sane proxy idle timeout


class _BoardHub:
    """Fan-out of board-changed hints to the browsers watching a tenant."""

    def __init__(self):
        self._subs: dict = {}          # tenant slug → set[asyncio.Queue]
        self._loop = None              # the serving event loop, bound at startup

    def bind_loop(self, loop) -> None:
        self._loop = loop

    # -- subscriber side (runs ON the event loop) ------------------------------
    def subscribe(self, tenant: str):
        q = asyncio.Queue(maxsize=BOARD_STREAM_QUEUE_MAX)
        self._subs.setdefault(tenant, set()).add(q)
        return q

    def unsubscribe(self, tenant: str, q) -> None:
        subs = self._subs.get(tenant)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(tenant, None)

    def subscriber_count(self, tenant: Optional[str] = None) -> int:
        if tenant is None:
            return sum(len(s) for s in self._subs.values())
        return len(self._subs.get(tenant, ()))

    # -- publisher side --------------------------------------------------------
    def _deliver(self, tenant: str, payload: dict) -> None:
        """Runs on the event loop, so iterating the subscriber set is safe."""
        for q in list(self._subs.get(tenant, ())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A client that far behind cannot use a backlog anyway — it is
                # about to re-read the whole board regardless. Dropping the
                # hint is the correct loss: the queue still holds 64 older
                # hints, each of which says the same "go and look".
                pass

    def publish(self, tenant: str, type_: str, **fields) -> None:
        """Announce a board change. Safe to call from a worker thread.

        The board handlers are sync `def`, so FastAPI runs them in a threadpool
        and this is nearly always called off-loop — hence the hop back via
        call_soon_threadsafe. Publishing is strictly best effort: a board write
        must never fail because nobody was listening or the loop had gone.
        """
        loop = self._loop
        if loop is None or not self._subs.get(tenant):
            # Racy read from a worker thread, and deliberately so: it is only a
            # fast path for the common case of nobody watching. Losing the race
            # schedules a callback that finds an empty set, which costs nothing.
            return
        payload = {"type": type_, "tenant": tenant,
                   "at": datetime.now(timezone.utc).isoformat(), **fields}
        try:
            loop.call_soon_threadsafe(self._deliver, tenant, payload)
        except RuntimeError:
            pass          # loop is shutting down; the client's poll will cover it


_board_hub = _BoardHub()


def _board_changed(tenant: str, type_: str, **fields) -> None:
    """Announce a committed board change, never raising into the caller.

    Called immediately after a commit, so a live board reflects only what the
    database actually holds. Wrapped because a notification is not worth
    failing a write that already succeeded.
    """
    try:
        _board_hub.publish(tenant, type_, **fields)
    except Exception:
        log.exception("board stream publish failed (type=%s tenant=%s)", type_, tenant)


def _clean_links(links) -> list:
    """Explicit wiki-page paths on a card: strip, drop the leading slash,
    drop empties, de-dup case-insensitively (first spelling wins)."""
    seen, out = set(), []
    for l in (links or []):
        l = (l or "").strip().lstrip("/")
        if l and l.lower() not in seen:
            seen.add(l.lower())
            out.append(l)
    return out


# ── GET /board/columns ────────────────────────────────────────────────────────
@app.get("/board/columns")
def board_columns(tenant_slug: str = "default"):
    """List the tenant's columns with their stage mapping. Seeds the default
    columns on first touch. `stages` is the architected vocabulary a column may
    map to — the current list, plus whichever legacy stages THIS board is
    actually using, so a picker built from it can never fail to offer a column
    its own current state. A board that has never seen the old vocabulary is
    never shown it."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        _ensure_seed_columns(conn, slug)
        rows = conn.execute(
            text("""
                SELECT * FROM board_columns
                WHERE tenant_id = :t AND archived_at IS NULL
                ORDER BY position
            """),
            {"t": slug},
        ).mappings().all()
        conn.commit()
    in_use = {r["stage"] for r in rows}
    legacy_in_use = [st for st in LEGACY_BOARD_STAGES if st in in_use]
    return {
        "columns": [_column_dict(r) for r in rows],
        "stages": BOARD_STAGES + legacy_in_use,
    }


# ── POST /board/cards (card_add) ──────────────────────────────────────────────
@app.post("/board/cards", status_code=201)
def card_add(req: CardAddRequest):
    """Drop a thin card on the board. Defaults to the leftmost `backlog` column;
    slug is derived from the title when omitted. Slug uniqueness is the one hard
    rule — a collision fails and names the holder rather than silently
    suffixing, because a wrong slug poisons every derived join for the card's
    life."""
    if not req.title or not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    slug = (req.tenant_slug or "default").strip() or "default"
    card_slug = _slugify_or_400(req.slug or req.title)
    # Resolved BEFORE the connection is opened, because it is an HTTP call to
    # the editor. Inside the transaction it would be held across the tenant's
    # advisory lock (taken by _ensure_seed_columns below and released only at
    # commit), so one slow directory lookup would stall every board endpoint
    # for that tenant — a card create must not be able to lock the board.
    reporter, assignee, participants = _people_for_new_card(slug, req)
    due = _clean_due(req.due)

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        _ensure_seed_columns(conn, slug)

        col = None
        if req.column:
            col = _resolve_column(conn, slug, req.column)
            if col is None:
                raise HTTPException(status_code=404, detail=f"No column matches {req.column!r}")
        if col is None:
            # The backlog column, under either vocabulary: `backlog` on a board
            # seeded with the current vocabulary, `spark` on one seeded before it. Ordered
            # so the current name wins on a board that somehow has both.
            col = conn.execute(
                text("""
                    SELECT * FROM board_columns
                    WHERE tenant_id = :t AND archived_at IS NULL
                      AND stage IN ('backlog', 'spark')
                    ORDER BY (stage <> 'backlog'), position LIMIT 1
                """),
                {"t": slug},
            ).mappings().fetchone()
        if col is None:  # no backlog column (user remapped everything) — fall to leftmost
            col = conn.execute(
                text("""
                    SELECT * FROM board_columns
                    WHERE tenant_id = :t AND archived_at IS NULL
                    ORDER BY position LIMIT 1
                """),
                {"t": slug},
            ).mappings().fetchone()
        if col is None:
            raise HTTPException(status_code=500, detail="No columns available for this tenant")

        holder = conn.execute(
            text("SELECT slug, title FROM cards WHERE tenant_id = :t AND lower(slug) = lower(:s)"),
            {"t": slug, "s": card_slug},
        ).mappings().fetchone()
        if holder:
            raise HTTPException(
                status_code=409,
                detail=f"slug {card_slug!r} is already held by card {holder['slug']!r} "
                       f"({holder['title']!r}). Pick another slug.",
            )

        pos = float(conn.execute(
            text("SELECT COALESCE(MAX(position), 0.0) FROM cards WHERE column_id = :c"),
            {"c": col["id"]},
        ).scalar()) + 1.0

        card_id = str(uuid.uuid4())
        source_norm = (req.source or "unknown").strip().lower() or "unknown"
        body_text = req.body or ""
        # One field, two spellings on the wire. `tags` is the pre-merge name
        # and is folded in rather than refused, because the published skill
        # ships it: a session running an older copy still sends the note-to-self
        # marker as a tag, and rejecting it would break sessions that never
        # asked to be upgraded. Hygiene is the same either way — strip, drop
        # empties, de-dup case-insensitively with the first spelling winning.
        labels = _clean_tags(list(req.labels) + list(req.tags))
        repo = _clean_repo(req.repo)
        wiki_links = _clean_links(req.wiki_links)
        number = _next_card_number(conn, slug)
        conn.execute(
            text("""
                INSERT INTO cards (id, tenant_id, column_id, slug, title, body, labels,
                                   repo, reporter, assignee, due, participants, wiki_links,
                                   position, source, number)
                VALUES (:id, :t, :c, :slug, :title, :body, :labels, :repo,
                        :reporter, :assignee, :due, :participants, :wl, :pos, :source, :number)
            """),
            {"id": card_id, "t": slug, "c": col["id"], "slug": card_slug,
             "title": req.title.strip(), "body": body_text,
             "labels": labels, "repo": repo, "reporter": reporter,
             "assignee": assignee, "due": due,
             "participants": participants, "wl": wiki_links, "pos": pos,
             "source": source_norm, "number": number},
        )
        conn.commit()

    log.info("card_add slug=%s tenant=%s column=%s", card_slug, slug, col["name"])
    _board_changed(slug, "card.added", slug=card_slug, column_id=str(col["id"]))
    return {
        "card": {
            "id": card_id, "number": number, "slug": card_slug, "title": req.title.strip(),
            "body": body_text, "aliases": [], "labels": labels,
            "repo": repo, "reporter": reporter, "assignee": assignee,
            "due": due.isoformat() if due else None,
            "participants": participants,
            "wiki_links": wiki_links,
            "column": col["name"],
            "column_id": str(col["id"]), "stage": col["stage"], "position": pos,
            "source": source_norm, "dispatch_ref": None, "archived": False,
        },
        "warnings": _nudge_warning(body_text, req.nudge_chars),
    }


# ── GET /board/stream (live board updates) ────────────────────────────────────
def _sse_pack(payload: dict) -> str:
    """One Server-Sent Event. `event:` carries the change type so a client can
    tell a card edit from a column rename without parsing the body, and `data:`
    carries the whole payload for the ones that want it."""
    return f"event: {payload.get('type', 'board.changed')}\ndata: {json.dumps(payload)}\n\n"


@app.get("/board/stream")
async def board_stream(tenant_slug: str = "default"):
    """Server-Sent Events: one line whenever this tenant's board changes.

    Deliberately thin. The event names what changed, not what it changed TO —
    the client re-reads the board through the ordinary endpoints, which keeps
    every filter, permission check and shape in one place instead of forking a
    second representation of a card that could drift from the first.

    The stream carries a keepalive comment every few seconds. That is not
    decoration: an idle proxy hop closes a silent connection, and a client that
    cannot tell "quiet" from "dead" would sit on a socket that stopped
    delivering. The comment keeps the hop honest and gives EventSource an error
    to reconnect from when it really has died.
    """
    slug = (tenant_slug or "default").strip() or "default"
    queue = _board_hub.subscribe(slug)
    log.info("board_stream open tenant=%s subscribers=%d", slug, _board_hub.subscriber_count(slug))

    async def events():
        try:
            # Announce the stream is live before anything happens on it, so the
            # client can stop polling on evidence rather than on optimism.
            yield _sse_pack({"type": "hello", "tenant": slug,
                             "at": datetime.now(timezone.utc).isoformat()})
            while True:
                try:
                    payload = await asyncio.wait_for(
                        queue.get(), timeout=BOARD_STREAM_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse_pack(payload)
        except asyncio.CancelledError:
            raise                      # client went away; unwinds through finally
        finally:
            _board_hub.unsubscribe(slug, queue)
            log.info("board_stream close tenant=%s subscribers=%d",
                     slug, _board_hub.subscriber_count(slug))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            # no-transform matters as much as no-cache: a proxy that "helpfully"
            # compresses or buffers this turns a live stream into a batch.
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx honours this per-response, so the stream stays unbuffered
            # even if a future location block forgets `proxy_buffering off`.
            "X-Accel-Buffering": "no",
        },
    )


# ── GET /board/labels ─────────────────────────────────────────────────────────
@app.get("/board/labels")
def board_labels(tenant_slug: str = "default"):
    """Every label currently ON at least one card, for the label field's
    autocomplete.

    IN USE is the whole definition, and it is what makes this a vocabulary
    rather than a registry. There is no create-a-label step and no list to
    prune: a label exists because a card carries it, and the moment the last
    card carrying it is relabelled it stops being offered. Nobody has to tidy
    up after a typo — dropping it from the one card that had it is the tidying.

    ARCHIVED CARDS DO NOT COUNT. Archive is how this board retires things, so
    a label surviving only on archived cards is vocabulary the user has already
    put down; offering it back would quietly resurrect it. The label is still
    ON those cards and still filterable — it is only absent from suggestions.

    Sorted case-insensitively, deliberately: the list is read by a human
    scanning for a word, and ASCII order would file every capitalised label
    ahead of every lowercase one.

    Also returns the RESERVED labels, so a client can mark them and can tell
    the difference between a person taking one off on purpose and a whole-list
    replace that happens not to mention it. Served from here rather than
    duplicated client-side: a UI holding its own copy of this list is a UI that
    is wrong the day the set changes, and it would be wrong silently.
    """
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        rows = conn.execute(text("""
            SELECT DISTINCT ON (lower(btrim(l))) btrim(l) AS label
            FROM cards c, unnest(c.labels) AS l
            WHERE c.tenant_id = :t AND c.archived_at IS NULL AND btrim(l) <> ''
            ORDER BY lower(btrim(l))
        """), {"t": slug}).mappings().all()
        conn.commit()
    return {"labels": [r["label"] for r in rows],
            "reserved": sorted(RESERVED_CARD_LABELS)}


# ── GET /board/cards (card_list) ──────────────────────────────────────────────
@app.get("/board/cards")
def card_list(
    tenant_slug: str = "default",
    column: Optional[str] = None,
    stage: Optional[str] = None,
    tag: Optional[str] = None,
    label: Optional[str] = None,
    repo: Optional[str] = None,
    reporter: Optional[str] = None,
    assignee: Optional[str] = None,
    participant: Optional[str] = None,
    person: Optional[str] = None,
    search: Optional[str] = None,
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    overdue: bool = False,
    closed: bool = False,
    archived: bool = False,
):
    """List cards, filterable by column (id/name), stage, label, repo,
    reporter, assignee, participant, person (any of the three), due date, or
    free-text search (title/body/slug/labels/repo). Excludes closed cards
    unless closed=true.

    `due_before` / `due_after` are inclusive (`YYYY-MM-DD`), and either one
    leaves out cards with no due date. `overdue` is a due date before today
    (UTC) on a card that is not in a done stage and not closed.

    `archived` is the pre-rename spelling of `closed` and means the same
    thing; either one includes closed cards, so a caller written against the
    old name keeps working across a rolling deploy.

    `tag` is the pre-merge spelling of `label` and filters on the same field;
    it stays because the published skill documents it."""
    slug = (tenant_slug or "default").strip() or "default"
    warnings: list = []
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        _ensure_seed_columns(conn, slug)

        q = ("SELECT c.*, bc.name AS column_name, bc.stage AS stage "
             "FROM cards c JOIN board_columns bc ON c.column_id = bc.id "
             "WHERE c.tenant_id = :t")
        params: dict = {"t": slug}
        if not (closed or archived):
            q += " AND c.archived_at IS NULL"
        if column:
            col = _resolve_column(conn, slug, column)
            if col is None:
                conn.commit()
                return {"cards": [], "warnings": [f"no column matches {column!r}"]}
            q += " AND c.column_id = :cid"
            params["cid"] = col["id"]
        if stage:
            q += " AND bc.stage = :stage"
            params["stage"] = stage.strip().lower()
        if tag:
            # `tag` is the pre-merge name for what is now a label, kept as a
            # filter because `card_list(tag="note-to-self")` is documented in
            # the published skill and is running in sessions today. It answers
            # off `labels`, so the same call keeps finding the same cards.
            q += " AND EXISTS (SELECT 1 FROM unnest(c.labels) l WHERE lower(l) = lower(:tag))"
            params["tag"] = tag.strip()
        if label:
            q += " AND EXISTS (SELECT 1 FROM unnest(c.labels) l WHERE lower(l) = lower(:label))"
            params["label"] = label.strip()
        if repo:
            # Compare canonically, so filtering by a pasted browser URL finds
            # cards stored as owner/name.
            #
            # A value with no owner is also accepted, because it is what people
            # type: the board's search bar takes `repo: elefant`, and nobody
            # writing that means "a repo literally named elefant with no owner"
            # — a card's repo always has one. Matched on the name SEGMENT rather
            # than with a LIKE, so an underscore in a repo name stays an
            # underscore instead of becoming a single-character wildcard.
            #
            # Strictly additive: a value that canonicalizes to owner/name takes
            # the exact branch exactly as before, and a bare name previously
            # matched nothing at all, since _clean_repo never stores one.
            canon = _match_repo(repo)
            if "/" in canon:
                q += " AND lower(c.repo) = lower(:repo)"
            else:
                q += (" AND (lower(c.repo) = lower(:repo)"
                      " OR lower(split_part(c.repo, '/', 2)) = lower(:repo))")
            params["repo"] = canon
        # "What did I raise", "what am I on", and the one a person actually
        # asks for — "what am I involved in at all", which is either.
        if reporter:
            q += " AND lower(c.reporter) = lower(:reporter)"
            params["reporter"] = _match_person(slug, reporter)
        if assignee:
            q += " AND lower(c.assignee) = lower(:assignee)"
            params["assignee"] = _match_person(slug, assignee)
        if participant:
            q += (" AND EXISTS (SELECT 1 FROM unnest(c.participants) p "
                  "WHERE lower(p) = lower(:participant))")
            params["participant"] = _match_person(slug, participant)
        if person:
            # `person` is the one somebody actually asks for — "what is Jon
            # involved in at all" — so it grows to include the assignee.
            # Leaving it out would mean a card assigned to Jon and nothing else
            # did not count as Jon being involved in it, which is the opposite
            # of what the field means.
            q += (" AND (lower(c.reporter) = lower(:person) "
                  "OR lower(c.assignee) = lower(:person) "
                  "OR EXISTS (SELECT 1 FROM unnest(c.participants) p "
                  "WHERE lower(p) = lower(:person)))")
            params["person"] = _match_person(slug, person)
        # Inclusive both ends: "due before Friday" asked on a card due Friday
        # wants it. An unparseable bound matches nothing and says so, rather
        # than 400ing a board whose URL carries a stale filter.
        for name, value, op in (("due_before", due_before, "<="),
                                ("due_after", due_after, ">=")):
            if value is None or not str(value).strip():
                continue
            d = _match_due(value)
            if d is None:
                conn.commit()
                return {"cards": [], "warnings": [f"{name}: {value!r} is not a YYYY-MM-DD date"]}
            q += f" AND c.due IS NOT NULL AND c.due {op} :{name}"
            params[name] = d
        if overdue:
            # Done work is not late. A card in a terminal stage, or closed, is
            # finished whatever its date said.
            q += (" AND c.due IS NOT NULL AND c.due < :today AND c.archived_at IS NULL"
                  f" AND bc.stage NOT IN ({', '.join(repr(s) for s in TERMINAL_STAGES)})")
            params["today"] = _today_utc()
        # A card's NUMBER is how a person refers to it out loud ("look at #57"),
        # so an all-digits search has to find that card — and find it first.
        # Free text alone cannot: the number is not in the ILIKE set, and even
        # if it were, "57" is a substring of #157, #570 and any title mentioning
        # 57. So an exact number match is matched explicitly and ranked above
        # every substring hit, while the text hits still follow behind rather
        # than being thrown away — typing "57" should not hide a card actually
        # titled "57 ways to fail".
        order = "bc.position, c.position"
        if search:
            digits = search.strip().lstrip("#").strip()
            # `number` is int4. psycopg happens to compare a wider value without
            # complaint, but that is the driver's choice rather than ours, so a
            # digit run too long to be a card number is simply not treated as
            # one — it stays an ordinary text search.
            num = int(digits) if digits.isdigit() and len(digits) <= 9 else None
            clause = ("c.title ILIKE :q OR c.body ILIKE :q OR c.slug ILIKE :q "
                      "OR c.repo ILIKE :q "
                      "OR EXISTS (SELECT 1 FROM unnest(c.labels) l WHERE l ILIKE :q)")
            params["q"] = f"%{search.strip()}%"
            if num is not None:
                clause += " OR c.number = :num"
                params["num"] = num
                # NOT DISTINCT FROM rather than `=`: a card with no number at
                # all yields NULL from `=`, and NULL sorts FIRST under DESC in
                # Postgres — which would rank every un-numbered card above the
                # exact match this ordering exists to surface.
                order = "(c.number IS NOT DISTINCT FROM :num) DESC, " + order
            q += f" AND ({clause})"
        q += f" ORDER BY {order}"
        rows = conn.execute(text(q), params).mappings().all()
        # Blocked-ness rides on the list because that is where it is READ: the
        # board draws a chip per card, and an agent scanning a column needs to
        # know which stickies are not actually startable. Resolved in one query
        # for the whole page rather than one per card.
        rel = _relationships_by_card(conn, slug, [r["id"] for r in rows])
        counts = _comment_counts(conn, slug, [r["id"] for r in rows])
        conn.commit()
    return {
        "cards": [
            {**_card_dict(r, r["column_name"], r["stage"]),
             **_blocked_summary(rel.get(str(r["id"]))),
             "comment_count": counts.get(str(r["id"]), 0)}
            for r in rows
        ],
        "warnings": warnings,
    }


# ── GET /board/next-card (card_next) ──────────────────────────────────────────
@app.get("/board/next-card")
def card_next(
    tenant_slug: str = "default",
    column: Optional[str] = None,
    stage: Optional[str] = None,
    repo: Optional[str] = None,
    label: Optional[str] = None,
):
    """The next card that can actually be STARTED, blockers resolved.

    The reason this lives on the server rather than in each agent: a dispatch
    routine reading a column top to bottom will pick up a card whose blocker is
    three rows further down, do the work in the wrong order, and have no way of
    knowing. Every agent reimplementing the traversal means every agent
    reimplementing the cycle guard too, and the one that forgets hangs.

    So: walk the column in the owner's own order, and for each card descend into
    its highest-priority unresolved blocker, transitively, until something has no
    blockers left. That card is the one to work — even though it may sit far
    below, or in another column entirely, which is the whole point.
    """
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        _ensure_seed_columns(conn, slug)

        q = ("SELECT c.*, bc.name AS column_name, bc.stage AS stage "
             "FROM cards c JOIN board_columns bc ON c.column_id = bc.id "
             "WHERE c.tenant_id = :t AND c.archived_at IS NULL")
        params: dict = {"t": slug}
        if column:
            col = _resolve_column(conn, slug, column)
            if col is None:
                conn.commit()
                return {"card": None, "warnings": [f"no column matches {column!r}"]}
            q += " AND c.column_id = :cid"
            params["cid"] = col["id"]
        if stage:
            q += " AND bc.stage = :stage"
            params["stage"] = stage.strip().lower()
        if repo:
            q += " AND lower(c.repo) = lower(:repo)"
            params["repo"] = _match_repo(repo)
        if label:
            q += " AND EXISTS (SELECT 1 FROM unnest(c.labels) l WHERE lower(l) = lower(:label))"
            params["label"] = label.strip()
        q += " ORDER BY bc.position, c.position"
        candidates = conn.execute(text(q), params).mappings().all()

        warnings: list = []
        for candidate in candidates:
            chain = [candidate["slug"]]
            seen = {str(candidate["id"])}
            node = candidate
            cycle = None
            # A chain cannot be longer than the board, so this can only ever be
            # hit if the `seen` check above is wrong. It is here because of what
            # that bug would DO: not a wrong card, but a routine that never
            # returns, holding a connection, with nothing in the log to say why.
            # A cheap bound turns the worst failure this code has into an
            # ordinary reported one.
            for _ in range(BOARD_BLOCKER_MAX_DEPTH):
                blockers = _unresolved_blockers(conn, slug, node["id"])
                if not blockers:
                    break
                nxt = blockers[0]
                if str(nxt["id"]) in seen:
                    # Unsatisfiable as declared. Flagged and stepped over rather
                    # than followed, which is the difference between a routine
                    # that reports a problem and one that never returns.
                    cycle = chain + [nxt["slug"]]
                    break
                seen.add(str(nxt["id"]))
                chain.append(nxt["slug"])
                node = conn.execute(
                    text("SELECT c.*, bc.name AS column_name, bc.stage AS stage "
                         "FROM cards c JOIN board_columns bc ON c.column_id = bc.id "
                         "WHERE c.tenant_id = :t AND c.id = :id"),
                    {"t": slug, "id": nxt["id"]},
                ).mappings().fetchone()
            else:
                # Ran the bound out without finding either a free card or a
                # cycle. Unreachable unless the cycle check is broken, and the
                # one thing not to do here is hand back `node` as if it were
                # startable — a card reported workable on the strength of a
                # traversal that gave up is worse than no answer.
                log.error("card_next: blocker chain from %r exceeded %d — "
                          "skipped; the board's links may contain a cycle the "
                          "traversal did not detect",
                          candidate["slug"], BOARD_BLOCKER_MAX_DEPTH)
                warnings.append(
                    f"{candidate['slug']!r} has a blocker chain deeper than "
                    f"{BOARD_BLOCKER_MAX_DEPTH}; skipped")
                continue

            if cycle:
                warnings.append(
                    f"{candidate['slug']!r} sits behind a blocking cycle (" +
                    " → ".join(cycle) + "); skipped, and nothing in that chain "
                    "can start until one of those links goes")
                continue

            rel = _relationships_by_card(conn, slug, [node["id"]]).get(str(node["id"]), {})
            conn.commit()
            return {
                "card": {**_card_dict(node, node["column_name"], node["stage"]),
                         **_blocked_summary(rel)},
                "relationships": rel,
                # Both halves of the answer. `requested` is the card the column
                # order pointed at; `chain` is how we got from there to the card
                # handed back, so a routine can say WHY it is working this one.
                "requested": candidate["slug"],
                "chain": chain,
                "warnings": warnings,
            }

        conn.commit()
    return {"card": None, "requested": None, "chain": [], "warnings": warnings or
            ["no startable card matches those filters"]}


# ── GET /board/cards/{slug} (card_get) ────────────────────────────────────────
@app.get("/board/cards/{card_slug}")
def card_get(card_slug: str, tenant_slug: str = "default"):
    """One card by slug or alias. Derived pointers (memories / wiki / repo) are
    resolved in a later slice; this returns the card and its column/stage."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            conn.commit()
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        joined = _card_joined(conn, slug, card["id"])
        rel = _relationships_by_card(conn, slug, [card["id"]]).get(str(card["id"]), {})
        comments = _comments_for_card(conn, slug, card["id"])
        conn.commit()
    return {
        "card": {
            **_card_dict(joined, joined["column_name"], joined["stage"]),
            **_blocked_summary(rel),
            "comment_count": sum(1 for c in comments if not c["deleted"]),
        },
        "relationships": rel,
        "comments": comments,
    }


# ── GET /board/cards/{slug}/status (card_status) ──────────────────────────────
@app.get("/board/cards/{card_slug}/status")
def card_status(card_slug: str, tenant_slug: str = "default"):
    """Lightweight live status for a card — powers the wiki "smart link" chips:
    the card's column, stage, colour, and closed flag. No wiki derivation, so
    it's cheap enough to call once per card reference on a page."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            conn.commit()
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        col = conn.execute(
            text("SELECT name, stage, color FROM board_columns WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": card["column_id"]},
        ).mappings().fetchone()
        conn.commit()
    return {
        "slug":     card["slug"],
        "title":    card["title"],
        "column":   col["name"] if col else None,
        "stage":    col["stage"] if col else None,
        "color":    col["color"] if col else None,
        "closed":   card["archived_at"] is not None,
        "archived": card["archived_at"] is not None,   # deprecated alias of `closed`
        # Here as well as on the full card: this endpoint powers the wiki's
        # smart-link chips, and a chip is one of the places somebody decides to
        # go and start working a card.
        "eval_lock": _eval_lock_dict(card),
    }


# ── GET /board/wiki-page-cards (reverse of a card's wiki_links) ───────────────
# Deliberately NOT under /board/cards/…: that prefix is owned by the
# {card_slug} route, and a literal sibling there is one reordering away from
# being shadowed by it.
@app.get("/board/wiki-page-cards")
def cards_referencing_wiki_page(path: str, tenant_slug: str = "default"):
    """Cards whose explicit `wiki_links` name this page — the reverse of the
    card→wiki link, so a wiki page can show which cards reference it.

    Matched case-insensitively on the stored spelling (links are stored as
    `_clean_links` left them: no leading slash), because the same page reached
    from different callers can differ only in case. Archived cards are included
    and flagged; the reference is still real, and the chip says so.
    """
    slug = (tenant_slug or "default").strip() or "default"
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return {"cards": []}
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        rows = conn.execute(
            text("""
                SELECT c.slug, c.title, c.archived_at,
                       bc.name AS column_name, bc.stage AS stage, bc.color AS color
                FROM cards c JOIN board_columns bc ON c.column_id = bc.id
                WHERE c.tenant_id = :t
                  AND EXISTS (SELECT 1 FROM unnest(c.wiki_links) l
                              WHERE lower(l) = lower(:p))
                ORDER BY bc.position, c.position
            """),
            {"t": slug, "p": rel},
        ).mappings().all()
        conn.commit()
    return {"cards": [{
        "slug":     r["slug"],
        "title":    r["title"],
        "column":   r["column_name"],
        "stage":    r["stage"],
        "color":    r["color"],
        "closed":   r["archived_at"] is not None,
        "archived": r["archived_at"] is not None,   # deprecated alias of `closed`
    } for r in rows]}


# ── PATCH /board/cards/{slug}/move (card_move) ────────────────────────────────
@app.patch("/board/cards/{card_slug}/move")
def card_move(card_slug: str, req: CardMoveRequest):
    """Move a card to another column (or reorder). Writes a `card.moved` event
    carrying both the column and the resolved stage — the seam v2 dispatch
    subscribes to."""
    slug = (req.tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None or card["archived_at"] is not None:
            raise HTTPException(status_code=404, detail=f"No active card matches {card_slug!r}")
        target = _resolve_column(conn, slug, req.column)
        if target is None:
            raise HTTPException(status_code=404, detail=f"No column matches {req.column!r}")

        from_col = conn.execute(
            text("SELECT name, stage FROM board_columns WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": card["column_id"]},
        ).mappings().fetchone()

        if req.position is not None:
            pos = float(req.position)
        else:
            pos = float(conn.execute(
                text("SELECT COALESCE(MAX(position), 0.0) FROM cards WHERE column_id = :c"),
                {"c": target["id"]},
            ).scalar()) + 1.0

        source_norm = (req.source or "unknown").strip().lower() or "unknown"
        conn.execute(
            text("""
                UPDATE cards SET column_id = :c, position = :pos, updated_at = now()
                WHERE tenant_id = :t AND id = :id
            """),
            {"c": target["id"], "pos": pos, "t": slug, "id": card["id"]},
        )
        # v1 owes v2 exactly this: a card.moved row on the existing events table.
        # No new infra; consumers read the events table.
        conn.execute(
            text("""
                INSERT INTO events (type, content, source, tags, metadata, tenant_id)
                VALUES ('card.moved', :content, :source, :tags, CAST(:meta AS JSONB), :t)
            """),
            {
                "content": f"card {card['slug']} moved "
                           f"{(from_col['name'] if from_col else '?')} → {target['name']}",
                "source":  source_norm,
                "tags":    [card["slug"]],
                "meta":    json.dumps({
                    "slug":        card["slug"],
                    "from_column": from_col["name"] if from_col else None,
                    "to_column":   target["name"],
                    "from_stage":  from_col["stage"] if from_col else None,
                    "stage":       target["stage"],
                    "source":      source_norm,
                }),
                "t": slug,
            },
        )
        joined = _card_joined(conn, slug, card["id"])
        conn.commit()

    log.info("card_move slug=%s tenant=%s -> %s (%s)",
             card["slug"], slug, target["name"], target["stage"])
    # Reordering inside one column is a move too, and one an open board has to
    # show — otherwise a drag on one device leaves every other view with the
    # old order and no way to notice.
    _board_changed(slug, "card.moved", slug=card["slug"],
                   column_id=str(target["id"]), stage=target["stage"])
    return {"card": _card_dict(joined, joined["column_name"], joined["stage"]), "warnings": []}


# ── PATCH /board/cards/{slug} (card_update) ───────────────────────────────────
@app.patch("/board/cards/{card_slug}")
def card_update(card_slug: str, req: CardUpdateRequest):
    """Edit a sticky. Renaming (new_slug) pushes the old slug onto aliases[]
    automatically so history keeps resolving. Slug uniqueness is enforced on
    rename the same way as card_add."""
    slug = (req.tenant_slug or "default").strip() or "default"
    # Resolved before the connection is opened, for the same reason as
    # card_add: it is an HTTP call to the editor, and holding a transaction
    # open across one makes a slow directory the board's problem.
    people = {}
    if req.reporter is not None or req.assignee is not None or req.participants is not None:
        people = _resolve_people(
            slug, [req.reporter or "", req.assignee or ""] + list(req.participants or []))

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")

        sets: list = []
        params: dict = {"t": slug, "id": card["id"]}

        # aliases: explicit replacement if provided, else keep existing.
        aliases = list(req.aliases) if req.aliases is not None else list(card["aliases"] or [])

        if req.new_slug:
            new_slug = _slugify_or_400(req.new_slug)
            if new_slug.lower() != card["slug"].lower():
                holder = conn.execute(
                    text("SELECT slug, title FROM cards "
                         "WHERE tenant_id = :t AND lower(slug) = lower(:s) AND id <> :id"),
                    {"t": slug, "s": new_slug, "id": card["id"]},
                ).mappings().fetchone()
                if holder:
                    raise HTTPException(
                        status_code=409,
                        detail=f"slug {new_slug!r} is already held by card {holder['slug']!r} "
                               f"({holder['title']!r}). Pick another slug.",
                    )
                # Auto-retain the old slug so old references still resolve.
                if card["slug"].lower() not in {a.lower() for a in aliases}:
                    aliases.append(card["slug"])
                sets.append("slug = :slug")
                params["slug"] = new_slug

        if req.title is not None:
            sets.append("title = :title")
            params["title"] = req.title.strip()
        if req.body is not None:
            sets.append("body = :body")
            params["body"] = req.body
        # aliases always re-written if we touched them or the caller set them.
        if req.aliases is not None or (req.new_slug and "slug = :slug" in sets):
            sets.append("aliases = :aliases")
            params["aliases"] = aliases
        # One field, two spellings — `tags` folded in for the same reason as
        # card_add. Passing both merges both rather than one winning.
        if req.labels is not None or req.tags is not None:
            incoming = _clean_tags(list(req.labels or []) + list(req.tags or []))
            # A replace must not silently drop a marker the board set on the
            # card. `allow_reserved` is how a caller says it means to — the
            # single-label path sets it when asked to remove one BY NAME, which
            # is a deliberate act rather than the side effect of a full rewrite.
            if not req.allow_reserved:
                incoming = _keep_reserved(list(card["labels"] or []), incoming)
            sets.append("labels = :labels")
            params["labels"] = incoming
        if req.repo is not None:
            sets.append("repo = :repo")
            params["repo"] = _clean_repo(req.repo)
        # Resolved above, in one call for both fields: an edit setting both
        # asks the directory once, and a bad participant fails the whole edit
        # rather than landing a reporter and then refusing.
        if req.reporter is not None:
            sets.append("reporter = :reporter")
            params["reporter"] = people.get((req.reporter or "").strip(), "")
        # Same None-vs-"" distinction: None leaves the assignee alone, "" is a
        # deliberate unassign. Without it a card could never be handed back.
        if req.assignee is not None:
            sets.append("assignee = :assignee")
            params["assignee"] = people.get((req.assignee or "").strip(), "")
        if req.due is not None:
            sets.append("due = :due")
            params["due"] = _clean_due(req.due)
        if req.participants is not None:
            sets.append("participants = :participants")
            params["participants"] = _dedup_refs(
                [people.get((p or "").strip(), "") for p in req.participants])
        if req.wiki_links is not None:
            sets.append("wiki_links = :wl")
            params["wl"] = _clean_links(req.wiki_links)
        if req.attachments is not None:
            sets.append("attachments = :att")
            params["att"] = _clean_attachments(req.attachments)

        if not sets:
            joined = _card_joined(conn, slug, card["id"])
            conn.commit()
            return {"card": _card_dict(joined, joined["column_name"], joined["stage"]),
                    "warnings": ["no fields to update"]}

        sets.append("updated_at = now()")
        conn.execute(
            text(f"UPDATE cards SET {', '.join(sets)} WHERE tenant_id = :t AND id = :id"),
            params,
        )
        joined = _card_joined(conn, slug, card["id"])
        conn.commit()

    body_for_nudge = req.body if req.body is not None else joined["body"]
    log.info("card_update slug=%s tenant=%s fields=%s",
             joined["slug"], slug, [s.split(" =")[0] for s in sets])
    _board_changed(slug, "card.updated", slug=joined["slug"])
    return {
        "card": _card_dict(joined, joined["column_name"], joined["stage"]),
        "warnings": _nudge_warning(body_for_nudge, req.nudge_chars),
    }


# ── Comments ──────────────────────────────────────────────────────────────────
COMMENT_MAX_CHARS = 10000


def _comment_dict(row) -> dict:
    deleted = row["deleted_at"] is not None
    return {
        "id": str(row["id"]),
        "author": row["author"] or "",
        # A deleted comment keeps its place and loses its text. The alternative
        # is a hole in a conversation somebody is reading, which reads as a
        # message that was never written rather than one that was withdrawn.
        "body": "" if deleted else (row["body"] or ""),
        "deleted": deleted,
        # "Edited" is shown rather than implied. Editing that rewrites history
        # invisibly is worse than not being able to edit at all.
        "edited": (not deleted) and row["updated_at"] > row["created_at"],
        "source": row["source"] or "unknown",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _comments_for_card(conn, tenant: str, card_id) -> list:
    rows = conn.execute(
        text("SELECT * FROM card_comments WHERE tenant_id = :t "
             "AND card_id = CAST(:c AS uuid) ORDER BY created_at, id"),
        {"t": tenant, "c": str(card_id)},
    ).mappings().all()
    return [_comment_dict(r) for r in rows]


def _comment_counts(conn, tenant: str, card_ids: list) -> dict:
    """How many live comments each card has, in ONE query for the page.

    The board draws a count per sticky, so a query per card would be a query
    per card on every poll. Deleted ones do not count — the sticky should say
    how much conversation there is, not how much there has been.
    """
    if not card_ids:
        return {}
    rows = conn.execute(
        text("SELECT card_id, COUNT(*) AS n FROM card_comments "
             "WHERE tenant_id = :t AND deleted_at IS NULL "
             "AND card_id = ANY(CAST(:ids AS uuid[])) GROUP BY card_id"),
        {"t": tenant, "ids": [str(i) for i in card_ids]},
    ).mappings().all()
    return {str(r["card_id"]): int(r["n"]) for r in rows}


class CommentAddRequest(BaseModel):
    tenant_slug: str = "default"
    body: str = ""
    author: str = ""
    source: Optional[str] = None


class CommentUpdateRequest(BaseModel):
    tenant_slug: str = "default"
    body: str = ""
    actor: str = ""
    source: Optional[str] = None


class CommentDeleteRequest(BaseModel):
    tenant_slug: str = "default"
    actor: str = ""
    source: Optional[str] = None


def _clean_comment_body(body: str) -> str:
    text_ = (body or "").strip()
    if not text_:
        raise HTTPException(status_code=400, detail="a comment needs something in it")
    if len(text_) > COMMENT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(f"that comment is {len(text_)} chars; the limit is "
                    f"{COMMENT_MAX_CHARS}. Long enough to be a wiki page."))
    return text_


@app.get("/board/cards/{card_slug}/comments")
def card_comment_list(card_slug: str, tenant_slug: str = "default"):
    """The thread on a card, oldest first — it is a conversation, not a feed."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        out = _comments_for_card(conn, slug, card["id"])
        conn.commit()
    return {"comments": out}


@app.post("/board/cards/{card_slug}/comments", status_code=201)
def card_comment_add(card_slug: str, req: CommentAddRequest):
    """Post a comment on a card."""
    slug = (req.tenant_slug or "default").strip() or "default"
    body = _clean_comment_body(req.body)
    # Resolved before the connection opens, like every other people write: it
    # is an HTTP call to the editor, and holding a transaction across one makes
    # a slow directory the board's problem.
    author = ""
    if (req.author or "").strip():
        author = _resolve_people(slug, [req.author]).get(req.author.strip(), "")

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        cid = str(uuid.uuid4())
        conn.execute(
            text("INSERT INTO card_comments (id, tenant_id, card_id, author, body, source) "
                 "VALUES (CAST(:id AS uuid), :t, CAST(:c AS uuid), :a, :b, :s)"),
            {"id": cid, "t": slug, "c": str(card["id"]), "a": author, "b": body,
             "s": (req.source or "unknown").strip().lower() or "unknown"},
        )
        row = conn.execute(
            text("SELECT * FROM card_comments WHERE tenant_id = :t AND id = CAST(:id AS uuid)"),
            {"t": slug, "id": cid},
        ).mappings().first()
        conn.commit()

    log.info("card_comment_add card=%s tenant=%s", card_slug, slug)
    _board_changed(slug, "card.commented", slug=card["slug"], comment_id=cid)
    return {"comment": _comment_dict(row)}


@app.patch("/board/cards/{card_slug}/comments/{comment_id}")
def card_comment_update(card_slug: str, comment_id: str, req: CommentUpdateRequest):
    """Edit a comment. The author only, and the edit is visible as one."""
    slug = (req.tenant_slug or "default").strip() or "default"
    body = _clean_comment_body(req.body)
    actor = ""
    if (req.actor or "").strip():
        actor = _resolve_people(slug, [req.actor]).get(req.actor.strip(), "")

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        row = conn.execute(
            text("SELECT * FROM card_comments WHERE tenant_id = :t AND id = CAST(:id AS uuid) "
                 "AND card_id = CAST(:c AS uuid)"),
            {"t": slug, "id": comment_id, "c": str(card["id"])},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="No such comment on that card")
        if row["deleted_at"] is not None:
            raise HTTPException(status_code=400, detail="that comment was deleted")
        # AUTHOR ONLY. Putting words in somebody else's mouth is the one thing
        # a comment thread must not allow, and an unauthenticated caller is not
        # the author of anything.
        if not actor or actor != (row["author"] or ""):
            raise HTTPException(
                status_code=403, detail="only the author can edit a comment")
        conn.execute(
            text("UPDATE card_comments SET body = :b, updated_at = now() "
                 "WHERE tenant_id = :t AND id = CAST(:id AS uuid)"),
            {"b": body, "t": slug, "id": comment_id},
        )
        row = conn.execute(
            text("SELECT * FROM card_comments WHERE tenant_id = :t AND id = CAST(:id AS uuid)"),
            {"t": slug, "id": comment_id},
        ).mappings().first()
        conn.commit()

    log.info("card_comment_update card=%s tenant=%s", card_slug, slug)
    _board_changed(slug, "card.commented", slug=card["slug"], comment_id=comment_id)
    return {"comment": _comment_dict(row)}


@app.delete("/board/cards/{card_slug}/comments/{comment_id}")
def card_comment_delete(card_slug: str, comment_id: str, req: CommentDeleteRequest):
    """Withdraw a comment. The author only, and it leaves a marker."""
    slug = (req.tenant_slug or "default").strip() or "default"
    actor = ""
    if (req.actor or "").strip():
        actor = _resolve_people(slug, [req.actor]).get(req.actor.strip(), "")

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        row = conn.execute(
            text("SELECT * FROM card_comments WHERE tenant_id = :t AND id = CAST(:id AS uuid) "
                 "AND card_id = CAST(:c AS uuid)"),
            {"t": slug, "id": comment_id, "c": str(card["id"])},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="No such comment on that card")
        if not actor or actor != (row["author"] or ""):
            raise HTTPException(
                status_code=403, detail="only the author can delete a comment")
        # The body goes with it. Soft delete keeps the shape of the
        # conversation, not a copy of what was withdrawn.
        conn.execute(
            text("UPDATE card_comments SET deleted_at = now(), body = '' "
                 "WHERE tenant_id = :t AND id = CAST(:id AS uuid) AND deleted_at IS NULL"),
            {"t": slug, "id": comment_id},
        )
        conn.commit()

    log.info("card_comment_delete card=%s tenant=%s", card_slug, slug)
    _board_changed(slug, "card.commented", slug=card["slug"], comment_id=comment_id)
    return {"deleted": comment_id}


# ── POST /board/cards/{slug}/link (card_link) ─────────────────────────────────
class CardLinkRequest(BaseModel):
    tenant_slug: str = "default"
    other: str = ""
    kind: str = "related"
    remove: bool = False
    source: Optional[str] = None


@app.post("/board/cards/{card_slug}/link")
def card_link(card_slug: str, req: CardLinkRequest):
    """Link two cards, or unlink them.

    `kind` is `related`, `blocks` or `blocked-by`, read from the perspective of
    `card_slug`: blocks means this card blocks the other one. Removing takes the
    same arguments as adding, and `blocked-by` removes the edge `blocks` added
    from the other side — one edge, addressed from whichever end is convenient.
    """
    slug = (req.tenant_slug or "default").strip() or "default"
    other_ref = (req.other or "").strip()
    if not other_ref:
        raise HTTPException(status_code=400, detail="other is required")

    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        other = _fetch_card(conn, slug, other_ref)
        if other is None:
            raise HTTPException(status_code=404, detail=f"No card matches {other_ref!r}")
        if str(card["id"]) == str(other["id"]):
            # The only relationship refused outright. A card blocking itself is
            # never satisfiable and never meant; a longer cycle might be a real
            # disagreement worth seeing, so those are flagged on read instead.
            raise HTTPException(
                status_code=400,
                detail=f"a card cannot be related to itself ({card['slug']!r})")

        kind, src, dst = _normalize_link(req.kind, card["id"], other["id"])
        source_norm = (req.source or "unknown").strip().lower() or "unknown"

        if req.remove:
            removed = conn.execute(
                text("DELETE FROM card_links WHERE tenant_id = :t AND kind = :k "
                     "AND src_id = CAST(:s AS uuid) AND dst_id = CAST(:d AS uuid)"),
                {"t": slug, "k": kind, "s": src, "d": dst},
            ).rowcount
            conn.commit()
            if removed:
                _board_changed(slug, "card.linked", slug=card["slug"],
                               other=other["slug"], kind=kind, removed=True)
            return {"ok": True, "removed": bool(removed), "kind": kind,
                    "card": card["slug"], "other": other["slug"],
                    "warnings": [] if removed else
                    [f"no {kind!r} link between {card['slug']!r} and {other['slug']!r}"]}

        # Idempotent: linking twice is the same one link, not a second row that
        # would then need unlinking twice to go away.
        conn.execute(
            text("""
                INSERT INTO card_links (id, tenant_id, src_id, dst_id, kind, source)
                VALUES (:id, :t, CAST(:s AS uuid), CAST(:d AS uuid), :k, :src)
                ON CONFLICT (tenant_id, kind, src_id, dst_id) DO NOTHING
            """),
            {"id": str(uuid.uuid4()), "t": slug, "s": src, "d": dst,
             "k": kind, "src": source_norm},
        )
        rel = _relationships_by_card(conn, slug, [card["id"]]).get(str(card["id"]), {})
        # Reported, never refused. "A blocks B, B blocks A" is a real statement
        # about a real disagreement, and the person who can resolve it needs to
        # SEE it — being told the second link is invalid tells them nothing
        # about the first. The traversal that picks work guards itself, so a
        # cycle in the data costs a warning here, not a hang there.
        warnings = []
        if kind == "blocks":
            cycle = _blocking_cycle(conn, slug, card["id"])
            if cycle:
                warnings.append(
                    "this makes a blocking cycle: " + " → ".join(cycle) +
                    " — nothing in that chain can start until one of those links goes")
        conn.commit()

    _board_changed(slug, "card.linked", slug=card["slug"],
                   other=other["slug"], kind=kind, removed=False)
    return {"ok": True, "kind": kind, "card": card["slug"], "other": other["slug"],
            "relationships": rel, "warnings": warnings}


# ── POST /board/cards/{slug}/close (card_close) ───────────────────────────────
# `/archive` is the route's old name, kept so an older image, an external
# caller or a half-rolled deploy still reaches the same handler. The stream
# event keeps its name too: it is plumbing between our own services, not the
# vocabulary a person reads, and renaming it would strand every subscriber.
@app.post("/board/cards/{card_slug}/close")
@app.post("/board/cards/{card_slug}/archive")
def card_archive(card_slug: str, req: CardArchiveRequest):
    """Close a card: take it off the board without deleting it (there is no
    card deletion — closing is the terminal state)."""
    slug = (req.tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        if card["archived_at"] is None:
            conn.execute(
                text("UPDATE cards SET archived_at = now(), updated_at = now() "
                     "WHERE tenant_id = :t AND id = :id"),
                {"t": slug, "id": card["id"]},
            )
            conn.commit()
            _board_changed(slug, "card.archived", slug=card["slug"])
    return {"ok": True, "slug": card["slug"], "closed": True, "archived": True}


# ── POST /board/cards/{slug}/reopen (card_reopen) ─────────────────────────────
@app.post("/board/cards/{card_slug}/reopen")
@app.post("/board/cards/{card_slug}/unarchive")
def card_unarchive(card_slug: str, req: CardArchiveRequest):
    """Reopen a closed card — back onto the board (clears the close time)."""
    slug = (req.tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        if card["archived_at"] is not None:
            conn.execute(
                text("UPDATE cards SET archived_at = NULL, updated_at = now() "
                     "WHERE tenant_id = :t AND id = :id"),
                {"t": slug, "id": card["id"]},
            )
            conn.commit()
            _board_changed(slug, "card.unarchived", slug=card["slug"])
    return {"ok": True, "slug": card["slug"], "closed": False, "archived": False}


# ── The evaluation lock ───────────────────────────────────────────────────────
#
# An automated reviewer takes this before it evaluates a card, so a second run
# cannot evaluate the same card at the same time and a human reading the board
# can see the card is already in somebody's hands.
#
# Three operations, and only two of them are endpoints: reading the lock is not
# a call, it is a field on the ordinary card payload (see `_eval_lock_dict`).
# That is the point of putting the lock on the card rather than in a store the
# reviewer owns -- any caller sees it by reading the card it was already
# reading.

class CardEvalLockRequest(BaseModel):
    tenant_slug: str = "default"
    # WHO holds it. Opaque to Elefant on purpose: it is the reviewer's own run
    # handle, and Elefant's only interest is whether two acquires name the same
    # one. Not a principal reference like `assignee` -- a lock is held by a RUN,
    # not by an account, and two runs of the same reviewer must be able to
    # conflict with each other.
    run_id: str = ""


@app.post("/board/cards/{card_slug}/eval-lock")
def card_eval_lock(card_slug: str, req: CardEvalLockRequest):
    """Take the evaluation lock on a card, or refuse and name who holds it.

    ATOMICITY IS THE WHOLE FEATURE, so read the UPDATE below before changing
    anything here. It is a single conditional UPDATE -- a compare-and-set -- and
    NOT a read of the lock followed by a write. The difference is the only thing
    separating this from a lock that lets two concurrent runs both believe they
    hold it, which is the exact failure it exists to prevent. Postgres
    re-evaluates that WHERE clause against the latest committed row if another
    transaction updated it first (EvalPlanQual), so the loser of a race matches
    nothing and is refused rather than overwriting the winner.

    The SELECT ... FOR UPDATE above it is NOT the compare, and both halves are
    load-bearing for DIFFERENT requirements. Verified by mutation, each way
    round, against a real database (see the concurrency test in
    test_card_eval_lock_integration.py):

      * Take out the conditional UPDATE and leave the row lock, and two
        concurrent acquires BOTH win. Mutual exclusion is the UPDATE's job.
      * Take out the row lock and leave the conditional UPDATE, and exactly one
        acquire still wins -- but the loser reports the holder as '', because it
        read the row before the winner committed and never saw the lock it lost
        to. Naming the holder is the row lock's job, and the definition of done
        requires it: a refusal that cannot say who holds the card leaves the
        caller nothing to do.

    So the row lock serialises the pair and gives this transaction the previous
    holder, which the UPDATE cannot return once it has overwritten it -- needed
    both to name the holder in a conflict and to record whose lock was broken.

    Re-acquiring a lock you already hold succeeds and is a no-op, so a reviewer
    that retries a step does not have to care whether its earlier acquire landed.
    It deliberately does NOT refresh the timestamp: `since` then always means
    when this holder actually took the lock, and a run that is genuinely wedged
    cannot hold a card forever by re-acquiring in a loop. The consequence is
    that CARD_EVAL_LOCK_STALE_SECONDS has to exceed a whole evaluation rather
    than one step of one.

    A stale lock (past that ceiling) is broken by a new acquire rather than
    refusing forever, because a run can die mid-evaluation and Elefant must not
    depend on the reviewer always releasing cleanly. Breaking one is recorded:
    it is reported to the caller, logged, and published on the board stream as
    its own event type, because it means a run died and that is worth being able
    to find afterwards.
    """
    slug = (req.tenant_slug or "default").strip() or "default"
    run = (req.run_id or "").strip()
    if not run:
        raise HTTPException(status_code=400, detail="run_id is required to take an evaluation lock")
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        # Archived cards are excluded: a card off the board is not evaluable,
        # and locking one would show an indicator nobody is looking at.
        card = _fetch_card(conn, slug, card_slug)
        if card is None or card["archived_at"] is not None:
            raise HTTPException(status_code=404, detail=f"No active card matches {card_slug!r}")
        prev = conn.execute(
            text("SELECT eval_lock_run, eval_lock_at FROM cards "
                 "WHERE tenant_id = :t AND id = :id FOR UPDATE"),
            {"t": slug, "id": card["id"]},
        ).mappings().fetchone()
        prev_run = (prev["eval_lock_run"] or "") if prev else ""
        prev_at = prev["eval_lock_at"] if prev else None

        took = conn.execute(
            text("""
                UPDATE cards
                SET eval_lock_run = :run,
                    eval_lock_at  = CASE WHEN eval_lock_run = :run
                                         THEN eval_lock_at ELSE now() END,
                    updated_at    = now()
                WHERE tenant_id = :t AND id = :id
                  AND (eval_lock_run = ''
                       OR eval_lock_run = :run
                       OR eval_lock_at IS NULL
                       OR eval_lock_at < now() - make_interval(secs => :stale))
                RETURNING eval_lock_run, eval_lock_at
            """),
            {"t": slug, "id": card["id"], "run": run,
             "stale": float(CARD_EVAL_LOCK_STALE_SECONDS)},
        ).mappings().fetchone()

        if took is None:
            # Refused. Name the holder: "locked" on its own leaves the caller
            # nothing to do, while a run id is something an operator can go and
            # look up.
            conn.commit()
            held = {"run_id": prev_run,
                    "since": prev_at.isoformat() if prev_at else None}
            raise HTTPException(
                status_code=409,
                detail=(f"Card {card['slug']!r} is already under evaluation by run "
                        f"{prev_run!r}" + (f" since {held['since']}" if held["since"] else "")),
                headers={"X-Eval-Lock-Holder": prev_run},
            )

        # Did this acquire take a lock somebody else was holding? Only a stale
        # one can have been taken, because any other conflict was refused above.
        broke = prev_run if (prev_run and prev_run != run) else ""
        already = prev_run == run
        conn.commit()

    if broke:
        log.warning(
            "evaluation lock on card %s broken: run %s took it from run %s, "
            "whose lock had been held since %s (stale past %ss)",
            card["slug"], run, broke, prev_at.isoformat() if prev_at else "?",
            CARD_EVAL_LOCK_STALE_SECONDS)
        _board_changed(slug, "card.eval_lock_broken", slug=card["slug"],
                       run_id=run, broke_run_id=broke,
                       broke_since=prev_at.isoformat() if prev_at else None)
    if not already:
        _board_changed(slug, "card.eval_locked", slug=card["slug"], run_id=run)
    return {
        "ok": True,
        "slug": card["slug"],
        "eval_lock": {"run_id": took["eval_lock_run"],
                      "since": took["eval_lock_at"].isoformat() if took["eval_lock_at"] else None,
                      "stale": False},
        # True when the caller already held it, so a reviewer can tell an
        # acquire from a re-acquire without comparing timestamps.
        "already_held": already,
        # The run whose lock this broke, or "" when it took a free card. Present
        # on the happy path too, so a caller that logs the response records the
        # break without having to ask for it.
        "broke_stale_lock_of": broke,
    }


@app.post("/board/cards/{card_slug}/eval-unlock")
def card_eval_unlock(card_slug: str, req: CardEvalLockRequest):
    """Release the evaluation lock, for the run that holds it.

    Releasing a lock held by a DIFFERENT run is refused, not honoured: a
    release that any run could call on any card is not a lock, and a reviewer
    releasing a card it never held is a bug worth surfacing rather than
    absorbing. Breaking somebody else's lock is `card_eval_lock`'s job, and only
    once it has gone stale.

    Releasing a card that is already unlocked succeeds and changes nothing, so
    a reviewer's cleanup path is safe to run twice and safe to run after a
    stale-break took the lock away.
    """
    slug = (req.tenant_slug or "default").strip() or "default"
    run = (req.run_id or "").strip()
    if not run:
        raise HTTPException(status_code=400, detail="run_id is required to release an evaluation lock")
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        # Archived cards ARE releasable here, unlike acquire: a card can be
        # archived while a run holds it, and refusing the release would leave a
        # lock nobody can clear.
        card = _fetch_card(conn, slug, card_slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"No card matches {card_slug!r}")
        # Same shape as acquire, and conditional for the same reason: the WHERE
        # clause is the holder check, so it cannot be raced between a read and
        # a write.
        freed = conn.execute(
            text("""
                UPDATE cards
                SET eval_lock_run = '', eval_lock_at = NULL, updated_at = now()
                WHERE tenant_id = :t AND id = :id AND eval_lock_run = :run
                RETURNING 1 AS freed
            """),
            {"t": slug, "id": card["id"], "run": run},
        ).mappings().fetchone()
        if freed is None:
            current = conn.execute(
                text("SELECT eval_lock_run, eval_lock_at FROM cards "
                     "WHERE tenant_id = :t AND id = :id"),
                {"t": slug, "id": card["id"]},
            ).mappings().fetchone()
            held = (current["eval_lock_run"] or "") if current else ""
            conn.commit()
            if not held:
                # Already unlocked. A no-op that succeeds.
                return {"ok": True, "slug": card["slug"], "eval_lock": None,
                        "was_locked": False}
            raise HTTPException(
                status_code=409,
                detail=(f"Card {card['slug']!r} is under evaluation by run {held!r}, "
                        f"not by {run!r}, so {run!r} cannot release it"),
                headers={"X-Eval-Lock-Holder": held},
            )
        conn.commit()
    _board_changed(slug, "card.eval_unlocked", slug=card["slug"], run_id=run)
    return {"ok": True, "slug": card["slug"], "eval_lock": None, "was_locked": True}


class ColumnCreateRequest(BaseModel):
    tenant_slug: str = "default"
    name: ShortStr
    stage: str
    position: Optional[float] = None
    color: Optional[str] = None


class ColumnUpdateRequest(BaseModel):
    tenant_slug: str = "default"
    name: Optional[ShortStr] = None
    stage: Optional[str] = None
    position: Optional[float] = None
    color: Optional[str] = None


class ColumnArchiveRequest(BaseModel):
    tenant_slug: str = "default"


def _column_dict(row) -> dict:
    return {
        "id":       str(row["id"]),
        "name":     row["name"],
        "stage":    row["stage"],
        "position": row["position"],
        "on_enter": row["on_enter"],
        "color":    row["color"],
    }


# ── POST /board/columns (column_add) ──────────────────────────────────────────
@app.post("/board/columns", status_code=201)
def column_add(req: ColumnCreateRequest):
    """Create a column. `stage` must be one of the architected stages — that
    vocabulary is fixed; the column's NAME is the user's own."""
    slug = (req.tenant_slug or "default").strip() or "default"
    name = (req.name or "").strip()
    stage = (req.stage or "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if stage not in VALID_BOARD_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown stage {stage!r}; must be one of: {', '.join(BOARD_STAGES)}",
        )
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        _ensure_seed_columns(conn, slug)
        if req.position is not None:
            pos = float(req.position)
        else:
            pos = float(conn.execute(
                text("SELECT COALESCE(MAX(position), 0.0) FROM board_columns "
                     "WHERE tenant_id = :t AND archived_at IS NULL"),
                {"t": slug},
            ).scalar()) + 1.0
        col_id = str(uuid.uuid4())
        color = (req.color or "").strip() or None
        conn.execute(
            text("""
                INSERT INTO board_columns (id, tenant_id, name, stage, position, color)
                VALUES (:id, :t, :name, :stage, :pos, :color)
            """),
            {"id": col_id, "t": slug, "name": name, "stage": stage, "pos": pos, "color": color},
        )
        conn.commit()
    _board_changed(slug, "column.added", column_id=col_id)
    return {"column": {"id": col_id, "name": name, "stage": stage,
                       "position": pos, "on_enter": None, "color": color}}


# ── PATCH /board/columns/{id} (column_update) ─────────────────────────────────
@app.patch("/board/columns/{column_id}")
def column_update(column_id: str, req: ColumnUpdateRequest):
    """Rename a column, remap it to a different stage, or reorder it."""
    slug = (req.tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        col = conn.execute(
            text("SELECT * FROM board_columns WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": column_id},
        ).mappings().fetchone()
        if col is None:
            raise HTTPException(status_code=404, detail=f"No column {column_id!r}")

        sets: list = []
        params: dict = {"t": slug, "id": column_id}
        if req.name is not None:
            nm = req.name.strip()
            if not nm:
                raise HTTPException(status_code=400, detail="name cannot be blank")
            sets.append("name = :name")
            params["name"] = nm
        if req.stage is not None:
            st = req.stage.strip().lower()
            if st not in VALID_BOARD_STAGES:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown stage {st!r}; must be one of: {', '.join(BOARD_STAGES)}",
                )
            sets.append("stage = :stage")
            params["stage"] = st
        if req.position is not None:
            sets.append("position = :pos")
            params["pos"] = float(req.position)
        if req.color is not None:
            sets.append("color = :color")
            params["color"] = (req.color or "").strip() or None
        if not sets:
            return {"column": _column_dict(col), "warnings": ["no fields to update"]}
        conn.execute(
            text(f"UPDATE board_columns SET {', '.join(sets)} WHERE tenant_id = :t AND id = :id"),
            params,
        )
        updated = conn.execute(
            text("SELECT * FROM board_columns WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": column_id},
        ).mappings().fetchone()
        conn.commit()
    _board_changed(slug, "column.updated", column_id=column_id)
    return {"column": _column_dict(updated)}


# ── POST /board/columns/{id}/archive (column_archive) ─────────────────────────
@app.post("/board/columns/{column_id}/archive")
def column_archive(column_id: str, req: ColumnArchiveRequest):
    """Archive a column. Its cards are never orphaned — they move to the nearest
    remaining column of the SAME stage (by position), or the leftmost remaining
    column if none shares the stage. Refuses to archive the last column."""
    slug = (req.tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        col = conn.execute(
            text("SELECT * FROM board_columns "
                 "WHERE tenant_id = :t AND id = :id AND archived_at IS NULL"),
            {"t": slug, "id": column_id},
        ).mappings().fetchone()
        if col is None:
            raise HTTPException(status_code=404, detail=f"No active column {column_id!r}")

        # Fallback: nearest same-stage column by position, else leftmost.
        target = conn.execute(
            text("""
                SELECT * FROM board_columns
                WHERE tenant_id = :t AND archived_at IS NULL AND id <> :id AND stage = :stage
                ORDER BY abs(position - :p), position
                LIMIT 1
            """),
            {"t": slug, "id": column_id, "stage": col["stage"], "p": col["position"]},
        ).mappings().fetchone()
        if target is None:
            target = conn.execute(
                text("""
                    SELECT * FROM board_columns
                    WHERE tenant_id = :t AND archived_at IS NULL AND id <> :id
                    ORDER BY position
                    LIMIT 1
                """),
                {"t": slug, "id": column_id},
            ).mappings().fetchone()
        if target is None:
            raise HTTPException(
                status_code=400,
                detail="cannot archive the last remaining column (its cards would have "
                       "nowhere to go); create another column first.",
            )

        # Re-home the column's active cards, appended to the target in order.
        moved = conn.execute(
            text("""
                UPDATE cards c
                SET column_id = :target, updated_at = now(),
                    position = sub.newpos
                FROM (
                    SELECT id,
                           (SELECT COALESCE(MAX(position), 0.0) FROM cards WHERE column_id = :target)
                             + row_number() OVER (ORDER BY position) AS newpos
                    FROM cards
                    WHERE column_id = :src AND archived_at IS NULL
                ) sub
                WHERE c.id = sub.id
            """),
            {"target": target["id"], "src": column_id},
        ).rowcount
        conn.execute(
            text("UPDATE board_columns SET archived_at = now() "
                 "WHERE tenant_id = :t AND id = :id"),
            {"t": slug, "id": column_id},
        )
        conn.commit()
    # Archiving a column re-homes its cards, so this is the one change that can
    # move many cards at once — all the more reason for an open board not to
    # keep showing a column that is gone.
    _board_changed(slug, "column.archived", column_id=column_id,
                   moved_to=str(target["id"]), moved_count=moved)
    return {"ok": True, "archived_column": col["name"],
            "moved_to": target["name"], "moved_count": moved}


# ── Compactor (PR B) ─────────────────────────────────────────────────────────
# Periodically scans memory_hot for pending rows, batch-embeds them, clusters
# by cosine similarity, scores by recurrence × source_diversity × recency,
# and either promotes survivors to the durable Qdrant tier (via the embedder)
# or marks them dropped after they age out below the score floor.
#
# Tuned for low-volume homelab use — the defaults assume a single user
# capturing tens of memories an hour, not a multi-tenant SaaS.

COMPACTOR_ENABLED            = os.getenv("COMPACTOR_ENABLED", "1").lower() not in ("0", "false", "no")
COMPACTOR_INTERVAL_SECONDS   = int(os.getenv("COMPACTOR_INTERVAL_SECONDS", "900"))      # 15 min
COMPACTOR_BATCH_SIZE         = int(os.getenv("COMPACTOR_BATCH_SIZE", "100"))
COMPACTOR_CLUSTER_THRESHOLD  = float(os.getenv("COMPACTOR_CLUSTER_THRESHOLD", "0.85"))
COMPACTOR_PROMOTE_SCORE      = float(os.getenv("COMPACTOR_PROMOTE_SCORE", "1.0"))
COMPACTOR_DROP_SCORE         = float(os.getenv("COMPACTOR_DROP_SCORE", "0.3"))
COMPACTOR_DROP_AGE_DAYS      = float(os.getenv("COMPACTOR_DROP_AGE_DAYS", "7"))
COMPACTOR_EXISTING_DEDUP_SIM = float(os.getenv("COMPACTOR_EXISTING_DEDUP_SIM", "0.9"))

_compactor_state: dict = {
    "last_started_at":  None,
    "last_finished_at": None,
    "last_duration_ms": None,
    "last_stats":       {},     # {tenant: {clusters, promoted, deduped, dropped, pending}}
    "running":          False,
}
_compactor_lock = asyncio.Lock()
_compactor_task: Optional[asyncio.Task] = None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cluster_rows(rows_with_vecs, threshold):
    """Greedy single-pass clustering. For each row pick the cluster whose
    rolling-mean centroid it most resembles; new cluster if best sim is
    below `threshold`. O(n × k) where k = number of clusters formed; fine
    for batch sizes in the low hundreds. Returns list[list[row]]."""
    clusters = []   # each: {"rows": [...], "centroid": [...], "n": int}
    for row, vec in rows_with_vecs:
        best_i, best_sim = -1, 0.0
        for i, c in enumerate(clusters):
            sim = _cosine(vec, c["centroid"])
            if sim > best_sim:
                best_i, best_sim = i, sim
        if best_i >= 0 and best_sim >= threshold:
            c = clusters[best_i]
            n = c["n"]
            c["centroid"] = [(cv * n + nv) / (n + 1) for cv, nv in zip(c["centroid"], vec)]
            c["n"] = n + 1
            c["rows"].append(row)
        else:
            clusters.append({"rows": [row], "centroid": list(vec), "n": 1})
    return [c["rows"] for c in clusters]


def _score_cluster(rows, now: datetime) -> float:
    """recurrence × source_diversity × recency.

    A single fresh capture from one source scores 1.0 (promotes by default).
    A multi-source cluster from three different hosts in the last day scores
    higher; a single stale capture decays toward the drop floor over ~14d.

    Recency is clamped to 1.0 for the first 24 hours so a single fresh
    capture actually clears the >=1.0 promote threshold — previously the
    linear `1 - age_days/14` decay started biting the instant the row
    landed, leaving every capture stranded at ~0.999 forever.
    """
    n = len(rows)
    sources = {(r.get("source") or "unknown") for r in rows}
    s = len(sources)
    times = [r["captured_at"] for r in rows if r.get("captured_at")]
    if not times:
        return 0.0
    newest = max(times)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_days = (now - newest).total_seconds() / 86400.0
    if age_days <= 1.0:
        recency = 1.0
    else:
        recency = max(0.1, 1.0 - (age_days - 1.0) / 14.0)
    return s * (1.0 + 0.3 * (n - 1)) * recency


def _cluster_canonical_text(rows) -> str:
    """Longest-member canonicalization — tends to carry the most context.
    LLM summarization would do better but is out of scope; deterministic
    and free is the right trade-off for the homelab path."""
    return max((r["content"] for r in rows), key=len)


def _cluster_canonical_tags(rows) -> list[str]:
    tags = set()
    for r in rows:
        for t in (r.get("tags") or []):
            tags.add(t)
    tags.add("compacted")
    return sorted(tags)


def _cluster_canonical_type(rows) -> str:
    hints = [r.get("type_hint") for r in rows if r.get("type_hint")]
    if not hints:
        return "fact"
    return Counter(hints).most_common(1)[0][0]


def _cluster_canonical_source(rows) -> str:
    """If all rows share one source, use it. Otherwise 'multi' so the durable
    memory's source field honestly reflects cross-host corroboration."""
    sources = {(r.get("source") or "unknown") for r in rows}
    if len(sources) == 1:
        return next(iter(sources))
    return "multi"


def _list_pending_tenants_sync() -> list[str]:
    """List tenants with pending hot rows. Uses the compactor_bypass RLS
    policy so this single query can see across all tenants."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SET LOCAL app.tenant_id = '__compactor__'"))
        rows = conn.execute(text("""
            SELECT DISTINCT tenant_id
            FROM memory_hot
            WHERE promoted_to IS NULL AND dropped_at IS NULL
        """)).fetchall()
    tenants = [r[0] for r in rows]
    log.info("compactor: _list_pending_tenants_sync returned %d tenant(s): %s",
             len(tenants), tenants)
    return tenants


def _fetch_pending_sync(tenant_slug: str, limit: int) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": tenant_slug})
        # Verify the GUC actually took. If this prints something other than
        # tenant_slug, the parameter never made it through and RLS will
        # filter every row — the symptom we're chasing.
        observed = conn.execute(text(
            "SELECT current_setting('app.tenant_id', true)"
        )).scalar()
        rows = conn.execute(text("""
            SELECT id, content, type_hint, tags, source, source_session_id,
                   captured_at, dedup_hash, tenant_id
            FROM memory_hot
            WHERE promoted_to IS NULL AND dropped_at IS NULL
            ORDER BY captured_at ASC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
    log.info("compactor: _fetch_pending_sync tenant=%r guc_observed=%r returned=%d",
             tenant_slug, observed, len(rows))
    return [
        {
            "id":                str(r[0]),
            "content":           r[1],
            "type_hint":         r[2],
            "tags":              list(r[3] or []),
            "source":            r[4],
            "source_session_id": r[5],
            "captured_at":       r[6],
            "dedup_hash":        r[7],
            "tenant_id":         r[8],
        }
        for r in rows
    ]


def _mark_promoted_sync(tenant_slug: str, ids: list[str], durable_id: str, score: float):
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": tenant_slug})
        conn.execute(text("""
            UPDATE memory_hot
            SET promoted_to = :did, promoted_at = now(), score = :score
            WHERE id = ANY(CAST(:ids AS uuid[]))
        """), {"did": durable_id, "score": score, "ids": ids})
        conn.commit()


def _mark_dropped_sync(tenant_slug: str, ids: list[str], score: float):
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": tenant_slug})
        conn.execute(text("""
            UPDATE memory_hot
            SET dropped_at = now(), score = :score
            WHERE id = ANY(CAST(:ids AS uuid[]))
        """), {"score": score, "ids": ids})
        conn.commit()


def _update_score_sync(tenant_slug: str, ids: list[str], score: float):
    """Refresh score on still-pending rows so the admin UI (PR C) can see
    the latest assessment even when no promote/drop decision fires."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": tenant_slug})
        conn.execute(text("""
            UPDATE memory_hot SET score = :score
             WHERE id = ANY(CAST(:ids AS uuid[]))
        """), {"score": score, "ids": ids})
        conn.commit()


def _emit_compaction_event_sync(tenant_slug: str, stats: dict):
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": tenant_slug})
        conn.execute(text("""
            INSERT INTO events (type, content, source, tags, metadata, tenant_id)
            VALUES ('compaction_run', :content, 'compactor', :tags,
                    CAST(:metadata AS JSONB), :tenant_id)
        """), {
            "content":   f"compaction_run tenant={tenant_slug} promoted={stats.get('promoted',0)} "
                         f"deduped={stats.get('deduped',0)} dropped={stats.get('dropped',0)} "
                         f"pending={stats.get('pending',0)}",
            "tags":      ["compactor"],
            "metadata":  json.dumps(stats),
            "tenant_id": tenant_slug,
        })
        conn.commit()


async def _embedder_embed(client: httpx.AsyncClient, text_input: str) -> Optional[list[float]]:
    try:
        resp = await client.post(f"{EMBEDDER_URL}/api/embed",
                                 json={"text": text_input},
                                 headers=_internal_service_headers(),
                                 timeout=30)
        resp.raise_for_status()
        return resp.json().get("embedding") or None
    except Exception as exc:
        log.warning("compactor: embed failed: %s", exc)
        return None


async def _existing_durable_match(
    client: httpx.AsyncClient, canonical: str, tenant_slug: str,
) -> Optional[dict]:
    """Search the durable tier for a memory close enough to skip a new
    insert. Returns the top hit dict (with score) if above
    EXISTING_DEDUP_SIM, else None."""
    try:
        resp = await client.get(
            f"{EMBEDDER_URL}/api/memories/search",
            params={"q": canonical, "tenant_slug": tenant_slug, "limit": 1},
            headers=_internal_service_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        top = results[0]
        if float(top.get("score") or 0.0) >= COMPACTOR_EXISTING_DEDUP_SIM:
            return top
        return None
    except Exception as exc:
        log.warning("compactor: existing-dedup search failed: %s", exc)
        return None


async def _promote_durable(
    client: httpx.AsyncClient, canonical: str, tags: list[str],
    type_: str, source: str, tenant_slug: str,
    *,
    hot_ids: Optional[list[str]] = None,
    session_ids: Optional[list[str]] = None,
    compactor_score: Optional[float] = None,
    oldest_captured_at: Optional[datetime] = None,
) -> Optional[str]:
    """Create a durable memory via the embedder. Returns the new memory id.

    Provenance kwargs are forwarded to the embedder so the durable memory
    carries the trail back to its hot-tier origin — the data that lets
    memory_provenance later answer "where did this come from?". Each is
    optional so legacy/direct callers don't have to know about them.
    """
    # Mark memories that graduated from the hot tier so the Memories UI can
    # flag them (green "hot tier promoted" badge). Covers both the compactor
    # and the manual single-row promote path, which both call this.
    tags = list(tags)
    if "hot-tier-promoted" not in tags:
        tags.append("hot-tier-promoted")

    body: dict = {
        "content":     canonical,
        "tags":        tags,
        "type":        type_,
        "source":      source,
        "tenant_slug": tenant_slug,
    }
    if hot_ids:
        body["originated_from_hot_ids"] = hot_ids
    if session_ids:
        # Dedup + drop falsy values; multiple captures in one cluster may
        # share or differ in session attribution.
        cleaned = sorted({s for s in session_ids if s})
        if cleaned:
            body["originated_session_ids"] = cleaned
    if compactor_score is not None:
        body["compactor_score"] = float(compactor_score)
    if oldest_captured_at is not None:
        body["oldest_captured_at"] = oldest_captured_at.isoformat()
    # Stamp promoted_at here (wall clock at promotion) rather than on the
    # embedder side, so it matches memory_hot.promoted_at within one tick.
    body["promoted_at"] = datetime.now(timezone.utc).isoformat()

    try:
        resp = await client.post(
            f"{EMBEDDER_URL}/api/memories",
            json=body,
            headers=_internal_service_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("id") or (data.get("memory") or {}).get("id")
    except Exception as exc:
        log.warning("compactor: promote failed: %s", exc)
        return None


async def _resolve_tenant_thresholds(tenant_slug: str) -> dict:
    """Effective compactor thresholds for one tenant.

    Reads per-tenant overrides from the editor's tenant-settings store; any
    field left unset there — or an unreachable editor — falls back to the
    global COMPACTOR_* env default, so compaction keeps working even if the
    editor is down.
    """
    eff = {
        "cluster_threshold": COMPACTOR_CLUSTER_THRESHOLD,
        "promote_score":     COMPACTOR_PROMOTE_SCORE,
        "drop_score":        COMPACTOR_DROP_SCORE,
        "drop_age_days":     COMPACTOR_DROP_AGE_DAYS,
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{EDITOR_URL}/api/internal/tenant-settings",
                params={"tenant_slug": tenant_slug},
            )
            resp.raise_for_status()
            settings = (resp.json() or {}).get("settings", {}) or {}
    except Exception as exc:
        log.warning("compactor: tenant=%s settings fetch failed (%s); using env defaults",
                    tenant_slug, exc)
        return eff
    overrides = {
        "cluster_threshold": "hot_tier_cluster_threshold",
        "promote_score":     "hot_tier_promote_score",
        "drop_score":        "hot_tier_drop_score",
        "drop_age_days":     "hot_tier_drop_age_days",
    }
    for local_key, settings_key in overrides.items():
        val = settings.get(settings_key)
        if val is not None:
            eff[local_key] = float(val)
    return eff


async def _compact_tenant(tenant_slug: str) -> dict:
    """One compaction pass for one tenant. Returns a stats dict."""
    now = datetime.now(timezone.utc)
    stats = {"clusters": 0, "promoted": 0, "deduped": 0, "dropped": 0, "pending": 0}

    rows = await asyncio.to_thread(_fetch_pending_sync, tenant_slug, COMPACTOR_BATCH_SIZE)
    if not rows:
        return stats

    # Tenant-integrity guard. _fetch_pending_sync relies on Postgres RLS
    # (app.tenant_id GUC) to return only this tenant's rows; never trust that
    # blindly. If a row's stored tenant_id doesn't match the tenant we're
    # processing, refuse to promote it (which would cross-tenant leak it into
    # this tenant's Qdrant collection) and log the mismatch loudly. Foreign
    # rows are left pending and handled when their own tenant is processed.
    foreign = [r for r in rows if r.get("tenant_id") != tenant_slug]
    if foreign:
        log.error(
            "compactor TENANT MISMATCH while processing slug=%r: %d/%d fetched row(s) "
            "carry foreign tenant_id(s)=%s row_ids=%s — refusing to promote them "
            "(RLS isolation breach on memory_hot)",
            tenant_slug, len(foreign), len(rows),
            sorted({r.get("tenant_id") for r in foreign}),
            [r.get("id") for r in foreign][:20],
        )
        rows = [r for r in rows if r.get("tenant_id") == tenant_slug]
        if not rows:
            return stats

    # Per-tenant threshold overrides from the editor settings store; the
    # global COMPACTOR_* env values apply wherever a tenant hasn't overridden.
    th = await _resolve_tenant_thresholds(tenant_slug)

    # Embed each row (sequential — embedder has no batch endpoint, and at
    # batch_size=100 this is ~2-3s on local CPU). Cache by dedup_hash to
    # avoid re-embedding identical content within the same run.
    embed_cache: dict[str, list[float]] = {}
    rows_with_vecs = []
    async with httpx.AsyncClient(timeout=60) as client:
        for r in rows:
            dh = r["dedup_hash"]
            vec = embed_cache.get(dh)
            if vec is None:
                vec = await _embedder_embed(client, r["content"])
                if vec is None:
                    continue
                embed_cache[dh] = vec
            rows_with_vecs.append((r, vec))

        if not rows_with_vecs:
            return stats

        clusters = _cluster_rows(rows_with_vecs, th["cluster_threshold"])
        stats["clusters"] = len(clusters)

        for cluster in clusters:
            ids = [r["id"] for r in cluster]
            score = _score_cluster(cluster, now)
            canonical = _cluster_canonical_text(cluster)
            oldest = min((r["captured_at"] for r in cluster if r.get("captured_at")),
                         default=now)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            age_days = (now - oldest).total_seconds() / 86400.0

            if score >= th["promote_score"]:
                existing = await _existing_durable_match(client, canonical, tenant_slug)
                if existing:
                    await asyncio.to_thread(
                        _mark_promoted_sync, tenant_slug, ids, str(existing["id"]), score,
                    )
                    stats["deduped"] += len(ids)
                else:
                    new_id = await _promote_durable(
                        client, canonical,
                        _cluster_canonical_tags(cluster),
                        _cluster_canonical_type(cluster),
                        _cluster_canonical_source(cluster),
                        tenant_slug,
                        hot_ids=ids,
                        session_ids=[r.get("source_session_id") for r in cluster],
                        compactor_score=score,
                        oldest_captured_at=oldest,
                    )
                    if new_id:
                        await asyncio.to_thread(
                            _mark_promoted_sync, tenant_slug, ids, new_id, score,
                        )
                        stats["promoted"] += len(ids)
                    else:
                        # Promote failed (embedder down?). Just refresh score
                        # and try again next cycle.
                        await asyncio.to_thread(
                            _update_score_sync, tenant_slug, ids, score,
                        )
                        stats["pending"] += len(ids)
            elif age_days >= th["drop_age_days"] and score < th["drop_score"]:
                await asyncio.to_thread(_mark_dropped_sync, tenant_slug, ids, score)
                stats["dropped"] += len(ids)
            else:
                await asyncio.to_thread(_update_score_sync, tenant_slug, ids, score)
                stats["pending"] += len(ids)

    try:
        await asyncio.to_thread(_emit_compaction_event_sync, tenant_slug, stats)
    except Exception as exc:
        log.warning("compactor: emitting event failed (non-fatal): %s", exc)

    return stats


async def _run_compactor_once() -> dict:
    """Process every tenant that has pending hot rows. Idempotent — safe
    to call ad-hoc via POST /memory/compact/run."""
    async with _compactor_lock:
        if _compactor_state["running"]:
            return {"status": "already_running"}
        _compactor_state["running"]          = True
        _compactor_state["last_started_at"]  = datetime.now(timezone.utc).isoformat()
        _compactor_state["last_finished_at"] = None
        _compactor_state["last_stats"]       = {}
        started = datetime.now(timezone.utc)
        try:
            tenants = await asyncio.to_thread(_list_pending_tenants_sync)
            for slug in tenants:
                try:
                    stats = await _compact_tenant(slug)
                    _compactor_state["last_stats"][slug] = stats
                    log.info("compactor: tenant=%s %s", slug, stats)
                except Exception as exc:
                    log.exception("compactor: tenant=%s failed: %s", slug, exc)
            return {
                "status":  "ok",
                "tenants": tenants,
                "stats":   _compactor_state["last_stats"],
            }
        finally:
            finished = datetime.now(timezone.utc)
            _compactor_state["last_finished_at"] = finished.isoformat()
            _compactor_state["last_duration_ms"] = int((finished - started).total_seconds() * 1000)
            _compactor_state["running"]          = False


async def _compactor_loop():
    """Periodic wrapper. Sleeps the configured interval between runs; logs
    and continues on failure so a single bad cycle doesn't kill the loop."""
    log.info("compactor: loop starting, interval=%ds promote_score=%.2f drop_age=%dd",
             COMPACTOR_INTERVAL_SECONDS, COMPACTOR_PROMOTE_SCORE, COMPACTOR_DROP_AGE_DAYS)
    while True:
        try:
            await _run_compactor_once()
        except Exception as exc:
            log.exception("compactor: run failed: %s", exc)
        await asyncio.sleep(COMPACTOR_INTERVAL_SECONDS)


@app.post("/memory/compact/run")
async def memory_compact_run():
    """Force a compaction pass now. Returns stats from the just-finished
    run, or 'already_running' if a pass is in progress."""
    return await _run_compactor_once()


@app.get("/memory/compact/config")
def memory_compact_config():
    """Surface current compactor tunables and last-run state. The admin UI
    reads this to render the Hot Tier section."""
    return {
        "enabled":            COMPACTOR_ENABLED,
        "interval_seconds":   COMPACTOR_INTERVAL_SECONDS,
        "batch_size":         COMPACTOR_BATCH_SIZE,
        "cluster_threshold":  COMPACTOR_CLUSTER_THRESHOLD,
        "promote_score":      COMPACTOR_PROMOTE_SCORE,
        "drop_score":         COMPACTOR_DROP_SCORE,
        "drop_age_days":      COMPACTOR_DROP_AGE_DAYS,
        "existing_dedup_sim": COMPACTOR_EXISTING_DEDUP_SIM,
        "state":              dict(_compactor_state),
    }


@app.get("/memory/compact/stats")
def memory_compact_stats(
    tenant_slug: str = Query("default"),
):
    """Compaction health snapshot for the admin UI. Covers two things
    that previously had no surface: (1) current queue depth — how many
    captures are stuck pending right now — and (2) the last few
    compactor runs' promote/drop/dedupe tallies, sourced from the
    compactor's emitted events. Without this view, ops can't see the
    hot tier transitioning into durable storage."""
    slug = (tenant_slug or "default").strip() or "default"

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        pending = conn.execute(text("""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug
              AND promoted_to IS NULL AND dropped_at IS NULL
        """), {"slug": slug}).scalar() or 0
        promoted_total = conn.execute(text("""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug AND promoted_to IS NOT NULL
        """), {"slug": slug}).scalar() or 0
        dropped_total = conn.execute(text("""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug AND dropped_at IS NOT NULL
        """), {"slug": slug}).scalar() or 0
        # Oldest pending capture — if this is hours/days old the
        # compactor is wedged.
        oldest_pending = conn.execute(text("""
            SELECT min(captured_at) FROM memory_hot
            WHERE tenant_id = :slug
              AND promoted_to IS NULL AND dropped_at IS NULL
        """), {"slug": slug}).scalar()
        # Last few compactor passes — pulled from the events table the
        # compactor writes to via _emit_compaction_event_sync. Per-tenant
        # so admins see only their own runs. NB: the events table's
        # timestamp column is `created`, not `created_at` (different
        # convention than memory_hot.captured_at) — using the wrong
        # name produced a silent 500 that left the admin UI's
        # "Recent compactor runs" panel stuck on Loading.
        recent_runs = conn.execute(text("""
            SELECT created, metadata
            FROM events
            WHERE tenant_id = :slug AND type = 'compaction_run'
            ORDER BY created DESC
            LIMIT 10
        """), {"slug": slug}).fetchall()

    return {
        "tenant_slug":      slug,
        "pending":          int(pending),
        "promoted_total":   int(promoted_total),
        "dropped_total":    int(dropped_total),
        "oldest_pending":   oldest_pending.isoformat() if oldest_pending else None,
        "compactor_state":  dict(_compactor_state),
        "recent_runs": [
            {
                "at":      row[0].isoformat() if row[0] else None,
                "stats":   row[1] or {},
            }
            for row in recent_runs
        ],
    }


# ── GET /memory/introspect ───────────────────────────────────────────────────
@app.get("/memory/introspect")
def memory_introspect(
    tenant_slug: str = Query("default"),
    window: str = Query("7d", description="One of: '24h', '7d', '30d', 'Nh', 'Nd', 'Nw', or 'all'"),
):
    """Aggregate retrospective of memory activity in the tenant — captures,
    promotions, drops, score distribution, type/tag/source breakdowns over
    a time window. Powers the memory_introspect MCP tool.

    All lifecycle counts derive from memory_hot (the lifecycle log lives
    here, not in Qdrant). 'Captured', 'promoted', and 'dropped' are
    bucketed by their respective timestamp falling inside the window;
    'pending' is always current state (a thing is either pending right
    now or it isn't — windowing it makes no sense).
    """
    slug = (tenant_slug or "default").strip() or "default"

    # Parse window. None means "no time filter" ('all').
    w = (window or "7d").strip().lower()
    since: Optional[datetime] = None
    if w == "all":
        since = None
    elif w.endswith("h") and w[:-1].isdigit():
        since = datetime.now(timezone.utc) - timedelta(hours=int(w[:-1]))
    elif w.endswith("d") and w[:-1].isdigit():
        since = datetime.now(timezone.utc) - timedelta(days=int(w[:-1]))
    elif w.endswith("w") and w[:-1].isdigit():
        since = datetime.now(timezone.utc) - timedelta(weeks=int(w[:-1]))
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported window {window!r}; use Nh / Nd / Nw / 'all'",
        )

    # Each lifecycle stage is windowed against ITS OWN timestamp column.
    # Internal-only string interpolation (not user input) — safe.
    where_cap     = "AND captured_at >= :since"  if since else ""
    where_promo   = "AND promoted_at >= :since"  if since else ""
    where_dropped = "AND dropped_at  >= :since"  if since else ""
    params: dict = {"slug": slug}
    if since:
        params["since"] = since

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})

        captures = conn.execute(text(f"""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug {where_cap}
        """), params).scalar() or 0

        promoted = conn.execute(text(f"""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug AND promoted_at IS NOT NULL {where_promo}
        """), params).scalar() or 0

        dropped = conn.execute(text(f"""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug AND dropped_at IS NOT NULL {where_dropped}
        """), params).scalar() or 0

        # Pending = current state, intentionally not windowed.
        pending = conn.execute(text("""
            SELECT count(*) FROM memory_hot
            WHERE tenant_id = :slug AND promoted_to IS NULL AND dropped_at IS NULL
        """), {"slug": slug}).scalar() or 0

        by_type = conn.execute(text(f"""
            SELECT coalesce(type_hint, 'fact') AS t, count(*) AS c
            FROM memory_hot
            WHERE tenant_id = :slug {where_cap}
            GROUP BY t ORDER BY c DESC
        """), params).fetchall()

        by_source = conn.execute(text(f"""
            SELECT coalesce(source, 'unknown') AS s, count(*) AS c
            FROM memory_hot
            WHERE tenant_id = :slug {where_cap}
            GROUP BY s ORDER BY c DESC LIMIT 20
        """), params).fetchall()

        # Tags is text[] — unnest before grouping.
        top_tags = conn.execute(text(f"""
            SELECT tag, count(*) AS c FROM (
                SELECT unnest(tags) AS tag FROM memory_hot
                WHERE tenant_id = :slug {where_cap}
            ) t
            GROUP BY tag ORDER BY c DESC LIMIT 15
        """), params).fetchall()

        # Score distribution over rows that have been scored (promoted or
        # dropped) within the window. percentile_cont is in core Postgres
        # since 9.4 so no extension dependency. Hand-promoted rows carry the
        # 999.0 sentinel score (see the /memory/hot/{row_id}/promote handler)
        # — exclude them so one manual promote doesn't wreck mean/max; real
        # compactor scores live in the ~0-2 range.
        score_dist = conn.execute(text(f"""
            SELECT
                count(*)                                                    AS n,
                min(score)                                                  AS s_min,
                max(score)                                                  AS s_max,
                avg(score)                                                  AS s_avg,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY score)         AS s_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY score)         AS s_p50,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY score)         AS s_p75,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY score)         AS s_p95
            FROM memory_hot
            WHERE tenant_id = :slug AND score IS NOT NULL AND score < 100 {where_cap}
        """), params).fetchone()

    def _f(v):
        return float(v) if v is not None else None

    return {
        "tenant_slug":  slug,
        "window":       w,
        "window_start": since.isoformat() if since else None,
        "window_end":   datetime.now(timezone.utc).isoformat(),
        "totals": {
            "captures": int(captures),
            "promoted": int(promoted),
            "dropped":  int(dropped),
            "pending":  int(pending),
        },
        "by_type":   [{"type":   r[0], "count": int(r[1])} for r in by_type],
        "by_source": [{"source": r[0], "count": int(r[1])} for r in by_source],
        "top_tags":  [{"tag":    r[0], "count": int(r[1])} for r in top_tags],
        "score_distribution": {
            "count": int(score_dist[0]) if score_dist and score_dist[0] is not None else 0,
            "min":   _f(score_dist[1]) if score_dist else None,
            "max":   _f(score_dist[2]) if score_dist else None,
            "mean":  _f(score_dist[3]) if score_dist else None,
            "p25":   _f(score_dist[4]) if score_dist else None,
            "p50":   _f(score_dist[5]) if score_dist else None,
            "p75":   _f(score_dist[6]) if score_dist else None,
            "p95":   _f(score_dist[7]) if score_dist else None,
        },
    }


# ── POST /memory/revisions ───────────────────────────────────────────────────
@app.post("/memory/revisions", status_code=201)
def memory_revisions_create(body: RevisionRecord):
    """Internal endpoint — record a pre-update snapshot of a durable memory.

    Called by the embedder's PUT /api/memories/{id} BEFORE mutating the
    Qdrant payload. Enforces the "never silently overwrite" policy at the
    data layer: if this insert fails, the embedder refuses the update,
    so the audit trail can't be bypassed from any client.
    """
    slug = (body.tenant_slug or "default").strip() or "default"
    durable_id = (body.durable_id or "").strip()
    if not durable_id:
        raise HTTPException(status_code=400, detail="durable_id required")

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        row = conn.execute(text("""
            INSERT INTO memory_revisions
                (tenant_id, durable_id, content, type, tags, source,
                 snapshot_created_at, snapshot_updated_at,
                 edited_by, changed_fields)
            VALUES
                (:tenant_id, CAST(:durable_id AS uuid), :content, :type, :tags, :source,
                 CAST(:s_created AS timestamptz), CAST(:s_updated AS timestamptz),
                 :edited_by, :changed_fields)
            RETURNING id, edited_at
        """), {
            "tenant_id":      slug,
            "durable_id":     durable_id,
            "content":        body.snapshot.content,
            "type":           body.snapshot.type,
            "tags":           list(body.snapshot.tags or []),
            "source":         body.snapshot.source,
            "s_created":      body.snapshot.created_at,
            "s_updated":      body.snapshot.updated_at,
            "edited_by":      body.edited_by,
            "changed_fields": list(body.changed_fields or []),
        }).fetchone()
        conn.commit()
    return {
        "id":         int(row[0]),
        "durable_id": durable_id,
        "edited_at":  row[1].isoformat() if row[1] else None,
    }


# ── GET /memory/revisions/{durable_id} ───────────────────────────────────────
@app.get("/memory/revisions/{durable_id}")
def memory_revisions_list(
    durable_id: str,
    tenant_slug: str = Query("default"),
    limit: int = Query(50, ge=1, le=200),
):
    """List the edit history of a durable memory — every pre-update
    snapshot recorded against it, newest first.

    Snapshots reflect the state BEFORE each update. A memory that has
    never been edited returns count=0; that's expected, not an error.
    """
    import uuid as _uuid
    slug = (tenant_slug or "default").strip() or "default"
    try:
        durable_uuid = str(_uuid.UUID(durable_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"invalid uuid: {durable_id}")

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT id, content, type, tags, source,
                   snapshot_created_at, snapshot_updated_at,
                   edited_by, edited_at, changed_fields
            FROM memory_revisions
            WHERE durable_id = CAST(:did AS uuid)
            ORDER BY edited_at DESC
            LIMIT :limit
        """), {"did": durable_uuid, "limit": limit}).fetchall()

    return {
        "durable_id":  durable_uuid,
        "tenant_slug": slug,
        "count":       len(rows),
        "revisions": [
            {
                "id":                  int(r[0]),
                "content":             r[1],
                "type":                r[2],
                "tags":                list(r[3] or []),
                "source":              r[4],
                "snapshot_created_at": r[5].isoformat() if r[5] else None,
                "snapshot_updated_at": r[6].isoformat() if r[6] else None,
                "edited_by":           r[7],
                "edited_at":           r[8].isoformat() if r[8] else None,
                "changed_fields":      list(r[9] or []),
            }
            for r in rows
        ],
    }


@app.get("/memory/hot/diag")
def memory_hot_diag():
    """Diagnostics for the hot tier: reports raw row counts using the
    compactor_bypass policy plus the state of every memory_hot policy
    in pg_policies. Used to explain mysteries like "the admin UI shows
    captures but the compactor processes zero" — typically an RLS or
    SET-LOCAL transaction-scope issue. No auth here on purpose; the
    editor's /api/admin/memory/* proxy is what gates this externally.
    """
    engine = get_engine()
    out: dict = {}
    with engine.connect() as conn:
        # Bypass policy: prove the compactor *can* see rows when its
        # GUC is set. Same transaction as the SELECT — if SET LOCAL
        # is silently a no-op for any pool/transaction reason, this
        # will report 0 too.
        conn.execute(text("SET LOCAL app.tenant_id = '__compactor__'"))
        setting_now = conn.execute(text(
            "SELECT current_setting('app.tenant_id', true)"
        )).scalar()
        total = conn.execute(text(
            "SELECT count(*) FROM memory_hot"
        )).scalar() or 0
        pending_total = conn.execute(text(
            "SELECT count(*) FROM memory_hot "
            "WHERE promoted_to IS NULL AND dropped_at IS NULL"
        )).scalar() or 0
        per_tenant = conn.execute(text("""
            SELECT tenant_id,
                   count(*) AS total,
                   count(*) FILTER (WHERE promoted_to IS NULL AND dropped_at IS NULL) AS pending,
                   count(*) FILTER (WHERE promoted_to IS NOT NULL) AS promoted,
                   count(*) FILTER (WHERE dropped_at  IS NOT NULL) AS dropped
            FROM memory_hot
            GROUP BY tenant_id
            ORDER BY tenant_id
        """)).fetchall()
        policies = conn.execute(text("""
            SELECT policyname, permissive, cmd, qual
            FROM pg_policies
            WHERE tablename = 'memory_hot'
            ORDER BY policyname
        """)).fetchall()

        out["bypass_setting_observed"] = setting_now
        out["bypass_visible_total"]    = int(total)
        out["bypass_visible_pending"]  = int(pending_total)
        out["per_tenant"] = [
            {
                "tenant_id": r[0],
                "total":     int(r[1]),
                "pending":   int(r[2]),
                "promoted":  int(r[3]),
                "dropped":   int(r[4]),
            }
            for r in per_tenant
        ]
        out["policies"] = [
            {
                "name":       r[0],
                "permissive": r[1],
                "command":    r[2],
                "qual":       r[3],
            }
            for r in policies
        ]
    return out


# ── GET /memory/hot/recent ───────────────────────────────────────────────────
@app.get("/memory/hot/recent")
def memory_hot_recent(
    tenant_slug: str = Query("default"),
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("all", regex="^(all|pending|promoted|dropped)$"),
):
    """List recent rows in the hot tier for the admin UI. Filters by status:
    pending = not yet processed, promoted = passed compaction into durable,
    dropped = aged out below the score floor."""
    slug = (tenant_slug or "default").strip() or "default"
    where = ["tenant_id = :tenant_id"]
    if status == "pending":
        where.append("promoted_to IS NULL AND dropped_at IS NULL")
    elif status == "promoted":
        where.append("promoted_to IS NOT NULL")
    elif status == "dropped":
        where.append("dropped_at IS NOT NULL")
    where_sql = " AND ".join(where)

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text(f"""
            SELECT id, content, type_hint, tags, source, source_session_id,
                   captured_at, promoted_to, promoted_at, dropped_at, score
            FROM memory_hot
            WHERE {where_sql}
            ORDER BY captured_at DESC
            LIMIT :limit
        """), {"tenant_id": slug, "limit": limit}).fetchall()
    return {
        "tenant_slug": slug,
        "status":      status,
        "count":       len(rows),
        "rows": [
            {
                "id":                str(r[0]),
                "content":           r[1],
                "type_hint":         r[2],
                "tags":              list(r[3] or []),
                "source":            r[4],
                "source_session_id": r[5],
                "captured_at":       r[6].isoformat() if r[6] else None,
                "promoted_to":       str(r[7]) if r[7] else None,
                "promoted_at":       r[8].isoformat() if r[8] else None,
                "dropped_at":        r[9].isoformat() if r[9] else None,
                "score":             float(r[10]) if r[10] is not None else None,
            }
            for r in rows
        ],
    }


# ── GET /memory/hot/by-ids ───────────────────────────────────────────────────
@app.get("/memory/hot/by-ids")
def memory_hot_by_ids(
    tenant_slug: str = Query("default"),
    ids: str = Query(..., description="Comma-separated hot-tier UUIDs"),
):
    """Fetch hot-tier rows by id (any status — pending, promoted, or dropped).

    Used by the memory_provenance MCP tool to surface the original capture
    text behind a durable memory's `originated_from_hot_ids` list. Hot rows
    survive promotion (they're marked, not deleted), so the original content
    is still retrievable. Returns only rows that exist + are visible under
    the caller's tenant RLS — missing ids are simply absent from the response.
    """
    import uuid as _uuid
    slug = (tenant_slug or "default").strip() or "default"
    raw_ids = [s.strip() for s in (ids or "").split(",") if s.strip()]
    parsed_ids: list[str] = []
    for raw in raw_ids:
        try:
            parsed_ids.append(str(_uuid.UUID(raw)))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail=f"invalid uuid: {raw}")
    if not parsed_ids:
        return {"tenant_slug": slug, "count": 0, "rows": []}
    if len(parsed_ids) > 200:
        raise HTTPException(status_code=400, detail="too many ids (max 200)")

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT id, content, type_hint, tags, source, source_session_id,
                   captured_at, promoted_to, promoted_at, dropped_at, score
            FROM memory_hot
            WHERE id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": parsed_ids}).fetchall()
    return {
        "tenant_slug": slug,
        "count":       len(rows),
        "rows": [
            {
                "id":                str(r[0]),
                "content":           r[1],
                "type_hint":         r[2],
                "tags":              list(r[3] or []),
                "source":            r[4],
                "source_session_id": r[5],
                "captured_at":       r[6].isoformat() if r[6] else None,
                "promoted_to":       str(r[7]) if r[7] else None,
                "promoted_at":       r[8].isoformat() if r[8] else None,
                "dropped_at":        r[9].isoformat() if r[9] else None,
                "score":             float(r[10]) if r[10] is not None else None,
            }
            for r in rows
        ],
    }


# ── GET /memory/hot/by-session/{session_id} ──────────────────────────────────
@app.get("/memory/hot/by-session/{session_id}")
def memory_hot_by_session(
    session_id: str,
    tenant_slug: str = Query("default"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Fetch hot-tier rows captured under one session (any status — pending,
    promoted, or dropped). Used by the memory_session MCP tool to assemble
    a session diary alongside the durable side.

    Hot rows persist after promotion (marked, not deleted), so the original
    captures remain retrievable indefinitely — useful for "what did I see
    in session X?" even if the captures have all been promoted into durable.
    """
    slug = (tenant_slug or "default").strip() or "default"
    sid  = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT id, content, type_hint, tags, source, source_session_id,
                   captured_at, promoted_to, promoted_at, dropped_at, score
            FROM memory_hot
            WHERE source_session_id = :sid
            ORDER BY captured_at ASC
            LIMIT :limit
        """), {"sid": sid, "limit": limit}).fetchall()

    return {
        "tenant_slug": slug,
        "session_id":  sid,
        "count":       len(rows),
        "rows": [
            {
                "id":                str(r[0]),
                "content":           r[1],
                "type_hint":         r[2],
                "tags":              list(r[3] or []),
                "source":            r[4],
                "source_session_id": r[5],
                "captured_at":       r[6].isoformat() if r[6] else None,
                "promoted_to":       str(r[7]) if r[7] else None,
                "promoted_at":       r[8].isoformat() if r[8] else None,
                "dropped_at":        r[9].isoformat() if r[9] else None,
                "score":             float(r[10]) if r[10] is not None else None,
            }
            for r in rows
        ],
    }


# ── GET /memory/hot/search ───────────────────────────────────────────────────
@app.get("/memory/hot/search")
def memory_hot_search(
    tenant_slug: str = Query("default"),
    q:           str = Query("", description="Free-text query"),
    tags:        str = Query("", description="Comma-separated tags (AND-filter)"),
    limit:       int = Query(10, ge=1, le=100),
    max_age_hours: float = Query(72.0, ge=0.0, le=720.0,
                                  description="Only return captures younger than this"),
):
    """Lexical (Postgres FTS) search over the hot tier. Designed for the
    short-term-recall path: agents capture aggressively via /memory/capture
    and need to query what they just learned without waiting for the
    compactor's 15-minute window.

    Returns pending rows ranked by ts_rank (against the GIN-indexed
    tsvector over content + tags) blended with a recency boost so a
    capture from 30 seconds ago beats one from 30 hours ago when both
    match the query equally. promoted/dropped rows are excluded — once
    they're in the durable tier, the embedder's /api/memories/search
    handles them.
    """
    slug = (tenant_slug or "default").strip() or "default"
    query_text = (q or "").strip()
    if not query_text:
        return {"tenant_slug": slug, "query": "", "count": 0, "results": []}

    tag_filter = [t.strip() for t in (tags or "").split(",") if t.strip()]

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        # plainto_tsquery handles user input safely (treats it as a
        # sequence of search words, no operator-injection surface).
        # The rank blends FTS score with an exponential recency decay so
        # fresh captures float to the top; clamp at 1.0 so the recency
        # boost can't fabricate matches with no lexical overlap.
        sql = """
            WITH base AS (
                SELECT id, content, type_hint, tags, source, source_session_id,
                       captured_at, score,
                       ts_rank_cd(
                         to_tsvector('english',
                           coalesce(content,'') || ' ' ||
                           coalesce(array_to_string(tags,' '),'')
                         ),
                         plainto_tsquery('english', :q)
                       ) AS lex_score,
                       extract(epoch FROM (now() - captured_at)) / 3600.0 AS age_hours
                FROM memory_hot
                WHERE tenant_id = :tenant_id
                  AND promoted_to IS NULL
                  AND dropped_at  IS NULL
                  AND captured_at > now() - (:max_age_hours || ' hours')::interval
                  AND to_tsvector('english',
                        coalesce(content,'') || ' ' ||
                        coalesce(array_to_string(tags,' '),'')
                      ) @@ plainto_tsquery('english', :q)
                  AND (:has_tags = false OR tags @> :tag_filter)
            )
            SELECT *,
                   lex_score * (1.0 + 0.5 * exp(-age_hours / 6.0)) AS blended_score
            FROM base
            ORDER BY blended_score DESC, captured_at DESC
            LIMIT :limit
        """
        rows = conn.execute(text(sql), {
            "tenant_id":     slug,
            "q":             query_text,
            "tag_filter":    tag_filter,
            "has_tags":      bool(tag_filter),
            "max_age_hours": max_age_hours,
            "limit":         limit,
        }).fetchall()

    return {
        "tenant_slug": slug,
        "query":       query_text,
        "count":       len(rows),
        "results": [
            {
                "id":                str(r[0]),
                "content":           r[1],
                "type_hint":         r[2],
                "tags":              list(r[3] or []),
                "source":            r[4],
                "source_session_id": r[5],
                "captured_at":       r[6].isoformat() if r[6] else None,
                "compactor_score":   float(r[7]) if r[7] is not None else None,
                "lex_score":         float(r[8]) if r[8] is not None else 0.0,
                "age_hours":         float(r[9]) if r[9] is not None else 0.0,
                "score":             float(r[10]) if r[10] is not None else 0.0,
                "tier":              "hot",
            }
            for r in rows
        ],
    }


class HotUpdate(BaseModel):
    """Body for PUT /memory/hot/{row_id} — in-place correction of a PENDING
    hot capture. Promoted/dropped rows are settled history and stay
    immutable (the durable tier owns post-promotion edits, with its
    memory_revisions audit trail)."""
    content: Optional[str] = Field(default=None, max_length=MAX_CONTENT_CHARS)
    tags: Optional[list[ShortStr]] = Field(default=None, max_length=MAX_LIST_ITEMS)
    type_hint: Optional[ShortStr] = None
    tenant_slug: str = "default"


# ── PUT /memory/hot/{row_id} ─────────────────────────────────────────────────
@app.put("/memory/hot/{row_id}")
async def memory_hot_update(row_id: str, body: HotUpdate):
    """Edit a pending hot-tier capture in place — content, tags, and/or
    type_hint. Gives memory_update two-tier symmetry: agents can correct a
    bad capture immediately instead of waiting for the compactor to
    reconcile it.

    Only PENDING rows are editable. Promoted rows 409 with the durable id
    (edit that instead — it has the revision audit trail); dropped rows 409.
    Content edits recompute dedup_hash so the 60s short-window dedup keeps
    working against the corrected text. No revision row is written — the
    hot tier is pre-compaction scratch; the "corrections preserve history"
    policy applies to durable memories.
    """
    slug = (body.tenant_slug or "default").strip() or "default"
    if body.content is None and body.tags is None and body.type_hint is None:
        raise HTTPException(status_code=400,
                            detail="provide at least one of content, tags, type_hint")
    if body.content is not None and not body.content.strip():
        raise HTTPException(status_code=400, detail="content cannot be blank")

    def _update():
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
            r = conn.execute(text("""
                SELECT id, content, type_hint, tags, promoted_to, dropped_at
                FROM memory_hot WHERE id = :id
            """), {"id": row_id}).fetchone()
            if not r:
                return ("not_found", None)
            if r[4]:
                return ("promoted", str(r[4]))
            if r[5]:
                return ("dropped", None)

            new_content = body.content if body.content is not None else r[1]
            new_type    = ((body.type_hint or "").strip().lower() or None
                           if body.type_hint is not None else r[2])
            new_tags    = list(body.tags) if body.tags is not None else list(r[3] or [])
            conn.execute(text("""
                UPDATE memory_hot
                SET content = :content, type_hint = :type_hint, tags = :tags,
                    dedup_hash = :dh
                WHERE id = :id
            """), {
                "content":   new_content,
                "type_hint": new_type,
                "tags":      new_tags,
                "dh":        _dedup_hash(new_content),
                "id":        row_id,
            })
            conn.commit()
            return ("ok", {
                "id":        row_id,
                "content":   new_content,
                "type_hint": new_type,
                "tags":      new_tags,
            })

    status, payload = await asyncio.to_thread(_update)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="hot row not found")
    if status == "promoted":
        raise HTTPException(
            status_code=409,
            detail=f"row already promoted to durable {payload}; edit that memory instead",
        )
    if status == "dropped":
        raise HTTPException(status_code=409, detail="row was dropped by the compactor")
    log.info("hot-update row=%s tenant=%s", row_id, slug)
    return {"status": "updated", "tier": "hot", **payload}


# ── POST /memory/hot/{row_id}/promote ────────────────────────────────────────
@app.post("/memory/hot/{row_id}/promote")
async def memory_hot_promote(row_id: str, tenant_slug: str = Query("default")):
    """Hand-promote a single hot row to durable. Bypasses the compactor's
    score threshold and existing-durable dedup check — admin is explicitly
    overriding. Tags the resulting durable memory with `hand-promoted` so
    the provenance is honest."""
    slug = (tenant_slug or "default").strip() or "default"

    def _fetch():
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
            r = conn.execute(text("""
                SELECT id, content, type_hint, tags, source, promoted_to, dropped_at,
                       source_session_id, captured_at
                FROM memory_hot WHERE id = :id
            """), {"id": row_id}).fetchone()
            return r

    row = await asyncio.to_thread(_fetch)
    if not row:
        raise HTTPException(status_code=404, detail="hot row not found")
    if row[5]:
        raise HTTPException(status_code=409,
                            detail=f"already promoted to {row[5]}")
    if row[6]:
        raise HTTPException(status_code=409, detail="row was dropped")

    tags = list(row[3] or [])
    if "compacted" not in tags:
        tags.append("compacted")
    if "hand-promoted" not in tags:
        tags.append("hand-promoted")
    type_ = (row[2] or "fact").lower()
    source = row[4] or "unknown"
    hot_session_id = row[7]
    hot_captured_at = row[8]

    async with httpx.AsyncClient(timeout=60) as client:
        new_id = await _promote_durable(
            client, row[1], tags, type_, source, slug,
            hot_ids=[row_id],
            session_ids=[hot_session_id] if hot_session_id else None,
            # 999.0 matches the score used by _mark_promoted_sync below for
            # hand-promotes — keeps the durable provenance and the hot tier
            # record agreeing on the value.
            compactor_score=999.0,
            oldest_captured_at=hot_captured_at,
        )
    if not new_id:
        raise HTTPException(status_code=502, detail="promote failed at embedder")

    await asyncio.to_thread(
        _mark_promoted_sync, slug, [row_id], new_id, 999.0
    )
    log.info("hand-promote row=%s -> durable=%s tenant=%s", row_id, new_id, slug)
    return {"id": row_id, "promoted_to": new_id, "status": "promoted"}


# ── POST /memory/hot/{row_id}/drop ───────────────────────────────────────────
@app.post("/memory/hot/{row_id}/drop")
async def memory_hot_drop(row_id: str, tenant_slug: str = Query("default")):
    """Hand-drop a single hot row. The compactor will not consider it again."""
    slug = (tenant_slug or "default").strip() or "default"

    def _fetch_and_drop():
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
            r = conn.execute(text("""
                SELECT promoted_to, dropped_at FROM memory_hot WHERE id = :id
            """), {"id": row_id}).fetchone()
            if not r:
                return None
            if r[0]:
                return "already_promoted"
            if r[1]:
                return "already_dropped"
            conn.execute(text("""
                UPDATE memory_hot SET dropped_at = now() WHERE id = :id
            """), {"id": row_id})
            conn.commit()
            return "dropped"

    result = await asyncio.to_thread(_fetch_and_drop)
    if result is None:
        raise HTTPException(status_code=404, detail="hot row not found")
    if result.startswith("already_"):
        raise HTTPException(status_code=409, detail=result)
    log.info("hand-drop row=%s tenant=%s", row_id, slug)
    return {"id": row_id, "status": "dropped"}


# ── Git source lexical index ─────────────────────────────────────────────────
# Write/delete is driven by the embedder's git-source sync; read/search is the
# agent-facing path exposed through the MCP server's git_search / git_read.
class GitIndexRequest(BaseModel):
    source_id:   str
    path:        str
    content:     str
    source_name: str = ""
    lang:        Optional[str] = None
    tenant_slug: str = "default"


@app.post("/git/index", status_code=201)
def git_index(req: GitIndexRequest):
    """Upsert one git-source file into the lexical index (verbatim content)."""
    if not req.source_id or not req.path:
        raise HTTPException(status_code=400, detail="source_id and path are required")
    slug = (req.tenant_slug or "default").strip() or "default"
    content = req.content or ""
    line_count = content.count("\n") + 1 if content else 0
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        conn.execute(text("""
            INSERT INTO git_files
                (tenant_id, source_id, source_name, path, lang,
                 size_bytes, line_count, content, indexed_at)
            VALUES
                (:tenant_id, :source_id, :source_name, :path, :lang,
                 :size_bytes, :line_count, :content, now())
            ON CONFLICT (source_id, path) DO UPDATE SET
                source_name = EXCLUDED.source_name,
                lang        = EXCLUDED.lang,
                size_bytes  = EXCLUDED.size_bytes,
                line_count  = EXCLUDED.line_count,
                content     = EXCLUDED.content,
                indexed_at  = now()
        """), {
            "tenant_id":   slug,
            "source_id":   req.source_id,
            "source_name": req.source_name or "",
            "path":        req.path,
            "lang":        req.lang,
            "size_bytes":  len(content.encode("utf-8")),
            "line_count":  line_count,
            "content":     content,
        })
        conn.commit()
    return {"status": "ok", "source_id": req.source_id, "path": req.path, "lines": line_count}


@app.delete("/git/file")
def git_delete_file(
    source_id:   str = Query(...),
    path:        str = Query(...),
    tenant_slug: str = Query("default"),
):
    """Drop one file from the lexical index (repo file deleted/renamed)."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        res = conn.execute(text(
            "DELETE FROM git_files WHERE source_id = :source_id AND path = :path"
        ), {"source_id": source_id, "path": path})
        conn.commit()
    return {"status": "ok", "deleted": res.rowcount}


@app.delete("/git/source")
def git_delete_source(
    source_id:   str = Query(...),
    tenant_slug: str = Query("default"),
):
    """Drop every file for a source (force-rebuild or source removed)."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        res = conn.execute(text(
            "DELETE FROM git_files WHERE source_id = :source_id"
        ), {"source_id": source_id})
        conn.commit()
    return {"status": "ok", "deleted": res.rowcount}


@app.get("/git/stats")
def git_stats(
    source_id:   str = Query("", description="Restrict to one source (optional)"),
    tenant_slug: str = Query("default"),
):
    """How many files this tenant actually has in the lexical index.

    The embedder's sync needs to tell two states apart that look identical
    from the git side: "the clone is current and its files are indexed" and
    "the clone is current and the index holds nothing for it". A diff against
    the previous HEAD is empty either way, so a source in the second state
    used to report a successful sync forever while serving no search results
    (issue #136). This is the question that separates them.

    With no source_id, returns per-source counts for the whole tenant.
    """
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT source_id, count(*) AS files
            FROM git_files
            WHERE tenant_id = :tenant_id
              AND (:source_id = '' OR source_id = :source_id)
            GROUP BY source_id
        """), {"tenant_id": slug, "source_id": source_id or ""}).fetchall()
    by_source = {r[0]: int(r[1]) for r in rows}
    if source_id:
        # An unindexed source has no rows to group, so it is absent rather
        # than zero. Say 0 explicitly, because the caller is asking about this one
        # source and "no row" is the answer it most needs to act on.
        return {"tenant_slug": slug, "source_id": source_id,
                "files": by_source.get(source_id, 0)}
    return {"tenant_slug": slug, "files": sum(by_source.values()),
            "by_source": by_source}


@app.get("/git/manifest")
def git_manifest(
    source_id:   str = Query(...),
    tenant_slug: str = Query("default"),
):
    """Every indexed path for one source, with the md5 of its stored content.

    The embedder's sync compares this to the checkout and re-indexes whatever
    differs. A diff between two HEADs only says what changed in the repo; it
    cannot say what the index missed, so a file that failed to index once
    stayed stale through every later sync (issue #269). Comparing content
    hashes answers the question the sync actually needs answered.
    """
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT path, md5(content)
            FROM git_files
            WHERE tenant_id = :tenant_id AND source_id = :source_id
        """), {"tenant_id": slug, "source_id": source_id}).fetchall()
    return {"tenant_slug": slug, "source_id": source_id,
            "files": {r[0]: r[1] for r in rows}}


@app.get("/git/search")
def git_search(
    tenant_slug:   str = Query("default"),
    q:             str = Query("", description="Keywords / identifiers / substrings"),
    source_id:     str = Query("", description="Restrict to one source (optional)"),
    limit:         int = Query(20, ge=1, le=100),
    context:       int = Query(2, ge=0, le=10, description="Context lines around each hit"),
    max_line_hits: int = Query(5, ge=1, le=50),
):
    """Lexical (Postgres FTS + trigram) search over synced git-source files.

    Ranks files by ts_rank_cd; a trigram ILIKE fallback catches substring /
    partial-identifier matches that token FTS misses. For each matching file
    we then scan its lines in Python to return grep-style line numbers and
    surrounding context — the precise hook an agent needs before git_read.
    """
    slug = (tenant_slug or "default").strip() or "default"
    query_text = (q or "").strip()
    if not query_text:
        return {"tenant_slug": slug, "query": "", "count": 0, "results": []}

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        rows = conn.execute(text("""
            SELECT source_id, source_name, path, lang, line_count, content,
                   ts_rank_cd(
                     to_tsvector('simple', content),
                     websearch_to_tsquery('simple', :q)
                   ) AS rank
            FROM git_files
            WHERE tenant_id = :tenant_id
              AND (:source_id = '' OR source_id = :source_id)
              AND (
                    to_tsvector('simple', content) @@ websearch_to_tsquery('simple', :q)
                    OR content ILIKE :like
                  )
            ORDER BY rank DESC, path
            LIMIT :limit
        """), {
            "tenant_id": slug,
            "q":         query_text,
            "source_id": source_id or "",
            "like":      "%" + query_text + "%",
            "limit":     limit,
        }).fetchall()

    terms = [t for t in re.split(r"\W+", query_text.lower()) if t]
    ql = query_text.lower()
    results = []
    for r in rows:
        content = r[5] or ""
        lines = content.split("\n")
        hits = []
        for idx, line in enumerate(lines):
            ll = line.lower()
            if ql in ll or any(t in ll for t in terms):
                start = max(0, idx - context)
                end = min(len(lines), idx + context + 1)
                hits.append({
                    "line":    idx + 1,
                    "snippet": "\n".join(lines[start:end]),
                })
                if len(hits) >= max_line_hits:
                    break
        results.append({
            "source_id":   r[0],
            "source_name": r[1],
            "path":        r[2],
            "lang":        r[3],
            "line_count":  r[4],
            "rank":        float(r[6]) if r[6] is not None else 0.0,
            "line_hits":   hits,
            "tier":        "git",
        })
    return {"tenant_slug": slug, "query": query_text, "count": len(results), "results": results}


@app.get("/git/read")
def git_read(
    source_id:   str = Query(...),
    path:        str = Query(...),
    tenant_slug: str = Query("default"),
    start:       int = Query(1, ge=1),
    end:         int = Query(0, ge=0, description="0 = end of file"),
):
    """Return a line span of an indexed git-source file (line-numbered)."""
    slug = (tenant_slug or "default").strip() or "default"
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :slug, true)"), {"slug": slug})
        row = conn.execute(text("""
            SELECT source_name, lang, content
            FROM git_files
            WHERE source_id = :source_id AND path = :path
        """), {"source_id": source_id, "path": path}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="file not indexed")

    content = row[2] or ""
    lines = content.split("\n")
    total = len(lines)
    s = max(1, start)
    e = end if (end and end >= s) else total
    e = min(e, total)
    span = lines[s - 1:e]
    numbered = "\n".join(f"{s + i:>6}\t{ln}" for i, ln in enumerate(span))
    return {
        "source_id":   source_id,
        "source_name": row[0],
        "path":        path,
        "lang":        row[1],
        "line_count":  total,
        "start":       s,
        "end":         e,
        "content":     "\n".join(span),
        "numbered":    numbered,
    }


# ── POST /memory/store  [REMOVED] ────────────────────────────────────────────
# Both /memory/store and /memory/search wrote to / read from the SHARED
# `knowledge_base` Qdrant collection with NO tenant tag in the payload.
# Anything written through them became globally visible to anyone hitting
# /memory/search regardless of tenant — a cross-tenant primitive disguised
# as a deprecated path. Returning 410 Gone instead of silently keeping
# them live; callers should use the embedder's tenant-scoped
# /api/memories endpoints.
@app.post("/memory/store")
async def memory_store_removed(*_args, **_kwargs):
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /memory/store has been removed because it bypassed "
            "tenant isolation. Use POST /api/memories on the embedder "
            "(scoped by the X-Auth-Org-Slug header from your session, "
            "or by tenant_slug for internal callers carrying the "
            "X-Internal-Service token)."
        ),
    )


# ── POST /memory/search  [REMOVED] ───────────────────────────────────────────
@app.post("/memory/search")
async def memory_search_removed(*_args, **_kwargs):
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /memory/search has been removed because it bypassed "
            "tenant isolation. Use GET /api/memories/search on the "
            "embedder (scoped by the X-Auth-Org-Slug header from your "
            "session)."
        ),
    )


# ── GET /events ───────────────────────────────────────────────────────────────
@app.get("/events")
def get_events(
    limit: int = Query(20, ge=1, le=200),
    type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    tenant_slug: str = Query("default"),
):
    slug = tenant_slug or "default"
    filters, params = [], {"limit": limit, "tenant_id": slug}
    filters.append("tenant_id = :tenant_id")
    if type:
        filters.append("type = :type")
        params["type"] = type
    if since:
        try:
            params["since"] = dateutil_parser.parse(since)
            filters.append("created >= :since")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'since' timestamp")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": slug})
        rows = conn.execute(
            text(f"""
                SELECT id, type, content, source, tags, metadata, created
                FROM events {where}
                ORDER BY created DESC
                LIMIT :limit
            """),
            params,
        ).fetchall()

    return {
        "events": [
            {
                "id":       r[0],
                "type":     r[1],
                "content":  r[2],
                "source":   r[3],
                "tags":     r[4] or [],
                "metadata": r[5] or {},
                "created":  r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
    }


# ── DELETE /tenant/{slug} ─────────────────────────────────────────────────────
@app.delete("/tenant/{slug}")
async def purge_tenant(slug: str):
    """
    Hard-delete every RLS-scoped row belonging to a tenant.  Called by the
    editor's org-purge endpoint after a tenant has been offboarded long
    enough to be unrecoverable.  The internal-service auth middleware gates
    access to this endpoint.

    Sets the RLS GUC before the DELETEs so the FORCEd row-level security
    policy actually evaluates correctly (without the GUC,
    current_setting('app.tenant_id') is NULL and the policy lets nothing
    through — the previous version silently deleted zero rows).  Purges all
    four tenant-scoped tables so a re-created tenant with the same slug
    cannot inherit stale state from any of them.
    """
    deleted = {"events": 0, "memory_hot": 0, "memory_revisions": 0, "git_files": 0}
    with get_engine().connect() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :slug, true)"),
            {"slug": slug},
        )
        for table in deleted.keys():
            try:
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :slug"),
                    {"slug": slug},
                )
                deleted[table] = result.rowcount
            except Exception as exc:
                # A table that doesn't exist on a given deployment (e.g. a
                # schema migration not yet applied) shouldn't abort the
                # whole purge — surface it but keep going.
                log.warning("purge_tenant(%s): DELETE FROM %s failed: %s",
                            slug, table, exc)
        conn.commit()
    return {"tenant_slug": slug, "deleted_rows": deleted}


# ── Org BCDR export / import ───────────────────────────────────────────────────
# Board + the hot memory tier + memory revisions live in THIS service's
# Postgres, so the editor's org snapshot pulls them through these internal
# endpoints (it can't reach this DB directly). Rows are tenant-scoped by RLS
# (app.tenant_id = the tenant slug) and an explicit tenant_id filter.

_BCDR_TABLES = ("board_columns", "cards", "card_links", "card_comments",
                "memory_hot", "memory_revisions")


def _bcdr_jsonable(row: dict) -> dict:
    """A row mapping → JSON-safe dict (UUID → str, datetime → isoformat)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _bcdr_ts(v):
    """Parse an exported timestamp back to a datetime (or None)."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@app.get("/internal/org-export")
def org_export(tenant_slug: str = "default"):
    """Export this tenant's Postgres-resident org data (boards, hot memories,
    memory revisions) for the platform org snapshot."""
    slug = (tenant_slug or "default").strip() or "default"
    tables: dict = {}
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        for tbl in _BCDR_TABLES:
            rows = conn.execute(
                text(f"SELECT * FROM {tbl} WHERE tenant_id = :t"), {"t": slug}
            ).mappings().all()
            tables[tbl] = [_bcdr_jsonable(dict(r)) for r in rows]
    return {"tenant_slug": slug,
            "counts": {k: len(v) for k, v in tables.items()},
            "tables": tables}


class OrgImportRequest(BaseModel):
    tenant_slug: str = "default"
    tables: dict = {}


def _bcdr_insert_columns(conn, slug, rows) -> int:
    for r in rows:
        conn.execute(text("""
            INSERT INTO board_columns
                (id, tenant_id, name, stage, position, on_enter, color, created_at, archived_at)
            VALUES (:id, :t, :name, :stage, :position, :on_enter, :color, :created_at, :archived_at)
        """), {
            "id": r.get("id"), "t": slug, "name": r.get("name"), "stage": r.get("stage"),
            "position": r.get("position"), "on_enter": r.get("on_enter"), "color": r.get("color"),
            "created_at": _bcdr_ts(r.get("created_at")), "archived_at": _bcdr_ts(r.get("archived_at")),
        })
    return len(rows)


def _bcdr_insert_cards(conn, slug, rows) -> int:
    for r in rows:
        conn.execute(text("""
            INSERT INTO cards
                (id, tenant_id, column_id, slug, title, body, aliases, labels, repo,
                 reporter, assignee, due, participants, wiki_links, attachments, position, source,
                 dispatch_ref, number,
                 created_at, updated_at, archived_at)
            VALUES (:id, :t, :column_id, :slug, :title, :body,
                    CAST(:aliases AS text[]), CAST(:labels AS text[]),
                    :repo, :reporter, :assignee, :due, CAST(:participants AS text[]),
                    CAST(:wiki_links AS text[]), CAST(:attachments AS text[]),
                    :position, :source, :dispatch_ref, :number,
                    :created_at, :updated_at, :archived_at)
        """), {
            "id": r.get("id"), "t": slug, "column_id": r.get("column_id"),
            "slug": r.get("slug"), "title": r.get("title"), "body": r.get("body") or "",
            "aliases": r.get("aliases") or [],
            # A snapshot taken before tags and labels merged carries both
            # arrays. Merge on the way in rather than restoring `labels` alone:
            # dropping the other column would quietly lose every marker and
            # classification the backup holds, which is the one failure a
            # restore must not have.
            "labels": _clean_tags(list(r.get("labels") or []) + list(r.get("tags") or [])),
            "repo": r.get("repo") or "",
            # Restored as stored, with NO revalidation against the directory.
            # A restore is history, not a new write: refusing a card because
            # the person who raised it has since left would edit the past to
            # make it pass today's gate.
            "reporter": r.get("reporter") or "",
            # A backup taken before this column existed simply has no assignee
            # to restore, and "" is exactly what that means.
            "assignee": r.get("assignee") or "",
            # Exported as an ISO string; one taken before the column existed
            # has none, which is what NULL means.
            "due": _match_due(r.get("due")) if r.get("due") else None,
            "participants": r.get("participants") or [],
            "wiki_links": r.get("wiki_links") or [],
            # A backup taken before this column existed simply has none.
            "attachments": r.get("attachments") or [],
            # `eval_lock_run` / `eval_lock_at` are DELIBERATELY not restored,
            # and this is the one card field the "a restore is history" rule
            # above does not cover. A lock is not history, it is a live claim by
            # a reviewer run, and by the time anybody is restoring a snapshot
            # that run is definitionally not running. Restoring it would bring a
            # card back showing "under evaluation" to a human, held by a run
            # that can never release it, until the staleness ceiling expired.
            # The column defaults leave every restored card unlocked, which is
            # both true and the safe direction. The export carries the columns
            # (it is SELECT *), so do not "fix" this by adding them here.
            "position": r.get("position"),
            "source": r.get("source") or "unknown", "dispatch_ref": r.get("dispatch_ref"),
            "number": r.get("number"),
            "created_at": _bcdr_ts(r.get("created_at")), "updated_at": _bcdr_ts(r.get("updated_at")),
            "archived_at": _bcdr_ts(r.get("archived_at")),
        })
    # Restore the counter alongside the cards, or the next card created after a
    # restore would be handed #1 again and collide with what was just restored.
    # Derived from the restored rows rather than trusting a separately exported
    # counter, so an older backup that predates this field still lands correct.
    conn.execute(text("""
        INSERT INTO card_number_seq (tenant_id, last_number)
        SELECT :t, COALESCE(MAX(number), 0) FROM cards WHERE tenant_id = :t
        ON CONFLICT (tenant_id) DO UPDATE
            SET last_number = GREATEST(card_number_seq.last_number, EXCLUDED.last_number)
    """), {"t": slug})
    return len(rows)


def _bcdr_insert_links(conn, slug, rows) -> int:
    """Restore the links between cards. Must run AFTER the cards, since the edge
    references both ends.

    A link whose other end is missing from the snapshot is skipped rather than
    failing the restore: an older backup taken before this table existed, or one
    hand-edited, should still come back as the cards it does have.
    """
    restored = 0
    for r in rows:
        try:
            conn.execute(text("""
                INSERT INTO card_links (id, tenant_id, src_id, dst_id, kind, source, created_at)
                SELECT :id, :t, CAST(:src AS uuid), CAST(:dst AS uuid), :kind, :source, :created_at
                WHERE EXISTS (SELECT 1 FROM cards WHERE tenant_id = :t AND id = CAST(:src AS uuid))
                  AND EXISTS (SELECT 1 FROM cards WHERE tenant_id = :t AND id = CAST(:dst AS uuid))
                ON CONFLICT (tenant_id, kind, src_id, dst_id) DO NOTHING
            """), {
                "id": r.get("id") or str(uuid.uuid4()), "t": slug,
                "src": r.get("src_id"), "dst": r.get("dst_id"),
                "kind": r.get("kind") or "related", "source": r.get("source") or "unknown",
                "created_at": _bcdr_ts(r.get("created_at")),
            })
            restored += 1
        except Exception as exc:
            log.warning("bcdr: skipped a card link (%s → %s): %s",
                        r.get("src_id"), r.get("dst_id"), exc)
    return restored


def _bcdr_insert_comments(conn, slug, rows) -> int:
    """Restore a card's thread. AFTER the cards, since a comment references one.

    A comment whose card is missing from the snapshot is skipped rather than
    failing the restore, same as a dangling link: an older backup should still
    come back as the cards it does have.
    """
    restored = 0
    for r in rows:
        try:
            conn.execute(text("""
                INSERT INTO card_comments
                    (id, tenant_id, card_id, author, body, source,
                     created_at, updated_at, deleted_at)
                SELECT :id, :t, CAST(:card AS uuid), :author, :body, :source,
                       :created_at, :updated_at, :deleted_at
                WHERE EXISTS (SELECT 1 FROM cards WHERE tenant_id = :t AND id = CAST(:card AS uuid))
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": r.get("id") or str(uuid.uuid4()), "t": slug,
                "card": r.get("card_id"),
                # Restored as stored, with no revalidation against the
                # directory: a restore is history, and refusing a comment
                # because its author has since left would edit the past.
                "author": r.get("author") or "",
                "body": r.get("body") or "",
                "source": r.get("source") or "unknown",
                "created_at": _bcdr_ts(r.get("created_at")),
                "updated_at": _bcdr_ts(r.get("updated_at")),
                "deleted_at": _bcdr_ts(r.get("deleted_at")),
            })
            restored += 1
        except Exception as exc:
            log.warning("bcdr: skipped a comment on card %s: %s", r.get("card_id"), exc)
    return restored


def _bcdr_insert_hot(conn, slug, rows) -> int:
    for r in rows:
        conn.execute(text("""
            INSERT INTO memory_hot
                (id, tenant_id, content, type_hint, tags, scope_hint, source, source_session_id,
                 captured_at, dedup_hash, promoted_to, promoted_at, dropped_at, score)
            VALUES (:id, :t, :content, :type_hint, CAST(:tags AS text[]), CAST(:scope_hint AS jsonb),
                    :source, :source_session_id, :captured_at, :dedup_hash, :promoted_to,
                    :promoted_at, :dropped_at, :score)
        """), {
            "id": r.get("id"), "t": slug, "content": r.get("content"),
            "type_hint": r.get("type_hint"), "tags": r.get("tags") or [],
            "scope_hint": json.dumps(r.get("scope_hint") or {}),
            "source": r.get("source") or "unknown", "source_session_id": r.get("source_session_id"),
            "captured_at": _bcdr_ts(r.get("captured_at")), "dedup_hash": r.get("dedup_hash") or "",
            "promoted_to": r.get("promoted_to"), "promoted_at": _bcdr_ts(r.get("promoted_at")),
            "dropped_at": _bcdr_ts(r.get("dropped_at")), "score": r.get("score"),
        })
    return len(rows)


def _bcdr_insert_revisions(conn, slug, rows) -> int:
    for r in rows:
        # id is BIGSERIAL and globally scoped — let it auto-assign to avoid a
        # cross-tenant PK collision; durable_id is what links the revision.
        conn.execute(text("""
            INSERT INTO memory_revisions
                (tenant_id, durable_id, content, type, tags, source,
                 snapshot_created_at, snapshot_updated_at, edited_by, edited_at, changed_fields)
            VALUES (:t, :durable_id, :content, :type, CAST(:tags AS text[]), :source,
                    :snapshot_created_at, :snapshot_updated_at, :edited_by, :edited_at,
                    CAST(:changed_fields AS text[]))
        """), {
            "t": slug, "durable_id": r.get("durable_id"), "content": r.get("content"),
            "type": r.get("type"), "tags": r.get("tags") or [], "source": r.get("source"),
            "snapshot_created_at": _bcdr_ts(r.get("snapshot_created_at")),
            "snapshot_updated_at": _bcdr_ts(r.get("snapshot_updated_at")),
            "edited_by": r.get("edited_by"), "edited_at": _bcdr_ts(r.get("edited_at")),
            "changed_fields": r.get("changed_fields") or [],
        })
    return len(rows)


@app.post("/internal/org-import")
def org_import(req: OrgImportRequest):
    """Restore this tenant's boards + hot memories + revisions from a snapshot.

    ALWAYS point-in-time: the tenant's rows are deleted first, so the board
    becomes the backup and anything created since it was taken is gone. The
    caller has to mean that — the editor makes restoring boards an explicit,
    separately-chosen area for exactly this reason.

    There is no merge mode, and that is a decision rather than an omission:
    the inserts below carry no conflict handling, and cards hold unique
    (tenant, slug) and (tenant, number) indexes, so upserting a backup over a
    live board would collide on a card whose slug was reused and abort the
    import half-done. A correct merge means resolving those collisions
    row-by-row, which is its own piece of work.

    Every row's tenant_id is remapped to the target slug so a restore into a
    differently named org still lands correctly.
    """
    slug = (req.tenant_slug or "default").strip() or "default"
    src = req.tables or {}
    restored: dict = {}
    engine = get_engine()
    with engine.connect() as conn:
        _set_tenant(conn, slug)
        # Delete children before parents (card_links → cards → board_columns).
        for tbl in ("card_links", "card_comments", "cards", "board_columns",
                    "memory_hot", "memory_revisions"):
            conn.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": slug})
        # Insert parents before children (board_columns → cards → card_links).
        restored["board_columns"]   = _bcdr_insert_columns(conn, slug, src.get("board_columns", []))
        restored["cards"]           = _bcdr_insert_cards(conn, slug, src.get("cards", []))
        restored["card_links"]      = _bcdr_insert_links(conn, slug, src.get("card_links", []))
        restored["card_comments"]   = _bcdr_insert_comments(conn, slug, src.get("card_comments", []))
        restored["memory_hot"]      = _bcdr_insert_hot(conn, slug, src.get("memory_hot", []))
        restored["memory_revisions"] = _bcdr_insert_revisions(conn, slug, src.get("memory_revisions", []))
        conn.commit()
    return {"tenant_slug": slug, "restored": restored}


# ── GET /health ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    status = {
        "service":  "memory-api",
        "healthy":  True,
        "reranker": RERANKER_ENDPOINT or "disabled",
        "components": {},
    }

    # Both probes are synchronous clients. /health is polled constantly, so
    # run them in the threadpool rather than stalling every other request on
    # the loop while Qdrant or Postgres is slow to answer.
    try:
        await asyncio.to_thread(lambda: get_qdrant().get_collections())
        status["components"]["qdrant"] = "ok"
    except Exception as exc:
        status["components"]["qdrant"] = f"degraded: {exc}"
        status["healthy"] = False

    def _pg_ping():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        await asyncio.to_thread(_pg_ping)
        status["components"]["postgres"] = "ok"
    except Exception as exc:
        status["components"]["postgres"] = f"degraded: {exc}"
        status["healthy"] = False

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{EMBEDDER_URL}/health")
            r.raise_for_status()
        status["components"]["embedder"] = "ok"
    except Exception as exc:
        status["components"]["embedder"] = f"degraded: {exc}"
        status["healthy"] = False

    return status


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
