"""
Knowledge Base MCP Server
─────────────────────────
Implements MCP spec 2025-03-26 with Streamable HTTP transport.
Compatible with: Open WebUI (v0.6.31+), Claude Desktop, Cursor, any
standard MCP client.

Transport:  streamable-http  (POST /mcp — single endpoint for all traffic)
Port:       8003
External URL (via your reverse proxy): https://your-domain.example.com/mcp

Auth:       Bearer token required on all requests.
            Tokens are generated in the editor Settings → API Tokens page.
            Validation calls editor /api/internal/tokens/validate (cached 60s).

Sample subset of tools exposed:
  search           — DEFAULT: semantic search across wiki + memories (wiki-boosted)
  wiki_search_only — semantic search over indexed wiki docs only
  memory_list      — list all memories (optional tag filter)
  memory_search    — semantic search over memories only
  memory_capture   — DEFAULT for writes: cheap streaming capture (hot tier);
                     the server compacts and promotes survivors to durable storage
  memory_add       — explicit durable add (use only when an exact, deduped
                     long-lived record is wanted right now — otherwise capture)
  memory_update    — update an existing memory's content, tags, and/or type
  memory_delete    — delete a memory by ID
  memory_events    — paginated structured event log (PostgreSQL)
  reindex_status   — check wiki reindex progress and embedding health
  wiki_reindex     — trigger a full reindex of the wiki corpus

Resources exposed:
  kb://status          — server health, embedding model, doc count, reindex state
  kb://wiki/index      — list of all indexed wiki documents with paths and tags
  kb://memories/{id}   — fetch a single memory by UUID

Prompts exposed:
  search_guide         — guidance on which search tool to use and when
  memory_template      — structured template for writing high-quality memories
"""

import hashlib
import json
import logging as _logging
import os
import re
import itertools
import base64
import functools
import secrets
import time
from datetime import date, datetime, timezone
from collections import defaultdict
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from typing import Literal
from starlette.types import ASGIApp, Receive, Scope, Send

# ── Config ─────────────────────────────────────────────────────────────────
EMBEDDER_URL        = os.getenv("EMBEDDER_URL",         "http://embedder:8001")
MEMORY_API_URL      = os.getenv("MEMORY_API_URL",       "http://memory-api:8002")
EDITOR_URL          = os.getenv("EDITOR_URL",           "http://editor:8080")
# Shared secret proving this MCP container is an Elefant peer service when
# calling memory-api or embedder. Must match the value in every other
# Elefant container's environment.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
if not INTERNAL_SERVICE_TOKEN:
    _logging.getLogger("mcp").critical(
        "INTERNAL_SERVICE_TOKEN is not set — calls from MCP to memory-api "
        "will be rejected. Set INTERNAL_SERVICE_TOKEN in your .env (run: "
        "openssl rand -hex 32) and restart embedder + memory-api + mcp + editor."
    )


def _internal_service_headers() -> dict:
    """Headers proving this MCP container is a peer Elefant service when
    calling an internal endpoint (memory-api). Empty when unconfigured —
    the peer fails closed, which is the correct posture."""
    if not INTERNAL_SERVICE_TOKEN:
        return {}
    return {"X-Internal-Service": INTERNAL_SERVICE_TOKEN}
# Public-facing URLs — used in OAuth discovery metadata and WWW-Authenticate headers.
# Must be set via EXTERNAL_MCP_URL / EXTERNAL_EDITOR_URL environment variables.
EXTERNAL_MCP_URL    = os.getenv("EXTERNAL_MCP_URL",    "").rstrip("/")
EXTERNAL_EDITOR_URL = os.getenv("EXTERNAL_EDITOR_URL", "").rstrip("/")
# RFC 8414: issuer = base URL with no path (discovery at /.well-known/oauth-authorization-server).
# EXTERNAL_EDITOR_URL is "https://host/editor"; strip the path to get just "https://host".
_parsed_editor = urlparse(EXTERNAL_EDITOR_URL)
AS_ISSUER = f"{_parsed_editor.scheme}://{_parsed_editor.netloc}" if EXTERNAL_EDITOR_URL else ""

CLASSIFICATION_TAGS = {"phi", "pii", "sensitive"}

# Paths that bypass Bearer auth entirely (OAuth discovery, health checks).
_AUTH_EXEMPT_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    # RFC 9728 path-aware form: when the resource has a path (e.g. /mcp),
    # clients append that path to the metadata URL.
    "/.well-known/oauth-protected-resource/mcp",
}

# FastMCP with streamable-http transport (MCP spec 2025-03-26)
mcp = FastMCP(
    name="knowledge-base",
    host="0.0.0.0",
    port=8003,
    # stateless_http=False (default): server issues mcp-session-id and maintains
    # an SSE stream per session. Required for Moltus and other SSE-transport clients
    # that expect tool results on the SSE stream, not in the POST response body.
    instructions=(
        "ELEFANT (said aloud, speech-to-text usually writes it \"elephant\" — same\n"
        "server). This server is a shared memory + wiki knowledge base with a two-tier\n"
        "memory.\n"
        "- Wiki: markdown documents (SOPs, runbooks, reference material, architecture).\n"
        "- Memory hot tier: cheap, un-embedded captures written with memory_capture. A\n"
        "  background compactor clusters them, scores recurrence/diversity/recency, and\n"
        "  promotes the survivors into the durable tier.\n"
        "- Memory durable tier: embedded long-term facts (memory_add, or promoted captures).\n"
        "- Board: a per-tenant post-it Kanban — thin cards in columns you name. card_add is\n"
        "  the reflex when the user floats an idea they are not building right now; the phrase\n"
        "  \"note to self\" is an explicit signal to capture as a card (note_to_self).\n\n"
        "START HERE. Unsure which tool fits? Call `tool_index`: it lists every tool by\n"
        "purpose, one line each, and is cheaper than guessing with keyword searches. For\n"
        "any question or lookup, the answer is almost always `search`.\n\n"
        "OPERATING MODE — act as a second brain in every session:\n"
        "1. SEARCH FIRST. Call `search` before answering any question or factual lookup — it\n"
        "   covers wiki and memories together. Don't rely on training data when this\n"
        "   knowledge base may have the answer.\n"
        "2. CAPTURE AGGRESSIVELY. `memory_capture` is the DEFAULT write — use it liberally and\n"
        "   mid-conversation, the moment durable signal appears: decisions, preferences,\n"
        "   project facts, host/repo names, where you left off, vetoed approaches, gotchas.\n"
        "   It's cheap (no embedding cost) and bloat is fine — the compactor dedups and\n"
        "   prunes. Missing context is the expensive failure. Don't wait to be asked.\n"
        "3. Use `memory_add` only when the user says 'remember this exactly' or you need one\n"
        "   high-confidence durable record now that shouldn't wait for compaction.\n"
        "4. Use wiki_search_only or memory_search only when the user explicitly wants a\n"
        "   single source.\n"
        "4b. EDITING THE WIKI. `wiki_write` replaces a whole page — use it to CREATE one.\n"
        "   To change a page that already exists, reach for `wiki_edit`: it swaps an exact\n"
        "   snippet (mode='replace'), or a heading's section (mode='replace_section' /\n"
        "   'append_section'), or appends to the end. Rewriting a long page to change a few\n"
        "   lines means reproducing every other line from context, and whatever you\n"
        "   misremember is silently lost — so don't. A bounded edit also makes the History\n"
        "   diff show the actual change, which is what a human reviews.\n"
        "   ALWAYS pass `content` — it is the text that goes IN. An edit mode with empty\n"
        "   content is refused, not treated as 'replace it with nothing'; removing text is\n"
        "   mode='delete' and you have to name it. If an edit reports a large negative byte\n"
        "   delta you did not intend, stop and use `wiki_history` / `wiki_revert`.\n"
        "   Reorganising is `wiki_move` (pages and folders, recursive, history follows) and\n"
        "   `wiki_delete` (recycle bin, 30 days; folders need recursive=true). Moving a page\n"
        "   is not 'write it at the new path' — that leaves the old one behind as a stray.\n"
        "   All of these are confined to the AI folder. A page a human moved OUT of it has\n"
        "   been promoted as reviewed and is permanently beyond your reach — which is also\n"
        "   why you cannot move a page out of the AI folder yourself.\n"
        "5. BOARDS. When the user floats an idea/proposal/backlog item they are NOT building\n"
        "   now, `card_add` it (defaults to the backlog column). Cards are thin post-its, not\n"
        "   documents — when one outgrows the sticky, write it up as a wiki page and keep the\n"
        "   card thin. Column names are the user's own vocabulary; call `board_columns` to\n"
        "   learn them before card_add / card_move. Don't pre-check permissions — just call\n"
        "   the board tools; a token that genuinely lacks access gets a clear error saying so.\n"
        "   LINK THE WRITE-UP TO THE CARD — never paste a page URL into the card body. Two\n"
        "   first-class ways: (a) by convention, write the page under\n"
        "   `<AI folder>/projects/<card-slug>/…` and it is associated automatically at read\n"
        "   time (survives renames); or (b) explicitly, via `card_link_wiki` (or the\n"
        "   `wiki_links` argument on card_add / card_update) for a page living anywhere else.\n"
        "   The link reads BOTH ways, so you never have to remember which end it was made\n"
        "   from: `card_get` lists the card's wiki pages, and `get_wiki_page` names the card\n"
        "   a page belongs to. When a page has a card, read that card before rewriting the\n"
        "   page, and move or update the card when the work it describes moves on.\n"
        "   LABELS classify a card — `bug`, `enhancement`, a repo, a sprint. Free-form, no\n"
        "   fixed vocabulary, shown as chips in the board UI. `card_label` adds or removes\n"
        "   ONE in place (human-added labels survive); `card_add`/`card_update` take a whole\n"
        "   `labels` list. `card_list(label=...)` filters on them, so classifying a card is\n"
        "   what makes it findable later — put the kind of work in a label, not the title.\n"
        "   TRIGGER PHRASE: when the user says \"note to self\" (or \"note-to-self\"), that is an\n"
        "   explicit instruction to capture the following text as a card via `note_to_self` —\n"
        "   NOT as a memory (memory_capture) and NOT as a wiki page. It files a card in the\n"
        "   backlog labelled `note-to-self` — a RESERVED label a label-replace cannot drop.\n"
        "\n"
        "SPOKEN PHRASEBOOK - what a person actually SAYS, and the tool it means. Tool\n"
        "descriptions are written to be findable by these words, so search with the\n"
        "user's phrasing rather than ours:\n"
        "   \"remember this\" / \"save that\" / \"note that\" / \"don't forget\" → memory_capture\n"
        "   \"remember this exactly\" / \"word for word\"                    → memory_add\n"
        "   \"note to self …\"                                            → note_to_self\n"
        "   \"what do I know about…\" / \"look it up\" / \"did I write…\"      → search\n"
        "   \"find anything about…\" / \"search the wiki for…\"             → search\n"
        "   \"which tool do I use\" / \"what can you do\" / \"help\"         → tool_index\n"
        "   \"what do you remember about…\"                               → memory_search\n"
        "   \"that's wrong, it's actually…\"                              → memory_update\n"
        "   \"forget that\"                                               → memory_delete\n"
        "   \"why do you remember that\" / \"where did that come from\"      → memory_provenance\n"
        "   \"what's on the board\" / \"what's up next\" / \"my tasks\"        → card_list\n"
        "   \"add a card\" / \"a task\" / \"a to-do\" / \"put it on the board\"  → card_add\n"
        "   \"what should I work on next\"                                → card_next\n"
        "   \"move that card to…\"                                        → card_move\n"
        "   \"mark it done\" / \"take it off the board\"                     → card_close\n"
        "   \"open that page\" / \"pull up the runbook\" / \"what does X say\" → get_wiki_page\n"
        "   \"write it up\" / \"start a page\" / \"draft a doc\"              → wiki_write\n"
        "   \"fix that page\" / \"add a section\" / \"tweak the wording\"      → wiki_edit\n"
        "   \"put this screenshot in the page\" / \"embed that image\"      → wiki_upload_image\n"
        "   \"draw a diagram\" / \"make a flowchart\"                       → diagram_create\n"
        "   \"change that box\" / \"add an arrow to the diagram\"          → diagram_patch\n"
        "   \"show me the diagram\" / \"what does the chart look like\"    → diagram_render\n"
        "   \"search the code\" / \"where is that function\"                → git_search\n"
        "LOAD BEFORE YOU CALL. If a tool's schema is not loaded yet, load it and read its\n"
        "arguments - never guess parameter names or values, and never invent a tool name\n"
        "that is not on the server. A spoken request is looser than a typed one; the tool\n"
        "it maps to is not."
    ),
)


# ── Backstop: no tool or resource leaks a raw exception ─────────────────────
#
# FastMCP answers an exception escaping a handler with "Error executing tool X:
# <exception text>", and that text is where internal hostnames, service URLs
# and collection names leak from. Most handlers catch their own failures and
# say something specific; this wrapper is for the ones that do not, so a
# handler that forgets still cannot leak. The detail is logged under a ref.

def _guarded(fn):
    @functools.wraps(fn)
    async def guarded(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            return _internal_error(f"{fn.__name__} failed", exc)
    return guarded


def _guard_registrar(register_factory):
    @functools.wraps(register_factory)
    def factory(*dargs, **dkwargs):
        register = register_factory(*dargs, **dkwargs)

        def decorator(fn):
            return register(_guarded(fn))
        return decorator
    return factory


mcp.tool = _guard_registrar(mcp.tool)
mcp.resource = _guard_registrar(mcp.resource)


# ── Auth: in-process state ──────────────────────────────────────────────────

# token_hash → (token_info_dict, expiry_timestamp)
_token_cache: dict[str, tuple[dict, float]] = {}
# token_hash → {rpm: [timestamps], rpd: [timestamps]}
_rate_counters: dict[str, dict] = defaultdict(lambda: {"rpm": [], "rpd": []})

# Per-session git-source toggles: mcp-session-id → {source_id: enabled?}.
# In-memory and intentionally ephemeral — overrides reset when the session
# ends (a new mcp-session-id starts empty), which is the "per session"
# semantics we want. Sources without an override fall back to the source's
# default_available flag. This narrows within ACL-visible sources only; it
# can never widen access (ACL is still enforced server-side).
_session_source_overrides: dict[str, dict[str, bool]] = defaultdict(dict)
# override key → last time the session touched its overrides (monotonic).
# Sessions end without telling us, so an idle one is dropped after
# SESSION_OVERRIDE_TTL and the map never holds more than SESSION_OVERRIDE_MAX.
_session_override_touched: dict[str, float] = {}
SESSION_OVERRIDE_TTL = 24 * 3600
SESSION_OVERRIDE_MAX = 10_000

# Every in-process map here is keyed by something a client controls (a bearer
# hash, a session id), so each one is bounded: TTL first, then max size.
TOKEN_CACHE_MAX = 10_000
RATE_COUNTERS_MAX = 10_000

CACHE_TTL = 5  # seconds between editor re-validation calls
# Was 60s, which meant token revocation, scope downgrades, and
# offboarding took up to a minute to take effect. The editor's
# /api/internal/tokens/validate is a single indexed lookup; re-checking
# every few seconds is cheap and closes the revocation blind window
# the security audit flagged. Set to 0 to disable the cache entirely
# in stricter deployments (e.g. via INTERNAL_VALIDATE_CACHE_TTL env
# var if it ever needs to be runtime-tunable).


# Negative cache (SEC-08): token_hash → expiry, for bearers the editor has
# said are not valid. Without it every random bearer sent to /mcp costs an
# editor DB lookup, so a flood of junk tokens becomes a flood against the
# editor. Only a definite "not valid" from the editor is cached — a timeout or
# a 5xx is not, or an editor blip would lock out every real token for the TTL.
# A token that does not exist yet cannot be guessed before it is minted, so
# caching its absence costs nothing; a revoked one stays refused, as it should.
# Bounded, so the flood it absorbs cannot become a memory problem instead.
_invalid_token_cache: dict[str, float] = {}
INVALID_CACHE_TTL = 60
INVALID_CACHE_MAX = 100_000


def _make_room(cache: dict, max_size: int, expired) -> None:
    """Leave room for one more entry in `cache`: drop what `expired(value)`
    says is stale, then, if it is still full of live entries, the oldest
    (dicts keep insertion order). A no-op below `max_size`."""
    if len(cache) < max_size:
        return
    for k in [k for k, v in cache.items() if expired(v)]:
        del cache[k]
    while cache and len(cache) >= max_size:
        del cache[next(iter(cache))]


def _remember_invalid(token_hash: str, now: float) -> None:
    if INVALID_CACHE_TTL <= 0:
        return
    _make_room(_invalid_token_cache, INVALID_CACHE_MAX, lambda exp: exp <= now)
    _invalid_token_cache[token_hash] = now + INVALID_CACHE_TTL


async def _validate_token(token_hash: str) -> dict | None:
    """Return token info dict if valid, None otherwise.

    The short cache TTL above is the relevant security knob: a longer
    cache makes revocation lag, a shorter one shifts load to the
    editor's validate endpoint.  5s is the compromise — fast enough
    that an operator can pull a leaked token by editing the DB and see
    it stop working within seconds, slow enough not to hammer the
    editor under a steady MCP workload.
    """
    now = time.monotonic()
    cached = _token_cache.get(token_hash)
    if cached:
        info, expiry = cached
        if now < expiry:
            return info
        del _token_cache[token_hash]

    refused_until = _invalid_token_cache.get(token_hash)
    if refused_until is not None:
        if now < refused_until:
            return None
        del _invalid_token_cache[token_hash]

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/tokens/validate",
                json={"token_hash": token_hash},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    if not data.get("valid"):
        _remember_invalid(token_hash, now)
        return None

    if CACHE_TTL > 0:
        _make_room(_token_cache, TOKEN_CACHE_MAX, lambda v: v[1] <= now)
        _token_cache[token_hash] = (data, now + CACHE_TTL)
    return data


# ── Rate limiting is OFF unless a deployment asks for it ────────────────────
#
# It used to be on, at 60/min and 1000/day, and it caused a production
# incident: the ceilings were unreachable from any UI, the 429 said nothing
# useful, and clients retried into a wall. Two things about the design made
# that a bad trade rather than an unlucky one.
#
# It counts HTTP REQUESTS, which is a poor proxy for load. One `search` fans
# out to the embedder's model; fifty `card_get`s are a few indexed reads. The
# counter cannot tell those apart, so it throttles the cheap caller and the
# expensive one identically. And it charges MCP protocol overhead —
# `initialize`, `tools/list` over ~49 tools, the event-stream GET — against
# what reads like a usage budget, so the ceiling measures something other
# than the thing it is named for.
#
# It is also per-process and in-memory, so it resets on every deploy and would
# not hold across replicas. As a defence that is leaky; as a quota it is
# arbitrary.
#
# So it is kept and disabled, rather than deleted: a self-hosted multi-tenant
# deployment may well want a ceiling, and having one to switch on is cheaper
# than re-implementing it. If you turn it on, know what you are buying — for
# protecting a shared box, a concurrency cap on the expensive paths is the
# better instrument than counting requests.
#
# 0 means unlimited. These are the MASTER control: per-token columns are an
# override that only takes effect once limiting is switched on here, so an
# existing token row carrying 60/1000 does not quietly keep limiting after a
# deployment has turned this off.
RATE_LIMIT_RPM = int(os.environ.get("ELEFANT_MCP_RATE_LIMIT_RPM", "0") or 0)
RATE_LIMIT_RPD = int(os.environ.get("ELEFANT_MCP_RATE_LIMIT_RPD", "0") or 0)
RATE_LIMITING_ON = RATE_LIMIT_RPM > 0 or RATE_LIMIT_RPD > 0

# (counter key, window seconds, token field, configured default, label).
# Keeping them in one table is what stops the limiter and the message it
# produces from drifting apart.
_RATE_WINDOWS = (
    ("rpm", 60,    "rate_limit_rpm", RATE_LIMIT_RPM, "per-minute"),
    ("rpd", 86400, "rate_limit_rpd", RATE_LIMIT_RPD, "per-day"),
)


def _check_rate_limit(token_hash: str, info: dict):
    """Sliding-window rate limiter, per token, across two windows.

    Returns None when the request is allowed. When it is refused, returns a
    dict describing WHICH ceiling refused it and when it will actually clear.

    That distinction is the whole point of this shape. This used to return a
    bare False, and the caller answered every refusal with the same sentence
    and a hardcoded `Retry-After: 60`. For the per-minute ceiling that is
    right; for the per-day one it is a lie that costs real time — a client
    told to come back in a minute retries on a 60-second loop for hours, and
    an exhausted daily quota presents as a hard outage with nothing in the
    response to say otherwise. Whoever is holding the 429 cannot tell a burst
    from a spent budget, which is exactly the question they need answered.

    Both ceilings are checked even when the first already refuses, because
    when both are blown the binding one is whichever clears LAST — reporting
    the per-minute window then would send the caller back in 60 seconds to be
    refused again.
    """
    if not RATE_LIMITING_ON:
        # Nothing is recorded either: a disabled limiter that still appended
        # would grow the counters for no benefit.
        return None

    now = time.time()
    if token_hash not in _rate_counters:
        # An entry whose newest timestamp is older than the longest enforced
        # window counts nothing any more, so it can go.
        horizon = max((w for _k, w, _f, d, _l in _RATE_WINDOWS if d > 0), default=0)
        _make_room(_rate_counters, RATE_COUNTERS_MAX,
                   lambda c: all(not ts or now - ts[-1] >= horizon for ts in c.values()))
    counters = _rate_counters[token_hash]
    refusals = []

    for key, window, field, default, label in _RATE_WINDOWS:
        if default <= 0:
            continue          # this window is switched off deployment-wide
        kept = [t for t in counters[key] if now - t < window]
        counters[key] = kept
        limit = info.get(field, default)
        if len(kept) >= limit:
            # The window frees a slot when its oldest entry ages out.
            retry_after = max(1, int(kept[0] + window - now) + 1)
            refusals.append({
                "window": label, "limit": limit, "used": len(kept),
                "retry_after": retry_after,
            })

    if refusals:
        # Do NOT record this attempt. Counting refused requests would let a
        # client that retries hard hold its own window permanently full.
        return max(refusals, key=lambda r: r["retry_after"])

    for key, _window, _field, default, _label in _RATE_WINDOWS:
        if default <= 0:
            continue          # a window nobody enforces must not accumulate
        counters[key].append(now)
    return None


def _filter_by_classification(results: list, token_info: dict) -> list:
    """Remove results tagged with classification tags the token can't access."""
    allowed = {c.lower() for c in (token_info.get("classification_access") or [])}
    filtered = []
    for r in results:
        tags_lower = {t.lower() for t in (r.get("tags") or [])}
        blocked = tags_lower & CLASSIFICATION_TAGS - allowed
        if not blocked:
            filtered.append(r)
    return filtered


def _filter_by_label_scope(results: list, token_info: dict) -> list:
    """If token has label scope, keep only results with at least one matching tag."""
    scope = [l.lower() for l in (token_info.get("allowed_labels") or [])]
    if not scope:
        return results
    return [
        r for r in results
        if any(t.lower() in scope for t in (r.get("tags") or []))
    ]


def _empty_reason(raw: list, kept: list, token_info: dict) -> str:
    """Explain an empty result set that the index actually had hits for.

    The two filters above are ACLs and stay exactly as strict as they are —
    but they were silent, so a token with a label scope got "No results" for a
    page it had just written and indexed successfully (issue #93). An untagged
    page matches no label, so it is always dropped. Saying so turns a
    apparently-broken search into an understandable one, without showing a
    single row the token may not see.
    """
    if kept or not raw:
        return ""
    scope = [str(l) for l in (token_info.get("allowed_labels") or [])]
    if scope:
        return (f"\n\n⚠️  {len(raw)} match(es) were found and then hidden by this "
                f"token's label scope ({', '.join(sorted(scope))}). A page with no "
                f"matching tag is never visible to this token — including a page "
                f"you just wrote. Tag the page with one of those labels, or use a "
                f"token without a label scope.")
    return ("\n\n⚠️  Matches were found but hidden by this token's classification "
            "access. Ask an admin to widen the token's classification_access.")


def _check_permission(token_info: dict, flag: str) -> bool:
    return bool(token_info.get(flag))


# ── Auth middleware ─────────────────────────────────────────────────────────
#
# Implemented as a pure ASGI middleware (not Starlette BaseHTTPMiddleware).
# BaseHTTPMiddleware buffers the full response before forwarding it, which
# breaks SSE / streaming — the MCP Streamable HTTP transport sends tool-call
# results as chunked event-stream data. The pure ASGI version passes bytes
# through untouched, so streaming works correctly.

class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # OAuth discovery paths are exempt from authentication.
        path = scope.get("path", "")
        if path in _AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract Authorization from raw ASGI headers (list of [name, value] bytes)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")

        www_auth = (
            f'Bearer realm="{EXTERNAL_MCP_URL}",'
            f' authorization_server="{AS_ISSUER}"'
        ).encode()

        if not auth.startswith("Bearer "):
            await self._respond(send, 401, {"error": "Unauthorized — Bearer token required"},
                                extra_headers=[(b"www-authenticate", www_auth)])
            return

        token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()
        token_info = await _validate_token(token_hash)
        if token_info is None:
            await self._respond(send, 401, {"error": "Invalid or expired token"},
                                extra_headers=[(b"www-authenticate", www_auth)])
            return

        refusal = _check_rate_limit(token_hash, token_info)
        if refusal is not None:
            # Say which ceiling, what it is, and when it genuinely clears.
            # Note the limit is per TOKEN, not per session: every agent
            # sharing this bearer token spends the same budget, which is the
            # first thing to check when a limit is hit that nobody expected.
            _logging.getLogger("mcp").warning(
                "Rate limited token %s…: %s ceiling %d reached, retry in %ds",
                token_hash[:12], refusal["window"], refusal["limit"],
                refusal["retry_after"],
            )
            await self._respond(
                send, 429,
                {"error": (
                    f"Rate limit exceeded: {refusal['used']}/{refusal['limit']} "
                    f"{refusal['window']} requests for this token. Retry in "
                    f"{refusal['retry_after']}s. This ceiling is per TOKEN, so "
                    f"every session sharing it shares the budget."
                 ),
                 "window": refusal["window"],
                 "limit": refusal["limit"],
                 "retry_after": refusal["retry_after"]},
                extra_headers=[(b"retry-after",
                                str(refusal["retry_after"]).encode())])
            return

        # Store token in the ASGI scope under a private key.
        # We intentionally avoid scope["state"] — Starlette's State object doesn't
        # support dict-style [] access, and FastMCP's routing code does use that
        # pattern internally, causing TypeError on the first request.
        scope["_kb_token"] = token_info
        # The token hash lets write tools forward it to the editor, which
        # re-validates authoritatively (the editor is the security boundary).
        scope["_kb_token_hash"] = token_hash

        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(
        send: Send,
        status: int,
        body: dict,
        extra_headers: list | None = None,
    ) -> None:
        encoded = json.dumps(body).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": encoded})


# BearerAuthMiddleware is wrapped around the Starlette app in the entrypoint.


# ── Helpers ─────────────────────────────────────────────────────────────────

def _token(ctx: Context) -> dict:
    """Extract token_info from the ASGI scope (set by BearerAuthMiddleware)."""
    return ctx.request_context.request.scope.get("_kb_token", {})


def _tenant_slug(ctx: Context) -> str:
    """Return the tenant slug for this request (from token info, default 'default')."""
    return _token(ctx).get("tenant_slug", "default")


def _token_hash(ctx: Context) -> str:
    """The caller's bearer-token hash (set by BearerAuthMiddleware). Forwarded
    to the editor on writes so it can re-validate the token authoritatively."""
    return ctx.request_context.request.scope.get("_kb_token_hash", "")


async def _ai_wiki_folder(slug: str) -> str:
    """The tenant's AI-writable wiki folder, resolved by the editor. Falls back
    to 'AI' if the lookup fails."""
    try:
        data = await _editor_get("/api/internal/tenant-settings", tenant_slug=slug)
        return (data.get("ai_wiki_folder") or "AI").strip("/")
    except Exception:
        return "AI"


def _strip_tenant_prefix(path: str, slug: str) -> str:
    """Strip a leading '<slug>/' so the tenant prefix stays invisible to
    agents. The embedder emits paths with the slug included; the editor's
    file API expects space-relative paths. This bridges the two."""
    if not path or not slug:
        return path
    prefix = f"{slug}/"
    return path[len(prefix):] if path.startswith(prefix) else path


async def _editor_get(path: str, **params) -> dict:
    """GET from the editor's internal API (where token validation lives too).
    Distinct from _get, which targets the embedder."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{EDITOR_URL}{path}",
            params={k: v for k, v in params.items() if v is not None},
        )
        resp.raise_for_status()
        return resp.json()


async def _editor_post(path: str, body: dict) -> dict:
    """POST to the editor's internal API. Distinct from _post (embedder)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{EDITOR_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


def _error_ref() -> str:
    """A short correlation id: shown to the client, logged beside the detail."""
    return secrets.token_hex(4)


def _internal_error(what: str, exc: BaseException) -> str:
    """Client-facing text for a tool failure, without the internals.

    Raw exception text carries internal hostnames, service URLs, Qdrant
    collection names and tracebacks, none of which the agent can act on and
    all of which it will happily repeat to the user. So the full detail goes
    to the server log under a short ref, and the client gets that ref.

    An upstream 4xx is still passed through: its `detail` is the typed,
    user-actionable part (not found, invalid argument, no permission), and
    UpstreamError has already stripped the URL from it.
    """
    status = 0
    if isinstance(exc, UpstreamError):
        status = exc.status
        if status < 500:
            return f"{what} ({status}): {exc.detail}"
    elif isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = exc.response.status_code
        if status < 500:
            return f"{what} ({status}): {_http_err_detail(exc)}"
    ref = _error_ref()
    _logging.getLogger("mcp").warning(
        "%s [ref %s]: %r", what, ref, exc, exc_info=exc)
    code = f" ({status})" if status else ""
    return (f"{what}{code}: an internal error occurred (ref {ref}). Try again; "
            "if it keeps happening, give this ref to your administrator.")


def _http_err_detail(exc: "httpx.HTTPStatusError") -> str:
    """Best-effort extraction of our typed JSON error `detail` from an upstream
    HTTPStatusError; logs and masks a non-JSON body rather than relaying raw
    upstream text to the agent.

    A 5xx `detail` is masked too, JSON or not: a service that failed tends to
    put the raw exception there, and that is exactly the text to keep in."""
    status = exc.response.status_code if exc.response is not None else 0
    try:
        detail = exc.response.json().get("detail", "") or "(no detail)"
    except Exception:
        _logging.getLogger("mcp").info(
            "upstream non-JSON error body: %s",
            exc.response.text[:200] if exc.response is not None else "",
        )
        detail = "(upstream error)"
    if status >= 500:
        ref = _error_ref()
        _logging.getLogger("mcp").warning(
            "upstream %s [ref %s]: %s", status, ref, str(detail)[:500])
        return f"internal error (ref {ref})"
    return detail


async def _render_settings(slug: str) -> str:
    """Format a tenant's programmatic settings as markdown for an agent.
    Shared by the get_user_settings tool and the kb://settings resource."""
    data = await _editor_get("/api/internal/tenant-settings", tenant_slug=slug)
    s = data.get("settings", {}) or {}

    def _fmt(v):
        return "system default" if v is None else v

    sb = s.get("second_brain_mode")
    sb_line = ("on" if bool(sb) else "off") + ("" if sb is not None else " (default)")
    return "\n".join([
        f"# Elefant Settings (tenant: {slug})\n",
        "Stored programmatic values, not memory — do NOT use search / "
        "memory_search to find these. `system default` means no override is set.\n",
        f"- **second_brain_mode:** {sb_line}",
        "",
        "## Hot-tier thresholds (per-tenant)",
        f"- promote_score: {_fmt(s.get('hot_tier_promote_score'))}",
        f"- drop_score: {_fmt(s.get('hot_tier_drop_score'))}",
        f"- drop_age_days: {_fmt(s.get('hot_tier_drop_age_days'))}",
        f"- cluster_threshold: {_fmt(s.get('hot_tier_cluster_threshold'))}",
        "",
        "## Board",
        f"- board_nudge_chars: {_fmt(s.get('board_nudge_chars'))}  "
        "(card body length past which card_add/card_update warn; off by default)",
    ])


def _session_id(ctx: Context) -> str:
    """The MCP session id (mcp-session-id header), or '' for stateless clients.
    Keys the per-session git-source toggles."""
    try:
        return ctx.request_context.request.headers.get("mcp-session-id", "") or ""
    except Exception:
        return ""


def _override_key(ctx: Context) -> str:
    """Key for the per-session git-source overrides.

    Namespaced by the caller's token hash AND the mcp-session-id so two
    different callers that happen to send the same (client-chosen)
    mcp-session-id string can't read or stomp each other's source
    toggles. The toggle can only ever narrow within ACL-visible sources,
    so a collision was never an access-widening bug — but it let one
    caller flip another caller's sources off, which this prevents.
    """
    return f"{_token_hash(ctx)}:{_session_id(ctx)}"


def _session_overrides(key: str, create: bool = False) -> dict[str, bool]:
    """This session's source toggles, or {} once it has been idle past
    SESSION_OVERRIDE_TTL. `create` makes room for and returns a live entry."""
    now = time.monotonic()
    touched = _session_override_touched.get(key)
    if touched is not None and now - touched >= SESSION_OVERRIDE_TTL:
        _session_source_overrides.pop(key, None)
        del _session_override_touched[key]
    if key not in _session_source_overrides:
        if not create:
            return {}
        _session_override_touched.pop(key, None)
        _make_room(_session_override_touched, SESSION_OVERRIDE_MAX,
                   lambda t: now - t >= SESSION_OVERRIDE_TTL)
        for gone in [k for k in _session_source_overrides
                     if k not in _session_override_touched]:
            del _session_source_overrides[gone]
    _session_override_touched.pop(key, None)
    _session_override_touched[key] = now   # re-insert: most recent last
    return _session_source_overrides[key]


async def _session_sources(ctx: Context, token_info: dict) -> list[dict]:
    """ACL-visible git sources annotated with per-session enabled state.

    `enabled` = this session's override if one is set, otherwise the source's
    `default_available` flag. Only sources the token can already see are
    returned — a session toggle narrows within that set, never widens it.
    """
    data = await _get_authed("/api/git-sources", token_info)
    sources = data.get("sources", []) or []
    overrides = _session_overrides(_override_key(ctx))
    out = []
    for s in sources:
        sid = s.get("id")
        ta = s.get("tool_availability") or "on"
        admin_off = ta == "admin_off"
        override = overrides.get(sid)
        if admin_off:
            enabled = False           # admin lock — sessions can't enable it
        elif override is not None:
            enabled = bool(override)  # explicit session toggle
        else:
            enabled = ta == "on"      # fall back to the default
        out.append({**s, "tool_availability": ta,
                    "admin_off": admin_off, "enabled": enabled})
    return out


async def _source_disabled_msg(ctx: Context, token_info: dict, source_id: str) -> str | None:
    """User-facing message if `source_id` is unavailable to this caller in
    this session, else None.

    Fail-CLOSED — previously this returned None on any exception or on
    a source_id that wasn't in the user's visible list, which meant a
    metadata-fetch hiccup OR an attempt to read a source the user had
    no ACL access to silently fell through to the actual git_read /
    source_tree call. memory-api's /git/read scopes by tenant via RLS
    but does not enforce per-user git-source ACL, so the fall-through
    let a low-privilege token read content from any source in the
    tenant whose id it could guess or harvest from git_search results.

    Now: if listing the user's accessible sources fails, refuse the
    caller; if source_id isn't in the visible list, refuse the caller.
    """
    try:
        sources = await _session_sources(ctx, token_info)
    except Exception as exc:
        _logging.getLogger("mcp").warning(
            "source ACL check failed (fail-closed): %s", exc,
        )
        return ("Could not verify access to this git source right now. "
                "Try again in a moment.")
    visible = {s.get("id"): s for s in sources}
    if source_id not in visible:
        return (f"Source `{source_id}` is not in the list of git sources "
                f"available to this token. If you believe it should be, "
                f"contact your org admin.")
    s = visible[source_id]
    if not s.get("enabled"):
        name = s.get("name") or source_id
        return (f"Source **{name}** (`{source_id}`) is turned off for this "
                f"session. Re-enable it with `set_source_state` "
                f"(e.g. /set git {name} on) before accessing it.")
    return None


def _auth_headers(token_info: dict) -> dict:
    """Auth headers expected by embedder _auth_ctx — same names nginx forwards."""
    hdrs = {}
    if token_info.get("user_id"):
        hdrs["X-Auth-User-Id"] = token_info["user_id"]
    if token_info.get("org_id") or token_info.get("tenant_id"):
        hdrs["X-Auth-Org-Id"] = token_info.get("org_id") or token_info["tenant_id"]
    if token_info.get("tenant_slug"):
        hdrs["X-Auth-Org-Slug"] = token_info["tenant_slug"]
    # Forward the token's display name so the embedder can match memories
    # owned only by an `owner:<name>` tag — compactor promotions carry no
    # author_id, so a UUID-only owner check hides them (issue #85). nginx
    # strips this header on browser routes; only this internal MCP→embedder
    # call sets it, so it isn't client-spoofable.
    if token_info.get("name"):
        hdrs["X-Auth-Owner-Name"] = token_info["name"]
    return hdrs


class UpstreamError(Exception):
    """An upstream service refused, described WITHOUT naming the upstream.

    httpx's own HTTPStatusError message embeds the full request URL, so an
    unhandled one told the agent — and through it, the user — that Elefant
    talks to `http://embedder:8001`. That is internal topology, and it reached
    a client because nine tools call these helpers with no handler of their
    own (issue #93). Raising this instead means a tool that forgets to catch
    still cannot leak the address, and the agent gets the upstream's own
    `detail`, which is the part it can actually act on.
    """

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail or _STATUS_HINTS.get(status, "the request was refused")
        super().__init__(f"{status}: {self.detail}")


_STATUS_HINTS = {
    400: "the request was malformed",
    401: "this token is not authenticated",
    403: "this token lacks permission for that",
    404: "not found",
    409: "conflicts with existing content",
    422: "the arguments were not valid",
    429: "rate limited — try again shortly",
    500: "the service failed to handle it",
    503: "the service is unavailable",
}


def _raise_clean(resp: "httpx.Response") -> None:
    """raise_for_status() without the URL in the message."""
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("error") or "")
    except Exception:
        pass
    if not detail:
        # Never relay a raw upstream body: it can carry the service name, a
        # traceback, or a path. Log it and hand the agent the status alone.
        _logging.getLogger("mcp").info(
            "upstream %s body: %s", resp.status_code,
            (resp.text or "")[:200] if resp.content else "(empty)")
    raise UpstreamError(resp.status_code, detail)


