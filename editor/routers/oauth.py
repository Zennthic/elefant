"""
OAuth 2.0 authorization server: metadata, dynamic client registration,
consent, token and revocation endpoints.

Moved out of main.py by CQ-04 phase 1; the paths are unchanged.
"""
import base64
import hashlib
import hmac
import json as _json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import login_throttle
from auth import require_session
from auth_helpers import (
    APP_NAME, _EDITOR_PATH_PREFIX, _SafeHTML, _STATIC,
    _editor_base_url, _render, _request_ip, _tenant_is_active,
)
from config_db import (
    get_db_session, OAuthAccessToken, OAuthClient, OAuthCode, Tenant, UserAccount,
    DEFAULT_TENANT_ID,
)

log = logging.getLogger("editor")

router = APIRouter()


# ── OAuth 2.0 Authorization Server ─────────────────────────────────────────
#
# Implements MCP OAuth (spec 2025-03-26) on top of the existing bearer-token
# auth system.  The MCP server advertises this editor as the authorization
# server via its /.well-known/oauth-authorization-server endpoint.
#
# Flow:
#   1. Client fetches /.well-known/oauth-authorization-server from the MCP server
#      → gets metadata pointing to these endpoints
#   2. Client does dynamic registration (POST /oauth/register)
#   3. Client redirects user to GET /oauth/authorize (consent page)
#   4. User approves → POST /oauth/authorize → redirect with ?code=...
#   5. Client exchanges code for token (POST /oauth/token, PKCE S256)
#   6. Client uses token as Authorization: Bearer on MCP requests

def _oauth_metadata(request: Request) -> dict:
    base = _editor_base_url(request)
    # RFC 8414: issuer must be the AS base URL with no path — so discovery resolves to
    # {scheme}://{host}/.well-known/oauth-authorization-server (no path suffix to append).
    # The auth/token endpoints still live under /editor/oauth/... which is fine.
    parsed = urlparse(base)
    issuer = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "issuer":                                issuer,
        "authorization_endpoint":               f"{base}/oauth/authorize",
        "token_endpoint":                        f"{base}/oauth/token",
        "registration_endpoint":                 f"{base}/oauth/register",
        "revocation_endpoint":                   f"{base}/oauth/revoke",
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code"],
        "code_challenge_methods_supported":      ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported":                      ["mcp"],
    }


@router.get("/.well-known/oauth-authorization-server")
def oauth_metadata(request: Request):
    """RFC 8414 authorization server metadata."""
    return JSONResponse(_oauth_metadata(request))


# ── Dynamic client registration (RFC 7591) ──────────────────────────────────

class OAuthRegisterRequest(BaseModel):
    client_name:   str
    redirect_uris: List[str]
    grant_types:   Optional[List[str]] = ["authorization_code"]
    response_types: Optional[List[str]] = ["code"]
    scope:         Optional[str] = "mcp"


_DANGEROUS_REDIRECT_SCHEMES = {"javascript", "data", "vbscript", "file", "blob", "about"}


def _validate_redirect_uri(uri: str) -> bool:
    """RFC 6749 §3.1.2 + RFC 8252 §7.3 redirect_uri validation.

    Rules:
      * Must parse to scheme + netloc (or scheme + path for custom-scheme
        native-app URIs like com.example.app:/cb).
      * https is always allowed.
      * http is allowed only for loopback (RFC 8252).
      * Custom schemes are allowed for native-app callbacks, but never
        the dangerous ones (javascript:, data:, vbscript:, file:, blob:,
        about:) which would let an attacker mint a code that redeems to
        a code execution context in the victim's browser.
      * Fragments are rejected (RFC 6749 §3.1.2).

    Previously /oauth/register accepted anything in redirect_uris with
    no validation, so an attacker could register a client with
    redirect_uris=["javascript:alert(...)"] or ["https://evil/cb"], then
    phish a logged-in user with /oauth/authorize?client_id=...
    &redirect_uri=https://evil/cb&... to mint a code that lands on
    attacker-controlled domain.  Combined with the open DCR endpoint
    this was a one-click token-theft chain.
    """
    if not uri or not isinstance(uri, str):
        return False
    try:
        from urllib.parse import urlparse as _urlparse
        p = _urlparse(uri)
    except Exception:
        return False
    scheme = (p.scheme or "").lower()
    if not scheme or scheme in _DANGEROUS_REDIRECT_SCHEMES:
        return False
    if p.fragment:
        return False
    if scheme == "https":
        return bool(p.netloc)
    if scheme == "http":
        host = (p.hostname or "").lower()
        return bool(host) and (
            host in ("localhost", "127.0.0.1", "::1")
            or host.endswith(".localhost")
        )
    # Custom scheme (native app) — require either a netloc or a non-empty
    # path so we don't accept bare "myscheme:" with nothing else.
    return bool(p.netloc or p.path)


