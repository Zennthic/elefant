"""
The board's web API: session-authed pass-throughs to memory-api for columns,
cards, labels, links and comments, the live event stream, the people
directory, a card's wiki thread and a page's back-links, plus the
peer-service board endpoints the MCP server calls.

Moved out of main.py by CQ-04 phase 3; the paths are unchanged.
"""
import json as _json
import logging
from typing import Optional
from urllib.parse import quote as _urlquote

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import people as board_people
from auth import SESSION_COOKIE, decode_session_token, require_authenticated
from auth_helpers import _board_stream_token, _effective_tenant_slug
from config_db import get_db_session, Tenant, TenantSettings, DEFAULT_TENANT_SLUG
from internal_service import (
    MEMORY_API_URL, _internal_service_headers, _require_internal_service,
)
from wiki_pages import _parse_frontmatter_meta, _record_wiki_revision, _share_page_title
from wiki_storage import (
    _is_within_ai_folder, _normalize_path, _resolve_ai_wiki_folder,
    _s3, _s3_exists, _s3_read, _s3_write, _trigger_embedder_reindex,
)

log = logging.getLogger("editor")

router = APIRouter()


# ── Board: session-authed pass-throughs to memory-api ─────────────────────────
# The Board WebView (board.html, a member-facing daily tool) talks to one origin
# (the editor); the editor resolves the tenant from the session and forwards to
# memory-api with the internal-service header. The board is member-tier / wiki-tier
# capability, so these use require_authenticated (any logged-in user), NOT the
# org-admin gate the hot-tier memory pass-throughs in main.py use. UI-originated
# card writes are stamped source="ui" so the board can distinguish human- from
# agent-written cards.

def _board_nudge_for(slug: str, session: Session) -> int:
    """The tenant's post-it nudge threshold (default 280 when no override)."""
    row = session.query(TenantSettings).filter_by(tenant_slug=slug).first()
    if row is not None and row.board_nudge_chars is not None:
        return int(row.board_nudge_chars)
    return 280


def _board_upstream(method: str, path: str, slug: str, *,
                    params: Optional[dict] = None, json_body: Optional[dict] = None,
                    timeout: int = 15):
    """Proxy a board call to memory-api, injecting the tenant_slug and
    propagating memory-api's status + detail (e.g. the slug-collision 409)."""
    # Drop None-valued query params. httpx serialises None as an EMPTY query
    # value (e.g. `archived=`), and memory-api's typed params then 422 on it
    # (bool("") is unparseable). Omitting them lets the endpoint defaults apply.
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if json_body is not None:
        json_body = dict(json_body)
        json_body["tenant_slug"] = slug
    else:
        params["tenant_slug"] = slug
    try:
        resp = httpx.request(method, f"{MEMORY_API_URL}{path}",
                             params=params, json=json_body,
                             headers=_internal_service_headers(), timeout=timeout)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory-api unreachable: {exc!r}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json() if resp.content else {"ok": True}


# ── Board: the live stream ────────────────────────────────────────────────────
# Server-Sent Events, relayed from memory-api. The editor is where the browser's
# session cookie becomes a tenant, so the stream has to come through here rather
# than the board talking to memory-api directly — and the tenant is resolved from
# the session exactly as every other board call resolves it, so a stream can only
# ever carry the board the caller is already allowed to read.
#
# An API token can open it too (`Authorization: Bearer ...`), so a service such
# as an automated reviewer hears a card move as it happens instead of polling. The token needs
# board_read, and its tenant comes from the token alone: a `tenant_slug` naming
# any other tenant is refused rather than honoured. The events stay thin (the
# change and the card slug), so a token-holder re-reads the card through the
# ordinary tools and every permission check stays where it already is.
#
# `async def` on purpose. These connections are long-lived and mostly idle; a
# sync handler would park a threadpool worker per open board and a handful of
# tabs would starve the pool that every other editor endpoint shares.

BOARD_STREAM_IDLE_TIMEOUT = 900.0     # give up on a stream that has gone quiet