async def _get_authed(path: str, token_info: dict, **params) -> dict:
    """Embedder GET that forwards user identity so ACL filtering applies."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{EMBEDDER_URL}{path}",
            params={k: v for k, v in params.items() if v is not None},
            headers=_auth_headers(token_info),
        )
        _raise_clean(resp)
        return resp.json()


async def _post(path: str, body: dict, token_info: dict) -> dict:
    """POST to the embedder, forwarding the caller's authenticated identity.

    token_info is the validated OAuth token record; _auth_headers extracts
    X-Auth-User-Id / X-Auth-Org-Id / X-Auth-Org-Slug from it so the
    embedder treats the call the same as a nginx-routed browser session.
    Without these headers the embedder's _resolve_tenant_slug refuses any
    non-default tenant_slug with 403 (correct fail-closed posture, but
    silent breakage for write tools like memory_add / wiki_reindex if the
    headers aren't passed)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{EMBEDDER_URL}{path}",
            json=body,
            headers=_auth_headers(token_info),
        )
        _raise_clean(resp)
        return resp.json()


async def _put(path: str, body: dict, token_info: dict) -> dict:
    """PUT to the embedder, forwarding the caller's authenticated identity."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{EMBEDDER_URL}{path}",
            json=body,
            headers=_auth_headers(token_info),
        )
        _raise_clean(resp)
        return resp.json()


async def _delete(path: str, token_info: dict) -> dict:
    """DELETE on the embedder, forwarding the caller's authenticated identity."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{EMBEDDER_URL}{path}",
            headers=_auth_headers(token_info),
        )
        _raise_clean(resp)
        # 204 No Content (the embedder's success response for DELETE) has no
        # body — calling .json() on it raises and made the agent think the
        # delete failed even though Qdrant returned 200 OK.
        if resp.status_code == 204 or not resp.content:
            return {"status": "ok"}
        return resp.json()


async def _get_memory_api(endpoint: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{MEMORY_API_URL}{endpoint}",
            params={k: v for k, v in params.items() if v is not None},
            headers=_internal_service_headers(),
        )
        _raise_clean(resp)
        return resp.json()


# ── Heading anchors ─────────────────────────────────────────────
# A copy of the editor's rule (editor/wiki_pages.py, "Heading anchors"), so a search
# hit on a section can be cited as `path#anchor`. Both copies, and memory-ui's
# reader, are tested against tests/fixtures/heading-anchors.json.

_ANCHOR_CODE_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_ANCHOR_ESC_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_ANCHOR_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_ANCHOR_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ANCHOR_AUTOLINK_RE = re.compile(r"<((?:[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^<>\s]*)|(?:[^<>\s@]+@[^<>\s@]+))>")
_ANCHOR_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_ANCHOR_UNDERSCORE_RE = re.compile(r"(?<![^\W_])(_{1,3})(?=[^\s_])(.+?)(?<=[^\s_])\1(?![^\W_])")


def _heading_anchor(raw: str) -> str:
    """The anchor a heading's markdown text gets, before de-duplication."""
    import html as _html
    import unicodedata
    held: list = []

    def _hold(s: str) -> str:
        held.append(s)
        return f"\x00{len(held) - 1}\x00"

    def _code(m: re.Match) -> str:
        body = m.group(2)
        if len(body) > 1 and body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]
        return _hold(body)

    s = re.sub(r"\s*#+\s*$", "", (raw or "").strip().lstrip("#"))
    s = _ANCHOR_CODE_RE.sub(_code, s)
    s = _ANCHOR_ESC_RE.sub(lambda m: _hold(m.group(1)), s)
    s = _ANCHOR_IMG_RE.sub("", s)
    s = _ANCHOR_LINK_RE.sub(r"\1", s)
    s = _ANCHOR_AUTOLINK_RE.sub(lambda m: _hold(m.group(1)), s)
    s = _ANCHOR_TAG_RE.sub("", s)
    prev = None
    while prev != s:
        prev, s = s, _ANCHOR_UNDERSCORE_RE.sub(r"\2", s)
    s = _html.unescape(s)
    s = re.sub("\x00(\\d+)\x00", lambda m: held[int(m.group(1))], s).strip()
    kept = [ch for ch in s.lower()
            if ch in " -" or unicodedata.category(ch)[0] in "LMN"
            or unicodedata.category(ch) == "Pc"]
    return "".join(kept).replace(" ", "-") or "section"


def _wiki_hit_path(r: dict) -> str:
    """A wiki hit's path, carrying the section's anchor when the hit is one
    section of the page — so an agent can cite `page.md#heading` rather than
    the whole page. A heading repeated on its page is anchored as its first
    occurrence; get_wiki_page lists every anchor exactly."""
    path = r.get("path", "") or ""
    title = r.get("section_title") or ""
    return f"{path}#{_heading_anchor(title)}" if (path and title) else path


def _fmt_wiki_result(r: dict, i: int) -> str:
    tags = ", ".join(r.get("tags", [])) or "none"
    section = f" § {r['section_title']}" if r.get("section_title") else ""
    src = r.get("source") or r.get("origin") or {}
    if isinstance(src, dict) and src.get("type") == "git_source":
        origin_line = f"**Origin:** git source `{src.get('name') or src.get('id')}` (repo file)"
    elif isinstance(src, dict) and src.get("type") == "wiki":
        origin_line = "**Origin:** wiki (hand-authored)"
    else:
        origin_line = ""
    return (
        f"### {i}. {r.get('title', r.get('path', 'Unknown'))}{section}\n"
        f"**Score:** {r.get('score', 0):.4f} | **Path:** {_wiki_hit_path(r)} | **Tags:** {tags}\n"
        + (origin_line + "\n\n" if origin_line else "\n")
        + f"{r.get('excerpt', '').strip()}\n"
    )


def _fmt_memory(m: dict, i: int | None = None, score: float | None = None) -> str:
    tags = ", ".join(m.get("tags", [])) or "none"
    mem_type = m.get("type", "fact")
    score_str = f" | **Score:** {score:.4f}" if score is not None else ""
    prefix = f"### {i}. " if i is not None else "- "
    return (
        f"{prefix}[Memory `{m.get('id', '?')}`]{score_str}\n"
        f"**Type:** {mem_type} | **Tags:** {tags} | "
        f"**Created:** {m.get('created_at', '')} | **Updated:** {m.get('updated_at', '')}\n\n"
        f"{m.get('content', '').strip()}\n"
    )


def _permission_denied(flag: str) -> str:
    labels = {
        "wiki_read":    "wiki_read",
        "wiki_write":   "wiki_write",
        "memory_read":  "memory_read",
        "memory_write": "memory_write",
        "board_read":   "board_read",
        "board_write":  "board_write",
    }
    return f"Permission denied — this token does not have `{labels.get(flag, flag)}` access."


# ── Tools ───────────────────────────────────────────────────────────────────

# ── Start here: the tool index ─────────────────────────────────────────────
#
# Card #416. A caller that does not know a tool's name finds one by fuzzy
# matching its words against every description, and a query that happens to
# say "card" or "page" lands on card_add rather than the search it meant.
# tool_index is the one obviously-named entry point to reach for instead: a
# categorised list of every tool, so the second hop is a pick, not a guess.
# tests/test_tool_index.py fails when a registered tool is missing from here,
# so the index cannot drift from what the server actually serves.

_TOOL_INDEX: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("start", "Start here", [
        ("tool_index", "This list. Call it whenever you are unsure which tool fits."),
    ]),
    ("search", "Find anything (call `search` first when unsure)", [
        ("search", "Search wiki pages AND memories in one ranked list. The default for any lookup."),
        ("wiki_search_only", "Search wiki pages only, when memories must be left out."),
        ("memory_search", "Search memories only, when wiki pages must be left out."),
        ("git_search", "Search the synced source code repositories."),
    ]),
    ("wiki", "Wiki pages: read, write, reorganise", [
        ("get_wiki_page", "Open one page by path, or one section of it by anchor."),
        ("wiki_tree", "List the pages and folders."),
        ("wiki_write", "Create a new page (replaces the whole page)."),
        ("wiki_edit", "Change part of an existing page: a snippet, a section, or an append."),
        ("wiki_upload_image", "Put an image into a page."),
        ("wiki_move", "Rename or move a page or folder."),
        ("wiki_delete", "Delete a page or folder (recycle bin, 30 days)."),
        ("wiki_history", "Who changed a page, and when."),
        ("wiki_revision", "Read an earlier version of a page."),
        ("wiki_diff", "Compare two versions of a page."),
        ("wiki_revert", "Put a page back to an earlier version."),
        ("wiki_reindex", "Rebuild the wiki search index."),
        ("reindex_status", "Check whether a reindex has finished."),
    ]),
    ("memory", "Memories: remember, correct, forget", [
        ("memory_capture", "Remember something. The default write; cheap, deduplicated later."),
        ("memory_add", "Remember something exactly, word for word, as a durable record now."),
        ("memory_list", "List stored memories."),
        ("memory_update", "Correct a memory."),
        ("memory_delete", "Forget a memory."),
        ("memory_provenance", "Where a memory came from."),
        ("memory_revisions", "Earlier versions of a memory."),
        ("memory_events", "The audit trail of memory changes."),
        ("memory_session", "What was saved in one session."),
        ("memory_introspect", "Stats on the memory store."),
    ]),
    ("board", "Board: cards, tasks and ideas not being built yet", [
        ("card_add", "Add a card (task, to-do, idea) to the board."),
        ("note_to_self", "\"Note to self ...\": file the text as a card labelled note-to-self."),
        ("card_list", "What is on the board; filter by column, label, repo or person."),
        ("card_next", "The next card that can be started, blockers resolved."),
        ("card_get", "Open one card, with its comments and linked wiki pages."),
        ("card_move", "Move a card to another column."),
        ("card_update", "Change a card's title, description or other fields."),
        ("card_comment", "Comment on a card."),
        ("card_label", "Add or remove one label."),
        ("card_assignee", "Set or clear who is doing a card."),
        ("card_participant", "Add or remove someone involved in a card."),
        ("card_reporter", "Who raised a card."),
        ("card_due", "Set or clear a due date."),
        ("card_repo", "Set the GitHub repository a card belongs to."),
        ("card_link", "Link two cards, or say one blocks the other."),
        ("card_link_wiki", "Attach a wiki page to a card."),
        ("card_close", "Close a card (mark it done)."),
        ("card_archive", "Old name for card_close."),
        ("card_eval_lock", "Lock a card while an automated reviewer evaluates it."),
        ("card_eval_unlock", "Release that lock."),
        ("board_columns", "The board's column names. Read before card_add or card_move."),
    ]),
    ("diagrams", "Diagrams", [
        ("diagram_create", "Draw a new diagram (Mermaid)."),
        ("diagram_get", "Read a diagram's source."),
        ("diagram_patch", "Change part of a diagram."),
        ("diagram_render", "Render a diagram to an image."),
        ("diagram_list", "List the diagrams."),
    ]),
    ("code", "Source code repositories", [
        ("list_sources", "Which repositories are synced."),
        ("source_tree", "Browse the files in a repository."),
        ("git_read", "Read a file, or a range of its lines."),
        ("set_source_state", "Turn a repository on or off for this session."),
    ]),
    ("settings", "Your settings", [
        ("get_user_settings", "Read your operating mode and configuration."),
        ("set_user_settings", "Change one of your settings."),
    ]),
]