def _oauth_access_token_ttl() -> int:
    """Lifetime of a newly minted OAuth access token, in seconds.

    OAUTH_ACCESS_TOKEN_TTL_DAYS, default 365. There is no refresh-token grant,
    so a client whose token expires has to be re-authorised by its user; keep
    that in mind before shortening it. Tokens already issued keep the expiry
    they were minted with.
    """
    try:
        days = float(os.environ.get("OAUTH_ACCESS_TOKEN_TTL_DAYS", "365"))
    except ValueError:
        days = 365.0
    if days <= 0:
        days = 365.0
    return int(days * 24 * 3600)


OAUTH_ACCESS_TOKEN_TTL = _oauth_access_token_ttl()


@router.post("/oauth/register", status_code=201)
def oauth_register(body: OAuthRegisterRequest, request: Request,
                   session: Session = Depends(get_db_session)):
    """
    Dynamic client registration.
    Returns client_id; no client_secret (public client — PKCE only).
    Open by design, so throttled per client address (SEC-10).
    """
    wait = login_throttle.oauth_register_limiter.hit(_request_ip(request))
    if wait:
        return JSONResponse(
            {"error": "slow_down",
             "error_description": "Too many client registrations from this address."},
            status_code=429, headers={"Retry-After": str(wait)},
        )
    if not body.redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")
    if not body.client_name.strip():
        raise HTTPException(status_code=400, detail="client_name is required")

    bad = [u for u in body.redirect_uris if not _validate_redirect_uri(u)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(
                "redirect_uri must use https, http on loopback, or a "
                "native-app custom scheme; no fragments, no javascript:/"
                "data:/file:/blob:/vbscript:/about: schemes. Rejected: "
                + ", ".join(bad)
            ),
        )

    tenants = session.query(Tenant).filter_by(is_active=True).all()
    tenant_id = tenants[0].id if len(tenants) == 1 else DEFAULT_TENANT_ID
    client = OAuthClient(
        client_name=body.client_name.strip(),
        redirect_uris=body.redirect_uris,
        scope=body.scope or "mcp",
        tenant_id=tenant_id,
    )
    session.add(client)
    session.commit()
    session.refresh(client)
    return JSONResponse(client.to_dict(), status_code=201)


# ── Authorization endpoint ───────────────────────────────────────────────────

def _pkce_verify(code_verifier: str, code_challenge: str) -> bool:
    """RFC 7636 S256 verification, constant-time.

    Returns True iff base64url(sha256(code_verifier)) == code_challenge.
    Uses hmac.compare_digest for the equality check so the comparison
    doesn't leak partial hash bytes via timing — defensive, not a
    practical attack against a SHA-256 digest, but the right primitive
    to use on a security-relevant equality.
    """
    if not code_verifier or not code_challenge:
        return False
    digest   = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, code_challenge)


def _safe_oauth_redirect(redirect_uri: str, **params) -> str:
    """Build a redirect URL with all params correctly url-encoded.

    Previously the consent flow concatenated state= straight into the
    URL — any '&' or '#' in state corrupted the redirect, and a
    sufficiently weird state value could inject extra query parameters
    the client would parse. Round-trip everything through urlencode.
    """
    from urllib.parse import urlencode as _urlencode, urlparse as _urlparse, urlunparse as _urlunparse
    p = _urlparse(redirect_uri)
    existing = p.query
    new = _urlencode({k: v for k, v in params.items() if v is not None})
    combined = "&".join(q for q in (existing, new) if q)
    return _urlunparse(p._replace(query=combined))


