"""
The editor's API token routes.

`router` is the org admin's /api/admin/tokens routes (list, create, update,
revoke) and the MCP server's /api/internal/tokens/validate. `me_router` is a
signed-in user's own tokens (/api/me/tokens) and the OAuth sessions issued
from their consent (/api/me/oauth-sessions). main.py includes each one where
its routes used to be declared, so the route order is unchanged.

Moved out of main.py by CQ-04 phase 7; the paths are unchanged.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import SESSION_COOKIE, decode_session_token, require_org_admin
from auth_helpers import _resolve_bearer_token
from config_db import (
    get_db_session, ApiToken, OAuthAccessToken, DEFAULT_TENANT_ID,
)

router = APIRouter()
me_router = APIRouter()


# ── API Token management ────────────────────────────────────────────────────

def _normalize_token_source(source: Optional[str]) -> str:
    """Lower-case and trim; empty/None becomes 'unknown'. Free-text accepted —
    the admin UI offers a curated dropdown (claude_code, claude_desktop, …)
    but custom sources are valid so the schema doesn't need to migrate when
    a new host shows up."""
    if not source:
        return "unknown"
    s = source.strip().lower()
    return s or "unknown"


class TokenCreateRequest(BaseModel):
    name: str
    rate_limit_rpm: int = 60
    rate_limit_rpd: int = 1000
    memory_read: bool = True
    memory_write: bool = False
    wiki_read: bool = True
    wiki_write: bool = False
    graph_read: bool = False
    board_read: bool = True
    board_write: bool = False
    allowed_labels: list[str] = []
    classification_access: list[str] = []
    source: str = "unknown"
    expires_at: Optional[str] = None   # ISO-8601 datetime string or None


class TokenUpdateRequest(BaseModel):
    rate_limit_rpm: Optional[int] = None
    rate_limit_rpd: Optional[int] = None
    memory_read: Optional[bool] = None
    memory_write: Optional[bool] = None
    wiki_read: Optional[bool] = None
    wiki_write: Optional[bool] = None
    graph_read: Optional[bool] = None
    board_read: Optional[bool] = None
    board_write: Optional[bool] = None
    allowed_labels: Optional[list[str]] = None
    classification_access: Optional[list[str]] = None
    source: Optional[str] = None
    expires_at: Optional[str] = None


def _parse_expires(expires_at: Optional[str]) -> Optional[datetime]:
    if not expires_at:
        return None
    try:
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid expires_at format: {expires_at!r}")


def _tokens_query_for(claims: dict, session: Session):
    """Restrict ApiToken queries to the caller's org (platform_admin sees all)."""
    q = session.query(ApiToken)
    if claims.get("role") != "platform_admin":
        org_id = claims.get("oid")
        if not org_id:
            raise HTTPException(status_code=400, detail="Caller has no org context")
        # tokens with NULL tenant_id resolve to DEFAULT_TENANT_ID via
        # ApiToken.effective_tenant_id(); filter on either match.
        from sqlalchemy import or_ as _or
        if org_id == DEFAULT_TENANT_ID:
            q = q.filter(_or(ApiToken.tenant_id == DEFAULT_TENANT_ID,
                             ApiToken.tenant_id.is_(None)))
        else:
            q = q.filter(ApiToken.tenant_id == org_id)
    return q