@mcp.tool()
async def tool_index(topic: str = "") -> str:
    """
    Which tool do I use / start here / help / what can Elefant do / list
    the tools - the Elefant ("elephant") tool index: every tool on this
    server, grouped by what it is for, with one line each.

    CALL THIS FIRST WHEN YOU DO NOT KNOW WHICH TOOL YOU NEED. It is cheaper
    than guessing with keyword searches over tool descriptions, which match
    the literal words of a request and can land on the wrong tool (a query
    mentioning "card" finds card_add when the person wanted to search).

    For any question or lookup, the answer is almost always `search`, which
    covers wiki pages and memories together.

    Args:
        topic: Optional. Narrow the list to one group: search, wiki, memory,
               board, diagrams, code or settings. Leave empty for everything.
    """
    want = topic.strip().lower()
    groups = [g for g in _TOOL_INDEX if not want or g[0] == want]
    if not groups:
        names = ", ".join(key for key, _, _ in _TOOL_INDEX)
        return f"No tool group named `{topic}`. Groups: {names}. Leave topic empty to list every tool."

    lines = ["# Elefant tools",
             "",
             "Unsure? Call `search` for any question or lookup: it covers wiki pages and memories.",
             "Load a tool's schema before calling it, and never guess a tool name that is not listed here.",
             ""]
    for key, title, tools in groups:
        lines.append(f"## {title}  (topic: `{key}`)")
        lines += [f"- `{name}`: {summary}" for name, summary in tools]
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
async def wiki_search_only(
    query: str,
    tags: str = "",
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """
    Search / look up / find - Elefant WIKI PAGES ONLY (notes, documents,
    runbooks). Does not look at memories.

    Do NOT use this for general queries — use the 'search' tool instead,
    which searches both wiki and memories together. Unsure which tool fits?
    Call `search` first, or `tool_index` for the full list.

    Only use this when the user specifically asks to search documentation,
    reference material, or the wiki only and wants to exclude memories.

    Args:
        query: Natural-language search query.
        tags:  Comma-separated tags to restrict results (e.g. "networking,infra").
               Leave empty to search all docs.
        limit: Maximum number of results to return (1–20, default 10).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    limit = max(1, min(limit, 20))
    slug = _tenant_slug(ctx)
    data = await _get_authed("/api/search", token_info,
                             q=query, tags=tags or None, limit=limit,
                             tenant_slug=slug)
    raw = data.get("results", [])
    results = _filter_by_label_scope(
        _filter_by_classification(raw, token_info), token_info)
    for r in results:
        r["path"] = _strip_tenant_prefix(r.get("path", ""), slug)

    if not results:
        return f"No wiki results found for: {query}{_empty_reason(raw, results, token_info)}"
    lines = [f"## Wiki Search: {query}\n"]
    lines += [_fmt_wiki_result(r, i) for i, r in enumerate(results, 1)]
    return "\n".join(lines)


@mcp.tool()
async def list_sources(ctx: Context = None) -> str:
    """
    List / show me / which repos / what code do you have - the synced git
    repositories available in this Elefant knowledge base.

    Use this tool to learn the *perimeter* of the knowledge base before
    searching — i.e. which repos exist as data sources, their names,
    upstream URLs, and branches. Then you can interpret `wiki_search`
    results correctly: a hit's `Origin: git source <name>` line tells you
    the chunk came from one of these repos, while `Origin: wiki` means
    a hand-authored wiki page that is *not* part of any repo.

    Tip: combine with `source_tree` to see the file layout of a specific
    repo before deciding which files to fetch with `get_wiki_page`.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")
    try:
        sources = await _session_sources(ctx, token_info)
    except Exception as exc:
        return _internal_error("Error listing git sources", exc)
    if not sources:
        return "No synced git sources are configured for this organization."
    active = sum(1 for s in sources if s.get("enabled"))
    lines = [
        "## Synced Git Sources\n",
        f"{active} of {len(sources)} active in this session. "
        "Active sources are searchable now; toggle with `set_source_state` "
        "(e.g. /set git <name> on|off). Availability is per-session.\n",
    ]
    for s in sources:
        last = s.get("last_synced_at") or "never"
        files = s.get("file_count")
        files_str = f" | files: {files}" if files is not None else ""
        indexable = s.get("indexable_files")
        # Say so when the index and the checkout disagree, rather than let a
        # stale or partial index pass for a complete one (issue #269).
        drift_str = ""
        if files is not None and indexable is not None and files != indexable:
            drift_str = (f"\n- ⚠️ index out of step with checkout: {files} indexed, "
                         f"{indexable} indexable in the tree. The next sync reconciles; "
                         "Rebuild forces a full re-index.")
        prefix = s.get("path_prefix") or ""
        prefix_str = f" | path_prefix: {prefix}" if prefix else ""
        ta = s.get("tool_availability") or "on"
        enabled = bool(s.get("enabled"))
        if ta == "admin_off":
            avail = "🚫 administratively off (cannot be enabled this session)"
        else:
            default_on = ta == "on"
            override = enabled != default_on
            if enabled:
                avail = "✅ active this session" + (" (session override)" if override else "")
            else:
                avail = "⬜ off this session" + (" (session override)" if override else " (off by default)")
        lines.append(
            f"### {s.get('name') or s.get('id')}\n"
            f"- id: `{s.get('id')}`\n"
            f"- repo: {s.get('repo_url')}\n"
            f"- branch: {s.get('branch')}{prefix_str}\n"
            f"- last synced: {last}{files_str}{drift_str}\n"
            f"- availability: {avail}\n"
        )
    return "\n".join(lines)


@mcp.tool()
async def set_source_state(source: str, enabled: bool, ctx: Context = None) -> str:
    """
    Turn on / turn off / enable / disable / include / ignore a repo or
    source - switch one Elefant git source on or off for THIS session
    only.
    Per-session and ephemeral - overrides reset when the session ends. This
    is the tool behind "/set git <name> on|off".

    Only affects sources the token can already see: it narrows the active set,
    never widens access. A source turned off is excluded from git_search and
    can't be browsed with source_tree / git_read until re-enabled. The
    per-source "available by default" flag (set in the admin Sources UI) is the
    starting point; this session override takes precedence.

    Args:
        source:  Source name or id (from list_sources).
        enabled: true to activate for this session, false to turn off.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")
    try:
        sources = await _session_sources(ctx, token_info)
    except Exception as exc:
        return _internal_error("Error resolving git sources", exc)
    if not sources:
        return "No git sources are visible to this token."

    q = (source or "").strip()
    ql = q.lower()
    matches = [s for s in sources
               if s.get("id") == q or (s.get("name") or "").lower() == ql]
    if not matches and ql:
        matches = [s for s in sources if ql in (s.get("name") or "").lower()]
    if not matches:
        names = ", ".join(f"`{s.get('name')}`" for s in sources) or "(none)"
        return f"No visible git source matches `{source}`. Visible: {names}."
    if len(matches) > 1:
        names = ", ".join(f"`{s.get('name')}`" for s in matches)
        return f"`{source}` is ambiguous — matches {names}. Use the exact name or id."

    s = matches[0]
    if s.get("tool_availability") == "admin_off":
        return (f"**{s.get('name') or s.get('id')}** is administratively disabled by an "
                "admin and cannot be enabled in a session.")
    _session_overrides(_override_key(ctx), create=True)[s.get("id")] = bool(enabled)
    state = "on (active this session)" if enabled else "off for this session"
    return f"✓ Turned **{s.get('name') or s.get('id')}** {state}."


@mcp.tool()
async def get_user_settings(ctx: Context = None) -> str:
    """
    Show / check / what are my settings / how am I configured - the caller's
    saved Elefant settings as programmatic values (booleans / numbers), NOT
    semantic memory.

    Call this ONCE near the start of a session, before answering, to learn how
    to operate this session — most importantly whether second-brain mode is on.
    These are real stored settings: do NOT use `search` or `memory_search` to
    discover them. The companion Elefant Skill maps what each key means and how
    to act on it. A value shown as "system default" has no tenant override set.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")
    try:
        return await _render_settings(_tenant_slug(ctx))
    except Exception as exc:
        return _internal_error("Error fetching settings", exc)


@mcp.tool()
async def set_user_settings(setting: str, value: str, ctx: Context = None) -> str:
    """
    Change / set / turn on / turn off / configure a setting or preference -
    update ONE saved Elefant setting for this tenant.

    APPROVAL REQUIRED — this writes a durable, tenant-wide configuration value.
    Treat every call as approve-once: only call it when the user has explicitly
    asked to change a setting (e.g. "turn on second-brain mode", "/secondBrain
    on", "set the promote score to 1.2"). Never call it speculatively or to
    "tidy up" — a human should confirm each change. Requires a token with
    memory_write permission.

    Args:
        setting: One of — second_brain_mode | hot_tier_promote_score |
                 hot_tier_drop_score | hot_tier_drop_age_days |
                 hot_tier_cluster_threshold | board_nudge_chars
        value:   New value.
                 - second_brain_mode: on / off (true/false/yes/no/1/0)
                 - thresholds: a non-negative number
                 - board_nudge_chars: a non-negative integer; 0 (the default)
                   means no description-length nudge at all
                 - "default" (or "none" / "clear") on any setting removes the
                   override so the system default applies again.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_write"):
        return _permission_denied("memory_write")

    key = (setting or "").strip().lower()
    raw = (value or "").strip()
    allowed = {
        "second_brain_mode",
        "hot_tier_promote_score",
        "hot_tier_drop_score",
        "hot_tier_drop_age_days",
        "hot_tier_cluster_threshold",
        "board_nudge_chars",
    }
    if key not in allowed:
        return f"Unknown setting `{setting}`. Valid settings: {', '.join(sorted(allowed))}."

    if raw.lower() in ("default", "none", "clear", ""):
        parsed = None
    elif key == "second_brain_mode":
        lv = raw.lower()
        if lv in ("on", "true", "yes", "1", "enable", "enabled"):
            parsed = True
        elif lv in ("off", "false", "no", "0", "disable", "disabled"):
            parsed = False
        else:
            return f"Invalid value `{value}` for second_brain_mode — use on/off (or 'default' to clear)."
    elif key == "board_nudge_chars":
        try:
            parsed = int(raw)
        except ValueError:
            return f"Invalid integer `{value}` for board_nudge_chars (or use 'default' to clear)."
        if parsed < 0:
            return "board_nudge_chars must be >= 0."
    else:
        try:
            parsed = float(raw)
        except ValueError:
            return f"Invalid number `{value}` for {key} (or use 'default' to clear)."
        if parsed < 0:
            return f"{key} must be >= 0."

    try:
        await _editor_post(
            "/api/internal/tenant-settings",
            {"tenant_slug": _tenant_slug(ctx), "patch": {key: parsed}},
        )
    except Exception as exc:
        return _internal_error("Error saving setting", exc)

    shown = "system default" if parsed is None else parsed
    note = (" Takes effect on the next compactor pass." if key.startswith("hot_tier_")
            else " Stored as the tenant default (read by get_user_settings at session start).")
    cleared = " (override cleared)" if parsed is None else ""
    return f"✓ Updated **{key}** to `{shown}`{cleared} for this tenant.{note}"


@mcp.tool()
async def source_tree(source_id: str, ctx: Context = None) -> str:
    """
    List / show me / browse the files in that repo - every file in one git
    source synced into Elefant, so you can see the repository's structure
    and boundaries.

    Use this after `list_sources` when you need to know what files belong to
    a repo (e.g. to decide whether a Docker-related question concerns the
    project's actual Dockerfiles or a hand-authored wiki page).

    Args:
        source_id: id from `list_sources` (also visible in search results as
                   `Origin: git source ...`).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")
    blocked = await _source_disabled_msg(ctx, token_info, source_id)
    if blocked:
        return blocked
    try:
        data = await _get_authed(f"/api/git-sources/{source_id}/tree", token_info)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Git source `{source_id}` has not been synced yet or does not exist."
        if exc.response.status_code == 403:
            return f"Access denied for git source `{source_id}`."
        return _internal_error("Error fetching source tree", exc)
    except Exception as exc:
        return _internal_error("Error fetching source tree", exc)
    meta  = data.get("source", {}) or {}
    files = data.get("files", []) or []
    header = (
        f"## {meta.get('name') or source_id}\n"
        f"- repo: {meta.get('repo_url', '')}\n"
        f"- branch: {meta.get('branch', '')}\n"
        f"- files: {data.get('file_count', len(files))}\n"
    )
    if not files:
        return header + "\n(empty)\n"
    return header + "\n```\n" + "\n".join(files) + "\n```\n"


@mcp.tool()
async def git_search(
    query: str,
    source_id: str = "",
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    Search / find / grep the code / where is that function - lexical
    (keyword / symbol) search across the git repositories synced into
    Elefant.

    This is the code-search counterpart to `search` (which is *semantic* over
    the wiki + durable memories). git_search runs an *exact* lexical query —
    Postgres full-text + trigram — over the raw contents of every git-source
    file, the way `grep` works over a checkout. Reach for it to find function
    names, error strings, config keys, routes, or any literal token in code,
    where exact matching beats semantic similarity.

    Returns matching files ranked by relevance, each with the specific line
    numbers that matched and a few lines of surrounding context. Follow a hit
    with `git_read` to pull a wider span around it. Iterate like you would with
    grep: if a query misses, reformulate (synonyms, the symbol you expect).

    Args:
        query:     Words / identifiers / substrings to find
                   (e.g. "plainto_tsquery", "retry backoff", "UndefinedFunction").
        source_id: Optional — restrict to one repo (id from `list_sources` or a
                   prior git_search hit). Empty searches all synced sources.
        limit:     Max files to return (1–100, default 20).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    if not query or not query.strip():
        return "Provide a search query (a symbol, string, or keywords to grep for)."
    limit = max(1, min(limit, 100))

    # Per-session source toggles: exclude sources turned off this session.
    try:
        sess = await _session_sources(ctx, token_info)
    except Exception:
        sess = None
    if sess is not None and source_id:
        match = next((s for s in sess if s.get("id") == source_id), None)
        if match and not match.get("enabled"):
            return (f"Source `{source_id}` is turned off for this session — enable "
                    "it with set_source_state before searching it.")

    try:
        data = await _get_memory_api(
            "/git/search",
            tenant_slug=_tenant_slug(ctx),
            q=query,
            source_id=source_id or None,
            limit=limit,
        )
    except Exception as exc:
        return _internal_error("Error searching git sources", exc)

    results = data.get("results", [])
    hidden = 0
    if sess is not None:
        enabled_ids = {s["id"] for s in sess if s.get("enabled")}
        before = len(results)
        results = [r for r in results if r.get("source_id") in enabled_ids]
        hidden = before - len(results)

    if not results:
        if hidden:
            return (f"No matches in sources active this session — {hidden} hit(s) were in "
                    "sources turned off for this session. See `list_sources` or re-enable "
                    "with `set_source_state`.")
        return (f"No git-source matches for: {query}\n\n"
                "(Nothing indexed yet for this tenant, or no file contains that "
                "token. Try `list_sources` to confirm a repo is synced.)")

    header = f"## Git Source Search: {query}  ({len(results)} file(s))"
    if hidden:
        header += f"  ·  {hidden} hidden (off this session)"
    lines = [header + "\n"]
    for i, r in enumerate(results, 1):
        name = r.get("source_name") or r.get("source_id", "")
        lines.append(
            f"### {i}. `{r.get('path','')}`  ·  source **{name}**  ·  "
            f"rank {r.get('rank', 0):.3f}  ·  {r.get('line_count', 0)} lines"
        )
        hits = r.get("line_hits", [])
        if not hits:
            lines.append("_(matched; no single-line hit — `git_read` to view)_")
        for h in hits:
            lines.append(f"```{r.get('lang') or ''}\n{h.get('snippet','').rstrip()}\n```")
            lines.append(f"_↳ line {h.get('line')}_")
        lines.append(f"_source_id: `{r.get('source_id','')}`_\n")
    return "\n".join(lines)


@mcp.tool()
async def git_read(
    source_id: str,
    path: str,
    start: int = 1,
    end: int = 0,
    ctx: Context = None,
) -> str:
    """
    Read / open / show me that file - a line span of a file from a git
    source synced into Elefant; the code-search counterpart to opening a
    file after a `git_search` hit.

    Args:
        source_id: Repo id (from a `git_search` result or `list_sources`).
        path:      File path within the repo (exactly as shown by `git_search`).
        start:     First line to return (1-based, default 1).
        end:       Last line to return (default 0 = end of file).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")
    blocked = await _source_disabled_msg(ctx, token_info, source_id)
    if blocked:
        return blocked

    try:
        data = await _get_memory_api(
            "/git/read",
            tenant_slug=_tenant_slug(ctx),
            source_id=source_id,
            path=path,
            start=start,
            end=end,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (f"`{path}` is not indexed for source `{source_id}` — it may not "
                    "be synced, was filtered out (binary/lockfile/too large), or the "
                    "path is wrong. Use `git_search` or `source_tree` to find the path.")
        return _internal_error("Error reading git file", exc)
    except Exception as exc:
        return _internal_error("Error reading git file", exc)

    name = data.get("source_name") or source_id
    return (
        f"# `{path}`  ·  source **{name}**  "
        f"(lines {data.get('start')}–{data.get('end')} of {data.get('line_count')})\n\n"
        f"```{data.get('lang') or ''}\n{data.get('numbered','')}\n```"
    )


_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')


def _resolve_img_paths(img_src: str, page_path: str) -> list[str]:
    """
    Resolve a markdown image src to the space-relative path(s) to fetch, in
    the order to try them. Empty for external URLs and data URIs (nothing to
    fetch), and for a src that climbs out of the space.

    Matches the wiki UI (`resolveImageSrcs` in memory-ui): a relative src is
    relative to the page's folder, `.` and `..` included — the UI itself
    writes `../../assets/images/x.png` for a pasted image. A leading `/` means
    the space root. One older convention is kept as a fallback: a bare
    `assets/...` src on a nested page was once read as root-relative, so it is
    tried there second.
    """
    src = (img_src or "").strip()
    if src.startswith("<") and src.endswith(">"):
        src = src[1:-1]
    src = src.split(" ", 1)[0]          # drop a markdown title: (x.png "title")
    if not src or src.startswith(("http://", "https://", "data:", "//")):
        return []
    page_dir = str(PurePosixPath(page_path.partition("#")[0]).parent)
    if src.startswith("/") or page_dir in (".", ""):
        parts = src.lstrip("/").split("/")
    else:
        parts = page_dir.split("/") + src.split("/")
    resolved: list[str] = []
    for p in parts:
        if p == "..":
            if not resolved:
                return []
            resolved.pop()
        elif p and p != ".":
            resolved.append(p)
    if not resolved:
        return []
    out = ["/".join(resolved)]
    root = src.lstrip("./")
    if src.startswith(("assets/", "./assets/")) and root not in out:
        out.append(root)
    return out


def _image_ref_for(asset_path: str, page_path: str) -> str:
    """The relative src that page `page_path` should use to embed
    `asset_path` — the inverse of `_resolve_img_paths`, and the form the wiki
    UI writes, so the reference works in the browser and in get_wiki_page."""
    page_dir = PurePosixPath(page_path.partition("#")[0]).parent.parts
    target = PurePosixPath(asset_path).parts
    common = 0
    while (common < len(page_dir) and common < len(target) - 1
           and page_dir[common] == target[common]):
        common += 1
    return "/".join([".."] * (len(page_dir) - common) + list(target[common:]))


async def _page_referring_cards(tenant_slug: str, path: str) -> list:
    """Board cards that reference this wiki page, from the editor.

    The mirror of `_card_wiki_thread`. The association lives on the card side
    (explicit `wiki_links`) or in the path convention (`<AI folder>/projects/
    <card-slug>/`), so a page on its own can't tell you it's the write-up for
    something. Best-effort: reading a page must never fail because the
    back-link lookup did.
    """
    if not tenant_slug or not path:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/board/page-cards",
                json={"tenant_slug": tenant_slug, "path": path},
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            return resp.json().get("cards") or []
    except Exception as exc:
        _logging.getLogger("mcp").warning(
            "page referring-cards lookup failed for %s: %s", path, exc)
        return []


def _fmt_referring_cards(cards: list) -> str:
    """One line naming the card(s) this page belongs to, or nothing."""
    if not cards:
        return ""
    def _one(c: dict) -> str:
        bits = [f"`{c.get('slug','?')}`", f"“{c.get('title','?')}”"]
        if c.get("column"):
            bits.append(f"in **{c['column']}**")
        if c.get("closed") or c.get("archived"):
            bits.append("_(closed)_")
        return " ".join(bits)
    lead = ("_Board card for this page:_" if len(cards) == 1
            else "_Board cards referencing this page:_")
    return f"{lead} " + "; ".join(_one(c) for c in cards) + \
           "\n_Use `card_get` for the full card, or `card_update` to keep it in step._\n"


@mcp.tool()
async def get_wiki_page(
    path: str,
    ctx: Context = None,
) -> str:
    """
    Read / open / show me / pull up / what does that page say - one Elefant
    ("elephant") wiki page (note, document, runbook) in full, including
    embedded images.

    Use this after a search returns a relevant wiki page to read the complete
    content. Images embedded in the page are fetched and included inline so
    you can see diagrams, screenshots, and other visual content.

    If a board card references this page — either because the card links it, or
    because the page lives under `<AI folder>/projects/<card-slug>/` — the card
    is named at the top of the result. That page is that card's write-up, so
    read the card before rewriting the page, and keep the two in step.

    ONE SECTION: add the heading's anchor to the path — `runbooks/db.md#rollback`
    — to get just that heading and everything under it, subsections included.
    Search results that hit a section already carry the anchor on their path.
    A full read ends with the page's section anchors, so you can cite or link a
    section as `path#anchor` (the same link works in the wiki UI and in other
    pages). If the heading has been renamed or removed, you get the whole page
    with a note saying the anchor was not found.

    Args:
        path: Wiki page path as returned by search results (e.g.
              "hardware/ESP32-C3.md"), optionally with `#anchor` for one
              section. Space-relative — do not prefix with the tenant slug;
              it is resolved from your token.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    # Tolerate a tenant-prefixed path on input (e.g. older clients) by stripping
    # the slug. The internal read endpoint expects space-relative paths.
    path = _strip_tenant_prefix(path or "", _tenant_slug(ctx))

    # Go through the editor's internal endpoint (mirrors /api/internal/wiki/write)
    # so the tenant is resolved from the bearer token, not from a UI session the
    # MCP doesn't have. /api/file alone would default-org-resolve and 404 on
    # pages outside the default tenant.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/wiki/read",
                json={"token_hash": _token_hash(ctx), "path": path},
            )
            resp.raise_for_status()
            file_data = resp.json()
    except Exception as exc:
        return _internal_error(f"Error fetching wiki page '{path}'", exc)

    content: str = file_data.get("content", "")
    if not content:
        return f"Wiki page '{path}' is empty or not found."

    # `path#anchor` → the editor cut out that section (or found no such heading,
    # in which case the reader gets the whole page and is told so).
    page_path = file_data.get("path") or path.partition("#")[0]
    anchor_note = ""
    section = file_data.get("section")
    if section:
        content = section.get("content") or content
        path = f"{page_path}#{section.get('anchor')}"
        anchor_note = (f"_Section “{section.get('heading')}” only. Read "
                       f"`{page_path}` for the whole page._\n\n")
    elif file_data.get("anchor"):
        wanted = str(file_data["anchor"]).lstrip("#")
        path = page_path
        anchor_note = (f"> No heading with anchor `#{wanted}` on this page (it may "
                       f"have been renamed), so here is the whole page.\n\n")

    # Replace each local image reference with a base64 data URI so the model
    # can see the image without needing a browser session cookie.
    async def _embed(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        candidates = _resolve_img_paths(src, page_path)
        if not candidates:
            return match.group(0)  # external / data URI — keep as-is
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for resolved in candidates:
                    # Same internal-endpoint pattern as the page read: tenant
                    # is resolved from the bearer token, not a UI session.
                    r = await client.post(
                        f"{EDITOR_URL}/api/internal/wiki/image",
                        json={"token_hash": _token_hash(ctx), "path": resolved},
                    )
                    if r.status_code == 200:
                        mime = r.headers.get("content-type", "image/png").split(";")[0]
                        b64  = base64.standard_b64encode(r.content).decode()
                        return f"![{alt}](data:{mime};base64,{b64})"
            return match.group(0)
        except Exception:
            return match.group(0)  # leave original if fetch fails

    # Process image references sequentially (avoid parallel httpx sessions)
    result = content
    for match in list(_IMG_RE.finditer(content)):
        replacement = await _embed(match)
        result = result.replace(match.group(0), replacement, 1)

    # Close the loop the other way: the wiki UI shows a chip for the card a page
    # belongs to, and an agent reading the same page should see it too.
    backlink = _fmt_referring_cards(
        await _page_referring_cards(_tenant_slug(ctx), page_path))

    # Every section's anchor, so the agent can cite `page.md#anchor` exactly —
    # including the -1/-2 a repeated heading gets, which it cannot guess.
    heads = file_data.get("headings") or []
    if heads and not section:
        result = (result.rstrip("\n") + "\n\n---\n_Section anchors (link one as "
                  f"`{page_path}#anchor`, or read just it the same way):_ "
                  + ", ".join(f"`#{h.get('anchor')}`" for h in heads) + "\n")

    # A locked page reads normally but refuses every write. Say so up front so
    # the agent plans around it instead of discovering it as a 423 mid-edit.
    lock = file_data.get("lock") or {}
    notice = ""
    if lock.get("locked"):
        who = lock.get("locked_by_name") or "a user"
        reason = f" — {lock['reason']}" if lock.get("reason") else ""
        notice = (f"> 🔒 **Locked for editing** by {who}{reason}. Writes to this "
                  f"page will be refused; only a human can unlock it.\n\n")

    head = f"# {path}\n\n{notice}{anchor_note}"
    return f"{head}{backlink}\n{result}" if backlink else f"{head}{result}"


@mcp.tool()
async def wiki_tree(
    prefix: str = "",
    ctx: Context = None,
) -> str:
    """
    List / show me / browse / what pages do I have - every page in the
    Elefant wiki, optionally scoped to one folder.

    Use this to discover what already exists before searching, or to browse a
    subtree like the AI/ folder. Paths returned are space-relative and can be
    passed directly to get_wiki_page.

    Args:
        prefix: Optional path prefix to scope the listing (e.g. "AI/" or
                "AI/youtube-transcripts/"). Empty = list everything.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    # Tolerate a tenant-prefixed input by stripping the slug; normalise the
    # trailing slash so "AI/" and "AI" behave (and render) identically.
    prefix = _strip_tenant_prefix(
        (prefix or "").lstrip("/"), _tenant_slug(ctx),
    ).rstrip("/")

    try:
        data = await _editor_post("/api/internal/wiki/tree", {
            "token_hash": _token_hash(ctx),
            "prefix": prefix,
        })
    except httpx.HTTPStatusError as exc:
        detail = _http_err_detail(exc)
        return f"Wiki tree failed ({exc.response.status_code}): {detail}"
    except Exception as exc:
        return _internal_error("Wiki tree failed", exc)

    paths = data.get("paths") or []
    count = data.get("count", len(paths))
    if not paths:
        scope = f" under `{prefix}/`" if prefix else ""
        return f"No wiki pages found{scope}."

    scope_note = f", scoped to `{prefix}/`" if prefix else ""
    plural     = "" if count == 1 else "s"
    lines = [f"# Wiki Tree ({count} page{plural}{scope_note})\n"]

    # Render an indented tree from the sorted flat list by tracking the
    # last path's directory components and only emitting a header for each
    # directory the first time it appears at its depth.
    last_dirs: list[str] = []
    for p in paths:
        parts = p.split("/")
        dirs, name = parts[:-1], parts[-1]
        common = 0
        while (common < len(dirs)
               and common < len(last_dirs)
               and dirs[common] == last_dirs[common]):
            common += 1
        for i in range(common, len(dirs)):
            lines.append(f"{'  ' * i}- **{dirs[i]}/**")
        lines.append(f"{'  ' * len(dirs)}- {name}")
        last_dirs = dirs

    return "\n".join(lines)


@mcp.tool()
async def search(
    query: str,
    tags: str = "",
    limit: int = 10,
    wiki_boost: float = 1.15,
    ctx: Context = None,
) -> str:
    """
    Search / look up / find anything / find out / what do I know about /
    did I write anything about - across the whole Elefant ("elephant")
    knowledge base: wiki pages AND memories, in one ranked list. Use this
    for every question or lookup.

    CALL THIS FIRST WHEN UNSURE WHICH SEARCH TOOL TO USE - it searches
    across everything, including any wiki page about a card, a routine or a
    board. If you are unsure which tool fits at all, `tool_index` lists
    every tool by purpose.

    Searches both wiki documents AND stored memories together in one ranked
    list. Results are labelled 📖 Wiki or 🧠 Memory. This is the right tool
    for virtually all queries — only switch to wiki_search_only or
    memory_search if the user explicitly asks for one source.

    Results are labelled 📖 Wiki or 🧠 Memory. Wiki results are scored slightly
    higher when relevance is similar (wiki_boost), but strong memory matches
    still appear above weak wiki matches.

    Results are filtered by your token's label scope and classification access.

    Args:
        query:      Natural-language search query.
        tags:       Comma-separated tags to filter (applied to both sources).
        limit:      Maximum total results (1–20, default 10).
        wiki_boost: Score multiplier for wiki results (1.0–2.0, default 1.15).
                    Set to 1.0 for an unbiased merge of wiki and memory scores.
    """
    token_info = _token(ctx)
    has_wiki   = _check_permission(token_info, "wiki_read")
    has_memory = _check_permission(token_info, "memory_read")
    if not has_wiki and not has_memory:
        return _permission_denied("wiki_read")

    limit = max(1, min(limit, 20))
    slug = _tenant_slug(ctx)
    data = await _get_authed(
        "/api/search/unified",
        token_info,
        q=query,
        tags=tags or None,
        limit=limit,
        wiki_boost=wiki_boost,
        tenant_slug=slug,
    )
    results = data.get("results", [])
    for r in results:
        if r.get("source", "wiki") == "wiki":
            r["path"] = _strip_tenant_prefix(r.get("path", ""), slug)

    # Filter by source permissions
    if not has_wiki:
        results = [r for r in results if r.get("source") != "wiki"]
    if not has_memory:
        results = [r for r in results if r.get("source") != "memory"]

    before_acl = results
    results = _filter_by_label_scope(
        _filter_by_classification(results, token_info), token_info)

    if not results:
        return (f"No results found for: {query}"
                f"{_empty_reason(before_acl, results, token_info)}")

    lines = [f"## Unified Search: {query}  (wiki boost ×{wiki_boost})\n"]
    for i, r in enumerate(results, 1):
        source = r.get("source", "wiki")
        icon = "📖" if source == "wiki" else "🧠"
        tags_str = ", ".join(r.get("tags", [])) or "none"
        raw = r.get("raw_score", r.get("score", 0))
        adj = r.get("score", 0)
        score_str = f"{adj:.4f}" + (f" (raw {raw:.4f})" if source == "wiki" else "")

        if source == "wiki":
            section = f" § {r['section_title']}" if r.get("section_title") else ""
            header = f"### {i}. {icon} {r.get('title', r.get('path', ''))}{section}"
            meta = f"**Score:** {score_str} | **Path:** {_wiki_hit_path(r)} | **Tags:** {tags_str}"
        else:
            mem_type = r.get("type", "fact")
            header = f"### {i}. {icon} Memory `{r.get('id', '?')}` [{mem_type}]"
            meta = f"**Score:** {score_str} | **Tags:** {tags_str}"

        lines.append(f"{header}\n{meta}\n\n{r.get('excerpt', '').strip()}\n")

    return "\n".join(lines)


@mcp.tool()
async def memory_list(
    tags: str = "",
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """
    List / show me / what do you remember / what have you saved - the
    Elefant memories on file, optionally filtered by tags.

    Memories are discrete facts, decisions, preferences, or observations stored
    explicitly — separate from the wiki corpus. Use this to recall things that
    were intentionally remembered (e.g. user preferences, project decisions,
    key facts for a specific context).

    Results are filtered by your token's label scope and classification access.

    Args:
        tags:  Comma-separated tags to filter by (e.g. "project-x,decision").
               Leave empty to list all memories.
        limit: Maximum memories to return (default 50).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    data = await _get_authed("/api/memories", token_info,
                             tags=tags or None, limit=limit,
                             tenant_slug=_tenant_slug(ctx))
    memories = data.get("memories", [])
    memories = _filter_by_classification(memories, token_info)
    memories = _filter_by_label_scope(memories, token_info)

    if not memories:
        return "No memories found." + (f" (filter: tags={tags})" if tags else "")
    lines = [f"## Memories ({len(memories)} found)\n"]
    lines += [_fmt_memory(m, i) for i, m in enumerate(memories, 1)]
    return "\n".join(lines)


@mcp.tool()
async def memory_search(
    query: str,
    tags: str = "",
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """
    Search / look up / recall / what do I remember about - Elefant
    ("elephant") MEMORIES ONLY. Does not look at wiki pages.

    This tool searches only the stored memories collection. Do NOT use this
    for general knowledge base questions — use the 'search' tool instead,
    which searches both wiki documents and memories together. Unsure which
    tool fits? Call `search` first, or `tool_index` for the full list.

    Only use this when the user specifically asks to search memories only,
    or when they want to find stored facts/decisions/preferences that were
    explicitly added with memory_add.

    Results are filtered by your token's label scope and classification access.

    Args:
        query: Natural-language search query.
        tags:  Comma-separated tags to restrict results (e.g. "project-x,decision").
               Leave empty to search all memories.
        limit: Maximum number of results to return (1–20, default 10).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    limit = max(1, min(limit, 20))
    data = await _get_authed("/api/memories/search", token_info,
                             q=query, tags=tags or None, limit=limit,
                             tenant_slug=_tenant_slug(ctx))
    results = data.get("results", [])
    results = _filter_by_classification(results, token_info)
    results = _filter_by_label_scope(results, token_info)

    if not results:
        return f"No memories found matching: {query}"
    lines = [f"## Memory Search: {query}\n"]
    for i, r in enumerate(results, 1):
        score = r.get("score", 0)
        lines.append(_fmt_memory(r, i, score))
    return "\n".join(lines)


@mcp.tool()
async def memory_capture(
    content: str,
    type_hint: str = "",
    tags: list[str] | None = None,
    ctx: Context = None,
) -> str:
    """
    Remember this / save this / note that / keep this / don't forget / make
    a note of / for future reference - the DEFAULT way to put something
    into Elefant ("elephant") memory. This is the memory capture tool: a
    cheap streaming memory capture into the hot tier.

    WHICH ONE OF THESE FOUR. "Remember / save / note this" → memory_capture.
    "Remember this exactly, word for word" → memory_add. "Note to self …" →
    note_to_self, which files a board card. "Add a card / task / to-do", or
    an idea the user is not building right now → card_add. Longer than a
    post-it, or reference material somebody will read later → wiki_write.

    USE THIS AGGRESSIVELY. Anything the user mentions or implies that might
    matter later — preferences, decisions, project facts, host names,
    "where we left off", recurring people, vetoed approaches — should go
    here. The server writes it to a hot tier with no embedding cost; a
    background compactor periodically clusters captures, scores recurrence
    and cross-source diversity, and promotes the durable survivors to
    long-term memory. Bloat is cheap, missing context is expensive.

    Prefer this over `memory_add` for almost every write. Only fall back
    to memory_add when the user explicitly says "remember this exactly"
    or you're certain a single high-confidence durable record is wanted
    right now and shouldn't wait for compaction.

    The token's source attribution is applied automatically — agents
    don't need to pass it. PHI/PII/Sensitive tags require the
    corresponding classification_access on the token (same gate as
    memory_add).

    Args:
        content:   The text to capture.
        type_hint: Optional hint for the compactor: fact / event /
                   procedure / conversation / derived. Leave empty if
                   unclear; the compactor will infer.
        tags:      Optional tags for filtering and clustering.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_write"):
        return _permission_denied("memory_write")

    tags = list(tags or [])

    # Mirror memory_add's classification gate so PHI/PII can't sneak in via
    # the cheaper path.
    allowed_class = {c.lower() for c in (token_info.get("classification_access") or [])}
    blocked = {t.lower() for t in tags} & CLASSIFICATION_TAGS - allowed_class
    if blocked:
        return f"Permission denied — this token cannot tag memories as: {', '.join(t.upper() for t in blocked)}"

    # Auto-inject owner tag (matches memory_add behavior)
    owner_name = token_info.get("name", "")
    if owner_name:
        owner_tag = f"owner:{owner_name}"
        if owner_tag not in tags:
            tags.append(owner_tag)

    source = token_info.get("source") or "unknown"

    body = {
        "content":     content,
        "type_hint":   (type_hint or "").strip().lower() or None,
        "tags":        tags,
        "source":      source,
        "tenant_slug": _tenant_slug(ctx),
        # Session attribution is the join key behind memory_session and the
        # `Sessions involved:` line on memory_provenance. The helper returns
        # '' for clients that don't send mcp-session-id (Open WebUI etc.) —
        # collapse to None so we store NULL rather than empty string.
        "source_session_id": _session_id(ctx) or None,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{MEMORY_API_URL}/memory/capture",
                json=body,
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return _internal_error("Capture failed", exc)

    # Surface the session id: it is the join key `memory_session` needs, and
    # without it here an agent had no way to learn its own session and could
    # only call that tool with a guess (issue #93). When the client doesn't
    # send mcp-session-id at all, say THAT rather than printing nothing —
    # otherwise the absence looks like a bug in memory_session instead of a
    # missing header from the client.
    sid = data.get("session") or body.get("source_session_id")
    session_line = (
        f"Session: {sid}   ← pass this to `memory_session` to replay this session"
        if sid else
        "Session: none — this MCP client did not send an `mcp-session-id`, so "
        "this capture has no session attribution and `memory_session` cannot "
        "find it."
    )

    if data.get("deduped"):
        return (f"Captured (deduped against an identical write in the last 60s).\n\n"
                f"ID:      {data.get('id', '?')}\n"
                f"{session_line}")
    return (f"Captured to hot tier.\n\n"
            f"ID:      {data.get('id', '?')}\n"
            f"Source:  {source}\n"
            f"Tags:    {', '.join(tags) or 'none'}\n"
            f"{session_line}")


@mcp.tool()
async def memory_add(
    content: str,
    tags: list[str] | None = None,
    type: str = "fact",
    ctx: Context = None,
) -> str:
    """
    Remember this exactly / save this word for word / commit this to memory
    permanently - store one Elefant memory straight into the durable tier,
    skipping the hot tier. Use memory_capture unless the user asked for
    exactly this.

    WHICH ONE OF THESE FOUR. "Remember / save / note this" → memory_capture.
    "Remember this exactly, word for word" → memory_add. "Note to self …" →
    note_to_self, which files a board card. "Add a card / task / to-do", or
    an idea the user is not building right now → card_add. Longer than a
    post-it, or reference material somebody will read later → wiki_write.

    This is the explicit, deduped, high-confidence path. For most writes
    you should prefer `memory_capture` instead — that streams to a cheap
    hot tier and the compactor promotes the survivors. Use memory_add
    only when:
      • the user explicitly says "remember this exactly" or "save this", or
      • you're certain a single durable record is wanted right now (not
        a candidate that might dedupe with future captures)

    The token owner's name is automatically added as an `owner:<name>` tag.
    Memories tagged with PHI/PII/Sensitive require the token to have the
    corresponding classification_access permission.

    Args:
        content: The text of the memory to store.
        tags:    Optional list of tags for categorisation and filtering.
        type:    Memory type — one of: fact, event, procedure, conversation, derived.
                 Defaults to "fact".
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_write"):
        return _permission_denied("memory_write")

    tags = list(tags or [])

    # Enforce classification write permission
    allowed_class = {c.lower() for c in (token_info.get("classification_access") or [])}
    blocked = {t.lower() for t in tags} & CLASSIFICATION_TAGS - allowed_class
    if blocked:
        return f"Permission denied — this token cannot tag memories as: {', '.join(t.upper() for t in blocked)}"

    # Auto-inject owner tag
    owner_name = token_info.get("name", "")
    if owner_name:
        owner_tag = f"owner:{owner_name}"
        if owner_tag not in tags:
            tags.append(owner_tag)

    # Source attribution is structural — read from the token rather than asking
    # the agent to pass it on every call. A pre-source-PR token reports
    # "legacy" via the editor's migration; tokens minted before the UI shipped
    # default to "unknown".
    source = token_info.get("source") or "unknown"

    data = await _post("/api/memories", {"content": content, "tags": tags, "type": type,
                                         "source": source,
                                         "tenant_slug": _tenant_slug(ctx)}, token_info)
    mem = data.get("memory", data)
    return (
        f"✅ Memory stored.\n\n"
        f"**ID:** `{mem.get('id', '?')}`\n"
        f"**Type:** {mem.get('type', type)}\n"
        f"**Tags:** {', '.join(mem.get('tags', [])) or 'none'}\n"
        f"**Created:** {mem.get('created_at', '')}\n\n"
        f"{mem.get('content', content)}"
    )


@mcp.tool()
async def memory_update(
    memory_id: str,
    content: str = "",
    tags: list[str] | None = None,
    type: str = "",
    ctx: Context = None,
) -> str:
    """
    Correct / change / fix / edit / that's wrong, it's actually - update the
    content, tags or type of a memory Elefant already holds, in either tier.

    Works on durable memories AND on pending hot-tier captures: if the id
    isn't durable, the hot tier is tried automatically, so you can correct
    a bad capture immediately without waiting for the compactor. Durable
    edits are re-embedded (when content changes) and recorded in the
    memory_revisions audit trail; hot edits are in-place (the hot tier is
    pre-compaction scratch). Hot captures that were already promoted or
    dropped are immutable — promoted ones point you at the durable id.

    At least one of `content`, `tags`, or `type` must be provided.

    Args:
        memory_id: The UUID of the memory to update (durable or hot).
        content:   New content text (leave empty to keep existing content).
        tags:      New tag list (replaces existing tags; omit to keep existing).
        type:      New memory type (fact, event, procedure, conversation, derived).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_write"):
        return _permission_denied("memory_write")

    body: dict = {}
    if content:
        body["content"] = content
    if tags is not None:
        # Enforce classification write permission on new tags
        allowed_class = {c.lower() for c in (token_info.get("classification_access") or [])}
        blocked = {t.lower() for t in tags} & CLASSIFICATION_TAGS - allowed_class
        if blocked:
            return f"Permission denied — this token cannot tag memories as: {', '.join(t.upper() for t in blocked)}"
        body["tags"] = tags
    if type:
        body["type"] = type
    if not body:
        return "❌ Nothing to update — provide content, tags, and/or type."

    body["tenant_slug"] = _tenant_slug(ctx)
    # The MCP path carries no UI session, so the embedder's _auth_ctx will
    # return None and the revision row would otherwise record edited_by=NULL.
    # Pass the token's display name through the body — embedder uses it
    # ONLY when no real session is present (it can't override authenticated
    # identity). Owner check stays untouched: we deliberately don't forward
    # X-Auth-User-Id here because the token's user_id (a UUID) wouldn't
    # match the `owner:<name>` tag auto-injected at capture time.
    token_name = (token_info.get("name") or "").strip()
    if token_name:
        body["edited_by_hint"] = token_name

    try:
        data = await _put(f"/api/memories/{memory_id}", body, token_info)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # Not durable — maybe it's a hot-tier capture that hasn't been
            # promoted yet. Try the hot tier so a bad capture can be
            # corrected immediately instead of waiting out the compactor.
            return await _update_hot_fallback(memory_id, content, tags, type, ctx)
        detail = _http_err_detail(exc)
        return f"Memory update failed ({exc.response.status_code}): {detail}"
    except Exception as exc:
        return _internal_error("Memory update failed", exc)

    mem = data.get("memory", data)
    return (
        f"✅ Memory `{memory_id}` updated.\n\n"
        f"**Type:** {mem.get('type', '')}\n"
        f"**Tags:** {', '.join(mem.get('tags', [])) or 'none'}\n"
        f"**Updated:** {mem.get('updated_at', '')}\n\n"
        f"{mem.get('content', '')}"
    )


async def _update_hot_fallback(
    memory_id: str, content: str, tags: list[str] | None, type: str,
    ctx: Context,
) -> str:
    """memory_update's second leg: the id wasn't durable, so try it as a
    pending hot-tier capture via memory-api PUT /memory/hot/{id}."""
    hot_body: dict = {"tenant_slug": _tenant_slug(ctx)}
    if content:
        hot_body["content"] = content
    if tags is not None:
        hot_body["tags"] = tags
    if type:
        hot_body["type_hint"] = type
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                f"{MEMORY_API_URL}/memory/hot/{memory_id}",
                json=hot_body,
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        detail = _http_err_detail(exc)
        if code == 404:
            return (f"❌ Memory `{memory_id}` not found in either tier — "
                    "not a durable memory and not a hot capture.")
        if code == 409:
            # Promoted or dropped — memory-api's detail explains which and,
            # for promoted rows, includes the durable id to edit instead.
            return f"❌ Cannot edit hot capture `{memory_id}`: {detail}"
        return f"Hot-tier update failed ({code}): {detail}"
    except Exception as exc:
        return _internal_error("Hot-tier update failed", exc)

    return (
        f"✅ Hot capture `{memory_id}` updated (pending — not yet promoted).\n\n"
        f"**Type hint:** {data.get('type_hint') or '—'}\n"
        f"**Tags:** {', '.join(data.get('tags', [])) or 'none'}\n\n"
        f"{data.get('content', '')}"
    )


@mcp.tool()
async def memory_delete(
    memory_id: str,
    ctx: Context = None,
) -> str:
    """
    Forget this / delete / remove / drop that - delete one Elefant memory by
    its id.

    This permanently removes the memory and its embedding from the vector store.
    Use with care — there is no undo.

    Args:
        memory_id: The UUID of the memory to delete.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_write"):
        return _permission_denied("memory_write")

    try:
        data = await _delete(
            f"/api/memories/{memory_id}?tenant_slug={_tenant_slug(ctx)}", token_info)
    except UpstreamError as exc:
        # A missing memory is an ordinary answer to "delete this", not a
        # failure — say so plainly instead of surfacing an exception.
        if exc.status == 404:
            return (f"No memory `{memory_id}` in this space — nothing to delete. "
                    f"It may already be gone, or belong to another tenant.")
        return _internal_error(f"Could not delete `{memory_id}`", exc)
    return f"🗑️ Memory `{memory_id}` deleted. Status: {data.get('status', 'ok')}"


@mcp.tool()
async def memory_events(
    limit: int = 20,
    offset: int = 0,
    ctx: Context = None,
) -> str:
    """
    Show the log / audit trail / what has been happening - the structured
    event log of Elefant memory activity.

    Returns a paginated list of recent storage/retrieval events recorded in
    PostgreSQL. Use this to audit what has been recently stored or retrieved,
    or to trace activity in an agent pipeline.

    Args:
        limit:  Number of events to return (default 20, max 100).
        offset: Pagination offset (default 0).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    limit = max(1, min(limit, 100))
    # Previously this call omitted tenant_slug, so memory-api fell back
    # to a default of "default" — every MCP caller saw the default
    # tenant's event log regardless of which org their token belonged
    # to. Pass the verified slug from the token so RLS scopes events to
    # the caller's tenant.
    data = await _get_memory_api(
        "/events", limit=limit, offset=offset, tenant_slug=_tenant_slug(ctx),
    )
    events = data.get("events", data) if isinstance(data, dict) else data

    if not events:
        return "No events found."

    lines = [f"## Event Log ({len(events)} events, offset={offset})\n"]
    for e in events:
        ev_id   = e.get("id", "?")
        ev_type = e.get("type", "unknown")
        ev_ts   = e.get("created", e.get("created_at", ""))
        content = str(e.get("content", ""))[:200]
        lines.append(f"### Event `{ev_id}` — {ev_type}\n**Time:** {ev_ts}\n\n{content}\n")
    return "\n".join(lines)


@mcp.tool()
async def memory_provenance(
    memory_id: str,
    ctx: Context = None,
) -> str:
    """
    Why do I remember this / where did this come from / how do you know
    that - trace one durable Elefant memory back to the captures it was
    made from.

    Returns the trail backward from a durable memory to the hot-tier
    captures it was synthesised from: when it was promoted, what its
    compactor score was, which sessions contributed, and the original
    text of each capture that merged into it.

    Use this to answer "why do I remember this?" or "where did this come
    from?" Especially useful when a search hit looks surprising and you
    want to understand its origin before acting on it.

    Memories created before promotion-time provenance tracking (or via
    direct memory_add, which bypasses the hot tier) are reported as
    "no provenance recorded" — that's expected, not an error.

    Args:
        memory_id: The durable memory id (UUID), as returned by search,
                   memory_search, or memory_list.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    slug = _tenant_slug(ctx)

    # 1. Fetch the durable memory itself via the embedder.
    try:
        durable = await _get_authed(f"/api/memories/{memory_id}", token_info,
                                    tenant_slug=slug)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Memory `{memory_id}` not found in your tenant."
        return _internal_error(f"Failed to fetch memory `{memory_id}`", exc)
    except Exception as exc:
        return _internal_error(f"Failed to fetch memory `{memory_id}`", exc)

    hot_ids       = durable.get("originated_from_hot_ids") or []
    session_ids   = durable.get("originated_session_ids") or []
    score         = durable.get("compactor_score")
    promoted_at   = durable.get("promoted_at")
    oldest_cap    = durable.get("oldest_captured_at")
    content       = (durable.get("content") or "").strip()
    mem_type      = durable.get("type", "fact")
    mem_tags      = durable.get("tags") or []
    mem_source    = durable.get("source", "unknown")
    mem_created   = durable.get("created_at", "")

    # 2. If there's no recorded provenance, say so plainly and show what we
    #    do know. Old durable memories and direct memory_add writes land here.
    if not hot_ids and promoted_at is None and score is None:
        lines = [
            f"# Provenance for memory `{memory_id}`\n",
            "**No provenance recorded.** This memory was created either",
            "before promotion-time provenance tracking, or via a direct",
            "`memory_add` call that bypasses the hot tier.\n",
            "## Current record",
            f"- **Type:** {mem_type}",
            f"- **Source:** {mem_source}",
            f"- **Created:** {mem_created or '—'}",
            f"- **Tags:** {', '.join(mem_tags) or 'none'}",
            "",
            "## Content",
            content or "_(empty)_",
        ]
        return "\n".join(lines)

    # 3. Fetch the original hot-tier captures (content + session + score)
    #    so the trail shows what was actually written, not just opaque IDs.
    hot_rows: list[dict] = []
    if hot_ids:
        try:
            data = await _get_memory_api(
                "/memory/hot/by-ids",
                tenant_slug=slug,
                ids=",".join(hot_ids),
            )
            hot_rows = data.get("rows") or []
        except Exception:
            # Soft-fail: still show what we know from the durable record.
            # mcp/main.py has no logger by design; the missing rows are
            # surfaced to the caller via the "Hot ids referenced but not
            # retrievable" note further down.
            pass

    found_ids   = {r.get("id") for r in hot_rows}
    missing_ids = [hid for hid in hot_ids if hid not in found_ids]

    lines = [
        f"# Provenance for memory `{memory_id}`\n",
        f"**Promoted at:** {promoted_at or '—'}",
        f"**Compactor score:** {score:.4f}" if isinstance(score, (int, float)) else "**Compactor score:** —",
        f"**Earliest contributing capture:** {oldest_cap or '—'}",
        f"**Hot captures merged:** {len(hot_ids)}",
    ]
    if session_ids:
        lines.append(f"**Sessions involved:** {', '.join(session_ids)}")
    lines.append("")
    lines.append("## Durable record")
    lines.append(f"- **Type:** {mem_type}")
    lines.append(f"- **Source:** {mem_source}")
    lines.append(f"- **Created:** {mem_created or '—'}")
    lines.append(f"- **Tags:** {', '.join(mem_tags) or 'none'}")
    lines.append("")
    lines.append("## Original captures")
    if not hot_rows:
        lines.append("_Hot-tier rows not retrievable (may have aged out or "
                     "memory-api unreachable). Hot ids: "
                     f"{', '.join(hot_ids)}_")
    else:
        # Order by captured_at so the trail reads chronologically.
        hot_rows.sort(key=lambda r: r.get("captured_at") or "")
        for i, r in enumerate(hot_rows, 1):
            status = ("promoted" if r.get("promoted_at")
                      else "dropped" if r.get("dropped_at")
                      else "pending")
            cap_ts = r.get("captured_at") or "—"
            src    = r.get("source") or "unknown"
            sess   = r.get("source_session_id") or "—"
            body   = (r.get("content") or "").strip()
            lines.append(
                f"\n### {i}. `{r.get('id')}` — {status}\n"
                f"**Captured:** {cap_ts} · **Source:** {src} · **Session:** {sess}\n\n"
                f"{body or '_(empty)_'}"
            )
        if missing_ids:
            lines.append(
                f"\n_Note: {len(missing_ids)} hot id(s) referenced by this "
                f"memory could not be fetched: {', '.join(missing_ids)}._"
            )
    return "\n".join(lines)


@mcp.tool()
async def memory_introspect(
    window: str = "7d",
    ctx: Context = None,
) -> str:
    """
    How is my memory doing / give me the stats / overview of what you have
    stored - an aggregate retrospective of Elefant memory activity in your
    tenant.

    Shows what you captured, what got promoted, what got dropped, and the
    type / tag / source / score distributions over the given time window.
    Use this to answer "how is my memory doing?" — to see whether recent
    captures are surviving compaction, spot tag/type imbalances, or
    sanity-check compactor behaviour.

    For the trail of a *specific* durable memory back to its hot captures,
    use memory_provenance(memory_id) instead.

    Args:
        window: Time window to summarise. "24h", "7d", "30d", "Nh"/"Nd"/"Nw"
                or "all". Default "7d". "Pending" is always current state
                regardless of window — a row is either pending right now or
                it isn't.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    try:
        data = await _get_memory_api(
            "/memory/introspect",
            tenant_slug=_tenant_slug(ctx),
            window=window,
        )
    except httpx.HTTPStatusError as exc:
        detail = _http_err_detail(exc)
        return f"Memory introspect failed ({exc.response.status_code}): {detail}"
    except Exception as exc:
        return _internal_error("Memory introspect failed", exc)

    totals    = data.get("totals") or {}
    by_type   = data.get("by_type") or []
    by_source = data.get("by_source") or []
    top_tags  = data.get("top_tags") or []
    score     = data.get("score_distribution") or {}
    win       = data.get("window") or window
    win_start = (data.get("window_start") or "")
    win_start_short = win_start.split("T")[0] if win_start else ""

    captures = int(totals.get("captures") or 0)
    promoted = int(totals.get("promoted") or 0)
    dropped  = int(totals.get("dropped")  or 0)
    pending  = int(totals.get("pending")  or 0)

    win_header = f"{win}" + (f", since {win_start_short}" if win_start_short else "")

    if captures == 0 and promoted == 0 and dropped == 0 and pending == 0:
        return f"# Memory introspection ({win_header})\n\nNo memory activity in this window."

    def _pct(numer: int, denom: int) -> str:
        return f"{100.0 * numer / denom:.0f}%" if denom > 0 else "—"

    lines = [
        f"# Memory introspection ({win_header})\n",
        "## Lifecycle",
        f"- **Captured:** {captures}",
        f"- **Promoted:** {promoted} ({_pct(promoted, captures)} of captures)",
        f"- **Dropped:** {dropped} ({_pct(dropped, captures)} of captures)",
        f"- **Pending (current):** {pending}",
        "",
    ]

    if by_type:
        lines.append("## By type")
        for row in by_type:
            lines.append(f"- **{row.get('type', 'fact')}**: {row.get('count', 0)}")
        lines.append("")

    if by_source:
        lines.append("## By source")
        for row in by_source[:10]:
            lines.append(f"- **{row.get('source', 'unknown')}**: {row.get('count', 0)}")
        lines.append("")

    if top_tags:
        lines.append("## Top tags")
        for row in top_tags[:10]:
            lines.append(f"- `{row.get('tag', '')}` ({row.get('count', 0)})")
        lines.append("")

    n_scored = int(score.get("count") or 0)
    if n_scored > 0:
        def _f(v):
            return f"{v:.3f}" if isinstance(v, (int, float)) else "—"
        lines.append(f"## Compactor scores ({n_scored} scored)")
        lines.append(f"- **Range:** {_f(score.get('min'))} – {_f(score.get('max'))}")
        lines.append(f"- **Mean:** {_f(score.get('mean'))}")
        lines.append(
            f"- **Percentiles:** "
            f"p25={_f(score.get('p25'))} · p50={_f(score.get('p50'))} · "
            f"p75={_f(score.get('p75'))} · p95={_f(score.get('p95'))}"
        )
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def memory_revisions(
    memory_id: str,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    What changed / edit history / earlier versions of this memory - the
    revision trail of one durable Elefant memory.

    Returns each prior version that existed before an update overwrote it —
    the snapshot of content/type/tags, when the change happened, who made
    it, and which fields they touched. Use this to audit how a memory's
    content evolved, or to answer "what did this say before I edited it
    last time?"

    Memories that have never been updated return an empty history — not
    an error, just no edits to show. Updates that didn't actually change
    any field (no-op PUTs) don't create revision rows either.

    Args:
        memory_id: The durable memory id (UUID).
        limit:     Max revisions to return (1–200, default 20).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    limit = max(1, min(int(limit or 20), 200))

    try:
        data = await _get_memory_api(
            f"/memory/revisions/{memory_id}",
            tenant_slug=_tenant_slug(ctx),
            limit=limit,
        )
    except httpx.HTTPStatusError as exc:
        detail = _http_err_detail(exc)
        return f"Memory revisions fetch failed ({exc.response.status_code}): {detail}"
    except Exception as exc:
        return _internal_error("Memory revisions fetch failed", exc)

    revisions = data.get("revisions") or []
    count     = int(data.get("count") or 0)

    if count == 0:
        return (
            f"# Edit history of memory `{memory_id}`\n\n"
            "No edits recorded. This memory has either never been updated, "
            "or its only updates were no-ops (didn't actually change any field)."
        )

    lines = [
        f"# Edit history of memory `{memory_id}`\n",
        f"{count} prior version{'s' if count != 1 else ''} (newest first).\n",
    ]
    # Revisions come back newest-first; number them so version N is the
    # most recent prior state, version 1 the oldest snapshot recorded.
    for idx, r in enumerate(revisions):
        version_num = count - idx
        edited_at   = r.get("edited_at") or "—"
        edited_by   = r.get("edited_by") or "—"
        fields      = r.get("changed_fields") or []
        snap_from   = r.get("snapshot_created_at") or "—"
        snap_until  = r.get("snapshot_updated_at") or "—"
        content     = (r.get("content") or "").strip()
        tags        = r.get("tags") or []
        rev_type    = r.get("type") or "—"

        # A snapshot whose created_at == updated_at had never been edited
        # before — "X → X" reads like a zero-length window, so render it as
        # "since X" instead.
        active_span = (f"since {snap_from}" if snap_from == snap_until
                       else f"{snap_from} → {snap_until}")

        lines.append(
            f"\n## Version {version_num} — overwritten {edited_at} by `{edited_by}`"
        )
        lines.append(
            f"**Changed fields:** {', '.join(fields) or '—'} · "
            f"**Was active:** {active_span}"
        )
        lines.append(f"**Type:** {rev_type} · **Tags:** {', '.join(tags) or 'none'}")
        lines.append("\n**Content (previous):**")
        lines.append(content if content else "_(empty)_")

    return "\n".join(lines)


@mcp.tool()
async def memory_session(
    session_id: str,
    limit: int = 200,
    ctx: Context = None,
) -> str:
    """
    What did we talk about / what did you save in that session - every
    Elefant memory captured under one session, as a diary.

    Returns two views joined into one chronological narrative:
    - **Durable memories** whose `originated_session_ids` contains the
      given session (i.e. they were promoted, in part, from captures
      this session made).
    - **Hot-tier captures** with the matching `source_session_id`
      (pending, promoted, or dropped) — the raw record of what was
      written during the session, including things that didn't survive
      compaction.

    Use this to pick up where another agent (or your past self) left off,
    or to audit "what happened in session X?" Especially useful for
    cross-agent handoffs.

    Memories captured before Tier 1.1 don't carry session attribution on
    the durable side and won't appear there even if the matching hot row
    is still present (it will still appear in the hot section).

    Args:
        session_id: The MCP session id, as set by the calling client.
        limit:      Cap per side (1-1000, default 200).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "memory_read"):
        return _permission_denied("memory_read")

    sid = (session_id or "").strip()
    if not sid:
        return "❌ session_id is required."

    limit = max(1, min(int(limit or 200), 1000))
    slug  = _tenant_slug(ctx)

    # Two independent backends — hot rows from memory-api, durable from
    # the embedder. Awaited sequentially; both are small (capped at `limit`)
    # so the round-trip difference vs gather() is negligible for this tool.
    hot_data: dict     = {}
    durable_data: dict = {}
    try:
        hot_data = await _get_memory_api(
            f"/memory/hot/by-session/{sid}", tenant_slug=slug, limit=limit,
        )
    except Exception as exc:
        return _internal_error("Session diary fetch failed (hot tier)", exc)
    try:
        durable_data = await _get_authed(
            "/api/memories",
            token_info,
            tenant_slug=slug,
            session_id=sid,
            limit=limit,
        )
    except Exception as exc:
        # Soft-fail durable: show at least the hot side.
        durable_data = {"memories": [], "_error": _internal_error("durable side unreachable", exc)}

    hot_rows = hot_data.get("rows") or []
    durables = durable_data.get("memories") or []

    if not hot_rows and not durables:
        return (
            f"# Session `{sid}`\n\n"
            "No captures recorded under this session in your tenant. "
            "Either the session id is wrong, the session has nothing yet, "
            "or its captures pre-date session tracking."
        )

    lines = [f"# Session `{sid}`\n"]
    durable_err = durable_data.get("_error")
    if durable_err:
        lines.append(f"_⚠ {durable_err}_\n")

    if durables:
        # Sort newest first so a fresh handoff sees the latest first.
        durables.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        lines.append(f"## Durable memories ({len(durables)})")
        for m in durables:
            mid     = m.get("id", "?")
            mtype   = m.get("type", "fact")
            tags    = m.get("tags") or []
            created = (m.get("created_at") or "")[:19]
            content = (m.get("content") or "").strip()
            lines.append(
                f"\n### `{mid}` · {mtype} · {created}\n"
                f"**Tags:** {', '.join(tags) or 'none'}\n\n"
                f"{content if content else '_(empty)_'}"
            )
        lines.append("")

    if hot_rows:
        lines.append(f"## Hot captures ({len(hot_rows)})")
        for r in hot_rows:
            status = ("promoted" if r.get("promoted_at")
                      else "dropped" if r.get("dropped_at")
                      else "pending")
            cap_ts = (r.get("captured_at") or "")[:19]
            tags   = r.get("tags") or []
            body   = (r.get("content") or "").strip()
            rid    = r.get("id", "?")
            promo  = r.get("promoted_to")
            promo_note = f" → durable `{promo}`" if promo else ""
            lines.append(
                f"\n### `{rid}` · {status}{promo_note} · {cap_ts}\n"
                f"**Tags:** {', '.join(tags) or 'none'}\n\n"
                f"{body if body else '_(empty)_'}"
            )

    return "\n".join(lines)


@mcp.tool()
async def reindex_status(ctx: Context = None) -> str:
    """
    How is the reindex going / is it finished yet - status and progress of
    the Elefant wiki reindex.

    Returns the indexing state (idle / indexing / watching), progress counters,
    timing of the last reindex, the active embedding provider and model, and
    any recent errors. Use this after calling wiki_reindex to monitor progress,
    or to verify the index is healthy before running a large search session.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    data    = await _get_authed("/api/status", token_info,
                                tenant_slug=_tenant_slug(ctx))
    state   = data.get("state", "unknown")
    total   = data.get("total_docs", 0)
    indexed = data.get("indexed_docs", 0)
    proc    = data.get("files_processed", 0)
    ftotal  = data.get("files_total", 0)
    cur     = data.get("current_file") or "—"
    prov    = data.get("provider", "unknown")
    model   = data.get("model", "unknown")
    vsize   = data.get("vector_size", "?")
    dur     = data.get("last_reindex_duration_s")
    end     = data.get("last_reindex_end") or "never"
    errors  = data.get("errors", [])
    failed        = data.get("last_reindex_failed", [])
    failed_count  = data.get("last_reindex_failed_count", 0)
    docs_total    = data.get("last_reindex_docs_total", 0)
    docs_indexed  = data.get("last_reindex_docs_indexed", 0)
    reconciled    = data.get("last_reindex_reconciled", 0)
    skipped       = data.get("last_reindex_skipped_unchanged", 0)

    progress = f"{proc}/{ftotal} files" if ftotal else "—"
    dur_str  = f"{dur}s" if dur is not None else "—"

    lines = [
        "## Reindex Status\n",
        f"**State:** {state}",
        f"**Wiki docs on disk:** {total} | **Indexed chunks:** {indexed}",
        f"**Progress:** {progress} | **Current file:** `{cur}`",
        f"**Last reindex:** {end} (took {dur_str})",
        f"**Embedding:** {prov} / `{model}` (dim={vsize})",
    ]
    # Coverage: how many docs the last run actually indexed vs. walked. A gap
    # here is the "docs on disk but not searchable" bug (issue #83) made loud.
    if docs_total:
        cov = f"**Coverage:** {docs_indexed}/{docs_total} docs indexed"
        if skipped:
            cov += f" ({skipped} unchanged, not re-embedded)"
        if failed_count:
            cov += f" — ⚠️ {failed_count} FAILED (not searchable)"
        lines.append(cov)
    if reconciled:
        lines.append(f"**Reconciled from S3:** {reconciled} doc(s) pulled into the mirror before indexing")
    if failed:
        shown = failed[:20]
        lines.append(f"\n**Docs that failed to index ({failed_count}):**")
        for f in shown:
            lines.append(f"- `{f}`")
        if failed_count > len(shown):
            lines.append(f"- …and {failed_count - len(shown)} more")
    if errors:
        lines.append(f"\n**Recent errors ({len(errors)}):**")
        for e in errors:
            lines.append(f"- {e}")
    return "\n".join(lines)


@mcp.tool()
async def wiki_reindex(ctx: Context = None) -> str:
    """
    Reindex / rebuild the search index / re-embed the wiki - trigger a full
    reindex of every Elefant wiki document in the vector store.

    Use this after bulk content changes (file uploads, git pulls) to ensure
    the semantic search index is up to date. The reindex runs in the background;
    search continues to work during indexing.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    data = await _post("/api/reindex", {"tenant_slug": _tenant_slug(ctx)}, token_info)
    return f"🔄 Reindex triggered. Status: {data.get('status', 'started')}"


@mcp.tool(structured_output=False)
async def wiki_write(
    path: str,
    content: str,
    ctx: Context = None,
) -> CallToolResult | str:
    """
    Write / create / start / draft / write up a new page - create an Elefant
    ("elephant") wiki page of long-form Markdown, or replace one whole.

    Agents may only write inside the org's designated AI folder (default
    "AI/"). Pages there are clearly flagged as AI-generated in the wiki UI;
    a human operator "promotes" a page by moving it out of the AI folder,
    after which agents can no longer modify it. You CAN organise content into
    subfolders under the AI folder (e.g. "AI/runbooks/db-restore.md").

    Use this for durable, structured, human-readable reference material —
    runbooks, summaries, design notes — not for ephemeral memory (use
    memory_capture for that).

    Every ```mermaid block in the content is parsed by Mermaid before the page
    is saved, as the diagram tools do. A block that does not parse is refused
    with its line number and nothing is written; a saved page that carries a
    diagram reports `validated` in its structured content. To add a diagram
    page on its own, `diagram_create` is the shorter path.

    Args:
        path:    Page path under the AI folder, ending in .md
                 (e.g. "AI/runbooks/db-restore.md"). A bare name like
                 "notes.md" is auto-placed under the AI folder.
        content: Full Markdown content of the page (replaces existing content
                 if the page already exists).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    slug   = _tenant_slug(ctx)
    folder = await _ai_wiki_folder(slug)

    # Normalise the path and keep it inside the AI folder. A bare filename or a
    # path missing the prefix is auto-placed under the folder for convenience.
    rel = (path or "").strip().lstrip("/")
    if ".." in rel.split("/"):
        return f"Invalid path: '{path}'. Path traversal ('..') is not allowed."
    if not rel:
        return "A page path is required (e.g. 'AI/notes.md')."
    if not (rel == folder or rel.startswith(folder + "/")):
        rel = f"{folder}/{rel}"
    if not rel.startswith(folder + "/") or len(rel) <= len(folder) + 1:
        return f"AI agents may only write pages under '{folder}/'."
    if not rel.lower().endswith((".md", ".markdown")):
        rel = rel + ".md"

    refusal, checked = await _check_page_diagrams(content or "")
    if refusal:
        return refusal

    try:
        data = await _editor_post("/api/internal/wiki/write", {
            "token_hash": _token_hash(ctx),
            "path": rel,
            "content": content,
        })
    except httpx.HTTPStatusError as exc:
        detail = _http_err_detail(exc)
        return f"Wiki write failed ({exc.response.status_code}): {detail}"
    except Exception as exc:
        return _internal_error("Wiki write failed", exc)

    saved = data.get("path", rel)
    verb  = "Created" if data.get("created") else "Updated"
    return _page_saved(
        f"✅ {verb} wiki page `{saved}` (flagged AI-generated). Reindex triggered.", checked)


_UPLOAD_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
                     "image/jpg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
_UPLOAD_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".gif": "image/gif", ".webp": "image/webp"}


@mcp.tool()
async def wiki_upload_image(
    path: str,
    image_base64: str,
    content_type: str = "",
    page: str = "",
    ctx: Context = None,
) -> str:
    """
    Attach / embed / upload / put a picture, screenshot, diagram or mockup in
    a wiki page - store an image in the Elefant ("elephant") wiki so a page
    can show it inline instead of linking out.

    The write half of what `get_wiki_page` already reads: upload the image
    here, then reference it from the page with ordinary Markdown image syntax
    `![alt](ref)` in `wiki_write` / `wiki_edit`. `get_wiki_page` returns the
    image inline, and the wiki UI renders it. Pass `page` and the reply gives
    you the exact `ref` to paste for that page.

    Same boundary as wiki_write: the image must live under the AI folder.
    Keep a page's images beside it, conventionally in an `assets/` folder
    (e.g. "AI/projects/tailor/assets/teaser.png" for pages in
    "AI/projects/tailor/"). Uploading to an existing path replaces the image.

    PNG, JPEG, GIF and WebP, up to about 7MB (base64 adds a third, and a
    tool call is capped at 10MB). SVG is refused — it can carry script. Export a diagram to PNG instead. For a PDF, export the page you
    want as an image.

    Args:
        path:         Where to store it, under the AI folder, ending in the
                      type's extension (.png, .jpg, .gif, .webp). A bare
                      name like "teaser.png" is placed under the AI folder; a
                      path with no extension gets one from content_type.
        image_base64: The file's bytes, base64-encoded. A `data:` URI works too.
        content_type: image/png | image/jpeg | image/gif | image/webp.
                      Optional when the path's extension already says.
        page:         Optional — the page that will embed it, e.g.
                      "AI/projects/tailor/branding.md". The reply then gives
                      the relative reference to use from that page.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    slug = _tenant_slug(ctx)
    folder = await _ai_wiki_folder(slug)
    try:
        rel = _ai_path(path, folder)
    except ValueError as exc:
        return str(exc)
    if not rel.startswith(folder + "/") or len(rel) <= len(folder) + 1:
        return f"Give the image a file name under '{folder}/', e.g. '{folder}/assets/diagram.png'."

    mime = (content_type or "").strip().lower()
    ext = PurePosixPath(rel).suffix.lower()
    if not mime:
        mime = _UPLOAD_IMAGE_MIME.get(ext, "")
        if not mime:
            return ("Say what kind of image this is: pass content_type "
                    "(image/png, image/jpeg, image/gif or image/webp), or end "
                    "the path in .png, .jpg, .gif or .webp.")
    elif not ext and mime in _UPLOAD_IMAGE_EXT:
        rel += _UPLOAD_IMAGE_EXT[mime]

    try:
        data = await _editor_post("/api/internal/wiki/upload-image", {
            "token_hash": _token_hash(ctx),
            "path": rel,
            "content_type": mime,
            "image_base64": image_base64 or "",
        })
    except httpx.HTTPStatusError as exc:
        return f"Image upload failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Image upload failed", exc)

    saved = data.get("path", rel)
    verb = "Uploaded" if data.get("created") else "Replaced"
    size = data.get("bytes") or 0
    size_txt = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} bytes"
    alt = PurePosixPath(saved).stem.replace("-", " ").replace("_", " ")
    page = _strip_tenant_prefix((page or "").strip(), slug).lstrip("/")
    if page:
        if not page.lower().endswith((".md", ".markdown")):
            page += ".md"
        ref = _image_ref_for(saved, page)
        how = f"From `{page}`, embed it with:\n\n    ![{alt}]({ref})"
    else:
        # Without the page, offer the reference for the folder the asset's
        # `assets/` sits in — the convention — and say how to work out others.
        parent = PurePosixPath(saved).parent
        home = parent.parent if parent.name == "assets" else parent
        ref = _image_ref_for(saved, f"{home}/page.md")
        how = (f"From a page in `{home}/`, embed it with:\n\n    ![{alt}]({ref})\n\n"
               f"The reference is relative to the embedding page's folder. Pass "
               f"`page` to have it worked out for a page somewhere else.")
    return (f"✅ {verb} image `{saved}` ({data.get('content_type', mime)}, {size_txt}). "
            f"{how}")


def _ai_path(rel: str, folder: str) -> str:
    """Normalise an agent-supplied wiki path and place it under the AI folder.

    Shared by the write tools so a bare 'notes.md' means the same thing to all
    of them. The editor re-checks the boundary regardless — this is convenience,
    not the security control.
    """
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("A path is required (e.g. 'AI/notes.md').")
    if ".." in rel.split("/"):
        raise ValueError(f"Invalid path: '{rel}'. Path traversal ('..') is not allowed.")
    if not (rel == folder or rel.startswith(folder + "/")):
        rel = f"{folder}/{rel}"
    return rel


def _ai_path_strict(rel: str, folder: str) -> str:
    """Normalise an agent-supplied path and REFUSE anything outside the AI
    folder, rather than placing it there.

    The counterpart to `_ai_path`, and the difference is the whole point.
    Auto-placement is right where a bare name has one sensible reading and
    nothing is misreported — `wiki_write("notes.md")` is a convenience. It is
    wrong where the caller named a full path and meant it: coercing a
    destination turns "move this out of the AI folder" into "move this
    somewhere else inside it" and reports success, so the request, the effect
    and the report diverge. That is the same failure class as the wiki_edit
    silent delete, and the same rule applies — fail loud, fail early, never let
    the caller proceed on a fiction.
    """
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("A path is required (e.g. 'AI/notes.md').")
    if ".." in rel.split("/"):
        raise ValueError(f"Invalid path: '{rel}'. Path traversal ('..') is not allowed.")
    if rel == folder or rel.startswith(folder + "/"):
        return rel
    # A bare name is refused here too. On a write it is obvious shorthand; on a
    # move, where a full source path was just given, it is ambiguous — and
    # guessing is what this function exists to stop.
    hint = (f" A bare name like '{rel}' is not shorthand here: give the full "
            f"destination, e.g. '{folder}/{rel}'.") if "/" not in rel else ""
    raise ValueError(
        f"Refused: '{rel}' is outside the {folder} folder, and wiki_move will not "
        f"quietly place it inside. Moving work out of {folder} is a human promoting "
        f"it as reviewed, not something an agent does. Nothing was moved.{hint}")


@mcp.tool(structured_output=False)
async def wiki_edit(
    path: str,
    mode: str,
    content: str = "",
    heading: str = "",
    old: str = "",
    ctx: Context = None,
) -> CallToolResult | str:
    """
    Edit / change / fix / update / add a section to / tweak a page - change
    PART of an Elefant wiki page, leaving the rest byte-for-byte identical.

    Prefer this over `wiki_write` for any change to a page that already exists.
    wiki_write replaces the whole page, so using it to adjust one line means
    reproducing every other line from context — and anything you misremember is
    silently lost. This edits in place, and because the History diff compares
    stored snapshots, a small edit shows up as a small diff a human can review.

    Modes:
      replace         Swap an exact snippet. `old` must appear EXACTLY ONCE —
                      include surrounding text if it doesn't. The precise tool
                      for a one-line fix.
      replace_section Replace the body under a heading, keeping the heading.
                      `heading` is the heading text ("Rollback", not
                      "## Rollback") or its anchor ("rollback", "steps-1" for
                      the second of two "Steps"). `path#anchor` works too, with
                      `heading` left empty. A section owns its subsections, so
                      replacing "Prepare" also replaces a "### Checklist"
                      nested under it.
      append_section  Add to the end of that section, leaving it otherwise
                      intact.
      append          Add to the end of the page.
      delete          Remove the snippet in `old` and put nothing in its place.
                      The ONLY mode that removes text — say it explicitly.

    `content` is required for every mode except `delete`, and empty content is
    refused rather than treated as "replace it with nothing". Deleting a page's
    contents is something you should have to ask for by name.

    Ambiguity is refused rather than guessed: a duplicate heading or a snippet
    matching twice comes back as an error telling you what to disambiguate. A
    wrong heading lists the headings the page actually has.

    Same boundary as wiki_write — pages under the AI folder only. A page a
    human promoted out of it cannot be edited.

    A ```mermaid block the edit adds or changes is parsed by Mermaid before
    anything is saved, so swapping an ASCII sketch for a diagram is checked on
    the first save. A block that does not parse is refused with its line
    number on the page and nothing is written; a saved edit that touched a
    diagram reports `validated` in its structured content. To change a
    diagram that is already on a page, `diagram_patch` edits it by line.

    Args:
        path:    Page path, e.g. "AI/runbooks/db-restore.md".
        mode:    replace | replace_section | append_section | append | delete
        content: The new text. For `replace` this is what `old` becomes.
                 Required unless mode='delete'.
        heading: Heading text — for replace_section / append_section.
        old:     Exact existing snippet — for `replace` and `delete`.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    slug = _tenant_slug(ctx)
    # `page.md#anchor` names the section; the fragment is never part of a filename.
    if "#" in (path or ""):
        path, _, frag = path.partition("#")
        heading = heading or frag
    try:
        rel = _ai_path(path, await _ai_wiki_folder(slug))
    except ValueError as exc:
        return str(exc)

    edit = {"token_hash": _token_hash(ctx), "path": rel, "mode": mode,
            "content": content, "heading": heading, "old": old}

    # Validate any diagram the edit adds or changes before it lands (card
    # #420). The edit arguments alone cannot say whether it touches a mermaid
    # fence (a one-line `replace` inside a diagram never mentions the fence),
    # so the editor previews the page as the edit leaves it and the real edit
    # is pinned to the bytes that were checked.
    try:
        preview = await _editor_post("/api/internal/wiki/edit/preview", edit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404 or _http_err_detail(exc) != "Not Found":
            return f"Wiki edit failed ({exc.response.status_code}): {_http_err_detail(exc)}"
        preview = None   # an editor from before the preview: check what was sent
    except Exception as exc:
        return _internal_error("Wiki edit failed", exc)

    if preview is not None:
        if not preview.get("changed"):
            return f"No change to `{rel}` — the edit produced identical content."
        before = frozenset(b["source"] for b in _mermaid_blocks(preview.get("before") or ""))
        refusal, checked = await _check_page_diagrams(preview.get("content") or "", before,
                                                      "the page as edited")
        edit["if_match"] = preview.get("sha256")
    else:
        refusal, checked = await _check_page_diagrams(content or "", where="your content")
    if refusal:
        return refusal

    try:
        data = await _editor_post("/api/internal/wiki/edit", edit)
    except httpx.HTTPStatusError as exc:
        return f"Wiki edit failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki edit failed", exc)

    if not data.get("changed"):
        return f"No change to `{rel}` — the edit produced identical content."
    before, after = data.get("bytes_before", 0), data.get("bytes_after", 0)
    delta = after - before
    sign = "+" if delta >= 0 else ""
    msg = (f"✅ Edited `{rel}` ({mode}, {sign}{delta} bytes, page is now {after}). "
           f"Reindex triggered; the change is one revision in History.")
    # Report the size the page ENDED at, not just the delta, and flag a big
    # loss. A cheerful success line quoting only a negative delta is what let a
    # page be emptied an edit at a time without anyone noticing.
    if before and after < before * 0.6:
        msg += (f"\n\n⚠️  That removed {before - after} of {before} bytes. If you "
                f"did not mean to cut the page down, `wiki_history` and "
                f"`wiki_revert` will put it back — the previous version is one "
                f"revision away.")
    return _page_saved(msg, checked)


@mcp.tool()
async def wiki_move(
    from_path: str,
    to_path: str,
    ctx: Context = None,
) -> str:
    """
    Move / rename / reorganise / file it under / put it somewhere else - an
    Elefant wiki page, or a whole folder and everything under it.

    Use this to reorganise — it is the alternative to writing a page at a new
    path and abandoning the old one, which leaves strays for a human to clean
    up. A page's edit history travels with it, so History at the new path still
    shows how the page got there.

    Folders are handled recursively: moving "AI/projects/old" to
    "AI/archive/old" takes every page beneath it.

    Both ends must be inside the AI folder. A destination outside it is
    REFUSED rather than placed inside — moving work out of the AI folder is a
    human promoting it as reviewed, not something an agent does. A refused move
    changes nothing, including for a folder: every page stays where it was.

    Unlike `wiki_write`, a bare name is not shorthand for `to_path`. You have
    just given a full source path, so an unqualified destination is ambiguous
    rather than convenient, and it is refused too.

    Args:
        from_path: Existing page or folder, e.g. "AI/notes/draft.md".
        to_path:   New path, e.g. "AI/runbooks/db-restore.md".
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    slug = _tenant_slug(ctx)
    folder = await _ai_wiki_folder(slug)
    try:
        src = _ai_path(from_path, folder)
        # The destination is CHECKED, not placed. See _ai_path_strict.
        dst = _ai_path_strict(to_path, folder)
    except ValueError as exc:
        return str(exc)

    try:
        data = await _editor_post("/api/internal/wiki/move", {
            "token_hash": _token_hash(ctx), "from_path": src, "to_path": dst,
        })
    except httpx.HTTPStatusError as exc:
        return f"Wiki move failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki move failed", exc)

    if data.get("kind") == "folder":
        return (f"✅ Moved folder `{src}` → `{dst}` "
                f"({data.get('moved', 0)} page(s)). Reindex triggered.")
    return f"✅ Moved `{src}` → `{dst}`. History moved with it; reindex triggered."


@mcp.tool()
async def wiki_delete(
    path: str,
    recursive: bool = False,
    ctx: Context = None,
) -> str:
    """
    Delete / remove / get rid of / bin / throw away - an Elefant wiki page,
    or a folder and everything under it.

    Markdown pages go to the same recycle bin a human deletion uses, so this is
    recoverable for 30 days rather than final — but it is still a deletion, and
    an agent should be sure. Deleting a folder requires recursive=true, so
    taking the contents with it is always something you asked for.

    Prefer `wiki_move` when you are relocating something rather than getting
    rid of it.

    AI folder only: a page a human promoted out of it cannot be deleted.

    Args:
        path:      Page or folder, e.g. "AI/notes/scratch.md" or "AI/notes".
        recursive: Required (true) to delete a folder with contents.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    slug = _tenant_slug(ctx)
    try:
        rel = _ai_path(path, await _ai_wiki_folder(slug))
    except ValueError as exc:
        return str(exc)

    try:
        data = await _editor_post("/api/internal/wiki/delete", {
            "token_hash": _token_hash(ctx), "path": rel, "recursive": bool(recursive),
        })
    except httpx.HTTPStatusError as exc:
        return f"Wiki delete failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki delete failed", exc)

    if data.get("kind") == "folder":
        return (f"🗑️ Deleted folder `{rel}` ({data.get('deleted', 0)} item(s)). "
                f"Markdown pages are in the recycle bin for 30 days.")
    tail = (" It is in the recycle bin for 30 days."
            if data.get("recoverable") else "")
    return f"🗑️ Deleted `{rel}`.{tail}"


@mcp.tool()
async def wiki_history(
    path: str,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """
    What changed / who edited this / earlier versions - the edit history of
    an Elefant wiki page: every saved version, newest first, with who
    changed it, when, which fields moved (content / tags / title), and any
    change note. Use it to audit how a page evolved, or to find the revision
    id to view (`wiki_revision`), compare (`wiki_diff`), or restore
    (`wiki_revert`).

    A page with no recorded history returns an empty list — not an error.
    Version history began when the feature shipped; a page edited only before
    then starts its history at its first edit since.

    Args:
        path:  Wiki page path (e.g. "AI/runbooks/db-restore.md").
        limit: Max revisions to return (1–200, default 20).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    limit = max(1, min(int(limit or 20), 200))
    try:
        data = await _editor_post("/api/internal/wiki/history", {
            "token_hash": _token_hash(ctx), "path": path, "limit": limit,
        })
    except httpx.HTTPStatusError as exc:
        return f"Wiki history fetch failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki history fetch failed", exc)

    revisions = data.get("revisions") or []
    count     = int(data.get("count") or 0)
    rel       = data.get("path", path)
    if count == 0:
        return (
            f"# History of `{rel}`\n\n"
            "No revisions recorded yet. The page either predates version "
            "history or hasn't been edited since tracking began."
        )

    lines = [
        f"# History of `{rel}`\n",
        f"{count} version{'s' if count != 1 else ''} (newest first).\n",
    ]
    for idx, r in enumerate(revisions):
        n      = count - idx
        who    = r.get("edited_by_name") or r.get("edited_by_id") or "—"
        when   = r.get("edited_at") or "—"
        src    = r.get("source") or "—"
        fields = ", ".join(r.get("changed_fields") or []) or "—"
        tags   = ", ".join(r.get("tags") or []) or "none"
        msg    = r.get("message")
        lines.append(f"\n## Version {n} · revision `{r.get('id')}`")
        lines.append(f"- **When:** {when} · **By:** {who} · **Via:** {src}")
        lines.append(
            f"- **Changed:** {fields} · **Tags:** {tags} · "
            f"**Size:** {r.get('size', 0)} chars"
        )
        if msg:
            lines.append(f"- **Note:** {msg}")
    lines.append(
        "\n_View a version with `wiki_revision`, compare with `wiki_diff`, "
        "restore with `wiki_revert`._"
    )
    return "\n".join(lines)


@mcp.tool()
async def wiki_revision(
    path: str,
    revision_id: int,
    ctx: Context = None,
) -> str:
    """
    Show me the old version / what did it say before - the full content of
    one historical Elefant wiki revision (ids come from `wiki_history`).

    Args:
        path:        Wiki page path.
        revision_id: The revision id, as listed by wiki_history.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    try:
        data = await _editor_post("/api/internal/wiki/revision", {
            "token_hash": _token_hash(ctx), "path": path, "revision_id": revision_id,
        })
    except httpx.HTTPStatusError as exc:
        return f"Wiki revision fetch failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki revision fetch failed", exc)

    rev = data.get("revision") or {}
    who = rev.get("edited_by_name") or rev.get("edited_by_id") or "—"
    header = (
        f"# `{data.get('path', path)}` — revision `{rev.get('id')}`\n"
        f"_Edited {rev.get('edited_at') or '—'} by {who} · "
        f"changed: {', '.join(rev.get('changed_fields') or []) or '—'}_\n"
    )
    return header + "\n---\n\n" + (rev.get("content") or "_(empty)_")


@mcp.tool()
async def wiki_revert(
    path: str,
    revision_id: int,
    ctx: Context = None,
) -> str:
    """
    Undo / roll back / restore / put it back the way it was - return an
    Elefant wiki page to an earlier revision's content (ids come from
    `wiki_history`).

    Non-destructive: the restored content is written forward as a NEW version,
    so the history is preserved and you can roll forward again. As with
    `wiki_write`, agents may only revert pages inside the AI folder.

    Args:
        path:        Wiki page path (must be under the AI folder).
        revision_id: The revision id to restore, as listed by wiki_history.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    try:
        data = await _editor_post("/api/internal/wiki/revert", {
            "token_hash": _token_hash(ctx), "path": path, "revision_id": revision_id,
        })
    except httpx.HTTPStatusError as exc:
        return f"Wiki revert failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki revert failed", exc)

    return (
        f"✅ Reverted `{data.get('path', path)}` to revision "
        f"`{data.get('revision_id', revision_id)}` "
        f"(from {data.get('restored_from') or '—'}). Reindex triggered."
    )


@mcp.tool()
async def wiki_diff(
    path: str,
    revision_id: int,
    against_id: int = 0,
    ctx: Context = None,
) -> str:
    """
    Compare / diff / what changed between those two versions - a unified
    diff of an Elefant wiki page.

    By default compares the given revision against the CURRENT page (its latest
    version). Pass `against_id` to compare two specific revisions instead.

    Args:
        path:        Wiki page path.
        revision_id: The base revision id (the "from" side).
        against_id:  Optional second revision id (the "to" side). 0 = current.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    th = _token_hash(ctx)

    async def _content(rid: int) -> str:
        d = await _editor_post("/api/internal/wiki/revision", {
            "token_hash": th, "path": path, "revision_id": rid,
        })
        return (d.get("revision") or {}).get("content") or ""

    try:
        to_id = int(against_id or 0)
        if to_id <= 0:
            hist = await _editor_post("/api/internal/wiki/history", {
                "token_hash": th, "path": path, "limit": 1,
            })
            revs = hist.get("revisions") or []
            if not revs:
                return f"No history for `{path}` to diff against."
            to_id = int(revs[0].get("id"))
        a = await _content(int(revision_id))
        b = await _content(to_id)
    except httpx.HTTPStatusError as exc:
        return f"Wiki diff failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Wiki diff failed", exc)

    if a == b:
        return f"No differences between revision `{revision_id}` and `{to_id}` of `{path}`."

    import difflib
    diff = difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile=f"{path}@{revision_id}", tofile=f"{path}@{to_id}",
        lineterm="",
    )
    rendered = "\n".join(diff)
    if len(rendered) > 12000:
        rendered = rendered[:12000] + "\n… (diff truncated)"
    return f"# Diff of `{path}`: revision `{revision_id}` → `{to_id}`\n\n```diff\n{rendered}\n```"


# ── Diagrams ────────────────────────────────────────────────────────────────
#
# A diagram is a wiki page holding a fenced ```mermaid block. The Mermaid TEXT
# is the source of truth: it is what is stored, patched, diffed and reverted,
# and every image is derived from it. Keeping it in the wiki is the whole
# design — history, diff and revert work on diagrams with no new storage, and
# patching one node label shows up in History as a one-line change.
#
# Revisions are pinned with a version: the first 12 hex digits of the sha256
# of the page's stored bytes. diagram_patch refuses a stale one, and the editor
# re-checks the full hash at write time (`if_match`), so two agents revising
# the same diagram cannot silently overwrite each other. Nothing here depends
# on the MCP session: a diagram is addressed by name and version, both of
# which the caller passes back explicitly.
#
# Validation goes to the diagram-validator sidecar: Mermaid's own parser in
# plain Node, no browser, always deployed. Images come from the OPTIONAL
# diagram-renderer (mermaid in headless Chromium), which validates too, so
# either one is enough to check a write. When neither answers, a write is
# refused rather than saved unchecked, unless the operator has opted out with
# DIAGRAM_ALLOW_UNVALIDATED. Both are separate images so this one stays light.

DIAGRAM_VALIDATOR_URL = os.getenv(
    "DIAGRAM_VALIDATOR_URL", "http://diagram-validator:8005").strip().rstrip("/")
# Above the sidecar's own parse timeout, for the same reason as below.
DIAGRAM_VALIDATE_TIMEOUT = float(os.getenv("DIAGRAM_VALIDATE_TIMEOUT", "10"))
DIAGRAM_RENDERER_URL = os.getenv(
    "DIAGRAM_RENDERER_URL", "http://diagram-renderer:8004").strip().rstrip("/")
# Above the sidecar's own render timeout, so its answer (a clean "timed out")
# arrives before ours would cut the connection.
DIAGRAM_RENDER_TIMEOUT = float(os.getenv("DIAGRAM_RENDER_TIMEOUT", "30"))
# Off by default: with no validator, a diagram is refused, not saved unchecked.
DIAGRAM_ALLOW_UNVALIDATED = os.getenv(
    "DIAGRAM_ALLOW_UNVALIDATED", "").strip().lower() in ("1", "true", "yes", "on")
# The sidecar enforces the same ceiling; checking here first saves a round trip.
DIAGRAM_MAX_SOURCE_BYTES = 64 * 1024

# The Mermaid the validator, the renderer and the wiki all ship. The tool
# descriptions name it because edge IDs and `animate` need a recent Mermaid;
# mcp/tests/test_diagrams.py fails when a package bump leaves this behind.
MERMAID_VERSION = "11.16.1"

# Appended to each diagram tool's description by _with_mermaid_guide, so the
# four tools teach the same things in the same words. Indented like the
# docstrings it joins, because the description is the raw docstring.
_MERMAID_GUIDE = f"""
    Writing Mermaid for Elefant (Mermaid {MERMAID_VERSION}, the version the
    validator, the renderer and the wiki all use):
    - Front matter config is allowed before the diagram type: a `---` block
      with `title:` and `config:` (theme, themeVariables, flowchart settings)
      goes first, then the type line, e.g. `flowchart TD`.
    - Use stable short node IDs and put the label text in quoted labels:
      `api["Orders API"]`, `db[("Orders DB")]`. A patch can then change one
      label without touching other lines, and the quotes keep punctuation in
      a label from breaking the parse.
    - Style with `classDef hot fill:#f96,stroke:#333` and then
      `class api,db hot` (or `api:::hot`); `style front fill:#eef` styles one
      node or subgraph. Shapes: `store@{{ shape: cyl, label: "Store" }}`.
    - Edge IDs and `animate` need Mermaid {MERMAID_VERSION} as shipped here:
      `a e1@--> b` names the edge, then `e1@{{ animate: true }}` animates it.
      An older Mermaid elsewhere may not draw them.
    - `block` selects a diagram when a page holds several ```mermaid blocks
      (0 = first).
    - `if_version` comes from `diagram_get` or the last create or patch
      result, and pins a patch to the version you read.
    - House style bans em and en dashes in labels. A write that contains one
      is still saved, with a warning naming the lines.

    Layout (flowcharts, drawn with the dagre layout). A write that breaks one
    of these rules is still saved, with a warning naming the lines:
    - A `direction` line inside a subgraph is ignored when any edge crosses
      that subgraph's border. Attach such edges to the subgraph itself.
    - Edges into nodes nested more than 2 subgraphs deep tangle and overlap
      labels. Attach the edge to the enclosing subgraph, and leave out edges
      that containment already shows.
    - Keep subgraph titles to one line: a `<br/>` in a subgraph title can
      overlap the first node inside it.
    - Past about 30 nodes, split the diagram into an overview and detail
      diagrams.
    """


def _with_mermaid_guide(fn):
    """Add the shared Mermaid guidance to a diagram tool's description."""
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + _MERMAID_GUIDE
    return fn


# Written as escapes so the house dash guard does not trip on this file.
_DASHES = {"\u2014": "em dash (U+2014)", "\u2013": "en dash (U+2013)"}


def _dash_warning(source: str) -> dict | None:
    """A non-blocking house-style warning when the source holds an em or en
    dash: which lines, so the caller can patch them. None when it holds none."""
    lines = [n for n, text in enumerate(source.split("\n"), 1)
             if any(d in text for d in _DASHES)]
    if not lines:
        return None
    kinds = [name for d, name in _DASHES.items() if d in source]
    where = ", ".join(str(n) for n in lines)
    return {
        "code": "dash",
        "lines": lines,
        "message": (f"House style: line{'s' if len(lines) > 1 else ''} {where} "
                    f"contain{'' if len(lines) > 1 else 's'} an {' and an '.join(kinds)}. "
                    "Use a comma, a colon or a plain hyphen instead. Saved anyway."),
    }


# ── Layout lint (card #419) ───────────────────────────────────────────────────
# Three flowchart shapes that parse cleanly but lay out badly with the dagre
# layout Elefant ships (layout-elk is left out for license reasons). Each one
# cost a patch-and-render round trip on a real diagram, and no parser can
# catch them, so a write that contains one is saved with a warning naming the
# lines. This is a light reading of flowchart syntax, not a parser: it only
# has to find subgraphs, the nodes declared in them, and the edges.

_LAYOUT_MAX_DEPTH = 2
_FLOW_SKIP_RE = re.compile(r"^(classDef|class|style|linkStyle|click|accTitle|accDescr)\b")
_SUBGRAPH_RE = re.compile(r"^subgraph\s+(.*)$")
_QUOTED_RE = re.compile(r'"[^"]*"')
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# An unquoted edge label in the middle of a link: `A -- text --> B`.
_INLINE_LABEL_RE = re.compile(r"(?:--|==|-\.)\s+[^-=.>|\s][^|>]*?\s+(?=-{2,}|={2,}|\.-)")
_LINK_RE = re.compile(r"<?(?:-{2,}|={2,}|-\.+-|~{3,})(?:>|[ox](?=\s|$))?")
_NODE_ID_RE = re.compile(r"^([A-Za-z0-9_][\w]*)")


def _strip_shapes(s: str) -> str:
    """Drop every bracketed shape or label body, innermost first."""
    while True:
        t = re.sub(r"\([^()\[\]{}]*\)|\[[^()\[\]{}]*\]|\{[^()\[\]{}]*\}", "", s)
        if t == s:
            return t
        s = t


def _flow_statements(source: str):
    """(line number, raw text, simplified text) for each flowchart statement,
    or nothing when the source is not a flowchart. Simplified means quoted
    strings blanked, edge labels and shape bodies removed."""
    lines = source.split("\n")
    n = 0
    if lines and lines[0].strip() == "---":
        n = next((k for k in range(1, len(lines)) if lines[k].strip() == "---"), len(lines)) + 1
    seen_type = False
    for k in range(n, len(lines)):
        raw = lines[k].strip()
        if not raw or raw.startswith("%%"):
            continue
        if not seen_type:
            if re.split(r"[\s;:{]", raw, maxsplit=1)[0] not in ("flowchart", "graph", "flowchart-elk"):
                return
            seen_type = True
            continue
        s = _QUOTED_RE.sub('""', raw)
        for part in s.split(";"):
            part = part.strip()
            if part:
                yield k + 1, raw, part


def _flow_model(source: str) -> dict | None:
    """Subgraphs (parent, title line, direction line), the subgraph each node
    first appears in, and every edge with its line. None when not a flowchart."""
    subs: dict[str, dict] = {}
    home: dict[str, str | None] = {}
    edges: list[tuple[int, str, str]] = []
    stack: list[str] = []
    edge_ids: set[str] = set()
    found = False
    for line, raw, s in _flow_statements(source):
        found = True
        m = _SUBGRAPH_RE.match(s)
        if m:
            rest = m.group(1).strip()
            ident = _NODE_ID_RE.match(rest)
            sid = ident.group(1) if ident and not rest.startswith('"') else f"line{line}"
            title = raw[len("subgraph"):].strip()
            subs[sid] = {"parent": stack[-1] if stack else None, "line": line,
                         "br": bool(_BR_RE.search(title)), "direction": None}
            home.setdefault(sid, stack[-1] if stack else None)
            stack.append(sid)
            continue
        if s == "end":
            if stack:
                stack.pop()
            continue
        if s.startswith("direction"):
            if stack:
                subs[stack[-1]]["direction"] = line
            continue
        if _FLOW_SKIP_RE.match(s):
            continue
        s = re.sub(r"@\{[^}]*\}", "", s)       # node shape or edge settings
        s = re.sub(r"\|[^|]*\|", "", s)         # |edge label|
        s = _INLINE_LABEL_RE.sub("", s)
        s = re.sub(r":::[\w-]+", "", s)
        s = _strip_shapes(s)
        pieces = _LINK_RE.split(s)
        groups = []
        for i, piece in enumerate(pieces):
            piece = piece.strip()
            if i < len(pieces) - 1:
                eid = re.search(r"([\w-]+)@$", piece)
                if eid:
                    edge_ids.add(eid.group(1))
                    piece = piece[:eid.start()].strip()
            ids = [mm.group(1) for p in piece.split("&")
                   if (mm := _NODE_ID_RE.match(p.strip()))]
            groups.append(ids)
        for ids in groups:
            for nid in ids:
                if nid not in edge_ids or len(groups) > 1:
                    home.setdefault(nid, stack[-1] if stack else None)
        for a_ids, b_ids in itertools.pairwise(groups):
            edges.extend((line, a, b) for a in a_ids for b in b_ids)
    if not found:
        return None
    for eid in edge_ids:            # `e1@{ animate: true }` is not a node
        if not any(eid in (a, b) for _, a, b in edges):
            home.pop(eid, None)
    return {"subgraphs": subs, "home": home, "edges": edges}


def _chain(model: dict, node: str) -> list[str]:
    """The subgraphs enclosing a node or subgraph, innermost first."""
    out, cur = [], model["home"].get(node)
    while cur and cur not in out:
        out.append(cur)
        cur = model["subgraphs"].get(cur, {}).get("parent")
    return out


def _plural(lines: list[int]) -> str:
    return f"line{'s' if len(lines) > 1 else ''} {', '.join(str(n) for n in lines)}"


def _layout_warnings(source: str) -> list[dict]:
    """Non-blocking layout warnings for a flowchart: a subgraph `direction`
    that dagre will ignore, a `<br/>` in a subgraph title, and edges that leave
    a cluster nested more than two deep. Empty for anything else."""
    model = _flow_model(source)
    if not model:
        return []
    subs, edges, out = model["subgraphs"], model["edges"], []

    ignored: dict[str, list[int]] = {}
    for sid, sg in subs.items():
        if not sg["direction"]:
            continue
        crossing = sorted({line for line, a, b in edges
                           if (sid in _chain(model, a)) != (sid in _chain(model, b))
                           and sid not in (a, b)})
        if crossing:
            ignored[sid] = crossing
    if ignored:
        lines = sorted(subs[s]["direction"] for s in ignored)
        names = "; ".join(f"{s}, edges on {_plural(v)}" for s, v in ignored.items())
        out.append({
            "code": "layout-direction-ignored", "lines": lines, "subgraphs": list(ignored),
            "message": (f"Layout: the `direction` on {_plural(lines)} will be ignored, because "
                        f"an edge crosses the subgraph's border ({names}). The renderer lays "
                        "those clusters out in the diagram's own direction. Attach crossing "
                        "edges to the subgraph itself, or drop the `direction`. Saved anyway."),
        })

    br = sorted(sg["line"] for sg in subs.values() if sg["br"])
    if br:
        out.append({
            "code": "layout-subgraph-title-br", "lines": br,
            "message": (f"Layout: the subgraph title on {_plural(br)} has a `<br/>`. A two line "
                        "cluster title can overlap the first node inside it. Keep subgraph "
                        "titles to one line and move the detail into a node. Saved anyway."),
        })

    deep: list[int] = []
    for line, a, b in edges:
        for x, y in ((a, b), (b, a)):
            cx = _chain(model, x)
            if len(cx) > _LAYOUT_MAX_DEPTH and cx[0] not in _chain(model, y) and cx[0] != y:
                deep.append(line)
                break
    if deep:
        deep = sorted(set(deep))
        out.append({
            "code": "layout-deep-nesting", "lines": deep,
            "message": (f"Layout: the edges on {_plural(deep)} reach a node nested more than "
                        f"{_LAYOUT_MAX_DEPTH} subgraphs deep from outside its cluster. These "
                        "tangle and overlap labels. Attach the edge to the enclosing subgraph, "
                        "and leave out edges that containment already shows. Saved anyway."),
        })
    return out

# What an agent is told when the optional renderer does not answer: the
# setting that turns it on, and what still works without it.
_RENDERER_OFF = {
    "available": False,
    "enable": {
        "compose": "COMPOSE_PROFILES=diagrams in .env, then docker compose up -d",
        "kubernetes": "the diagram-renderer component of the base "
                      "(deploy/k8s/base/diagram-renderer.yaml), with the MCP "
                      "server's DIAGRAM_RENDERER_URL pointing at it",
    },
    "still_works": ["parse validation by diagram-validator", "create, patch and get",
                    "the wiki draws diagrams in the browser"],
    "does_not_work": ["images (PNG or SVG) from the diagram tools"],
}


def _renderer_off_text() -> str:
    e = _RENDERER_OFF["enable"]
    return ("The optional diagram renderer is not running, so there is no image. "
            f"To turn it on: Docker Compose, set {e['compose']}; Kubernetes, deploy "
            f"{e['kubernetes']}. Parse validation still works (diagram-validator), "
            "and the wiki still draws diagrams in the browser.")

_DIAGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

# The first keyword of every diagram type Mermaid 11 knows. Used only for the
# cheap static check that runs before Mermaid's own parser, the real validator.
_MERMAID_TYPES = frozenset({
    "flowchart", "flowchart-elk", "graph", "sequenceDiagram", "classDiagram",
    "classDiagram-v2", "stateDiagram", "stateDiagram-v2", "erDiagram", "journey",
    "gantt", "pie", "quadrantChart", "requirementDiagram", "gitGraph",
    "C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment",
    "mindmap", "timeline", "zenuml", "sankey", "sankey-beta", "xychart",
    "xychart-beta", "block", "block-beta", "packet", "packet-beta", "kanban",
    "architecture", "architecture-beta", "radar-beta", "treemap", "treemap-beta",
})


class DiagramEdit(BaseModel):
    """One targeted change to a diagram's Mermaid source.

    Every edit addresses the source AS YOU READ IT (the version you pass as
    if_version), so the edits in one call do not shift each other's line
    numbers. Edits that overlap are refused.
    """
    op: Literal["replace", "replace_line", "insert_after", "delete_line"] = Field(
        "replace", description=(
            "replace: swap the exact text `old` (must occur exactly once) for `new`. "
            "replace_line: replace line `line` with `text`. "
            "insert_after: insert `text` after line `line` (0 = at the top). "
            "delete_line: remove line `line`."))
    old: str = Field("", description="replace: exact existing text, unique in the source.")
    new: str = Field("", description="replace: what `old` becomes (may be empty).")
    line: int = Field(0, description="Line ops: 1-based line number in the source you read.")
    text: str = Field("", description="replace_line / insert_after: the new line(s).")


def _mermaid_blocks(content: str) -> list[dict]:
    """Every fenced ```mermaid block on a page, as line spans.

    `open` and `close` are the indexes of the fence lines themselves, so the
    source is lines[open+1:close]. Other fenced blocks are skipped whole, so a
    mermaid fence quoted inside a ````markdown example is not mistaken for a
    diagram. An unclosed fence ends the scan rather than eating the page.
    """
    lines = content.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i].rstrip("\r"))
        if not m:
            i += 1
            continue
        fence, info = m.group(1), m.group(2).strip()
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        j = i + 1
        while j < len(lines) and not close_re.match(lines[j].rstrip("\r")):
            j += 1
        if j >= len(lines):
            break
        if info.split(" ", 1)[0].lower() == "mermaid":
            blocks.append({"open": i, "close": j,
                           "source": "\n".join(l.rstrip("\r") for l in lines[i + 1:j])})
        i = j + 1
    return blocks