def _bearer_from_request(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header[7:].strip()


def _board_stream_caller(request: Request) -> dict:
    """Who is opening the stream: a browser session or an API token.

    A session cookie wins and takes exactly the path it always has. Without
    one, a bearer token is checked here, on a session closed before the stream
    opens, and the caller becomes `{"token_tenant_slug": ...}`. With neither,
    the session gate answers as it always did."""
    if (decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
            or not _bearer_from_request(request)):
        return require_authenticated(request)
    gen = get_db_session()
    session = next(gen)
    try:
        status, info = _board_stream_token(session, _bearer_from_request(request))
    finally:
        gen.close()
    if status == 401:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if status == 403:
        raise HTTPException(status_code=403, detail="Token lacks the board_read scope")
    return {"token_tenant_slug": info.get("tenant_slug") or DEFAULT_TENANT_SLUG}


def _board_stream_tenant(claims: dict, requested: Optional[str]) -> str:
    """Resolve the stream's tenant on a session that is closed before the
    stream opens. Holding the request-scoped session for the life of an SSE
    connection would pin a database connection per open board for hours.

    A token's tenant is the token's own. `tenant_slug` may repeat it but never
    widen it: naming another tenant is refused, not quietly ignored."""
    if "token_tenant_slug" in claims:
        slug = claims["token_tenant_slug"]
        if requested and requested != slug:
            raise HTTPException(status_code=403,
                                detail="A token can only stream its own tenant's board")
        return slug
    gen = get_db_session()
    session = next(gen)
    try:
        return _effective_tenant_slug(claims, requested, session)
    finally:
        gen.close()


@router.get("/api/board/stream")
async def board_stream(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(_board_stream_caller),
):
    """Relay memory-api's board event stream to the browser.

    A pure relay: this does not parse, re-frame or filter the events, it copies
    bytes. The events carry no board content — only the fact that something
    changed — so there is nothing here to leak and nothing to keep in step with
    the card shape.
    """
    slug = await run_in_threadpool(_board_stream_tenant, claims, tenant_slug)

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=10.0, read=BOARD_STREAM_IDLE_TIMEOUT,
                    write=10.0, pool=10.0)) as client:
                async with client.stream(
                    "GET", f"{MEMORY_API_URL}/board/stream",
                    params={"tenant_slug": slug},
                    headers={**_internal_service_headers(), "Accept": "text/event-stream"},
                ) as upstream:
                    if upstream.status_code >= 400:
                        # Report it ON the stream rather than as a status code:
                        # the response has already begun, and an EventSource
                        # that sees a clean close just reconnects in a loop with
                        # no idea why. A named error event gives the client
                        # something to act on — it falls back to polling.
                        yield (f"event: stream.error\ndata: "
                               f"{_json.dumps({'status': upstream.status_code})}\n\n")
                        return
                    async for chunk in upstream.aiter_raw():
                        yield chunk
        except Exception as exc:
            log.warning("board stream relay ended: %r", exc)
            yield f"event: stream.error\ndata: {_json.dumps({'detail': 'upstream ended'})}\n\n"

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/board/columns")
def board_columns_list(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", "/board/columns", slug)
    data["nudge_chars"] = _board_nudge_for(slug, session)  # for the sticky clip
    return data


@router.post("/api/board/columns", status_code=201)
def board_column_create(
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", "/board/columns", slug, json_body={
        "name": body.get("name"), "stage": body.get("stage"),
        "position": body.get("position"), "color": body.get("color"),
    })