@router.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize_page(
    request: Request,
    client_id:             str,
    redirect_uri:          str,
    response_type:         str,
    code_challenge:        str,
    code_challenge_method: str = "S256",
    scope:                 str = "mcp",
    state:                 str = "",
    _user: str = Depends(require_session),
    session: Session = Depends(get_db_session),
):
    """Serve the consent page (requires login)."""
    if response_type != "code":
        raise HTTPException(status_code=400, detail="Only response_type=code is supported")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="Only code_challenge_method=S256 is supported")

    user_id = _user.get("uid") if isinstance(_user, dict) else None
    cred = session.query(UserAccount).filter_by(id=user_id).first() if user_id else None
    if not cred or not cred.can_oauth:
        raise HTTPException(status_code=403,
            detail="Your account is not permitted to authorize OAuth clients.")

    client = session.query(OAuthClient).filter_by(client_id=client_id).first()
    if not client:
        raise HTTPException(status_code=400, detail="Unknown client_id")
    if redirect_uri not in (client.redirect_uris or []):
        raise HTTPException(status_code=400, detail="redirect_uri not registered for this client")

    import html as _html
    user_label = cred.full_username or cred.username or ""
    consent_html = _render(_STATIC / "authorize.html", {
        "<!-- APP_NAME -->": APP_NAME,
        # The <strong> tags are static markup; only user_label is dynamic
        # and it's already _html.escape()'d. Mark the whole fragment safe
        # so _render doesn't double-escape the tags.
        "<!-- USER_LINE -->": (
            _SafeHTML(f"Signed in as <strong>{_html.escape(user_label)}</strong>")
            if user_label else ""
        ),
    })
    consent_html = consent_html.replace(
        "/* __INJECTED__ */",
        f"const _injectedClientName = {_json.dumps(client.client_name)};\n"
        f"  const _injectedAppName = {_json.dumps(APP_NAME)};",
    )
    # Make the form action explicit so it doesn't depend on the browser
    # canonicalising the current URL. nginx strips /editor before forwarding
    # to FastAPI, so request.url.path is /oauth/authorize — we re-prepend the
    # public path prefix so the form posts back through nginx, not directly.
    form_action = f"{_EDITOR_PATH_PREFIX}{request.url.path}"
    query_string = request.scope.get("query_string", b"")
    if query_string:
        if isinstance(query_string, bytes):
            query_string = query_string.decode()
        form_action += f"?{query_string}"
    consent_html = consent_html.replace(
        '<form id="consent-form" method="POST" action="">',
        f'<form id="consent-form" method="POST" action="{form_action}">',
    )
    return HTMLResponse(consent_html)