def _replace_block(content: str, block: dict, source: str) -> str:
    """The page with one block's source swapped, every other byte untouched."""
    lines = content.split("\n")
    return "\n".join(lines[:block["open"] + 1] + source.split("\n") + lines[block["close"]:])


def _diagram_page(title: str, description: str, source: str) -> str:
    parts = [f"# {' '.join(title.split())}", ""]
    if description.strip():
        parts += [description.strip(), ""]
    parts += ["```mermaid", source, "```", ""]
    return "\n".join(parts)


def _normalise_source(source: str) -> str:
    return (source or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _diagram_version(sha256_hex: str) -> str:
    return (sha256_hex or "")[:12]


def _diagram_path(name: str, folder: str) -> str:
    """A bare name is a diagram in `<AI folder>/diagrams/`; anything with a
    slash is a wiki page path, so a diagram inside a design page works too."""
    n = (name or "").strip().strip("/")
    if not n:
        raise ValueError("A diagram name is required (e.g. 'login-flow').")
    if "/" in n:
        if ".." in n.split("/"):
            raise ValueError(f"Invalid path: '{n}'. Path traversal ('..') is not allowed.")
        return n if n.lower().endswith((".md", ".markdown")) else n + ".md"
    stem = n[:-3] if n.lower().endswith(".md") else n
    if not _DIAGRAM_NAME_RE.match(stem):
        raise ValueError(
            f"'{n}' is not a usable diagram name: use letters, digits, '.', '_' "
            "and '-' (e.g. 'login-flow'), or give a full wiki page path.")
    return f"{folder}/diagrams/{stem}.md"


def _diagram_label(path: str, folder: str) -> str:
    """How a diagram is named back to the caller: the bare name it was created
    with when it lives in the diagrams folder, the page path otherwise."""
    prefix = f"{folder}/diagrams/"
    if path.startswith(prefix) and "/" not in path[len(prefix):] and path.endswith(".md"):
        return path[len(prefix):-3]
    return path


def _mermaid_static_check(source: str) -> dict | None:
    """The checks that need no Mermaid: size, the fence, and a known diagram
    type on the first meaningful line. Returns a parse-error dict or None."""
    if not source.strip():
        return {"message": "The diagram source is empty.", "line": 1}
    if len(source.encode("utf-8")) > DIAGRAM_MAX_SOURCE_BYTES:
        return {"message": f"The diagram source is over {DIAGRAM_MAX_SOURCE_BYTES // 1024} KiB.",
                "line": None}
    lines = source.split("\n")
    for n, line in enumerate(lines, 1):
        if _FENCE_RE.match(line):
            return {"message": "A line starting with ``` or ~~~ would end the wiki's "
                               "mermaid fence early. Remove it.", "line": n}
    n = 0
    if lines[0].strip() == "---":        # YAML front matter (title, config)
        n = next((k for k in range(1, len(lines)) if lines[k].strip() == "---"), len(lines)) + 1
    while n < len(lines):
        s = lines[n].strip()
        if s and not s.startswith("%%"):
            word = re.split(r"[\s;:{]", s, maxsplit=1)[0]
            if word in _MERMAID_TYPES:
                return None
            return {"message": f"'{word}' is not a Mermaid diagram type. The first line "
                               "must declare one, e.g. 'flowchart TD' or 'sequenceDiagram'.",
                    "line": n + 1}
        n += 1
    return {"message": "No diagram type found: the source is only comments.", "line": 1}


def _apply_diagram_edits(source: str, edits: list) -> str:
    """Apply targeted edits to a Mermaid source. Raises ValueError, naming the
    edit, when one cannot be applied exactly as asked."""
    if not edits:
        raise ValueError("Give at least one edit.")
    lines = source.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1

    def _line(n: int, k: int, lo: int = 1) -> int:
        if not isinstance(n, int) or not lo <= n <= len(lines):
            raise ValueError(f"Edit {k}: line {n} is outside the source (1-{len(lines)}).")
        return n

    spans = []
    for k, raw in enumerate(edits, 1):
        e = raw if isinstance(raw, DiagramEdit) else DiagramEdit.model_validate(raw)
        if e.op == "replace":
            if not e.old:
                raise ValueError(f"Edit {k}: `old` is required for op 'replace'.")
            hits = source.count(e.old)
            if hits == 0:
                raise ValueError(f"Edit {k}: `old` was not found in the source. "
                                 "Copy it exactly from diagram_get.")
            if hits > 1:
                raise ValueError(f"Edit {k}: `old` matches {hits} places; include more "
                                 "of the line so it matches exactly one.")
            at = source.index(e.old)
            spans.append((at, at + len(e.old), e.new, k))
        elif e.op == "replace_line":
            n = _line(e.line, k)
            spans.append((starts[n - 1], starts[n - 1] + len(lines[n - 1]), e.text, k))
        elif e.op == "delete_line":
            n = _line(e.line, k)
            if len(lines) == 1:
                raise ValueError(f"Edit {k}: cannot delete the only line.")
            if n < len(lines):
                spans.append((starts[n - 1], starts[n], "", k))
            else:   # the last line: take the newline before it instead
                spans.append((starts[n - 1] - 1, len(source), "", k))
        elif e.op == "insert_after":
            n = 0 if e.line == 0 else _line(e.line, k)
            if n == 0:
                spans.append((0, 0, e.text + "\n", k))
            elif n < len(lines):
                spans.append((starts[n], starts[n], e.text + "\n", k))
            else:
                spans.append((len(source), len(source), "\n" + e.text, k))

    ordered = sorted(spans)
    for a, b in zip(ordered, ordered[1:]):
        if b[0] < a[1] or (a[0] == a[1] == b[0] and b[1] > b[0]):
            raise ValueError(f"Edits {a[3]} and {b[3]} touch the same text; combine them.")
    out = source
    for start, end, text, _k in sorted(spans, key=lambda s: (s[0], s[3]), reverse=True):
        out = out[:start] + text + out[end:]
    return out


class _RendererUnavailable(Exception):
    """The sidecar is not deployed, not reachable, or failed internally."""


class _ValidatorUnavailable(Exception):
    """The validator is not deployed, not reachable, or failed internally."""


async def _parse_mermaid(source: str) -> dict:
    """Ask the diagram-validator sidecar whether Mermaid accepts a source.

    Returns {"ok": True} or {"ok": False, "error": {"message", "line"}}, the
    line counted in the source as given. Raises _ValidatorUnavailable when
    there is nobody to ask.
    """
    if not DIAGRAM_VALIDATOR_URL:
        raise _ValidatorUnavailable("not configured")
    try:
        async with httpx.AsyncClient(timeout=DIAGRAM_VALIDATE_TIMEOUT) as client:
            resp = await client.post(
                f"{DIAGRAM_VALIDATOR_URL}/validate",
                json={"source": source},
                headers=_internal_service_headers(),
            )
    except httpx.HTTPError as exc:
        _logging.getLogger("mcp").info("diagram validator unreachable: %r", exc)
        raise _ValidatorUnavailable("unreachable") from exc
    if resp.status_code == 422:
        try:
            err = resp.json().get("error") or {}
        except Exception:
            err = {}
        return {"ok": False, "error": {"message": str(err.get("message") or "Mermaid rejected the source."),
                                       "line": err.get("line")}}
    if resp.status_code != 200:
        _logging.getLogger("mcp").warning(
            "diagram validator answered %s: %s", resp.status_code, (resp.text or "")[:200])
        raise _ValidatorUnavailable(f"status {resp.status_code}")
    return {"ok": True}


async def _render_mermaid(source: str, fmt: str) -> dict:
    """Ask the diagram-renderer sidecar to parse and render a source.

    Returns {"ok": True, "mime", "data"} or {"ok": False, "error": {...}} for a
    source Mermaid rejected. Raises _RendererUnavailable when there is nobody
    to ask — the caller decides whether that is fatal.
    """
    if not DIAGRAM_RENDERER_URL:
        raise _RendererUnavailable("not configured")
    try:
        async with httpx.AsyncClient(timeout=DIAGRAM_RENDER_TIMEOUT) as client:
            resp = await client.post(
                f"{DIAGRAM_RENDERER_URL}/render",
                json={"source": source, "format": fmt},
                headers=_internal_service_headers(),
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": {
            "message": "Rendering took too long and was stopped. Simplify the diagram "
                       "or split it in two.", "line": None}}
    except httpx.HTTPError as exc:
        _logging.getLogger("mcp").info("diagram renderer unreachable: %r", exc)
        raise _RendererUnavailable("unreachable") from exc
    if resp.status_code == 422:
        try:
            err = resp.json().get("error") or {}
        except Exception:
            err = {}
        return {"ok": False, "error": {"message": str(err.get("message") or "Mermaid rejected the source."),
                                       "line": err.get("line")}}
    if resp.status_code != 200:
        _logging.getLogger("mcp").warning(
            "diagram renderer answered %s: %s", resp.status_code, (resp.text or "")[:200])
        raise _RendererUnavailable(f"status {resp.status_code}")
    return resp.json()