@router.get("/api/admin/tokens")
def list_tokens(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """List API tokens visible to the caller (org-scoped; platform sees all)."""
    tokens = _tokens_query_for(claims, session).order_by(ApiToken.created_at.desc()).all()
    return {"tokens": [t.to_dict() for t in tokens]}


@router.post("/api/admin/tokens", status_code=201)
def create_token(
    body: TokenCreateRequest,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """
    Create a new API bearer token (org-scoped — tenant_id forced from caller).
    Returns the raw token exactly once — it cannot be retrieved again.
    """
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Token name is required")

    # Platform admins administer the platform; tokens belong to an org and
    # are minted by that org's admins.  Refuse to silently fall back to the
    # default org when the caller has no org context.
    if claims.get("role") == "platform_admin":
        raise HTTPException(
            status_code=400,
            detail="Platform admins cannot mint API tokens.  Log in as an "
                   "org admin (or use that org's admin console) to create tokens.",
        )
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")

    # Graph visualization tokens must be linked to a user account — the graph
    # endpoint scopes memory visibility to that user. Admin-minted org tokens
    # have no user, so a graph_read flag here would only mint a token the
    # auth gateway rejects. Users mint their own under My Settings.
    if body.graph_read:
        raise HTTPException(
            status_code=400,
            detail="graph_read tokens are user-linked; create them via the "
                   "user's own profile (My Settings → API tokens).",
        )

    raw, hashed = ApiToken.generate()
    token = ApiToken(
        name=body.name.strip(),
        token_hash=hashed,
        tenant_id=org_id,
        rate_limit_rpm=max(1, body.rate_limit_rpm),
        rate_limit_rpd=max(1, body.rate_limit_rpd),
        memory_read=body.memory_read,
        memory_write=body.memory_write,
        wiki_read=body.wiki_read,
        wiki_write=body.wiki_write,
        graph_read=body.graph_read,
        board_read=body.board_read,
        board_write=body.board_write,
        allowed_labels=body.allowed_labels,
        classification_access=[t.upper() for t in body.classification_access],
        source=_normalize_token_source(body.source),
        expires_at=_parse_expires(body.expires_at),
    )
    session.add(token)
    session.commit()
    session.refresh(token)

    result = token.to_dict()
    result["raw_token"] = raw   # shown once, not stored
    return result


@router.patch("/api/admin/tokens/{token_id}")
def update_token(
    token_id: str,
    body: TokenUpdateRequest,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Update an existing token's limits and permissions. Name cannot be changed."""
    token = _tokens_query_for(claims, session).filter(ApiToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    if body.rate_limit_rpm is not None:
        token.rate_limit_rpm = max(1, body.rate_limit_rpm)
    if body.rate_limit_rpd is not None:
        token.rate_limit_rpd = max(1, body.rate_limit_rpd)
    if body.memory_read is not None:
        token.memory_read = body.memory_read
    if body.memory_write is not None:
        token.memory_write = body.memory_write
    if body.wiki_read is not None:
        token.wiki_read = body.wiki_read
    if body.wiki_write is not None:
        token.wiki_write = body.wiki_write
    if body.board_read is not None:
        token.board_read = body.board_read
    if body.board_write is not None:
        token.board_write = body.board_write
    if body.graph_read is not None:
        if body.graph_read and not token.user_id:
            raise HTTPException(
                status_code=400,
                detail="graph_read requires a user-linked token; this org "
                       "token has no user to scope graph visibility to.",
            )
        token.graph_read = body.graph_read
    if body.allowed_labels is not None:
        token.allowed_labels = body.allowed_labels
    if body.classification_access is not None:
        token.classification_access = [t.upper() for t in body.classification_access]
    # Source is intentionally set-once at creation. Reject any change attempt
    # rather than silently dropping it so the caller knows the request had
    # an effect-less field. This is the impersonation-hardening surface (PR E):
    # if source could be changed after issuance, an attacker who got into the
    # admin UI could relabel a token they control to look like a different
    # host and poison cross-source corroboration in the compactor.
    if body.source is not None and body.source != token.source:
        raise HTTPException(
            status_code=400,
            detail="source is immutable after token creation — revoke and reissue to change",
        )
    if body.expires_at is not None:
        token.expires_at = _parse_expires(body.expires_at) if body.expires_at else None

    session.commit()
    session.refresh(token)
    return token.to_dict()


@router.delete("/api/admin/tokens/{token_id}")
def revoke_token(
    token_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Permanently revoke an API token (org-scoped lookup)."""
    token = _tokens_query_for(claims, session).filter(ApiToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    session.delete(token)
    session.commit()
    return {"status": "revoked", "id": token_id}


@router.post("/api/internal/tokens/validate")
async def validate_token_internal(request: Request, session: Session = Depends(get_db_session)):
    """
    Internal endpoint used by the MCP server to validate a bearer token hash.
    Returns full token permissions on success; {valid: false} on failure.
    Updates last_used_at as a side effect.
    Should not be exposed through nginx/Traefik (internal network only).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    info, _tenant = _resolve_bearer_token(session, body.get("token_hash", ""))
    return JSONResponse(info or {"valid": False})


# Every scope flag an ApiToken carries, all of which the owner may set on
# their own token. A flag added to ApiToken and left out of this tuple is a
# grant the owner can see enforced but cannot give, which is how board_write
# went missing from My Settings; tests/test_me_tokens_scopes.py holds the two
# in step.
SELF_SERVICE_TOKEN_SCOPES = (
    "memory_read", "memory_write",
    "wiki_read", "wiki_write",
    "graph_read",
    "board_read", "board_write",
)


@me_router.get("/api/me/tokens")
def list_my_tokens(
    request: Request,
    session: Session = Depends(get_db_session),
):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = session.query(ApiToken).filter_by(user_id=claims["uid"]).order_by(
        ApiToken.created_at.desc()
    ).all()
    return {"tokens": [t.to_dict() for t in rows]}


@me_router.post("/api/me/tokens", status_code=201)
def create_my_token(
    body: dict,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Self-service token creation — any authenticated user can create tokens for themselves."""
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Token name is required")
    raw, hashed = ApiToken.generate()
    # Self-service tokens have source hardcoded server-side. Letting the user
    # pick (or pass via API body) would let any logged-in user spoof a source
    # they don't actually represent — admins can issue host-distinct tokens
    # via /api/admin/tokens with their trust level; self-service tokens are
    # tagged generically.
    tok = ApiToken(
        name=name,
        token_hash=hashed,
        tenant_id=claims.get("oid"),
        user_id=claims["uid"],
        memory_read=bool(body.get("memory_read", True)),
        memory_write=bool(body.get("memory_write", False)),
        wiki_read=bool(body.get("wiki_read", True)),
        # wiki_write defaults to TRUE for self-service tokens. The scope
        # only ever lets an agent create/update pages inside the org's
        # designated AI folder (default "AI/"); pages there are flagged
        # as AI-generated and become read-only to agents the moment a
        # human moves them out. The blast radius is small enough that
        # default-on is the right UX — users who want the agent to
        # write notes/runbooks don't have to remember to flip a
        # checkbox, and the AI/ confinement is enforced server-side at
        # the editor's /api/internal/wiki/write boundary regardless.
        wiki_write=bool(body.get("wiki_write", True)),
        # graph_read added in PR #67 (memory-graph standalone). Default
        # off — this is a narrow, opt-in scope for the graph view's
        # bearer-token rendering path.
        graph_read=bool(body.get("graph_read", False)),
        # Board scopes follow the column defaults and the admin form: read
        # on, write off. Write is what lets an agent create, move, comment
        # on, label and lock cards, so it is opt-in like memory_write.
        board_read=bool(body.get("board_read", True)),
        board_write=bool(body.get("board_write", False)),
        # Rate limits are admin-managed (PATCH /api/admin/tokens), the same
        # as on the self-service edit below, so the body cannot set them:
        # the column defaults apply.
        allowed_labels=[],
        classification_access=[],
        source="user_self_service",
    )
    if body.get("expires_days"):
        tok.expires_at = datetime.utcnow() + timedelta(days=int(body["expires_days"]))
    session.add(tok)
    session.commit()
    return {**tok.to_dict(), "token": raw}


@me_router.patch("/api/me/tokens/{token_id}")
def update_my_token(
    token_id: str,
    body: dict,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Self-service edit of the scope flags on a user-owned API token.

    Free-form edit — the user can both narrow and widen scopes on their
    own personal access token (matches the conventional PAT model: it
    IS the user). OAuth-issued tokens are edited the same way via
    PATCH /api/me/oauth-sessions/{prefix} below, except every change
    there is audit-stamped, since that token also carries an external
    consent grant.
    """
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tok = session.query(ApiToken).filter_by(id=token_id, user_id=claims["uid"]).first()
    if not tok:
        raise HTTPException(status_code=404, detail="Token not found")
    # Only the scope flags are editable here. Name, source, tenant_id,
    # user_id, classification, label scope, rate limits are admin-managed
    # (POST /api/admin/tokens/* / PATCH there) so a user can't quietly
    # widen the trust level of a host-attributed token by editing its
    # source string. graph_read is safe to toggle: a self-service token is
    # always user-linked (user_id=claims["uid"]), which is the only
    # constraint the admin path guards for it. The board scopes are the
    # user's own board in their own tenant, no wider than wiki_write, which
    # this token could already be given.
    for key in SELF_SERVICE_TOKEN_SCOPES:
        if key in body:
            setattr(tok, key, bool(body[key]))
    session.commit()
    return tok.to_dict()


@me_router.delete("/api/me/tokens/{token_id}", status_code=204)
def revoke_my_token(
    token_id: str,
    request: Request,
    session: Session = Depends(get_db_session),
):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tok = session.query(ApiToken).filter_by(id=token_id, user_id=claims["uid"]).first()
    if not tok:
        raise HTTPException(status_code=404, detail="Token not found")
    session.delete(tok)
    session.commit()
    return Response(status_code=204)


def _oauth_session_scopes(tok) -> dict:
    """The scopes an OAuth session holds, decomposed for the settings page.

    The four stored flags are how the consent-page approval was captured;
    scope_modified_at flags any later owner edit. The board flags are not
    stored: they are what the MCP server will enforce, taken from the same
    to_validate_dict() it is sent, so the page cannot disagree with it. They
    follow wiki read/write and are not separately editable.
    """
    enforced = tok.to_validate_dict()
    return {
        "token_id":     tok.token_hash[:8],
        "memory_read":  bool(tok.memory_read),
        "memory_write": bool(tok.memory_write),
        "wiki_read":    bool(tok.wiki_read),
        "wiki_write":   bool(tok.wiki_write),
        "board_read":   bool(enforced["board_read"]),
        "board_write":  bool(enforced["board_write"]),
    }


@me_router.get("/api/me/oauth-sessions")
def list_my_oauth_sessions(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """List active OAuth access tokens issued from this user's consent."""
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = session.query(OAuthAccessToken).filter_by(user_id=claims.get("uid")).all()
    return {"sessions": [{**_oauth_session_scopes(r),
        "client_id":   r.client_id,
        "client_name": r.client_name,
        "scope":       r.scope,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
        "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        "expires_at":   r.expires_at.isoformat() if r.expires_at else None,
        # Non-null once the owner has edited this session's scope after
        # issuance — lets the UI flag that the scope no longer matches the
        # original consent grant.
        "scope_modified_at": r.scope_modified_at.isoformat() if r.scope_modified_at else None,
    } for r in rows]}


@me_router.patch("/api/me/oauth-sessions/{token_id_prefix}")
def update_my_oauth_session(
    token_id_prefix: str,
    body: dict,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Edit scopes on an OAuth-issued access token without revoking it.

    The resource owner may both narrow AND widen scope here. Widening is
    allowed because the consent page always surfaces all four scopes (so
    the owner can never grant more than that flow itself offered) and
    wiki_write stays AI-folder-confined server-side regardless — the same
    owner could otherwise just mint a full-scope personal token for the
    client, so refusing the in-place edit only adds friction without
    containing anything.

    Any change is stamped with ``scope_modified_at`` so a session whose
    scope was edited out-of-band can be told apart from one that still
    matches its original consent grant (surfaced as a "scope edited"
    marker in the session list / UI).
    """
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if len(token_id_prefix) < 8:
        raise HTTPException(status_code=400, detail="token id prefix too short (min 8 chars)")
    rows = session.query(OAuthAccessToken).filter_by(user_id=claims.get("uid")).all()
    tok = next((t for t in rows if t.token_hash.startswith(token_id_prefix)), None)
    if tok is None:
        raise HTTPException(status_code=404, detail="OAuth session not found")
    changed = False
    for key in ("memory_read", "memory_write", "wiki_read", "wiki_write"):
        if key not in body:
            continue
        desired = bool(body[key])
        if desired != bool(getattr(tok, key)):
            setattr(tok, key, desired)
            changed = True
    if changed:
        # Stamp out-of-band owner edits (narrow or widen) so the session's
        # scope can be distinguished from one untouched since consent.
        tok.scope_modified_at = datetime.utcnow()
    session.commit()
    return {
        **_oauth_session_scopes(tok),
        "scope_modified_at": tok.scope_modified_at.isoformat() if tok.scope_modified_at else None,
    }


@me_router.delete("/api/me/oauth-sessions/{token_id_prefix}", status_code=204)
def revoke_my_oauth_session(
    token_id_prefix: str,
    request: Request,
    session: Session = Depends(get_db_session),
):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # The list view exposes token_hash[:8] as the id, so require at least
    # 8 chars here — a 1-char prefix would match (and revoke) an arbitrary
    # session of the caller's, since the loop deletes the first match.
    if len(token_id_prefix) < 8:
        raise HTTPException(status_code=400, detail="token id prefix too short (min 8 chars)")
    rows = session.query(OAuthAccessToken).filter_by(user_id=claims.get("uid")).all()
    for tok in rows:
        if tok.token_hash.startswith(token_id_prefix):
            session.delete(tok)
            session.commit()
            return Response(status_code=204)
    raise HTTPException(status_code=404, detail="OAuth session not found")