@router.post("/oauth/authorize")
async def oauth_authorize_submit(
    request: Request,
    _user: str = Depends(require_session),
    session: Session = Depends(get_db_session),
):
    """Process the consent form submission (approve or deny)."""
    user_id = _user.get("uid") if isinstance(_user, dict) else None
    cred = session.query(UserAccount).filter_by(id=user_id).first() if user_id else None
    if not cred or not cred.can_oauth:
        log.warning("OAuth consent denied: user %r lacks can_oauth", _user)
        raise HTTPException(status_code=403,
            detail="Your account is not permitted to authorize OAuth clients.")

    form = await request.form()

    client_id             = (form.get("client_id", "") or "").strip()
    redirect_uri          = (form.get("redirect_uri", "") or "").strip()
    code_challenge        = (form.get("code_challenge", "") or "").strip()
    code_challenge_method = form.get("code_challenge_method", "S256")
    scope                 = form.get("scope", "mcp")
    state                 = form.get("state", "")
    action                = form.get("action", "deny")

    # Permissions selected on the consent page
    memory_read  = form.get("memory_read")  == "on"
    memory_write = form.get("memory_write") == "on"
    wiki_read    = form.get("wiki_read")    == "on"
    wiki_write   = form.get("wiki_write")   == "on"

    log.info(
        "OAuth consent submission: user=%s client_id=%s action=%s redirect_uri=%s",
        _user, client_id, action, redirect_uri,
    )

    if not client_id or not redirect_uri:
        log.warning("OAuth consent missing required form fields")
        raise HTTPException(status_code=400, detail="Missing client_id or redirect_uri in form")

    client = session.query(OAuthClient).filter_by(client_id=client_id).first()
    if not client:
        log.warning("OAuth consent: unknown client_id=%s", client_id)
        raise HTTPException(status_code=400, detail="Unknown client_id")

    # RFC 6749 §3.1.2.3 requires exact-match redirect_uri.  The previous
    # rstrip("/") fallback meant a client registered with .../cb also
    # matched .../cb/ — soft enough to keep working but the start of
    # canonicalization confusion attacks once refresh tokens land.
    registered = client.redirect_uris or []
    if redirect_uri not in registered:
        log.warning(
            "OAuth consent: redirect_uri %r not registered for client %s. Registered: %r",
            redirect_uri, client_id, registered,
        )
        raise HTTPException(status_code=400, detail="redirect_uri not registered for this client")

    # The GET-page handler enforces these but the POST handler used to
    # take whatever the form sent — store-side only, but worth being
    # consistent so the code row's metadata can be trusted.
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400,
                            detail="Only code_challenge_method=S256 is supported")
    if not code_challenge or len(code_challenge) < 43 or len(code_challenge) > 128:
        # RFC 7636 §4.2 mandates 43..128 chars for an S256 base64url challenge.
        raise HTTPException(status_code=400,
                            detail="code_challenge must be a 43..128 char base64url string")

    if action != "approve":
        return RedirectResponse(
            _safe_oauth_redirect(redirect_uri, error="access_denied", state=state or None),
            status_code=302,
        )

    code = secrets.token_urlsafe(32)
    oauth_code = OAuthCode(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        user_id=user_id,
        memory_read=memory_read,
        memory_write=memory_write,
        wiki_read=wiki_read,
        wiki_write=wiki_write,
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    session.add(oauth_code)
    session.commit()

    return RedirectResponse(
        _safe_oauth_redirect(redirect_uri, code=code, state=state or None),
        status_code=302,
    )


# ── Token endpoint ───────────────────────────────────────────────────────────

@router.post("/oauth/token")
async def oauth_token(request: Request, session: Session = Depends(get_db_session)):
    """
    Exchange an authorization code for an access token (PKCE S256 required).
    Accepts application/x-www-form-urlencoded or application/json.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type    = body.get("grant_type", "")
    code_val      = body.get("code", "")
    redirect_uri  = body.get("redirect_uri", "")
    client_id     = body.get("client_id", "")
    code_verifier = body.get("code_verifier", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    # Atomic compare-and-swap: claim the code in a single UPDATE so two
    # concurrent token requests racing on the same code can't both pass
    # the "is it used?" check and mint two access tokens for the same
    # authorization. The RETURNING clause delivers the captured row so
    # we can apply the rest of the validation against frozen state.
    # NB: oauth_codes' primary key is `code` — there is no `id` column, so the
    # UPDATE returns `code` (returning `id` 500s the whole exchange).
    claimed_code = session.execute(
        text(
            "UPDATE oauth_codes SET used = TRUE "
            "WHERE code = :code AND used = FALSE RETURNING code"
        ),
        {"code": code_val},
    ).scalar()
    session.commit()
    if not claimed_code:
        # The claim failed. If the code exists, is already used, and hasn't
        # expired, this is a REPLAY of a redeemed code — almost always a
        # sign the code was intercepted in transit. Revoke the entire
        # family of tokens minted from this code (RFC 6819 §5.2.1.1) so an
        # attacker racing the legitimate client can't keep a token it
        # already exchanged.
        if code_val:
            replayed = session.query(OAuthCode).filter_by(code=code_val).first()
            if (replayed and replayed.used
                    and replayed.expires_at and datetime.utcnow() <= replayed.expires_at):
                revoked = session.query(OAuthAccessToken).filter_by(
                    from_code=code_val,
                ).delete(synchronize_session=False)
                session.commit()
                log.warning(
                    "OAuth code replay detected (code=%s…, client=%s, user=%s) — "
                    "revoked %d token(s) from that code",
                    (replayed.code or "")[:8],
                    replayed.client_id, replayed.user_id, revoked,
                )
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    code = session.query(OAuthCode).filter_by(code=code_val).first()
    if not code:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if datetime.utcnow() > code.expires_at:
        return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)
    if code.client_id != client_id:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if code.redirect_uri != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not _pkce_verify(code_verifier, code.code_challenge):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    client = session.query(OAuthClient).filter_by(client_id=client_id).first()
    client_name = client.client_name if client else client_id

    # Tie the token to the actual authorizing user, not the OAuthClient's
    # tenant_id (which was guessed at registration time and is wrong for
    # multi-org deployments).
    auth_user = session.query(UserAccount).filter_by(id=code.user_id).first() if code.user_id else None
    if not auth_user:
        return JSONResponse(
            {"error": "invalid_grant",
             "error_description": "Authorizing user not found"},
            status_code=400,
        )
    target_tenant_id = auth_user.org_id or DEFAULT_TENANT_ID
    if not _tenant_is_active(session, target_tenant_id):
        return JSONResponse(
            {"error": "invalid_grant",
             "error_description": "Organization is not active"},
            status_code=400,
        )

    raw, token_hash = OAuthAccessToken.generate()
    access_token = OAuthAccessToken(
        token_hash=token_hash,
        client_id=client_id,
        client_name=client_name,
        scope=code.scope,
        tenant_id=target_tenant_id,
        user_id=auth_user.id,
        memory_read=code.memory_read,
        memory_write=code.memory_write,
        wiki_read=code.wiki_read,
        wiki_write=code.wiki_write,
        from_code=code_val,
        expires_at=datetime.utcnow() + timedelta(seconds=OAUTH_ACCESS_TOKEN_TTL),
    )
    session.add(access_token)
    session.commit()

    return JSONResponse({
        "access_token": raw,
        "token_type":   "Bearer",
        "expires_in":   OAUTH_ACCESS_TOKEN_TTL,
        "scope":        code.scope,
    })


# ── Revocation endpoint (RFC 7009) ───────────────────────────────────────────

@router.post("/oauth/revoke")
async def oauth_revoke(request: Request, session: Session = Depends(get_db_session)):
    """Revoke an OAuth access token."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    raw_token = body.get("token", "")
    if not raw_token:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token = session.query(OAuthAccessToken).filter_by(token_hash=token_hash).first()
    if token:
        session.delete(token)
        session.commit()

    # Per RFC 7009, always return 200 even if token wasn't found
    return Response(status_code=200)