async def _validate_mermaid(source: str, render: bool) -> tuple[dict | None, str, dict | None]:
    """(parse error or None, how it was checked, rendered image or None).

    How: "mermaid" when Mermaid itself parsed it (the validator, or the
    renderer), "static" when the static check already refused it, "unchecked"
    when neither sidecar answered and only the static check ran. The caller
    refuses an "unchecked" write unless DIAGRAM_ALLOW_UNVALIDATED is set.

    The validator is asked first: it is cheap and always deployed. The
    renderer is asked when an image is wanted, or when the validator could not
    answer; a render also catches the rare error Mermaid only finds at layout.
    """
    err = _mermaid_static_check(source)
    if err:
        return err, "static", None
    try:
        parsed = await _parse_mermaid(source)
    except _ValidatorUnavailable:
        parsed = None
    if parsed is not None and not parsed.get("ok"):
        return parsed.get("error") or {"message": "Mermaid rejected the source.", "line": None}, "mermaid", None
    if parsed is not None and not render:
        return None, "mermaid", None
    try:
        res = await _render_mermaid(source, "png" if render else "none")
    except _RendererUnavailable:
        return None, ("mermaid" if parsed is not None else "unchecked"), None
    if not res.get("ok"):
        return res.get("error") or {"message": "Mermaid rejected the source.", "line": None}, "mermaid", None
    return None, "mermaid", res if render else None