@router.patch("/api/board/columns/{column_id}")
def board_column_update(
    column_id: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("PATCH", f"/board/columns/{column_id}", slug, json_body={
        "name": body.get("name"), "stage": body.get("stage"),
        "position": body.get("position"), "color": body.get("color"),
    })


@router.post("/api/board/columns/{column_id}/archive")
def board_column_archive(
    column_id: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", f"/board/columns/{column_id}/archive", slug, json_body={})


# ── Board people: the directory a card's reporter/participants resolve to ────
# A card names people by opaque reference; the NAME is derived here, at read
# time, and never stored on the card. So every board response that carries a
# reference is enriched with a `people` map before it reaches a browser or an
# agent — the same shape both, so the two surfaces can't drift.

def _card_people_refs(cards: list) -> list:
    """Every principal reference mentioned by these cards, de-duplicated."""
    refs, seen = [], set()
    for c in cards or []:
        for ref in [(c or {}).get("reporter") or ""] + list((c or {}).get("participants") or []):
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _enrich_people(data: dict, slug: str, session: Session) -> dict:
    """Attach `people` (ref → display record) to a board payload, in place.

    Best-effort by construction: a card must still render when the directory
    lookup fails, so a failure leaves the references exactly as they are and
    the UI falls back to showing the raw reference rather than nothing.
    """
    try:
        cards = data.get("cards") if isinstance(data, dict) else None
        if cards is None and isinstance(data, dict) and data.get("card"):
            cards = [data["card"]]
        refs = _card_people_refs(cards or [])
        # Comment authors are people too, and are rendered by name like every
        # other person on a card — a thread of raw `user:9f2c…` would be the
        # one place the board still showed references at somebody.
        seen = set(refs)
        for c in (data.get("comments") if isinstance(data, dict) else None) or []:
            ref = (c or {}).get("author") or ""
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
        if isinstance(data, dict) and isinstance(data.get("comment"), dict):
            ref = data["comment"].get("author") or ""
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
        data["people"] = board_people.describe(session, slug, refs) if refs else {}
    except Exception:
        log.warning("board: people enrichment failed for tenant %s", slug, exc_info=True)
        data["people"] = {}
    return data


@router.get("/api/board/people")
def board_people_directory(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """Who this board can attribute a card to — its members and its agent
    accounts. Powers the reporter/participant pickers, so what the picker
    offers is exactly what the store will accept."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    # `me` so the board can tell the caller's own comments from everybody
    # else's without a second round trip — and without the browser deciding
    # who it is, which is the server's answer to give.
    return {
        "people": board_people.directory(session, slug),
        "me": _comment_actor(claims),
    }


@router.post("/api/internal/board/people")
def internal_board_people(
    request: Request,
    body: dict = Body(...),
    session: Session = Depends(get_db_session),
):
    """Peer-service identity resolution for the board.

    Two jobs behind one call, because both are the same lookup from opposite
    ends: `resolve` turns what a caller typed into canonical references (the
    WRITE gate memory-api gates card writes on), and `describe` turns stored
    references back into display records (the READ enrichment the MCP renderer
    uses). `directory` asks for the whole list.

    Internal network only — it names accounts, so it gets the same peer-token
    gate as the rest of /api/internal.
    """
    _require_internal_service(request)
    slug = (body.get("tenant_slug") or DEFAULT_TENANT_SLUG).strip() or DEFAULT_TENANT_SLUG
    out: dict = {}
    if body.get("resolve"):
        out.update(board_people.resolve(session, slug, body.get("resolve")))
    if body.get("describe"):
        out["people"] = board_people.describe(session, slug, body.get("describe"))
    if body.get("directory"):
        out["directory"] = board_people.directory(session, slug)
    return out


@router.get("/api/board/labels")
def board_labels_list(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """The labels in use on this tenant's board, for the label field's
    autocomplete. Read-only and cheap; the field refetches it after a save so a
    label just coined is offered to the next card without a reload."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("GET", "/board/labels", slug)


@router.get("/api/board/cards")
def board_cards_list(
    column: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    reporter: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    participant: Optional[str] = Query(None),
    person: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    due_before: Optional[str] = Query(None),
    due_after: Optional[str] = Query(None),
    overdue: bool = Query(False),
    # `closed` is what the board sends now; `archived` is the same filter under
    # its old name, kept because an older memory-ui served from cache is still
    # sending it. Either one includes closed cards.
    closed: bool = Query(False),
    archived: bool = Query(False),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", "/board/cards", slug, params={
        "column": column, "stage": stage, "tag": tag, "label": label,
        "repo": repo, "reporter": reporter, "assignee": assignee,
        "participant": participant,
        "person": person, "search": search,
        "due_before": due_before, "due_after": due_after,
        "overdue": "true" if overdue else None,
        "closed": "true" if (closed or archived) else None,
    })
    return _enrich_people(data, slug, session)


@router.post("/api/board/cards", status_code=201)
def board_card_create(
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", "/board/cards", slug, json_body={
        "title": body.get("title"), "slug": body.get("slug"),
        "body": body.get("body", ""), "column": body.get("column"),
        "tags": body.get("tags") or [], "labels": body.get("labels") or [],
        "repo": body.get("repo") or "", "wiki_links": body.get("wiki_links") or [],
        "reporter": body.get("reporter") or "",
        "assignee": body.get("assignee") or "",
        "due": body.get("due") or "",
        "participants": body.get("participants") or [],
        # Who is making this call, as opposed to who the card is ABOUT. The
        # session's user, always — a card added in the UI is reported by the
        # person adding it unless they say otherwise, and if they do say
        # otherwise they are still on the card as a participant.
        "actor": f"user:{claims.get('uid')}" if claims.get("uid") else "",
        "source": "ui", "nudge_chars": _board_nudge_for(slug, session),
    })


def _card_page_title(tenant, key: str) -> str:
    """Best display title for a wiki page: frontmatter title, else the first
    '# ' heading, else the filename."""
    try:
        body = _s3_read(tenant, key).decode("utf-8", "replace")
        _tags, title = _parse_frontmatter_meta(body)
        if title:
            return title
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# "):
                return s[2:].strip()
    except Exception:
        pass
    return _share_page_title(key)


def _card_wiki_slugs(slug: str, aliases) -> list:
    """The slug + its aliases, cleaned to safe single segments (for path use)."""
    out, seen = [], set()
    for s in [slug] + list(aliases or []):
        s = (s or "").strip().lower()
        if s and s not in seen and "/" not in s and ".." not in s and "\x00" not in s:
            seen.add(s)
            out.append(s)
    return out


def _card_wiki_pages(session: Session, tenant, slug: str, aliases) -> list:
    """Wiki pages associated with a card, DERIVED at read time (never stored):
    the .md pages under <AI folder>/projects/<slug>/ (and any alias folder).
    Because it's derived live, renaming the card (aliases keep resolving) or the
    pages themselves never breaks the thread — self-healing by construction."""
    folder = _resolve_ai_wiki_folder(session, tenant.slug)
    out, seen = [], set()
    for s in _card_wiki_slugs(slug, aliases):
        base = f"{folder}/projects/{s}"
        try:
            keys = _s3().walk(tenant.slug, base)
        except Exception:
            keys = []
        for k in keys:
            if k.lower().endswith(".md") and k not in seen:
                seen.add(k)
                out.append({"path": k, "title": _card_page_title(tenant, k)})
    out.sort(key=lambda p: p["path"])
    return out


def _card_wiki_thread(session: Session, tenant_slug: str, card_slug: str,
                      aliases, wiki_links) -> list:
    """A card's full wiki thread, as the board UI shows it:

      - DERIVED pages under <AI folder>/projects/<card-slug>/ — associated by
        convention at read time, so renaming the card or a page never breaks
        the thread (kind="derived").
      - EXPLICIT wiki_links to a page anywhere else, flagged when the target no
        longer exists (kind="linked", missing=True).

    Shared by the session-authed board route and the peer-service endpoint below
    so the browser and an MCP agent see exactly the same thread.
    """
    tenant = session.query(Tenant).filter_by(slug=tenant_slug, is_active=True).first()
    if tenant is None:
        return []
    derived = _card_wiki_pages(session, tenant, card_slug, aliases)
    for p in derived:
        p["kind"] = "derived"
    seen = {p["path"] for p in derived}
    explicit = []
    for link in (wiki_links or []):
        rel = _normalize_path(link)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        try:
            exists = _s3_exists(tenant, rel)
        except Exception:
            exists = False
        explicit.append({
            "path": rel, "kind": "linked",
            "title": _card_page_title(tenant, rel) if exists else rel.rsplit("/", 1)[-1],
            "missing": (not exists),
        })
    return derived + explicit


@router.get("/api/board/cards/{card_slug}")
def board_card_get(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", f"/board/cards/{card_slug}", slug)
    # Enrich with the wiki thread (best-effort — never fail card_get on it).
    card = data.get("card") if isinstance(data, dict) else None
    if card and card.get("slug"):
        try:
            card["wiki_pages"] = _card_wiki_thread(
                session, slug, card["slug"], card.get("aliases"), card.get("wiki_links"))
        except Exception:
            card["wiki_pages"] = []
    return _enrich_people(data, slug, session)


# ── Card comments ─────────────────────────────────────────────────────────────
# The author is stamped from the SESSION, never taken from the body. Author-only
# editing is only worth anything if the author cannot be claimed by asking.

def _comment_actor(claims: dict) -> str:
    return f"user:{claims.get('uid')}" if claims.get("uid") else ""


@router.get("/api/board/cards/{card_slug}/comments")
def board_comments_list(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", f"/board/cards/{card_slug}/comments", slug)
    return _enrich_people(data, slug, session)


@router.post("/api/board/cards/{card_slug}/comments", status_code=201)
def board_comment_add(
    card_slug: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("POST", f"/board/cards/{card_slug}/comments", slug, json_body={
        "body": body.get("body") or "",
        "author": _comment_actor(claims),
        "source": "ui",
    })
    return _enrich_people(data, slug, session)


@router.patch("/api/board/cards/{card_slug}/comments/{comment_id}")
def board_comment_update(
    card_slug: str,
    comment_id: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream(
        "PATCH", f"/board/cards/{card_slug}/comments/{comment_id}", slug, json_body={
            "body": body.get("body") or "",
            "actor": _comment_actor(claims),
            "source": "ui",
        })
    return _enrich_people(data, slug, session)


@router.delete("/api/board/cards/{card_slug}/comments/{comment_id}")
def board_comment_delete(
    card_slug: str,
    comment_id: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream(
        "DELETE", f"/board/cards/{card_slug}/comments/{comment_id}", slug, json_body={
            "actor": _comment_actor(claims),
            "source": "ui",
        })


@router.post("/api/internal/board/wiki-pages")
def internal_board_wiki_pages(
    request: Request,
    body: dict = Body(...),
    session: Session = Depends(get_db_session),
):
    """Peer-service view of a card's wiki thread (MCP `card_get`).

    The MCP server reads cards straight from memory-api, which stores only the
    explicit `wiki_links` — it can't see the pages associated by convention,
    because resolving those needs the tenant's AI folder and S3. This exposes
    the same enrichment the browser gets, so an agent's view of a card matches
    the board UI's. Takes what the caller already holds (slug, aliases, links)
    rather than re-fetching the card.
    """
    _require_internal_service(request)
    tenant_slug = (body.get("tenant_slug") or "").strip()
    card_slug = (body.get("slug") or "").strip()
    if not tenant_slug or not card_slug:
        raise HTTPException(status_code=400, detail="tenant_slug and slug are required")
    try:
        pages = _card_wiki_thread(session, tenant_slug, card_slug,
                                  body.get("aliases"), body.get("wiki_links"))
    except Exception:
        log.exception("internal wiki-thread lookup failed (tenant=%s card=%s)",
                      tenant_slug, card_slug)
        pages = []
    return {"wiki_pages": pages}


@router.get("/api/board/wiki-pages")
def board_wiki_pages(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """All .md wiki page paths for the tenant — powers the card's
    'link an existing page' picker. Any page is linkable (not just the AI
    folder); the AI-folder restriction only governs writes."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    tenant = session.query(Tenant).filter_by(slug=slug, is_active=True).first()
    if tenant is None:
        return {"pages": []}
    try:
        keys = _s3().walk(tenant.slug, "")
    except Exception:
        keys = []
    return {"pages": sorted(k for k in keys if k.lower().endswith(".md"))}


@router.get("/api/board/cards/{card_slug}/status")
def board_card_status(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("GET", f"/board/cards/{card_slug}/status", slug)


def _cards_referencing_page(session: Session, slug: str, path: str) -> list:
    """Which board cards reference this wiki page — the reverse of
    `_card_wiki_thread`, so a page can show the card it belongs to.

    Covers BOTH ways a card and a page are associated, because a reader
    shouldn't have to know which one was used:

      - DERIVED: the page lives under <AI folder>/projects/<card-slug>/, so the
        folder name IS the card reference. Resolved through the card's aliases,
        which means a renamed card still owns its old folder.
      - EXPLICIT: the card's wiki_links names the page (memory-api does that
        lookup, since it holds the cards).

    Derived first — that's the card whose thread this page is part of — then any
    explicit referrers, de-duped by slug. Each entry has the same shape as
    /status, so the browser and an MCP agent read one kind of answer.

    Shared by the session-authed route and the peer-service endpoint below.
    """
    try:
        rel = _normalize_path((path or "").strip())
    except HTTPException:
        return []    # an unusable path just has no cards, it isn't an error

    out, seen = [], set()

    def _add(card):
        s = (card or {}).get("slug")
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(card)

    # Derived: <AI folder>/projects/<card-slug>/<anything>
    folder = _resolve_ai_wiki_folder(session, slug)
    prefix = f"{folder}/projects/"
    if rel.startswith(prefix):
        seg = rel[len(prefix):].split("/")[0]
        # The page has to be INSIDE the slug folder; one sitting directly in
        # projects/ names no card.
        if seg and rel.startswith(f"{prefix}{seg}/"):
            # The segment is arbitrary text off a wiki path, so quote it — an
            # unescaped '?' or '#' in a folder name would otherwise re-point the
            # upstream URL at a different endpoint.
            try:
                st = _board_upstream(
                    "GET", f"/board/cards/{_urlquote(seg, safe='')}/status", slug)
                if isinstance(st, dict) and st.get("slug"):
                    _add({**st, "via": "derived"})
            except HTTPException:
                pass    # no such card — the folder just isn't a card thread

    # Explicit: any card whose wiki_links names this page.
    try:
        data = _board_upstream("GET", "/board/wiki-page-cards", slug, params={"path": rel})
        for c in (data or {}).get("cards") or []:
            _add({**c, "via": "linked"})
    except HTTPException:
        pass            # never fail the page render on the reverse lookup

    return out


@router.get("/api/board/wiki-page-cards")
def board_wiki_page_cards(
    path: str = Query(...),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """The wiki page's back-link to its board card(s), for the browser."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return {"cards": _cards_referencing_page(session, slug, path)}


@router.post("/api/internal/board/page-cards")
def internal_board_page_cards(
    request: Request,
    body: dict = Body(...),
    session: Session = Depends(get_db_session),
):
    """Peer-service view of a page's referring cards (MCP `get_wiki_page`).

    The mirror of `/api/internal/board/wiki-pages`: that one tells an agent
    which pages belong to a card, this one tells it which cards point at a
    page. An agent reading a write-up should be able to see it's the write-up
    *for* something, the same way the wiki UI now shows a chip.
    """
    _require_internal_service(request)
    tenant_slug = (body.get("tenant_slug") or "").strip()
    path = (body.get("path") or "").strip()
    if not tenant_slug or not path:
        raise HTTPException(status_code=400, detail="tenant_slug and path are required")
    try:
        cards = _cards_referencing_page(session, tenant_slug, path)
    except Exception:
        log.exception("internal page-cards lookup failed (tenant=%s path=%s)",
                      tenant_slug, path)
        cards = []
    return {"cards": cards}


@router.patch("/api/board/cards/{card_slug}/move")
def board_card_move(
    card_slug: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("PATCH", f"/board/cards/{card_slug}/move", slug, json_body={
        "column": body.get("column"), "position": body.get("position"), "source": "ui",
    })


@router.patch("/api/board/cards/{card_slug}")
def board_card_update(
    card_slug: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("PATCH", f"/board/cards/{card_slug}", slug, json_body={
        "title": body.get("title"), "body": body.get("body"),
        "aliases": body.get("aliases"), "new_slug": body.get("new_slug"),
        "tags": body.get("tags"), "labels": body.get("labels"),
        # Coerced rather than forwarded raw: upstream types this as a plain
        # bool, so an absent key must arrive as False and not as null.
        "allow_reserved": bool(body.get("allow_reserved")),
        "repo": body.get("repo"), "wiki_links": body.get("wiki_links"),
        "reporter": body.get("reporter"), "participants": body.get("participants"),
        # None (key absent) leaves each alone upstream; "" is a deliberate
        # unassign or clear, which is what the card's own clear button sends.
        "assignee": body.get("assignee"), "due": body.get("due"),
        "nudge_chars": _board_nudge_for(slug, session),
    })
    return _enrich_people(data, slug, session)


@router.post("/api/board/cards/{card_slug}/link")
def board_card_link(
    card_slug: str,
    body: dict = Body(default={}),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """Relate two cards, or say one blocks the other. `remove` undoes it."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", f"/board/cards/{card_slug}/link", slug, json_body={
        "other": body.get("other"), "kind": body.get("kind"),
        "remove": bool(body.get("remove")), "source": "ui",
    })


@router.get("/api/board/next-card")
def board_next_card(
    column: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """The next card in a column that can actually be started — its blockers
    resolved transitively, and a blocking cycle reported rather than followed."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", "/board/next-card", slug, params={
        "column": column, "stage": stage, "repo": repo, "label": label,
    })
    return _enrich_people(data, slug, session)


@router.post("/api/board/cards/{card_slug}/archive")
def board_card_archive(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", f"/board/cards/{card_slug}/archive", slug, json_body={})


@router.post("/api/board/cards/{card_slug}/unarchive")
def board_card_unarchive(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    return _board_upstream("POST", f"/board/cards/{card_slug}/unarchive", slug, json_body={})


@router.post("/api/board/cards/{card_slug}/wiki", status_code=201)
def board_card_create_wiki(
    card_slug: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_authenticated),
    session: Session = Depends(get_db_session),
):
    """Start the card's wiki thread: create AI/projects/<slug>/index.md seeded
    from the card. Idempotent — returns the existing page if it's already there.
    The page is AI-generated by virtue of living under the AI folder."""
    slug = _effective_tenant_slug(claims, tenant_slug, session)
    data = _board_upstream("GET", f"/board/cards/{card_slug}", slug)
    card = (data or {}).get("card") or {}
    cslugs = _card_wiki_slugs(card.get("slug") or card_slug, [])
    if not cslugs:
        raise HTTPException(status_code=400, detail="Invalid card slug")
    cslug = cslugs[0]
    tenant = session.query(Tenant).filter_by(slug=slug, is_active=True).first()
    if tenant is None:
        raise HTTPException(status_code=400, detail="No tenant context")
    folder = _resolve_ai_wiki_folder(session, slug)
    rel = _normalize_path(f"{folder}/projects/{cslug}/index.md")
    if not _is_within_ai_folder(rel, folder):
        raise HTTPException(status_code=403, detail="Resolved path is outside the AI folder")
    if _s3_exists(tenant, rel):
        return {"path": rel, "created": False}
    title = (card.get("title") or cslug).strip()
    body_text = (card.get("body") or "").strip()
    # Link back to the board card. Uses the slug; board.html?card=<slug> resolves
    # through the card's aliases[], so a later card rename doesn't break this link
    # (self-healing, same as the card→wiki direction).
    content = (f"# {title}\n\n{body_text}\n\n---\n"
               f"_Started from board card "
               f"[`{cslug}`](/editor/board.html?card={cslug}) — open it on the board._\n")
    _s3_write(tenant, rel, content.encode("utf-8"))
    try:
        _trigger_embedder_reindex(rel, org=tenant)
    except Exception:
        pass
    try:
        _record_wiki_revision(session, tenant, rel, content.encode("utf-8"),
                              edited_by_id=claims.get("uid"),
                              edited_by_name=claims.get("username"),
                              source="board", created=True)
    except Exception:
        pass
    return {"path": rel, "created": True}