def _numbered(source: str, around: int | None = None, radius: int = 2) -> str:
    lines = source.split("\n")
    lo, hi = 1, len(lines)
    if around:
        lo, hi = max(1, around - radius), min(len(lines), around + radius)
    width = len(str(hi))
    return "\n".join(
        f"{'>' if n == around else ' '}{n:>{width}} | {lines[n - 1]}" for n in range(lo, hi + 1))


def _fmt_parse_error(err: dict, source: str) -> str:
    line = err.get("line") if isinstance(err.get("line"), int) else None
    where = f" at line {line} of the source" if line else ""
    out = [f"❌ Mermaid could not parse this diagram{where}. Nothing was saved.",
           "", "```", str(err.get("message") or "").strip()[:1500], "```"]
    if line and 1 <= line <= len(source.split("\n")):
        out += ["", "```text", _numbered(source, line), "```"]
    out += ["", "Fix the source and call again. Line numbers count from the first "
                "line inside the mermaid fence."]
    return "\n".join(out)


def _validation_note(how: str) -> str:
    if how == "mermaid":
        return "Validated by Mermaid."
    return ("⚠️ NOT validated by Mermaid: no diagram validator is running in this "
            "deployment, and it is set to save diagrams anyway "
            "(DIAGRAM_ALLOW_UNVALIDATED), so only a basic check ran. This source may "
            "not draw. Look at it with `diagram_render` or in the wiki.")


_UNVALIDATED_REFUSAL = (
    "❌ Nothing was saved: no diagram validator is running in this deployment, so "
    "Mermaid could not check the source, and unchecked diagrams are refused.\n\n"
    "To fix it, an administrator runs the `diagram-validator` service (it is part of "
    "the standard Docker Compose and Kubernetes setups; with Compose, "
    "`docker compose up -d diagram-validator`). The optional `diagram-renderer` "
    "validates too. To save diagrams without validation instead, set "
    "DIAGRAM_ALLOW_UNVALIDATED=true on the MCP server.")


def _diagram_result(text: str, *, saved: bool, validated: bool,
                    image: dict | None = None, **fields) -> CallToolResult:
    """A diagram write's result: the text and image an agent reads, plus the
    same outcome as structured fields, so `validated` and `saved` can be
    checked without parsing a sentence."""
    data = {"saved": saved, "validated": validated}
    data.update({k: v for k, v in fields.items() if v is not None})
    return CallToolResult(content=[TextContent(type="text", text=text), *_image_block(image)],
                          structuredContent=data, isError=not saved)


def _refused(err: dict | None, how: str, source: str) -> CallToolResult:
    """The result of a write that validation stopped: a parse error with its
    line, or no validator at all."""
    if err is None:
        return _diagram_result(_UNVALIDATED_REFUSAL, saved=False, validated=False,
                               error={"message": "no diagram validator is available", "line": None})
    line = err.get("line") if isinstance(err.get("line"), int) else None
    return _diagram_result(_fmt_parse_error(err, source), saved=False,
                           validated=(how == "mermaid"),
                           error={"message": str(err.get("message") or ""), "line": line})


async def _check_page_diagrams(content: str, skip: frozenset = frozenset(),
                               where: str = "the page") -> tuple[CallToolResult | None, list[dict]]:
    """Validate every ```mermaid block in `content` before a wiki_write or
    wiki_edit saves it (card #420), with the same fail-closed rule as the
    diagram tools. Blocks whose source is in `skip` (already on the page before
    the edit) are not re-checked, so an old diagram does not block an edit
    elsewhere on its page.

    Returns (refusal or None, the blocks checked). Line numbers are 1-based
    lines of `content`; `where` names it in the refusal text."""
    checked = []
    for b in _mermaid_blocks(content):
        if b["source"] in skip:
            continue
        fence = b["open"] + 1
        err, how, _ = await _validate_mermaid(b["source"], False)
        if err or (how == "unchecked" and not DIAGRAM_ALLOW_UNVALIDATED):
            return _page_diagram_refused(err, b["source"], fence, where), checked
        checked.append({"line": fence, "validated": how == "mermaid"})
    return None, checked


def _page_diagram_refused(err: dict | None, source: str, fence: int, where: str) -> CallToolResult:
    if err is None:
        return _diagram_result(_UNVALIDATED_REFUSAL, saved=False, validated=False,
                               error={"message": "no diagram validator is available",
                                      "line": None, "fence_line": fence})
    line = err.get("line") if isinstance(err.get("line"), int) else None
    page_line = fence + line if line else None
    at = (f" The error is at line {page_line} of {where} (line {line} inside the fence)."
          if page_line else "")
    out = [f"❌ The ```mermaid block that opens at line {fence} of {where} does not parse."
           f"{at} Nothing was saved.",
           "", "```", str(err.get("message") or "").strip()[:1500], "```"]
    if line and 1 <= line <= len(source.split("\n")):
        out += ["", "Numbered from the first line inside the fence:",
                "```text", _numbered(source, line), "```"]
    out += ["", "Fix the diagram and call again. `diagram_render` with `source` checks "
                "Mermaid without saving anything, and `diagram_patch` edits a diagram "
                "already on a page by line."]
    return _diagram_result("\n".join(out), saved=False, validated=True,
                           error={"message": str(err.get("message") or ""), "line": page_line,
                                  "block_line": line, "fence_line": fence})


def _page_saved(text: str, checked: list[dict]) -> CallToolResult | str:
    """A wiki_write or wiki_edit that saved: plain text when it carried no
    diagram, otherwise the text plus `validated` and the fences checked."""
    if not checked:
        return text
    validated = all(c["validated"] for c in checked)
    lines = ", ".join(str(c["line"]) for c in checked)
    n = len(checked)
    text += (f"\n\nMermaid: {n} diagram{'s' if n != 1 else ''} checked "
             f"(fence{'s' if n != 1 else ''} at line{'s' if n != 1 else ''} {lines}). "
             f"{_validation_note('mermaid' if validated else 'unchecked')}")
    return _diagram_result(text, saved=True, validated=validated, diagrams=checked)


def _saved_result(text: str, source: str, how: str, image: dict | None, render: bool,
                  **fields) -> CallToolResult:
    """A write that was saved: say why there is no image when one was asked
    for, and add the house-style dash and layout warnings. None of them blocks
    the write."""
    renderer = None
    if render and not image and how == "mermaid":
        text += f"\n\n{_renderer_off_text()}"
        renderer = _RENDERER_OFF
    dash = _dash_warning(source)
    warnings = ([dash] if dash else []) + _layout_warnings(source)
    for w in warnings:
        text += f"\n\n⚠️ {w['message']}"
    return _diagram_result(text, saved=True, validated=(how == "mermaid"), image=image,
                           renderer=renderer, warnings=warnings or None, **fields)


def _image_block(res: dict | None) -> list:
    if not res or not res.get("data"):
        return []
    if res.get("mime") == "image/svg+xml":
        return [TextContent(type="text", text=f"```svg\n{res['data']}\n```")]
    return [ImageContent(type="image", data=res["data"], mimeType=res.get("mime") or "image/png")]


async def _read_page(ctx: Context, path: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{EDITOR_URL}/api/internal/wiki/read",
                                 json={"token_hash": _token_hash(ctx), "path": path})
        resp.raise_for_status()
        data = resp.json()
    content = data.get("content") or ""
    sha = data.get("sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"path": data.get("path") or path, "content": content, "sha256": sha}


async def _read_diagram(ctx: Context, name: str, folder: str) -> dict:
    """Read the page a diagram name refers to. A path that is not found as
    given is tried again under the AI folder, because that is where
    diagram_create put it (`notes/flow` is saved as `AI/notes/flow.md`)."""
    path = _diagram_path(name, folder)
    try:
        return await _read_page(ctx, path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404 or path.startswith(folder + "/"):
            raise
        return await _read_page(ctx, _ai_path(path, folder))


def _pick_block(blocks: list, block: int, path: str) -> dict:
    if not blocks:
        raise ValueError(f"`{path}` has no ```mermaid block, so it holds no diagram.")
    if not 0 <= block < len(blocks):
        raise ValueError(f"`{path}` has {len(blocks)} mermaid block(s); `block` must be "
                         f"0-{len(blocks) - 1}.")
    return blocks[block]


@mcp.tool(structured_output=False, annotations=ToolAnnotations(
    title="Create a diagram", readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False))
@_with_mermaid_guide
async def diagram_create(
    name: str,
    source: str,
    title: str = "",
    description: str = "",
    render: bool = True,
    ctx: Context = None,
) -> CallToolResult | str:
    """
    Create / draw / make a new diagram, flowchart, sequence diagram or chart in
    Elefant from Mermaid text, and get the rendered picture back to check it.

    The Mermaid text is what is stored (a wiki page holding one ```mermaid
    block), never pixels, so the wiki's history, diff and revert work on it.
    The source is parsed by Mermaid before anything is saved; a mistake comes
    back with its line number (counted in the source you sent, front matter
    included) and nothing is written. The result's structured content carries
    `saved` and `validated` (true when Mermaid's parser checked the source).

    Give nodes short, explicit, stable IDs and put the wording in the label
    (`A[Log in] --> B[Check password]`, not `Log in --> Check password`). Later
    revisions can then change one label without touching anything else.

    To change an existing diagram use `diagram_patch`, never create it again.

    Args:
        name:        Short name, e.g. "login-flow". Stored as
                     `<AI folder>/diagrams/login-flow.md`. A full wiki page path
                     (with a slash) is used as given.
        source:      Mermaid source: the diagram type line (e.g.
                     "flowchart TD"), optionally after a front matter block.
        title:       Heading for the page. Defaults to the name.
        description: Optional text above the diagram.
        render:      Return a PNG of the result (default true).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")

    folder = await _ai_wiki_folder(_tenant_slug(ctx))
    try:
        path = _diagram_path(name, folder)
        if "/" in (name or "").strip().strip("/"):
            path = _ai_path(path, folder)
    except ValueError as exc:
        return str(exc)

    source = _normalise_source(source)
    err, how, image = await _validate_mermaid(source, render)
    if err or (how == "unchecked" and not DIAGRAM_ALLOW_UNVALIDATED):
        return _refused(err, how, source)

    page = _diagram_page(title or _diagram_label(path, folder), description, source)
    try:
        data = await _editor_post("/api/internal/wiki/write", {
            "token_hash": _token_hash(ctx), "path": path, "content": page,
            "if_none_match": True,
        })
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return (f"A diagram already exists at `{path}`. Read it with `diagram_get` "
                    "and change it with `diagram_patch`, or pick another name.")
        return f"Diagram create failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Diagram create failed", exc)

    saved = data.get("path", path)
    sha = data.get("sha256") or hashlib.sha256(page.encode("utf-8")).hexdigest()
    text = (f"✅ Created diagram `{_diagram_label(saved, folder)}` at `{saved}` · "
            f"version `{_diagram_version(sha)}` (pass it as `if_version` to diagram_patch). "
            f"{_validation_note(how)}")
    return _saved_result(text, source, how, image, render,
                         path=saved, version=_diagram_version(sha))


@mcp.tool(annotations=ToolAnnotations(
    title="Read a diagram", readOnlyHint=True, destructiveHint=False, openWorldHint=False))
@_with_mermaid_guide
async def diagram_get(
    name: str,
    block: int = 0,
    ctx: Context = None,
) -> str:
    """
    Show me / open / read the Mermaid source of an Elefant diagram, with line
    numbers and the version to pass to `diagram_patch`.

    Read this before patching: the line numbers it shows are what line edits
    and parse errors refer to, and the version it reports is what the patch is
    pinned to.

    Args:
        name:  The diagram's name (e.g. "login-flow") or a wiki page path.
        block: Which mermaid block, when a page holds several (0 = first).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    folder = await _ai_wiki_folder(_tenant_slug(ctx))
    path = name
    try:
        path = _diagram_path(name, folder)
        page = await _read_diagram(ctx, name, folder)
        blocks = _mermaid_blocks(page["content"])
        b = _pick_block(blocks, block, page["path"])
    except ValueError as exc:
        return str(exc)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"No diagram at `{path}`. `diagram_list` shows the ones that exist."
        return f"Diagram read failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Diagram read failed", exc)

    label = _diagram_label(page["path"], folder)
    which = f" · block {block} of {len(blocks)}" if len(blocks) > 1 else ""
    return "\n".join([
        f"# Diagram `{label}`",
        "",
        f"- **Page:** `{page['path']}`{which}",
        f"- **Version:** `{_diagram_version(page['sha256'])}` (pass as `if_version` to diagram_patch)",
        "",
        "Source with line numbers:",
        "```text",
        _numbered(b["source"]),
        "```",
        "",
        "Raw source:",
        "```mermaid",
        b["source"],
        "```",
    ])


@mcp.tool(structured_output=False, annotations=ToolAnnotations(
    title="Patch a diagram", readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False))
@_with_mermaid_guide
async def diagram_patch(
    name: str,
    if_version: str,
    edits: list[DiagramEdit],
    block: int = 0,
    render: bool = True,
    ctx: Context = None,
) -> CallToolResult | str:
    """
    Change / fix / update one part of an Elefant diagram - relabel a node, add
    or remove an arrow - without redrawing it, and get the new picture back.

    Each edit is targeted: swap an exact snippet (op "replace"), or replace,
    insert or delete a line by number. Everything else in the source stays
    byte-for-byte the same, so the change reads as a small diff in History.

    Pinned with `if_version` (from `diagram_get` or the last create/patch): if
    the diagram changed since, nothing is written and you are told to re-read
    it. The result is parsed by Mermaid before it is saved; an edit that breaks
    the diagram comes back with the line number and is not saved. The result's
    structured content carries `saved` and `validated`, as for diagram_create.

    Undo a patch with `wiki_history` and `wiki_revert` on the diagram's page.

    Args:
        name:       The diagram's name (e.g. "login-flow") or a wiki page path.
        if_version: The version you read. Required.
        edits:      One or more edits, all addressing the source as you read it,
                    e.g. [{"op": "replace", "old": "B[Check password]",
                    "new": "B[Check password and MFA]"}].
        block:      Which mermaid block, when a page holds several (0 = first).
        render:     Return a PNG of the result (default true).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_write"):
        return _permission_denied("wiki_write")
    want = (if_version or "").strip().lower()
    if not want:
        return "`if_version` is required: read the diagram with `diagram_get` first."
    if len(want) < 8:
        return (f"`if_version` '{if_version}' is too short to pin anything: pass the "
                "12-character version `diagram_get` reports.")

    folder = await _ai_wiki_folder(_tenant_slug(ctx))
    try:
        path = _diagram_path(name, folder)
        if "/" in (name or "").strip().strip("/"):
            path = _ai_path(path, folder)
        page = await _read_page(ctx, path)
    except ValueError as exc:
        return str(exc)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"No diagram at `{path}`. Create it with `diagram_create`."
        return f"Diagram read failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Diagram read failed", exc)

    current = _diagram_version(page["sha256"])
    if not page["sha256"].startswith(want):
        return (f"⚠️ Version conflict: `{_diagram_label(page['path'], folder)}` is at version "
                f"`{current}`, not `{if_version}`. It changed since you read it. Nothing was "
                "written. Call `diagram_get`, then apply your edit to what is there now.")
    try:
        b = _pick_block(_mermaid_blocks(page["content"]), block, page["path"])
        patched = _apply_diagram_edits(b["source"], edits)
    except ValueError as exc:
        return f"{exc} Nothing was written."
    except Exception as exc:   # a malformed edit object
        return f"Could not read the edits: {exc}. Nothing was written."

    patched = _normalise_source(patched)
    if patched == b["source"]:
        return f"No change: the edits leave the diagram as it is (version `{current}`)."
    err, how, image = await _validate_mermaid(patched, render)
    if err or (how == "unchecked" and not DIAGRAM_ALLOW_UNVALIDATED):
        return _refused(err, how, patched)

    content = _replace_block(page["content"], b, patched)
    try:
        data = await _editor_post("/api/internal/wiki/write", {
            "token_hash": _token_hash(ctx), "path": page["path"], "content": content,
            "if_match": page["sha256"],
        })
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return ("⚠️ Version conflict: the diagram changed while this patch was being "
                    "checked. Nothing was written. Call `diagram_get` and apply it again.")
        return f"Diagram patch failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Diagram patch failed", exc)

    import difflib
    sha = data.get("sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest()
    diff = "\n".join(difflib.unified_diff(
        b["source"].split("\n"), patched.split("\n"), "before", "after", lineterm="", n=1))
    text = (f"✅ Patched diagram `{_diagram_label(page['path'], folder)}` "
            f"({len(edits)} edit{'s' if len(edits) != 1 else ''}) · version `{current}` → "
            f"`{_diagram_version(sha)}`. {_validation_note(how)}\n\n```diff\n{diff}\n```")
    return _saved_result(text, patched, how, image, render,
                         path=page["path"], version=_diagram_version(sha))


@mcp.tool(structured_output=False, annotations=ToolAnnotations(
    title="Render a diagram", readOnlyHint=True, destructiveHint=False, openWorldHint=False))
@_with_mermaid_guide
async def diagram_render(
    name: str = "",
    source: str = "",
    format: str = "png",
    block: int = 0,
    ctx: Context = None,
) -> CallToolResult | list | str:
    """
    Look at / see / render / preview an Elefant diagram or chart drawn out as
    a picture, to check that it says what you meant. A diagram that parses can
    still be wrong.

    Renders a stored diagram by name, or a Mermaid `source` you have not saved
    yet (nothing is written either way).

    The renderer is an optional service. When it is off, the result says so
    in structured content: `rendered: false`, and `renderer` names the
    setting that enables it (`COMPOSE_PROFILES=diagrams` for Docker Compose,
    the diagram-renderer component of the Kubernetes base), what still works
    (parse validation) and what does not (images). The source is still
    parsed, so a mistake still comes back with its line number.

    Args:
        name:   The diagram's name (e.g. "login-flow") or a wiki page path.
        source: Mermaid source to preview instead of a stored diagram.
        format: "png" (default, an image) or "svg" (the SVG markup as text).
        block:  Which mermaid block, when a page holds several (0 = first).
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")
    fmt = (format or "png").strip().lower()
    if fmt not in ("png", "svg"):
        return "format must be 'png' or 'svg'."
    if bool(name.strip()) == bool(source.strip()):
        return "Give either `name` (a stored diagram) or `source` (a preview), not both."

    label = "preview"
    if name.strip():
        folder = await _ai_wiki_folder(_tenant_slug(ctx))
        path = name
        try:
            path = _diagram_path(name, folder)
            page = await _read_diagram(ctx, name, folder)
            src = _pick_block(_mermaid_blocks(page["content"]), block, page["path"])["source"]
        except ValueError as exc:
            return str(exc)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return f"No diagram at `{path}`. `diagram_list` shows the ones that exist."
            return f"Diagram read failed ({exc.response.status_code}): {_http_err_detail(exc)}"
        except Exception as exc:
            return _internal_error("Diagram read failed", exc)
        label = f"`{_diagram_label(page['path'], folder)}` at version `{_diagram_version(page['sha256'])}`"
    else:
        src = _normalise_source(source)

    err = _mermaid_static_check(src)
    if err:
        return _fmt_parse_error(err, src).replace(" Nothing was saved.", "")
    try:
        res = await _render_mermaid(src, fmt)
    except _RendererUnavailable:
        return await _render_unavailable(src, label)
    if not res.get("ok"):
        return _fmt_parse_error(res.get("error") or {}, src).replace(" Nothing was saved.", "")
    return [TextContent(type="text", text=f"Rendered {label} as {fmt.upper()}."),
            *_image_block(res)]


async def _render_unavailable(source: str, label: str) -> CallToolResult:
    """diagram_render with no renderer: name the setting that turns it on, and
    still say whether Mermaid parses the source, since the validator can."""
    what = "the source" if label == "preview" else label
    try:
        parsed = await _parse_mermaid(source)
    except _ValidatorUnavailable:
        parsed = None
    if parsed is not None and not parsed.get("ok"):
        err = parsed.get("error") or {"message": "Mermaid rejected the source.", "line": None}
        text = _fmt_parse_error(err, source).replace(" Nothing was saved.", "")
    elif parsed is not None:
        text = f"Mermaid parses {what}, but it cannot be drawn here."
    else:
        text = f"Nothing could check or draw {what}."
    data = {"rendered": False, "validated": parsed is not None,
            "renderer": _RENDERER_OFF}
    if parsed is not None and not parsed.get("ok"):
        data["error"] = {"message": str(err.get("message") or ""),
                         "line": err.get("line") if isinstance(err.get("line"), int) else None}
    return CallToolResult(content=[TextContent(type="text", text=f"{text}\n\n{_renderer_off_text()}")],
                          structuredContent=data, isError=False)


@mcp.tool(annotations=ToolAnnotations(
    title="List diagrams", readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def diagram_list(ctx: Context = None) -> str:
    """
    List / what diagrams, flowcharts and charts do I have - every diagram
    stored in Elefant's diagrams folder, by name.

    Diagrams embedded in other wiki pages are not listed here; read those with
    `diagram_get` and the page's path.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "wiki_read"):
        return _permission_denied("wiki_read")

    folder = await _ai_wiki_folder(_tenant_slug(ctx))
    prefix = f"{folder}/diagrams"
    try:
        data = await _editor_post("/api/internal/wiki/tree", {
            "token_hash": _token_hash(ctx), "prefix": prefix,
        })
    except httpx.HTTPStatusError as exc:
        return f"Diagram list failed ({exc.response.status_code}): {_http_err_detail(exc)}"
    except Exception as exc:
        return _internal_error("Diagram list failed", exc)

    paths = [p for p in (data.get("paths") or []) if p.lower().endswith(".md")]
    if not paths:
        return (f"No diagrams yet under `{prefix}/`. Make one with `diagram_create`.")
    lines = [f"# Diagrams ({len(paths)}) under `{prefix}/`", ""]
    for p in paths:
        lines.append(f"- `{_diagram_label(p, folder)}` — `{p}`")
    lines.append("\n_Read one with `diagram_get`, look at it with `diagram_render`._")
    return "\n".join(lines)


# ── Resources ───────────────────────────────────────────────────────────────

@mcp.resource("kb://status")
async def resource_status(ctx: Context = None) -> str:
    """
    Knowledge base server health and index status.

    Returns the current embedding provider, model name, vector dimensions,
    number of wiki docs on disk, number of indexed chunks in Qdrant,
    current reindex state, and any recent indexing errors.

    URI: kb://status
    """
    # Resources previously called _get with no tenant context, which
    # routed every reader to the embedder's "default" collection and
    # exposed the default org's indexed_docs/errors to any authenticated
    # token. Scope to the caller's tenant when ctx is available; if
    # not, return a graceful pointer to the tool equivalent instead of
    # leaking default-tenant counts.
    if ctx is None:
        return ("kb://status requires a tenant-aware MCP client. "
                "Call the equivalent tool, or use kb://status from a "
                "client that forwards request context to resource handlers.")
    slug = _tenant_slug(ctx)
    data    = await _get_authed("/api/status", _token(ctx), tenant_slug=slug)
    state   = data.get("state", "unknown")
    total   = data.get("total_docs", 0)
    indexed = data.get("indexed_docs", 0)
    prov    = data.get("provider", "unknown")
    model   = data.get("model", "unknown")
    vsize   = data.get("vector_size", "?")
    end     = data.get("last_reindex_end") or "never"
    dur     = data.get("last_reindex_duration_s")
    errors  = data.get("errors", [])

    dur_str = f"{dur}s" if dur is not None else "—"
    lines = [
        "# Knowledge Base Status\n",
        f"- **State:** {state}",
        f"- **Wiki docs on disk:** {total}",
        f"- **Indexed chunks:** {indexed}",
        f"- **Last reindex:** {end} (took {dur_str})",
        f"- **Embedding provider:** {prov}",
        f"- **Model:** `{model}` ({vsize} dims)",
    ]
    if errors:
        lines.append("\n## Recent Errors")
        for e in errors:
            lines.append(f"- {e}")
    return "\n".join(lines)


@mcp.resource("kb://wiki/index")
async def resource_wiki_index(ctx: Context = None) -> str:
    """
    List of all indexed wiki documents with their paths and tags.

    Fetches the top 200 wiki chunks from a broad semantic query and de-duplicates
    by path to produce a document inventory. Useful for browsing the knowledge
    base structure before choosing what to search.

    Note: This is an approximation — documents not covered by the 200-result
    window may be omitted. A future /api/docs endpoint will provide exact listing.

    URI: kb://wiki/index
    """
    if ctx is None:
        return ("kb://wiki/index requires a tenant-aware MCP client. "
                "Use the `wiki_tree` tool, which scopes to your tenant via "
                "the OAuth token.")
    slug = _tenant_slug(ctx)
    data    = await _get_authed(
        "/api/search",
        _token(ctx),
        q="knowledge base documentation index",
        limit=200,
        tenant_slug=slug,
    )
    results = data.get("results", [])

    seen: dict[str, dict] = {}
    for r in results:
        path = r.get("path", "")
        if path and path not in seen:
            seen[path] = r

    if not seen:
        return "No wiki documents are currently indexed."

    lines = [f"# Wiki Document Index ({len(seen)} documents)\n"]
    for path, r in sorted(seen.items()):
        tags  = ", ".join(r.get("tags", [])) or "none"
        title = r.get("title", path)
        lines.append(f"- **{title}** — `{path}` | tags: {tags}")
    return "\n".join(lines)


@mcp.resource("kb://memories/{memory_id}")
async def resource_memory(memory_id: str, ctx: Context = None) -> str:
    """
    Fetch a single memory by its UUID.

    Use this to read a specific memory when you already know its ID (from
    memory_list, memory_add, or a previous memory_search result).

    URI: kb://memories/{memory_id}
    Args:
        memory_id: The UUID of the memory to retrieve.
    """
    # Previously this read from the embedder's default collection with
    # no tenant scoping — any authenticated token from any org could
    # fetch any default-tenant memory by guessing its UUID. Require a
    # tenant context; fall through to a pointer when the client doesn't
    # provide one.
    if ctx is None:
        return ("kb://memories/{id} requires a tenant-aware MCP client. "
                "Use the `memory_provenance` or `memory_list` tool, which "
                "scope to your tenant via the OAuth token.")
    slug = _tenant_slug(ctx)
    data = await _get_authed(f"/api/memories/{memory_id}", _token(ctx),
                             tenant_slug=slug)
    return _fmt_memory(data)


@mcp.resource("kb://settings")
async def resource_settings() -> str:
    """
    The caller's persisted Elefant settings (programmatic values, not memory).

    Mirror of the `get_user_settings` tool, for resource-aware clients and the
    admin UI. Resources don't carry per-request token context in all clients,
    so this serves the default tenant; `get_user_settings` is the tenant-aware,
    load-bearing path. URI: kb://settings
    """
    try:
        return await _render_settings("default")
    except Exception as exc:
        return _internal_error("Error fetching settings", exc)


# ── Prompts ─────────────────────────────────────────────────────────────────

@mcp.prompt()
def search_guide() -> str:
    """
    Guidance on which search tool to use and when.

    Use this prompt to understand the tradeoffs between search,
    wiki_search_only, memory_search, and memory_list before running a query.
    """
    return """# Knowledge Base Search Guide

## Tool Selection

**Default: always call `search` first.** It covers both wiki docs and
memories in one ranked list. Only switch to a narrower tool when you have a
specific reason.

| Situation | Tool |
|-----------|------|
| Any general question or lookup (DEFAULT) | `search` |
| User explicitly wants wiki/doc results only | `wiki_search_only` |
| User explicitly wants stored memories only | `memory_search` |
| Browsing all stored memories to find one to update or delete | `memory_list` |
| Reviewing recent storage/retrieval activity | `memory_events` |
| Checking index health | `reindex_status` |

## Search Strategy

1. **Always start with `search`** — it searches wiki docs AND memories
   together and is the right tool for virtually every question.
2. **Refine with tags** — if results are noisy, add `tags="topic,subtopic"` to filter.
3. **Go narrow only if needed** — use `wiki_search_only` or `memory_search` only when
   the user has specifically asked for one source, or after `search` misses.
4. **Adjust limit** — the default (10) is usually enough; increase to 20 for
   exploratory queries where recall matters more than precision.

## Wiki vs Memory

- **Wiki** — markdown documents in the knowledge base. Human-authored. Covers
  infrastructure, SOPs, runbooks, architecture, and reference material. Content
  is chunked by heading; search returns the most relevant section.
- **Memory** — discrete facts stored programmatically via `memory_add`. May cover
  user preferences, session decisions, project-specific facts, or observations
  that don't belong in a formal document.

## wiki_boost in search

The `wiki_boost` parameter (default 1.15) applies a score multiplier to wiki
results so they rank above memories of similar relevance. Increase it (up to 2.0)
to strongly prefer documented knowledge. Set it to 1.0 for an unbiased merge.

## Resource Shortcuts

- `kb://status` — check if the index is healthy before a large search session.
- `kb://wiki/index` — browse the document inventory without running a search.
- `kb://memories/{id}` — fetch a specific memory when you already know its ID.

## Access Control

Search results are automatically filtered by your token's label scope and
data classification access. Results tagged with PHI, PII, or Sensitive are
hidden unless your token explicitly grants access to those classifications.
"""


@mcp.prompt()
def memory_template() -> str:
    """
    A structured template for writing high-quality, retrievable memories.

    Use this prompt to understand the conventions for creating memories that
    are easy to find, update, and reason about later.
    """
    return """# Memory Writing Guide

## Memory Types

| Type | When to use |
|------|-------------|
| `fact` | Stable truths — configs, preferences, decisions that don't change often |
| `event` | Time-based records — deployments, incidents, meetings, milestones |
| `procedure` | Step-by-step instructions — runbooks, how-tos, workflows |
| `conversation` | Chat-derived summaries — key points from a session |
| `derived` | AI-generated insights — patterns, analysis, inferred knowledge |

## What Makes a Good Memory

A good memory is:
- **Specific** — one discrete fact, decision, or observation per memory
- **Self-contained** — readable without context from the conversation that created it
- **Tagged well** — 2–5 tags that reflect topic, project, and memory type
- **Dated** (for time-sensitive facts) — include a date if the fact may change

## Template

```
content: |
  [Date if relevant: YYYY-MM-DD]

  [Subject/entity the memory is about — be specific]

  [The fact, decision, preference, or observation — one clear sentence or short paragraph]

  [Optional: why this was recorded]

tags: ["<domain>", "<project-or-system>", "<memory-type>"]
type: fact | event | procedure | conversation | derived
```

## Tag Conventions

| Tag type | Examples |
|----------|----------|
| Domain | `infra`, `networking`, `security`, `ai`, `dev`, `ops` |
| Project/system | `project-x`, `kubernetes`, `postgres`, `llm-stack` |
| Memory type | `decision`, `preference`, `fact`, `runbook`, `observation` |
| Lifecycle | `temporary`, `review-needed`, `deprecated` |
| Classification | `PHI`, `PII`, `Sensitive` (requires token classification_access) |

## Owner Tagging

Every memory stored via the MCP API automatically receives an `owner:<name>` tag
where `<name>` is the token owner's name. This lets you filter memories by source:
  `memory_list(tags="owner:my-agent")`

## Examples

**Good memory (fact):**
```
content: "Decided to use nomic-embed-text-v1.5 via LM Studio as the embedding model.
          Chosen for 768-dim quality on technical text and local inference speed."
tags: ["infra", "ai", "knowledge-base", "decision"]
type: fact
```

**Good memory (event):**
```
content: "2025-04-07 — Deployed MCP auth system with bearer tokens and RBAC.
          Includes PHI/PII/Sensitive classification tags and owner auto-tagging."
tags: ["infra", "deployment", "mcp"]
type: event
```

**Bad memory (too vague):**
```
content: "we decided on the embedding model"
tags: ["ai"]
```

## When NOT to Create a Memory

- The information already exists in a wiki document (use `search` first)
- The fact is trivially short-lived
- A duplicate may already exist (use `memory_search` first to check)
"""


# ── OAuth discovery ASGI handler ────────────────────────────────────────────
#
# Serves /.well-known/oauth-authorization-server without requiring a Bearer
# token (exempt path in BearerAuthMiddleware).  Points all OAuth endpoints at
# the editor service, which is the actual authorization server.

async def _oauth_discovery_app(scope: "Scope", receive: "Receive", send: "Send") -> None:
    """
    Serves two OAuth discovery documents without requiring a Bearer token.

    /.well-known/oauth-protected-resource  (RFC 9728)
        Identifies this server as a resource server and points to the AS.
        Claude and other MCP clients use this to find the authorization server.

    /.well-known/oauth-authorization-server  (RFC 8414, proxied for convenience)
        Returns AS metadata so clients that skip the two-step RS→AS lookup
        still work. The issuer is the AS base URL (no /editor path suffix) so
        RFC 8414 validation passes.
    """
    path = scope.get("path", "")

    if path in ("/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp"):
        # RFC 9728: resource server metadata — points clients at the real AS.
        metadata = {
            "resource":                 EXTERNAL_MCP_URL,
            "authorization_servers":   [AS_ISSUER],
            "bearer_methods_supported": ["header"],
        }
    else:
        # RFC 8414: authorization server metadata.
        # issuer has no path so discovery URL is simply {issuer}/.well-known/oauth-authorization-server.
        metadata = {
            "issuer":                                AS_ISSUER,
            "authorization_endpoint":               f"{EXTERNAL_EDITOR_URL}/oauth/authorize",
            "token_endpoint":                        f"{EXTERNAL_EDITOR_URL}/oauth/token",
            "registration_endpoint":                 f"{EXTERNAL_EDITOR_URL}/oauth/register",
            "revocation_endpoint":                   f"{EXTERNAL_EDITOR_URL}/oauth/revoke",
            "response_types_supported":              ["code"],
            "grant_types_supported":                 ["authorization_code"],
            "code_challenge_methods_supported":      ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported":                      ["mcp"],
        }

    body = json.dumps(metadata).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type",   b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control",  b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


# ── Liveness ────────────────────────────────────────────────────────────────
#
# /health is what the Compose healthcheck probes (card #300). It answers from
# this process alone: no token, no I/O, and nothing in the body a caller could
# learn from beyond "the server is up". Readiness of the services behind it is
# not its question — a slow editor must not mark this container unhealthy.
# nginx routes only /mcp and /mcp/* here, so from the outside this is reachable
# only on the directly published MCP port, where it says no more than a TCP
# connect would.

_HEALTH_PATH = "/health"
_HEALTH_BODY = json.dumps({"status": "ok"}).encode()


async def _health_app(scope: "Scope", receive: "Receive", send: "Send") -> None:
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type",   b"application/json"),
            (b"content-length", str(len(_HEALTH_BODY)).encode()),
            (b"cache-control",  b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": _HEALTH_BODY})


class _OAuthDiscoveryRouter:
    """Routes OAuth well-known paths to _oauth_discovery_app and /health to
    _health_app, both without auth; everything else to inner app."""

    def __init__(self, app: "ASGIApp") -> None:
        self.app = app

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope.get("type") == "http" and scope.get("path") in _AUTH_EXEMPT_PATHS:
            await _oauth_discovery_app(scope, receive, send)
        elif scope.get("type") == "http" and scope.get("path") == _HEALTH_PATH:
            await _health_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# ── Board (post-it Kanban) ───────────────────────────────────────────────────
# Thin cards on a per-tenant board — a third durable surface alongside memory
# and the wiki. card_add is the reflex when the user floats an idea they aren't
# building today. Column names are the user's own vocabulary — call
# board_columns() to learn them.
#
# Permissions: reads check board_read, writes check board_write. Deliberately
# NOT restated in the tool docstrings — agents that read them literally were
# concluding they lacked a grant they had to go obtain, and refusing to call the
# tools at all. There is no separate board consent for the OAuth path: an OAuth
# token derives board_read/board_write from its wiki scopes (see
# OAuthAccessToken.to_validate_dict). ApiTokens carry their own board_read
# (default on) and board_write (default off), which the owner sets in My
# Settings → API tokens and an admin sets in the admin console. Enforcement
# below is unchanged; a genuine denial returns _permission_denied(), which
# names the missing scope.

async def _board_call(method: str, endpoint: str, *, params: dict | None = None,
                      json_body: dict | None = None) -> tuple[int, dict]:
    """Call a memory-api /board endpoint with the internal-service header.
    Returns (status_code, payload). On error the payload is the parsed body
    (usually {"detail": ...}) so the tool can surface memory-api's message —
    e.g. the slug-collision holder — verbatim to the agent."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(
            method,
            f"{MEMORY_API_URL}{endpoint}",
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json_body,
            headers=_internal_service_headers(),
        )
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if resp.status_code >= 500 or (resp.status_code >= 400 and not isinstance(payload, dict)):
        # A failure body is never relayed raw: a 5xx carries the exception and
        # a non-JSON body can carry anything. Log it; hand the agent a ref.
        ref = _error_ref()
        _logging.getLogger("mcp").warning(
            "board %s %s -> %s [ref %s]: %s", method, endpoint, resp.status_code,
            ref, (resp.text or "")[:500])
        payload = {"detail": f"internal error (ref {ref})"}
    elif not isinstance(payload, dict):
        payload = {}
    return resp.status_code, payload


async def _card_wiki_thread(tenant_slug: str, card: dict) -> list:
    """The card's wiki thread as the board UI shows it, from the editor.

    memory-api stores only the card's explicit `wiki_links`; the pages
    associated by convention (under `<AI folder>/projects/<card-slug>/`) are
    resolved by the editor, which owns the AI-folder config and S3. Best-effort:
    a card read must never fail because the thread lookup did.
    """
    slug = (card or {}).get("slug")
    if not slug:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/board/wiki-pages",
                json={
                    "tenant_slug": tenant_slug,
                    "slug": slug,
                    "aliases": card.get("aliases") or [],
                    "wiki_links": card.get("wiki_links") or [],
                },
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            return resp.json().get("wiki_pages") or []
    except Exception as exc:
        _logging.getLogger("mcp").warning(
            "card wiki-thread lookup failed for %s: %s", slug, exc)
        return []


def _actor_ref(token_info: dict) -> str:
    """The principal this caller acts AS, or '' when it has no durable one.

    A token carrying a user IS that user: the agent is holding the person's
    credential, so the person is who acted, and a card it files says so. That
    is the case of an agent working for a person, and it falls
    out of the token rather than the agent having to reason about it.

    A standalone token — a routine's own credential, linked to nobody — is its
    own agent account, which is the other case the specification names: an
    agent that finds something and files a card on its own is the reporter.

    An OAuth token with no user is the honest empty case. Its id is derived
    from the token hash and would be a different "agent" after every login, so
    minting a reference from it would scatter one actor across the board as a
    crowd of strangers. The card then simply has no reporter unless the caller
    names one.
    """
    info = token_info or {}
    user_id = (info.get("user_id") or "").strip()
    if user_id:
        return f"user:{user_id}"
    if (info.get("principal_kind") or "") == "api":
        token_id = (info.get("id") or "").strip()
        if token_id:
            return f"agent:{token_id}"
    return ""


async def _card_people(tenant_slug: str, cards: list) -> dict:
    """Display records for every principal these cards name.

    memory-api stores references; the NAMES live in the editor. Without this
    an agent reads `user:9f2c…` off a card and has no way to know whose card
    it is looking at — which is the failure the attribute exists to fix, moved
    one layer along. Best-effort: a card renders with raw references rather
    than not rendering.
    """
    refs, seen = [], set()
    for c in cards or []:
        for ref in ([(c or {}).get("reporter") or "", (c or {}).get("assignee") or ""]
                    + list((c or {}).get("participants") or [])):
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    if not refs:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/board/people",
                json={"tenant_slug": tenant_slug, "describe": refs},
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            return resp.json().get("people") or {}
    except Exception as exc:
        _logging.getLogger("mcp").warning("card people lookup failed: %s", exc)
        return {}


async def _resolve_person(tenant_slug: str, value: str) -> tuple:
    """Resolve one typed name to a principal reference. Returns (ref, error).

    Exactly one of the two is set. The editor owns the directory, so it also
    owns the wording of a refusal — an unresolvable name comes back with the
    message that names the accounts which do exist, rather than a second
    phrasing invented here that would drift from the store's.
    """
    who = (value or "").strip()
    if not who:
        return (None, "Name the person.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{EDITOR_URL}/api/internal/board/people",
                json={"tenant_slug": tenant_slug, "resolve": [who]},
                headers=_internal_service_headers(),
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as exc:
        _logging.getLogger("mcp").warning("person resolution failed: %s", exc)
        return (None, "Could not reach the identity directory to resolve that "
                      "name, so nothing was changed.")
    ref = (data.get("resolved") or {}).get(who)
    if ref:
        return (ref, None)
    errors = data.get("errors") or {}
    return (None, errors.get(who) or f"Could not resolve {who!r} to an account on this board.")


def _person_label(ref: str, people: dict) -> str:
    """One person, as an agent should see them: the name, then the reference.

    Both, always. The name is what a human conversation uses; the reference is
    what a follow-up call has to pass back. Printing only the name would make
    the agent guess at the identifier, and guessing identifiers is what this
    field replaced.
    """
    rec = (people or {}).get(ref) or {}
    name = rec.get("display")
    if not name:
        # Nothing resolved this one, so the reference is all there is. Printing
        # it twice — as the name and as the reference — would read like two
        # facts where there is one.
        return f"`{ref}`"
    marks = []
    if rec.get("kind") == "agent":
        marks.append("agent")
    if rec.get("known") and not rec.get("active"):
        marks.append("inactive")
    if not rec.get("known"):
        marks.append("no longer on this board")
    suffix = f", {'; '.join(marks)}" if marks else ""
    return f"{name} (`{ref}`{suffix})"


# Off unless a tenant asks for one. There is no maximum description
# length; this only ever produced an advisory warning, and now it produces none
# until somebody sets board_nudge_chars.
_BOARD_NUDGE_OFF = 0


async def _board_nudge_chars(slug: str) -> int:
    """The tenant's description-length nudge (0 — off — when no override)."""
    try:
        data = await _editor_get("/api/internal/tenant-settings", tenant_slug=slug)
        v = (data.get("settings") or {}).get("board_nudge_chars")
        return int(v) if v is not None else _BOARD_NUDGE_OFF
    except Exception:
        return _BOARD_NUDGE_OFF


def _fmt_warnings(payload: dict) -> str:
    ws = payload.get("warnings") or []
    return ("\n\n" + "\n".join(f"⚠️  {w}" for w in ws)) if ws else ""


def _fmt_card(c: dict) -> str:
    aliases = ", ".join(c.get("aliases") or []) or "none"
    # The number is how a human refers to this card out loud ("look at #33"), so
    # an agent has to be able to join that back to a slug. Rendered next to the
    # slug for exactly that reason. Absent on a card written before the backfill
    # ran, in which case it is simply left off.
    num = f"#{c['number']}  " if c.get("number") is not None else ""
    head = (
        f"{num}**{c.get('title','?')}**  `{c.get('slug','?')}`\n"
        f"- Column: **{c.get('column','?')}**  ·  Stage: `{c.get('stage','?')}`\n"
        f"- Source: {c.get('source','?')}  ·  Aliases: {aliases}"
        + ("  ·  _closed_" if (c.get("closed") or c.get("archived")) else "")
    )
    # Labels are how a card says what KIND of thing it is (bug, enhancement,
    # a repo, a sprint). An agent that can't see them can't act on them, so
    # they go on every rendered card, not just card_get.
    if c.get("labels"):
        head += "\n- Labels: " + ", ".join(f"`{lb}`" for lb in c["labels"])
    # The repo decides which repository work on this card lands in, so an agent
    # acting on the card needs it on the card, not inferred from prose.
    if c.get("repo"):
        head += f"\n- Repo: `{c['repo']}`"
    # Who raised the card and who is on it. `people` is the resolved display
    # map the caller attached; without it the references still render, which
    # is worse to read but never wrong.
    people = c.get("people") or {}
    if c.get("reporter"):
        head += f"\n- Reporter: {_person_label(c['reporter'], people)}"
    if c.get("assignee"):
        head += f"\n- Assignee: {_person_label(c['assignee'], people)}"
    if c.get("due"):
        head += f"\n- Due: {c['due']}{_due_note(c)}"
    if c.get("participants"):
        head += "\n- Participants: " + ", ".join(
            _person_label(p, people) for p in c["participants"])
    # Surface the thread between a card and its write-up — including links a
    # human made in the board UI. Without this the agent can't see that a page
    # is already attached and re-pastes a URL into the body.
    #
    # `wiki_pages` is the editor's enriched view (convention-associated pages +
    # explicit links, missing targets flagged) and is present on card_get. The
    # write tools echo memory-api's card, which carries only the raw
    # `wiki_links` — fall back to those so a link is never invisible.
    pages = c.get("wiki_pages")
    if pages:
        def _one(p: dict) -> str:
            kind = "linked" if p.get("kind") == "linked" else "auto"
            miss = ", missing" if p.get("missing") else ""
            return f"`{p.get('path','?')}` ({kind}{miss})"
        head += "\n- Wiki pages: " + ", ".join(_one(p) for p in pages)
    elif c.get("wiki_links"):
        head += "\n- Linked wiki pages: " + ", ".join(
            f"`{p}`" for p in c["wiki_links"])
    # The evaluation lock, on every rendered card for the same reason the
    # blocker is: an agent reading a card top-to-bottom must not start one that
    # a reviewer run already has in hand. Said in the imperative for that
    # reason, rather than as a field called `eval_lock`.
    held = c.get("eval_lock") or None
    if held:
        since = (held.get("since") or "")[:16].replace("T", " ")
        head += (f"\n- ⏳ **Under evaluation** by run `{held.get('run_id','?')}`"
                 + (f" since {since}" if since else "")
                 + (" — that lock is STALE, so a new acquire would break it"
                    if held.get("stale") else " — leave it alone"))
    head += _fmt_relationships(c.get("relationships"), c.get("blocked_by"))
    head += _fmt_comments(c.get("comments"), people)
    body = (c.get("body") or "").strip()
    return head + (f"\n\n{body}" if body else "")


_DONE_STAGES = ("done", "shipped", "dropped")


def _due_note(c: dict) -> str:
    """How a due date reads against today: overdue, due today, or due soon
    (within a week). Empty for anything further out, for a card with no due
    date, and for finished work, which is not late whatever its date said.

    Said on the card because a date alone makes the reader do the arithmetic,
    and an agent scanning a column for what is late should not have to.
    """
    due = c.get("due")
    if not due or c.get("closed") or c.get("archived") or c.get("stage") in _DONE_STAGES:
        return ""
    try:
        d = date.fromisoformat(str(due)[:10])
    except ValueError:
        return ""
    days = (d - datetime.now(timezone.utc).date()).days
    if days < 0:
        return f" ⚠ **overdue** by {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return " ⏰ due today"
    if days <= 7:
        return f" ⏰ due in {days} day{'s' if days != 1 else ''}"
    return ""


def _fmt_comments(comments: list | None, people: dict | None) -> str:
    """The conversation about the card, oldest first.

    Rendered on the card rather than behind a second call, because the whole
    point is that an agent picking up a card reads what has already been said
    about it. A withdrawn comment keeps its place and says so — a thread that
    silently loses a message reads as one that never had it.
    """
    if not comments:
        return ""
    lines = ["", f"**Comments** ({sum(1 for c in comments if not c.get('deleted'))})"]
    for c in comments:
        who = _person_label(c.get("author") or "", people or {})
        when = (c.get("created_at") or "")[:16].replace("T", " ")
        if c.get("deleted"):
            lines.append(f"- _{who} · {when} — withdrawn_")
            continue
        edited = " _(edited)_" if c.get("edited") else ""
        body = (c.get("body") or "").strip()
        # Indented so a multi-line comment stays visibly one comment rather
        # than merging into the next bullet.
        body = body.replace("\n", "\n  ")
        lines.append(f"- **{who}** · {when}{edited}\n  {body}")
    return "\n".join(lines) + "\n"


def _fmt_card_ref(r: dict) -> str:
    num = f"#{r['number']} " if r.get("number") is not None else ""
    return f"{num}`{r.get('slug','?')}`"


def _fmt_relationships(rel: dict | None, blocked_by: list | None) -> str:
    """A card's links to other cards, led by the one that changes what you do.

    Blockers come FIRST and say so in the imperative, because the whole point of
    the field is that an agent reading a card top-to-bottom should not start it.
    An unresolved blocker is not a footnote about the card; it is the answer to
    "can I work this".
    """
    out = ""
    rel = rel or {}
    unresolved = [b for b in rel.get("blocked_by", []) if not b.get("resolved")]
    # The list view carries only the summary, so fall back to it — a blocked
    # card must never render as startable just because it came from card_list.
    if not rel and blocked_by:
        unresolved = list(blocked_by)
    if unresolved:
        out += ("\n- ⛔ **BLOCKED** by " + ", ".join(_fmt_card_ref(b) for b in unresolved) +
                " — finish those first, or work the blocker instead")
    done = [b for b in rel.get("blocked_by", []) if b.get("resolved")]
    if done:
        out += "\n- Was blocked by (now done): " + ", ".join(_fmt_card_ref(b) for b in done)
    if rel.get("blocks"):
        open_ = [b for b in rel["blocks"] if not b.get("resolved")]
        if open_:
            out += "\n- Blocks: " + ", ".join(_fmt_card_ref(b) for b in open_)
    if rel.get("related"):
        out += "\n- Related: " + ", ".join(_fmt_card_ref(b) for b in rel["related"])
    return out


def _fmt_card_list(cards: list, people: dict | None = None) -> str:
    if not cards:
        return "No cards."
    lines: list = []
    cur = None
    for c in cards:
        key = (c.get("column"), c.get("stage"))
        if key != cur:
            cur = key
            lines.append(f"\n### {c.get('column','?')} · `{c.get('stage','?')}`")
        arch = " _(closed)_" if (c.get("closed") or c.get("archived")) else ""
        labels = "".join(f" `{lb}`" for lb in (c.get("labels") or []))
        repo = f" · {c['repo']}" if c.get("repo") else ""
        num = f"#{c['number']} " if c.get("number") is not None else ""
        # A blocked card has to be visibly blocked in the LIST, not only when
        # you open it. A dispatch routine scans the column and takes the top
        # card; if blocked-ness only showed on card_get it would take one it
        # cannot do and find out afterwards.
        blocked = ""
        if c.get("blocked"):
            blocked = " ⛔ blocked by " + ", ".join(
                _fmt_card_ref(b) for b in (c.get("blocked_by") or []))
        # One name in a list — enough to scan "whose cards are these", without
        # the references that would drown a long board. The full people block is
        # on the card itself.
        #
        # The ASSIGNEE is that name when there is one: scanning a column is
        # mostly asking who has what, and who raised it is the older question.
        # Falls back to the reporter so an unassigned card still says something.
        who_ref = c.get("assignee") or c.get("reporter") or ""
        rep = (people or {}).get(who_ref) or {}
        mark = "→" if c.get("assignee") else "·"
        who = f" {mark} {rep['display']}" if rep.get("display") else ""
        n = c.get("comment_count") or 0
        # A count on a list row, never the text: "3 comments" is useful when
        # scanning a column and the conversation itself would drown it.
        talk = f" · {n}💬" if n else ""
        due = f" · due {c['due']}{_due_note(c)}" if c.get("due") else ""
        lines.append(f"- {num}**{c.get('title','?')}**  `{c.get('slug','?')}`{labels}{repo}{who}{due}{talk} — {c.get('source','?')}{arch}{blocked}")
    return "\n".join(lines).strip()


@mcp.tool()
async def board_columns(ctx: Context = None) -> str:
    """
    What columns / what lists / how is the board laid out - the columns of
    the Elefant ("elephant") Kanban board, left → right, with the
    architected stage each maps to. Call this before card_add / card_move so
    you use the user's own column names (they're arbitrary and user-defined
    - "Up Next", "Backlog", "Retrospective"). Seeds the default columns on
    first use.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_read"):
        return _permission_denied("board_read")
    status, payload = await _board_call("GET", "/board/columns",
                                        params={"tenant_slug": _tenant_slug(ctx)})
    if status >= 400:
        return f"Could not list columns: {payload.get('detail', status)}"
    cols = payload.get("columns", [])
    stages = payload.get("stages", [])
    lines = ["## Board columns (left → right)\n",
             "Column names are the user's vocabulary; each maps to one architected stage.\n"]
    for c in cols:
        oe = "  ·  on_enter: dispatch" if c.get("on_enter") == "dispatch" else ""
        lines.append(f"- **{c['name']}** → `{c['stage']}`{oe}")
    # The list the server sends, not a constant: it is the current vocabulary
    # plus any older stage THIS board still uses, so a column's own stage is
    # always among the ones offered.
    lines.append("\nStages this board may use: " + ", ".join(f"`{s}`" for s in stages))
    return "\n".join(lines)


@mcp.tool()
async def card_add(
    title: str,
    slug: str = "",
    body: str = "",
    column: str = "",
    labels: list[str] | None = None,
    repo: str = "",
    wiki_links: list[str] | None = None,
    reporter: str = "",
    assignee: str = "",
    participants: list[str] | None = None,
    due: str = "",
    ctx: Context = None,
) -> str:
    """
    Add / create / file / raise / open / put / stick a card, task, ticket,
    to-do or idea on the Elefant ("elephant") board - the reflex when the
    user floats an idea, proposal, or backlog item they are NOT building
    right now. A card is a tracked issue: a title and a description,
    organised on a board. The description has no length limit, so write what
    the card needs. What it is still not is a document - when an idea wants
    its own page, write the page and LINK it to the card (see below); never
    paste a page URL into the body.

    WHICH ONE OF THESE FOUR. "Remember / save / note this" → memory_capture.
    "Remember this exactly, word for word" → memory_add. "Note to self …" →
    note_to_self, which files a board card. "Add a card / task / to-do", or
    an idea the user is not building right now → card_add. Longer than a
    post-it, or reference material somebody will read later → wiki_write.

    Defaults to the leftmost backlog column. The slug is derived from
    the title when omitted and is the card's durable identity — it must be
    unique, so a collision fails and names the holder rather than guessing.

    LINKING A CARD TO WIKI PAGES — two ways, both first-class:
      1. By convention (self-healing, preferred): write the page under
         `<AI folder>/projects/<card-slug>/…` (e.g. `AI/projects/netdisco/
         design.md`). Pages there are associated with the card automatically at
         read time — renaming the card or the page never breaks the thread.
      2. Explicitly: pass `wiki_links` here (or use `card_link_wiki` later) to
         attach any existing wiki page, wherever it lives.

    LABELS classify the card — `bug`, `enhancement`, a sprint. Free-form
    strings, shown as chips on the card in the board UI and usable as a
    `card_list` filter. Set them here at capture time; afterwards reach for
    `card_label`, which adds or removes ONE without touching the rest.

    REPO is the card's GitHub repository, and it is its own structured field
    rather than a label or a line of body text. Set it whenever the card is
    work in a known repo: several repos share one board, and the repo field is
    what lets `card_list(repo=...)` and the board UI show one project at a
    time. Afterwards use `card_repo` to change or clear it.

    PEOPLE — reporter and participants — are resolved accounts, not names in
    prose. **When you are filing this card because a person asked you to, pass
    that person as `reporter`.** You are then added as a participant
    automatically, so the card records both who wanted it and who wrote it. A
    card you raise on your own initiative needs neither argument: it defaults
    to you. Give a person as a username, an email, a display name, or a
    `user:`/`agent:` reference read off another card; a name that matches
    nobody is refused, and the error names the accounts that do exist.

    Args:
        title:  Short card title.
        slug:   Optional stable identifier (lowercase-hyphen). Derived from the
                title when empty. This is the key every future reference joins on.
        body:   The card's DESCRIPTION. Markdown, including fenced code blocks,
                renders in the board UI. No length limit — write what the card
                needs. (The field is still called `body` on the wire; a person
                reads it as the description.)
        column: Optional target column — its name (from board_columns) or id.
                Defaults to the backlog.
        labels: Optional classification labels, e.g. ["bug", "elefant"].
                Free-form; de-duplicated case-insensitively.
        repo:   Optional GitHub repository this card's work belongs to. Give it
                as `owner/name`, or paste a github.com URL and it is normalized
                to that. One repo per card.
        wiki_links: Optional wiki page paths to attach to the card, e.g.
                ["AI/projects/netdisco/design.md"]. These show as linked pages
                on the card in the board UI.
        reporter: Who RAISED this card. Defaults to you. Pass the person you
                are working with when you are filing it for them — you go on
                the card as a participant either way.
        assignee: Who is DOING this card. Unlike `reporter` it has no default:
                whoever files a card is very often not whoever will do it, so
                leaving it blank says "unassigned" rather than guessing. Agents
                can be assigned.
        participants: Other people involved in the card.
        due:    Optional due DATE, `YYYY-MM-DD` (no time of day). Set it when
                the card is time-bound, rather than writing the date into the
                title or a label. Change or clear it later with `card_due`.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    slug_t = _tenant_slug(ctx)
    body_req = {
        "tenant_slug": slug_t,
        "title": title,
        "slug": slug or None,
        "body": body or "",
        "column": column or None,
        "labels": labels or [],
        "repo": repo or "",
        "wiki_links": wiki_links or [],
        "reporter": reporter or "",
        "assignee": assignee or "",
        "due": (due or "").strip(),
        "participants": participants or [],
        "actor": _actor_ref(token_info),
        "source": token_info.get("source") or "unknown",
        "nudge_chars": await _board_nudge_chars(slug_t),
    }
    status, payload = await _board_call("POST", "/board/cards", json_body=body_req)
    if status >= 400:
        return f"Could not add card: {payload.get('detail', status)}"
    card = payload.get("card", {})
    card["people"] = await _card_people(slug_t, [card])
    return "Card added.\n\n" + _fmt_card(card) + _fmt_warnings(payload)


def _note_title_body(note: str) -> tuple[str, str]:
    """Split a free-form note into a scannable card (title, body). A single short
    line becomes the title alone; a long or multi-line note keeps a lead line as
    the title with the full text preserved in the body."""
    text = (note or "").strip()
    if not text:
        return "", ""
    first_line = text.split("\n", 1)[0].strip()
    if "\n" not in text and len(text) <= 200:
        return text, ""
    title = first_line if len(first_line) <= 120 else first_line[:117].rsplit(" ", 1)[0] + "…"
    return title, text


@mcp.tool()
async def note_to_self(note: str, ctx: Context = None) -> str:
    """
    Note to self / remind me / jot this down / don't let me forget - file
    the thought as a card on the Elefant ("elephant") board. This is the
    reflex when the user prefixes something with the phrase **"note to
    self"** (or "note-to-self") - that phrase is the explicit signal the
    thought belongs on the Kanban board as a thin post-it, NOT in memory
    (memory_capture) and NOT as a wiki page.

    WHICH ONE OF THESE FOUR. "Remember / save / note this" → memory_capture.
    "Remember this exactly, word for word" → memory_add. "Note to self …" →
    note_to_self, which files a board card. "Add a card / task / to-do", or
    an idea the user is not building right now → card_add. Longer than a
    post-it, or reference material somebody will read later → wiki_write.

    Drops one card in the leftmost backlog column, labelled
    `note-to-self` so these captures are easy to find and filter later —
    `card_list(label="note-to-self")`. The board reserves that label: replacing
    a card's labels cannot drop it, so a later edit by any agent leaves the
    record of how the card was captured intact. A card is a sticky, not a
    document — when a note outgrows it, promote it to a wiki page and keep the
    card thin.

    Args:
        note: The note text (the words after "note to self"). A short note
              becomes the card title; a longer or multi-line note keeps a
              scannable lead line as the title with the full text in the body.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    title, body = _note_title_body(note)
    if not title:
        return "Nothing to capture — the note was empty."
    slug_t = _tenant_slug(ctx)
    body_req = {
        "tenant_slug": slug_t,
        "title": title,
        "body": body,
        # The marker is a label like any other classification; what keeps it
        # from being lost is that the board reserves it, not that it lives in a
        # field of its own.
        "labels": ["note-to-self"],
        # A note to self has a self. Whoever's credential this is reported it,
        # which is exactly the defaulting rule, so no explicit reporter here.
        "actor": _actor_ref(token_info),
        "source": token_info.get("source") or "unknown",
        "nudge_chars": await _board_nudge_chars(slug_t),
    }
    status, payload = await _board_call("POST", "/board/cards", json_body=body_req)
    if status >= 400:
        return f"Could not capture the note: {payload.get('detail', status)}"
    card = payload.get("card", {})
    card["people"] = await _card_people(slug_t, [card])
    return "Noted — card added.\n\n" + _fmt_card(card) + _fmt_warnings(payload)


@mcp.tool()
async def card_list(
    column: str = "",
    stage: str = "",
    tag: str = "",
    label: str = "",
    repo: str = "",
    reporter: str = "",
    assignee: str = "",
    participant: str = "",
    person: str = "",
    search: str = "",
    due_before: str = "",
    due_after: str = "",
    overdue: bool = False,
    closed: bool = False,
    archived: bool = False,
    ctx: Context = None,
) -> str:
    """
    What's on the board / what's up next / show me my tasks / what am I
    working on / what's in progress - read the Elefant ("elephant") board.
    Optionally filter by column (name or id), by stage (backlog /
    selected-for-development / in-progress / scheduled / blocked / review /
    done - or, on a board created before that vocabulary, one of spark /
    shaping / designed / ready / building / shipped / dropped), by tag, by
    label, by GitHub repo, by the people on a card, or by free text.
    Closed cards are hidden unless closed=true.

    `archived` is what `closed` used to be called and still does the same
    thing, so a caller written against the old name keeps working.

    Filters combine, so `stage="designed", label="bug"` is every bug card in
    the designed columns. Label matching is case-insensitive and exact — it
    is not a substring search.

    `repo` is the filter to reach for when several projects share this board
    and you want one project's cards: `card_list(repo="owner/name")`. It is
    matched canonically, so a full github.com URL finds the same cards as the
    bare `owner/name`.

    PEOPLE FILTERS answer the questions worth asking about a person:
    `reporter=` is what they raised, `assignee=` is what they are DOING,
    `participant=` is what they are on, and `person=` is any of the three —
    usually the one you want for "what is Jon involved in". `assignee=` is the
    one to reach for to answer "what is on my plate". All of them take a
    username, a display name, or a `user:`/`agent:` reference; a value matching
    nobody returns no cards rather than an error, so a stale filter is empty
    rather than broken.

    `tag` is the PRE-MERGE spelling of `label` and filters the same field —
    cards carried two string arrays once and now carry one. It still answers,
    so `tag="note-to-self"` keeps finding what the user filed with that phrase,
    but `label=` is the name to reach for.
    `search` is free text over title, body, slug, labels and repo, for
    when you know roughly what a card said but not its slug. An all-digits
    query (`57`, or `#57`) is also read as a card NUMBER: that card is matched
    even though the digits appear nowhere in its text, and it is returned
    first, ahead of any card that merely mentions 57.

    DUE DATES: `due_before="2026-11-30"` and `due_after="2026-11-01"` are
    inclusive `YYYY-MM-DD` bounds, and either one leaves out cards with no due
    date. `overdue=True` is every card whose due date has passed (UTC) and that
    is not done or closed — the one to reach for to answer "what is late".
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_read"):
        return _permission_denied("board_read")
    slug_t = _tenant_slug(ctx)
    status, payload = await _board_call("GET", "/board/cards", params={
        "tenant_slug": slug_t,
        "column": column or None,
        "stage": stage or None,
        "tag": tag.strip() or None,
        "label": label.strip() or None,
        "repo": repo.strip() or None,
        "reporter": reporter.strip() or None,
        "assignee": assignee.strip() or None,
        "participant": participant.strip() or None,
        "person": person.strip() or None,
        "search": search.strip() or None,
        "due_before": due_before.strip() or None,
        "due_after": due_after.strip() or None,
        "overdue": "true" if overdue else None,
        "closed": "true" if (closed or archived) else None,
    })
    if status >= 400:
        return f"Could not list cards: {payload.get('detail', status)}"
    cards = payload.get("cards", [])
    people = await _card_people(slug_t, cards)
    return _fmt_card_list(cards, people) + _fmt_warnings(payload)


@mcp.tool()
async def card_get(slug: str, ctx: Context = None) -> str:
    """
    Open / read / show me / pull up that card or ticket - fetch one Elefant
    board card by its slug (or any former slug - renames keep resolving).

    Includes the card's wiki thread: pages associated by convention (written
    under `<AI folder>/projects/<card-slug>/`) and any explicitly linked page,
    the same set the board UI shows. A linked page whose target no longer exists
    is flagged as missing.

    Also names the card's people — who reported it, who is doing it and who is
    on it — with the reference to pass back to `card_reporter`,
    `card_assignee` or `card_participant`.

    Carries the card's COMMENT THREAD, which is where the conversation about a
    card lives: what was decided, what is blocked, what somebody needs. Read it
    before working a card — the design note says what the work is, the thread
    says what has happened to it since.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_read"):
        return _permission_denied("board_read")
    slug_t = _tenant_slug(ctx)
    status, payload = await _board_call(
        "GET", f"/board/cards/{slug}", params={"tenant_slug": slug_t})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not fetch card: {payload.get('detail', status)}"
    card = payload.get("card", {})
    card["wiki_pages"] = await _card_wiki_thread(slug_t, card)
    card["comments"] = payload.get("comments") or []
    # Comment authors are resolved alongside the card's own people, in the same
    # round trip — a thread of raw `user:9f2c…` would be the one place an agent
    # still saw references instead of names.
    card["people"] = await _card_people(
        slug_t, [card, {"participants": [c.get("author") or "" for c in card["comments"]]}])
    card["relationships"] = payload.get("relationships") or {}
    return _fmt_card(card)


@mcp.tool()
async def card_move(
    slug: str,
    column: str,
    position: float | None = None,
    ctx: Context = None,
) -> str:
    """
    Move / drag / shift / put a card in another column, or reorder it - on
    the Elefant board, using the user's own column vocabulary (see
    board_columns). Records the move as an event.

    Args:
        slug:     The card to move.
        column:   Target column name or id.
        position: Optional explicit ordering value; omit to append to the
                  column's end.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    status, payload = await _board_call("PATCH", f"/board/cards/{slug}/move", json_body={
        "tenant_slug": _tenant_slug(ctx),
        "column": column,
        "position": position,
        "source": token_info.get("source") or "unknown",
    })
    if status == 404:
        return f"Could not move card: {payload.get('detail', 'card or column not found')}."
    if status >= 400:
        return f"Could not move card: {payload.get('detail', status)}"
    return "Card moved.\n\n" + _fmt_card(payload.get("card", {}))


@mcp.tool()
async def card_update(
    slug: str,
    title: str = "",
    body: str = "",
    aliases: list[str] | None = None,
    new_slug: str = "",
    labels: list[str] | None = None,
    repo: str = "",
    wiki_links: list[str] | None = None,
    reporter: str = "",
    assignee: str = "",
    participants: list[str] | None = None,
    due: str = "",
    ctx: Context = None,
) -> str:
    """
    Edit / change / rename / reword / rewrite a card or ticket - update an
    Elefant board card. Renaming (new_slug) automatically pushes the old
    slug onto the card's aliases so past references keep resolving. Only the
    fields you pass are changed.

    Args:
        slug:     The card to edit (current slug or an alias).
        title:    New title (empty = leave unchanged).
        body:     New DESCRIPTION (empty = leave unchanged). Markdown, fenced
                  code blocks included, renders in the board UI.
        aliases:  Replace the alias list (e.g. to record that "Tarsier" is this
                  card's old name). Omit to leave unchanged.
        new_slug: Rename the card's identity; the old slug is retained as an alias.
        labels:   REPLACE the card's labels with this exact list ([] clears them).
                  Omit to leave unchanged. To add or remove ONE label without
                  disturbing the others (including labels a human added), use
                  `card_label` instead — it edits the list in place.
        repo:     Set the card's GitHub repo (`owner/name`, or a github.com URL
                  that normalizes to one). Empty leaves it unchanged — to take
                  a repo OFF a card, use `card_repo(slug, clear=True)`, which
                  can say the difference.
        wiki_links: REPLACE the card's linked wiki pages with this exact list.
                  Omit to leave unchanged. To attach or detach one page without
                  disturbing the others (including links a human added), use
                  `card_link_wiki` instead — it edits the list in place.
        reporter: Set who raised the card. Empty leaves it unchanged; use
                  `card_reporter` for the same thing on its own.
        assignee: Set who is doing the card. Empty leaves it unchanged — to
                  hand a card back use `card_assignee(slug, clear=True)`, so
                  that unassigning is always asked for by name.
        participants: REPLACE the card's participants with this exact list
                  ([] clears them). Omit to leave unchanged. To add or remove
                  ONE without disturbing the others, use `card_participant`.
        due:      Set the due date, `YYYY-MM-DD`. Empty leaves it unchanged —
                  to take a due date off use `card_due(slug, clear=True)`, so
                  that clearing one is always asked for by name.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    slug_t = _tenant_slug(ctx)
    body_req: dict = {"tenant_slug": slug_t, "nudge_chars": await _board_nudge_chars(slug_t)}
    if title:
        body_req["title"] = title
    if body:
        body_req["body"] = body
    if aliases is not None:
        body_req["aliases"] = aliases
    if new_slug:
        body_req["new_slug"] = new_slug
    if labels is not None:
        body_req["labels"] = labels
    if repo.strip():
        body_req["repo"] = repo.strip()
    if wiki_links is not None:
        body_req["wiki_links"] = wiki_links
    if reporter.strip():
        body_req["reporter"] = reporter.strip()
    # Empty leaves it alone here, matching every other field on this tool —
    # `card_assignee(clear=True)` is how a card is handed back, so that an
    # unassign is always something the caller asked for by name.
    if assignee.strip():
        body_req["assignee"] = assignee.strip()
    if participants is not None:
        body_req["participants"] = participants
    if due.strip():
        body_req["due"] = due.strip()
    status, payload = await _board_call("PATCH", f"/board/cards/{slug}", json_body=body_req)
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not update card: {payload.get('detail', status)}"
    card = payload.get("card", {})
    card["people"] = await _card_people(slug_t, [card])
    return "Card updated.\n\n" + _fmt_card(card) + _fmt_warnings(payload)


@mcp.tool()
async def card_link_wiki(
    slug: str,
    path: str,
    unlink: bool = False,
    ctx: Context = None,
) -> str:
    """
    Link / attach a wiki page, note or write-up to an Elefant board card (or
    detach one) - the same "Link" action the board UI offers on a card.

    This is what to call after writing up a card's idea as a wiki page: link the
    page to the card instead of pasting its URL into the card body. The link is
    stored on the card and rendered as a clickable page chip in the board UI.

    Adds to (or removes from) the card's existing links — it never replaces the
    whole list, so links added by a human or an earlier session survive. Linking
    a page that's already linked is a no-op.

    Note: a page written under `<AI folder>/projects/<card-slug>/…` is already
    associated with the card automatically (derived at read time, survives
    renames) — you don't need to link those explicitly. Use this tool for pages
    that live anywhere else.

    Args:
        slug:   The card (current slug or an alias).
        path:   Wiki page path, e.g. "AI/projects/netdisco/design.md".
        unlink: Set true to remove the link instead of adding it.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return "Provide a wiki page path to link."
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "GET", f"/board/cards/{slug}", params={"tenant_slug": slug_t})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not read card: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    current = list(card.get("wiki_links") or [])

    if unlink:
        updated = [p for p in current if p.lower() != rel.lower()]
        if len(updated) == len(current):
            return f"`{rel}` is not linked to card `{slug}` — nothing to unlink."
        verb = "Unlinked"
    else:
        if any(p.lower() == rel.lower() for p in current):
            return f"`{rel}` is already linked to card `{slug}`."
        updated = current + [rel]
        verb = "Linked"

    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "wiki_links": updated})
    if status >= 400:
        return f"Could not update card links: {payload.get('detail', status)}"
    return f"{verb} `{rel}`.\n\n" + _fmt_card(payload.get("card", {})) + _fmt_warnings(payload)


@mcp.tool()
async def card_link(
    slug: str,
    other: str,
    kind: str = "related",
    remove: bool = False,
    ctx: Context = None,
) -> str:
    """
    Link / relate / connect two cards, or say one blocks / depends on / has
    to be done before the other - on the Elefant board.

    `kind` is read from `slug`'s point of view:
      related     a loose link, no ordering implied
      blocks      THIS card has to be finished before `other` can start
      blocked-by  the other way round: `other` has to be finished first

    Blocks and blocked-by are one relationship seen from two sides, so setting
    either shows on both cards, and removing it from either end removes it.

    Why it matters beyond documentation: `card_next` reads these. An unattended
    routine pulling from a column will skip a blocked card and work its blocker
    instead, even when the blocker sits further down the board — which is the
    ordering a human does in their head and an agent otherwise cannot.

    A card cannot link to itself. A longer cycle (A blocks B, B blocks A) is
    allowed and REPORTED rather than refused: it is a real disagreement about
    the work, and the person who can settle it needs to see it stated.

    Args:
        slug:   The card (current slug or an alias).
        other:  The card to link it to (slug, alias, or number-free slug).
        kind:   related | blocks | blocked-by.
        remove: Set true to take the link off instead of putting it on.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    target = (other or "").strip()
    if not target:
        return "Provide the other card to link to."
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "POST", f"/board/cards/{slug}/link",
        json_body={"tenant_slug": slug_t, "other": target,
                   "kind": kind, "remove": bool(remove),
                   "source": token_info.get("source") or "unknown"})
    if status == 404:
        return f"Could not link: {payload.get('detail', 'no such card')}"
    if status >= 400:
        return f"Could not link cards: {payload.get('detail', status)}"

    stored = payload.get("kind", kind)
    if remove:
        if not payload.get("removed"):
            return (f"No `{stored}` link between `{slug}` and `{target}` — "
                    f"nothing to remove.")
        return f"Unlinked `{slug}` and `{target}` (`{stored}`)." + _fmt_warnings(payload)

    rel = payload.get("relationships") or {}
    return (f"Linked `{slug}` → `{target}` (`{stored}`)."
            + (_fmt_relationships(rel, None) or "")
            + _fmt_warnings(payload))


@mcp.tool()
async def card_next(
    column: str = "",
    stage: str = "",
    repo: str = "",
    label: str = "",
    ctx: Context = None,
) -> str:
    """
    What should I work on next / what can I start / what's unblocked - the
    next Elefant board card that can actually be STARTED, blockers already
    resolved.

    Reach for this instead of taking the top of `card_list` whenever you are
    picking up work unattended. Reading a column top to bottom will hand you a
    card whose blocker is three rows further down, and nothing about that card
    says so until you have already started it.

    This walks the column in the board owner's own order and, for any card with
    an unresolved blocker, descends into that blocker instead — transitively,
    and regardless of where the blocker sits on the board. What comes back is
    the card to work now, plus the chain that led to it, so you can say why.

    A blocker counts as resolved once it reaches a terminal stage (`shipped` or
    `dropped`) or is closed. A blocking cycle is reported and stepped over
    rather than followed, since nothing in such a chain can start until somebody
    removes one of the links.

    Args:
        column: Which column to pull from, e.g. "Up Next" (name or id).
        stage:  Or pull by stage, e.g. "designed".
        repo:   Restrict to one project's cards, e.g. "owner/name".
        label:  Restrict to one kind of work, e.g. "bug".
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_read"):
        return _permission_denied("board_read")
    slug_t = _tenant_slug(ctx)
    status, payload = await _board_call("GET", "/board/next-card", params={
        "tenant_slug": slug_t,
        "column": column.strip() or None,
        "stage": stage.strip() or None,
        "repo": repo.strip() or None,
        "label": label.strip() or None,
    })
    if status >= 400:
        return f"Could not pick a card: {payload.get('detail', status)}"

    card = payload.get("card")
    if not card:
        return "No startable card." + (_fmt_warnings(payload) or "")

    card["wiki_pages"] = await _card_wiki_thread(slug_t, card)
    card["people"] = await _card_people(slug_t, [card])
    card["relationships"] = payload.get("relationships") or {}

    chain = payload.get("chain") or []
    lead = ""
    # Only worth saying when the answer is NOT the card the column pointed at.
    # Otherwise it is noise on the ordinary case.
    if len(chain) > 1:
        lead = ("Working a blocker rather than the top of the column: "
                + " → ".join(f"`{s}`" for s in chain) + "\n\n")
    return lead + _fmt_card(card) + _fmt_warnings(payload)


@mcp.tool()
async def card_label(
    slug: str,
    label: str,
    remove: bool = False,
    ctx: Context = None,
) -> str:
    """
    Label / tag / categorise / mark a card as - add ONE label to an Elefant
    board card, or remove one; the same chips the board UI shows on the
    card.

    Labels are free-form classification: `bug`, `enhancement`, a repo name, a
    sprint, whatever vocabulary the team already uses. There is no fixed list
    and no need to create a label before using it. `card_list(label=...)`
    filters on them, and `card_get` reads them back.

    This edits the card's label list IN PLACE — it never replaces the whole
    set, so labels added by a human or an earlier session survive. Adding a
    label the card already has is a no-op, and removing one it doesn't have
    says so rather than pretending. Matching is case-insensitive, so `Bug`
    removes `bug`; the first spelling used is the one the card keeps.

    This is also the ONLY way to take off a label the board reserves
    (`note-to-self`). A whole-list replace through `card_update` re-attaches
    those, so a marker cannot be lost by an agent rewriting labels for some
    other reason; removing one here still works, because naming the single
    label you want gone is a deliberate act rather than a side effect.

    To set several at once, pass `labels` to `card_add` (at capture time) or
    `card_update` (a deliberate full replace).

    Args:
        slug:   The card (current slug or an alias).
        label:  The label to add or remove, e.g. "bug".
        remove: Set true to take the label off instead of putting it on.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    name = (label or "").strip()
    if not name:
        return "Provide a label to add or remove."
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "GET", f"/board/cards/{slug}", params={"tenant_slug": slug_t})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not read card: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    current = list(card.get("labels") or [])

    if remove:
        updated = [lb for lb in current if lb.lower() != name.lower()]
        if len(updated) == len(current):
            return f"`{name}` is not a label on card `{slug}` — nothing to remove."
        verb = "Removed label"
    else:
        if any(lb.lower() == name.lower() for lb in current):
            return f"`{name}` is already a label on card `{slug}`."
        updated = current + [name]
        verb = "Added label"

    # `allow_reserved` on a REMOVE is what distinguishes this call from the
    # routine label rewrite the board protects markers against: the caller
    # named one label and asked for it to go.
    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "labels": updated,
                   "allow_reserved": bool(remove)})
    if status >= 400:
        return f"Could not update card labels: {payload.get('detail', status)}"
    return f"{verb} `{name}`.\n\n" + _fmt_card(payload.get("card", {})) + _fmt_warnings(payload)


@mcp.tool()
async def card_repo(
    slug: str,
    repo: str = "",
    clear: bool = False,
    ctx: Context = None,
) -> str:
    """
    Which repo / what codebase / set the repository - set or clear the
    GitHub repository an Elefant board card belongs to.

    The repo is a STRUCTURED field on the card, not a line of body prose. That
    is the whole point of it: several projects share one board, so the repo is
    the dimension that answers "show me only this project's cards", via
    `card_list(repo=...)` or the board UI's repo filter. Reading a card's repo
    means reading this field — never grep the body for a `repo:` line, which is
    the old convention this replaces.

    One repo per card. Give it as `owner/name`, or paste any github.com form
    (https://github.com/owner/name, github.com/owner/name.git,
    git@github.com:owner/name.git) and it is normalized to `owner/name` so that
    one repository is one value the filter can match. Anything that is not a
    repo reference is refused rather than stored.

    Clearing is deliberate and separate: pass `clear=True`. Calling this with
    no repo and no `clear` does nothing and says so, because silently emptying
    a field the caller meant to set would be the worse failure.

    Args:
        slug:  The card (current slug or an alias).
        repo:  The repository to set, e.g. "zennthic/elefant".
        clear: Set true to take the repo off the card instead of setting one.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    name = (repo or "").strip()
    if clear and name:
        return ("Pass a repo to set it, or clear=true to remove it — not both. "
                f"Nothing changed on card `{slug}`.")
    if not clear and not name:
        return ("Provide a repo to set (e.g. `owner/name`), or pass clear=true "
                f"to remove the one card `{slug}` has.")
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "GET", f"/board/cards/{slug}", params={"tenant_slug": slug_t})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not read card: {payload.get('detail', status)}"
    current = (payload.get("card", {}) or {}).get("repo") or ""

    if clear:
        if not current:
            return f"Card `{slug}` has no repo — nothing to clear."
        verb, target = f"Cleared the repo (was `{current}`)", ""
    else:
        verb, target = f"Set repo to `{name}`", name

    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "repo": target})
    if status >= 400:
        return f"Could not update card repo: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    if not clear and card.get("repo") and card["repo"] != name:
        # Say so when the stored value is not the string that was passed, so a
        # caller comparing what it sent against what it reads back is not
        # surprised by the canonical form.
        verb = f"Set repo to `{card['repo']}` (normalized from `{name}`)"
    return f"{verb} on card `{slug}`.\n\n" + _fmt_card(card) + _fmt_warnings(payload)


@mcp.tool()
async def card_reporter(
    slug: str,
    person: str,
    ctx: Context = None,
) -> str:
    """
    Who raised it / who asked for this / on whose behalf - set or clear the
    reporter of an Elefant board card, the person the card is on behalf of.

    The reporter is a resolved account, not a name in prose. Give a username,
    an email, a display name, or a `user:`/`agent:` reference read off another
    card; anything that matches nobody is refused rather than stored, and the
    error names the accounts that exist. One reporter per card: provenance
    that could name two people is not provenance.

    Reach for this when you learn who a card was really for — a card you filed
    on your own that turns out to be somebody's ask, or one attributed to the
    wrong person. There is no "clear": a card that has recorded who raised it
    should not be able to forget, and correcting it means naming somebody
    else. To say you are involved without claiming you raised it, use
    `card_participant` instead.

    Args:
        slug:   The card (current slug or an alias).
        person: Who raised it.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    who = (person or "").strip()
    if not who:
        return ("Name who raised this card (a username, a display name, or a "
                f"`user:`/`agent:` reference). Nothing changed on card `{slug}`.")
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "reporter": who})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not set the reporter: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    card["people"] = await _card_people(slug_t, [card])
    label = _person_label(card.get("reporter") or "", card["people"])
    return (f"Reporter of `{slug}` is now {label}.\n\n"
            + _fmt_card(card) + _fmt_warnings(payload))


@mcp.tool()
async def card_assignee(
    slug: str,
    person: str = "",
    clear: bool = False,
    ctx: Context = None,
) -> str:
    """
    Assign / give it to / put me on it / who is doing it / hand it back -
    set or clear who is DOING an Elefant board card.

    Reporter says who raised a card and participants say who is involved;
    neither answers the question a board is mostly asked. A participant list of
    three people does not say which one has it.

    One assignee per card, because two assignees is no assignee. Like every
    other people field it is a resolved account rather than a name in prose:
    give a username, an email, a display name, or a `user:`/`agent:` reference
    read off another card, and anything matching nobody is refused rather than
    stored. Agents are assignable — most of this board is agent-worked, and a
    field that could only name humans could not say who is doing most of it.

    Unlike `card_reporter` this one CAN be cleared, with `clear=True`. Who
    raised a card is history and does not stop being true; who is doing it is a
    present fact, and work gets handed back. An unassigned card says nobody has
    it, which is different from nobody knowing.

    Assigning somebody does not also add them to the participants — one told
    fact writes one fact. Use `card_participant` if you mean both.

    Args:
        slug:   The card (current slug or an alias).
        person: Who is doing it. Ignored when `clear` is set.
        clear:  Take the assignee off, leaving the card unassigned.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    who = "" if clear else (person or "").strip()
    if not who and not clear:
        return ("Name who is doing this card (a username, a display name, or a "
                "`user:`/`agent:` reference), or pass `clear=True` to leave it "
                f"unassigned. Nothing changed on card `{slug}`.")
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "assignee": who})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not set the assignee: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    card["people"] = await _card_people(slug_t, [card])
    if not card.get("assignee"):
        return (f"`{slug}` is now unassigned.\n\n"
                + _fmt_card(card) + _fmt_warnings(payload))
    label = _person_label(card.get("assignee") or "", card["people"])
    return (f"`{slug}` is now assigned to {label}.\n\n"
            + _fmt_card(card) + _fmt_warnings(payload))


@mcp.tool()
async def card_due(
    slug: str,
    due: str = "",
    clear: bool = False,
    ctx: Context = None,
) -> str:
    """
    Set / change / clear a due date or deadline / when is it due by - set
    or clear when an Elefant board card is DUE.

    A calendar date, `YYYY-MM-DD`, with no time of day. It shows on the card
    in the board UI as a chip that turns amber within a week and red once it
    has passed, and `card_list(overdue=True)` finds every late card. Set it
    instead of writing the date into the title or a `due-…` label, which
    nothing can sort or warn on.

    A card waiting on a date usually belongs in the board's Scheduled column
    too; moving it there is `card_move`.

    Clearing is its own named action, `clear=True`, like `card_assignee`: an
    empty `due` here is refused rather than read as "clear", so a due date is
    never lost to a blank argument.

    Args:
        slug:  The card (current slug or an alias).
        due:   The due date, e.g. "2026-11-27". Ignored when `clear` is set.
        clear: Take the due date off.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    when = "" if clear else (due or "").strip()
    if not when and not clear:
        return ("Give the due date as YYYY-MM-DD, or pass `clear=True` to take it "
                f"off. Nothing changed on card `{slug}`.")
    slug_t = _tenant_slug(ctx)
    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "due": when})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not set the due date: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    card["people"] = await _card_people(slug_t, [card])
    if not card.get("due"):
        return (f"`{slug}` has no due date now.\n\n"
                + _fmt_card(card) + _fmt_warnings(payload))
    return (f"`{slug}` is now due {card['due']}.\n\n"
            + _fmt_card(card) + _fmt_warnings(payload))


@mcp.tool()
async def card_comment(
    slug: str,
    body: str,
    ctx: Context = None,
) -> str:
    """
    Comment / say / add a note / reply / leave a message on an Elefant board
    card - what you found, decided, or need.

    A card records what the work IS. The thread is where the conversation about
    it lives, and without one that conversation happens in a chat window and is
    gone by the next session.

    REACH FOR THIS INSTEAD OF the two things agents do otherwise: writing a
    finding into a wiki page nobody asked for, or editing the card's
    description to say what has happened since. The description is what the
    work IS; what happened to it is a comment, and rewriting the first to
    record the second loses both. "Here is what I found, here is what I need"
    is a comment. So is "blocked on a judgement call", "this turned
    out to be the same bug as #57", and "done, but the fix needs a second pair
    of eyes".

    You post as yourself. A comment is attributed to the account behind your
    token, so a thread says who said what without anybody having to sign their
    messages.

    Read the thread with `card_get`, which carries it.

    Args:
        slug: The card (current slug or an alias).
        body: What you want to say. Markdown renders in the board UI.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    said = (body or "").strip()
    if not said:
        return f"A comment needs something in it. Nothing was posted on `{slug}`."
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "POST", f"/board/cards/{slug}/comments",
        json_body={"tenant_slug": slug_t, "body": said,
                   "author": _actor_ref(token_info),
                   "source": token_info.get("source") or "unknown"})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not post the comment: {payload.get('detail', status)}"
    return f"Commented on `{slug}`."


@mcp.tool()
async def card_participant(
    slug: str,
    person: str,
    remove: bool = False,
    ctx: Context = None,
) -> str:
    """
    Add / involve / loop in / cc / take off - put someone on an Elefant
    board card, or remove them.

    Participants are the people involved in a card, as resolved accounts
    rather than names in prose — so "show me everything Jon is on"
    (`card_list(person=...)`) is a real query rather than a text search.

    This edits the list IN PLACE, so a participant added by a human or an
    earlier session survives; adding somebody already on the card is a no-op,
    and removing somebody who is not says so rather than pretending. To set
    several at once, pass `participants` to `card_add` or `card_update` (a
    deliberate full replace).

    Note that you are added automatically when you file a card reported by
    somebody else, so the common case needs no call at all. This is for the
    others: a colleague who joins the work, or somebody who has left it.

    Args:
        slug:   The card (current slug or an alias).
        person: Who to add or remove — a username, a display name, or a
                `user:`/`agent:` reference.
        remove: Set true to take them off instead of putting them on.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    who = (person or "").strip()
    if not who:
        return "Name the person to add or remove."
    slug_t = _tenant_slug(ctx)

    status, payload = await _board_call(
        "GET", f"/board/cards/{slug}", params={"tenant_slug": slug_t})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not read card: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    current = list(card.get("participants") or [])

    # Resolve first, then compare against what the card holds. Comparing the
    # typed name against stored references would never match, so "remove jon"
    # from a card holding `user:9f2c…` would report nothing to remove while
    # Jon sat there in plain sight.
    resolved, why = await _resolve_person(slug_t, who)
    if not resolved:
        return why

    if remove:
        updated = [p for p in current if p.lower() != resolved.lower()]
        if len(updated) == len(current):
            return f"`{who}` is not a participant on card `{slug}` — nothing to remove."
        verb = "Removed"
    else:
        if any(p.lower() == resolved.lower() for p in current):
            return f"`{who}` is already a participant on card `{slug}`."
        updated = current + [resolved]
        verb = "Added"

    status, payload = await _board_call(
        "PATCH", f"/board/cards/{slug}",
        json_body={"tenant_slug": slug_t, "participants": updated})
    if status >= 400:
        return f"Could not update participants: {payload.get('detail', status)}"
    card = payload.get("card", {}) or {}
    card["people"] = await _card_people(slug_t, [card])
    label = _person_label(resolved, card["people"])
    return (f"{verb} {label} {'from' if remove else 'to'} card `{slug}`.\n\n"
            + _fmt_card(card) + _fmt_warnings(payload))


async def _card_close(slug: str, ctx: Context) -> str:
    """Shared body for card_close and its old name, card_archive."""
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    status, payload = await _board_call(
        "POST", f"/board/cards/{slug}/close",
        json_body={"tenant_slug": _tenant_slug(ctx)})
    if status == 404:
        return f"No card matches `{slug}`."
    if status >= 400:
        return f"Could not close card: {payload.get('detail', status)}"
    return f"Card `{slug}` closed (off the board, not deleted)."


@mcp.tool()
async def card_close(slug: str, ctx: Context = None) -> str:
    """
    Close / finish / mark done / take it off the board - close an Elefant
    board card without deleting it (there is no card deletion - closing is
    the terminal state). The card stays resolvable by slug, and a human can
    reopen it from the board's Closed column.
    """
    return await _card_close(slug, ctx)


@mcp.tool()
async def card_archive(slug: str, ctx: Context = None) -> str:
    """
    Archive / close / take it off the board - an Elefant board card.
    DEPRECATED - this is `card_close` under its old name, and does exactly
    the same thing. Prefer
    `card_close`; this stays so a session or routine written against the old
    name keeps working.
    """
    return await _card_close(slug, ctx)


@mcp.tool()
async def card_eval_lock(
    slug: str,
    run_id: str,
    ctx: Context = None,
) -> str:
    """
    Lock / claim / take / hold a card while something evaluates it - take
    the evaluation lock on an Elefant board card, so nothing else evaluates
    it at once.

    This is for an automated reviewer working a column of cards: take the lock,
    run the checks, release it. While you hold it the board shows the card as
    under evaluation, so a human scanning it does not start working a card you
    already have in hand, and a second run of your own is refused rather than
    duplicating your work.

    Acquire is atomic. If two runs ask at the same moment exactly one gets it
    and the other is refused, naming the run that beat it — so a refusal is a
    real answer ("somebody else has this one, move on"), not something to retry
    around. Re-asking for a lock you already hold succeeds and changes nothing,
    so a retried step is safe.

    You do NOT need this to read a card, comment on one, or move one. Reading is
    free: every card already carries its lock state, so `card_get` and
    `card_list` tell you whether a card is in somebody's hands without a call.
    Reach for this only when you are about to EVALUATE a card and want the rest
    of the world to leave it alone while you do.

    Release with `card_eval_unlock` when you are done, including when the
    evaluation failed. A lock nobody releases is broken automatically once it is
    old enough, on the assumption that the run holding it died, but that takes
    half an hour during which nothing else can touch the card.

    Args:
        slug:   The card (current slug or an alias).
        run_id: Your own handle for this evaluation run. Opaque to Elefant, which
                only cares whether two acquires name the same one — so it has to
                be unique per run, not per reviewer, or two of your runs would
                each think they hold every card.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    run = (run_id or "").strip()
    if not run:
        return ("Name the run taking the lock (`run_id`) — it is what tells two "
                f"concurrent evaluations apart. Nothing changed on card `{slug}`.")
    status, payload = await _board_call(
        "POST", f"/board/cards/{slug}/eval-lock",
        json_body={"tenant_slug": _tenant_slug(ctx), "run_id": run})
    if status == 404:
        return f"No active card matches `{slug}`."
    if status == 409:
        # The holder is named in memory-api's detail. Surfaced verbatim, and
        # said as a decision rather than an error: the useful next move is to
        # skip this card, not to retry.
        return (f"`{slug}` is already under evaluation, so `{run}` did not get it: "
                f"{payload.get('detail', status)}.\n\nSkip it and come back on the "
                f"next pass — do not wait on the lock.")
    if status >= 400:
        return f"Could not take the evaluation lock: {payload.get('detail', status)}"
    held = payload.get("eval_lock") or {}
    if payload.get("already_held"):
        return (f"`{run}` already held the evaluation lock on `{slug}` (since "
                f"{(held.get('since') or '?')[:16].replace('T', ' ')}). Nothing changed.")
    out = f"`{run}` now holds the evaluation lock on `{slug}`."
    broke = payload.get("broke_stale_lock_of") or ""
    if broke:
        out += (f"\n\n⚠️ It took the lock from run `{broke}`, whose lock had gone "
                f"stale — that run almost certainly died mid-evaluation without "
                f"releasing. Recorded on the board stream. Worth a look if it is "
                f"not a run you know about.")
    return out + "\n\nRelease it with `card_eval_unlock` when you are done, pass or fail."


@mcp.tool()
async def card_eval_unlock(
    slug: str,
    run_id: str,
    ctx: Context = None,
) -> str:
    """
    Unlock / release / let go of / hand back a card after evaluating it -
    release the evaluation lock on an Elefant board card, clearing the
    board's indicator.

    Call this when your evaluation finishes, whichever way it went. Releasing a
    card you never locked succeeds and changes nothing, so a cleanup path is
    safe to run twice and safe to run after your lock was broken for being
    stale — you do not have to check first.

    You cannot release somebody else's lock. If another run holds the card this
    is refused, naming the holder, because a release any run could call on any
    card would not be a lock at all.

    Args:
        slug:   The card (current slug or an alias).
        run_id: The run that holds the lock. The same one you passed to
                `card_eval_lock`.
    """
    token_info = _token(ctx)
    if not _check_permission(token_info, "board_write"):
        return _permission_denied("board_write")
    run = (run_id or "").strip()
    if not run:
        return ("Name the run releasing the lock (`run_id`) — only its holder can "
                f"release it. Nothing changed on card `{slug}`.")
    status, payload = await _board_call(
        "POST", f"/board/cards/{slug}/eval-unlock",
        json_body={"tenant_slug": _tenant_slug(ctx), "run_id": run})
    if status == 404:
        return f"No card matches `{slug}`."
    if status == 409:
        return (f"`{run}` cannot release `{slug}`: {payload.get('detail', status)}. "
                f"Only the run holding a lock can release it.")
    if status >= 400:
        return f"Could not release the evaluation lock: {payload.get('detail', status)}"
    if not payload.get("was_locked"):
        return (f"`{slug}` was not locked by `{run}` — nothing to release, and "
                f"nothing changed. If you expected to hold it, your lock was "
                f"probably broken for going stale.")
    return f"`{run}` released the evaluation lock on `{slug}`. The board no longer shows it under evaluation."


# ── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build the Starlette app first so we can attach middleware before serving.
    # mcp.app does not exist in FastMCP 1.6+; call streamable_http_app() directly.
    import anyio
    import uvicorn

    async def _serve():
        starlette_app = mcp.streamable_http_app()
        # Layer order (outermost → innermost):
        #   _OAuthDiscoveryRouter  — serves /.well-known/* and /health without auth
        #   BearerAuthMiddleware   — validates Bearer token, attaches to scope
        #   starlette_app          — FastMCP tool/resource/prompt handlers
        authed_app   = BearerAuthMiddleware(starlette_app)
        routed_app   = _OAuthDiscoveryRouter(authed_app)
        config = uvicorn.Config(routed_app, host="0.0.0.0", port=8003, log_level="info")
        await uvicorn.Server(config).serve()

    anyio.run(_serve)
