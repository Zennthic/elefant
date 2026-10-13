"""
Helpers the editor's routers share with main.py.

They lived in main.py until CQ-04 started splitting it into routers. A router
that imported them back from main would be a circular import, so they live
here and main.py imports them like any other module does.
"""
import hashlib
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

import login_throttle
import deployment_mode
from config_db import (
    ApiToken, OAuthAccessToken, Tenant, UserAccount, DEFAULT_TENANT_SLUG,
)

# Public-facing base URL for this editor service (no trailing slash).
# If not set, OAuth metadata auto-detects from proxy headers; OIDC/SAML
# callbacks require this to be explicitly configured.
_EDITOR_EXTERNAL_URL_OVERRIDE = os.environ.get("EDITOR_EXTERNAL_URL", "").rstrip("/")
EDITOR_EXTERNAL_URL = _EDITOR_EXTERNAL_URL_OVERRIDE  # used by OIDC/SAML callback builders
# Path prefix under which the editor is mounted (used for URL auto-detection).
_EDITOR_PATH_PREFIX = os.environ.get("EDITOR_PATH_PREFIX", "/editor").rstrip("/")

APP_NAME          = os.environ.get("APP_NAME", "Elefant")


def _editor_base_url(request: Request) -> str:
    """Return the public base URL for this editor instance (no trailing slash)."""
    if _EDITOR_EXTERNAL_URL_OVERRIDE:
        return _EDITOR_EXTERNAL_URL_OVERRIDE
    host  = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    proto = request.headers.get("x-forwarded-proto") or "http"
    return f"{proto}://{host}{_EDITOR_PATH_PREFIX}"


def _request_ip(request: Request) -> str:
    return login_throttle.client_ip(
        request.client.host if request.client else None, request.headers)


class _SafeHTML(str):
    """Marker for a string that is already HTML-safe and should be inserted
    verbatim by _render() rather than HTML-escaped.  Use sparingly — the
    only legitimate cases are pre-composed fragments like
    `f"Signed in as <strong>{html.escape(name)}</strong>"` where the
    outer tags are static and the dynamic part is already escaped."""


def _render(template_path: Path, replacements: dict) -> str:
    """Load an HTML file and apply placeholder substitutions, HTML-escaping
    every value unless it is marked as _SafeHTML.

    Previously this used a literal .replace() with no escaping, so any
    value that ended up at a placeholder (most dangerously the
    <!-- ERROR --> placeholder, fed by error strings from
    _normalize_username_input that interpolated user-supplied 'local'
    and 'dom' fragments) was reflected as raw HTML.  Posting
    `username=x@<img src=x onerror=...>.com` to /setup or /invite
    landed the tag in the response body unmodified.
    """
    import html as _html_mod
    html_text = template_path.read_text()
    for key, value in replacements.items():
        if isinstance(value, _SafeHTML):
            s = str(value)
        else:
            s = _html_mod.escape("" if value is None else str(value))
        html_text = html_text.replace(key, s)
    return html_text


_STATIC = Path(__file__).parent / "static"


def _tenant_is_active(session: "Session", org_id: Optional[str]) -> bool:
    """Return True iff the org exists and is in 'active' lifecycle_status."""
    if not org_id:
        return False
    org = session.query(Tenant).filter_by(id=org_id).first()
    return bool(org and org.lifecycle_status == "active")


def _upsert_idp_user(
    session: Session,
    *,
    tenant_id: str,
    email: str,
    display_name: str,
    external_sub: str,
    oidc_provider_id: Optional[str] = None,
    saml_provider_id: Optional[str] = None,
) -> UserAccount:
    """Find-or-create a UserAccount for an IdP-asserted identity."""
    auth_method = "oidc" if oidc_provider_id else "saml"
    provider_filter = (
        UserAccount.oidc_provider_id == oidc_provider_id
        if oidc_provider_id
        else UserAccount.saml_provider_id == saml_provider_id
    )
    user = session.query(UserAccount).filter(
        UserAccount.org_id == tenant_id,
        UserAccount.external_sub == external_sub,
        provider_filter,
    ).first()
    if user:
        user.email = email or user.email
        user.display_name = display_name or user.display_name
        user.last_login_at = datetime.utcnow()
        return user

    org = session.query(Tenant).filter_by(id=tenant_id).first()
    domain = (org.domain if org else None) or f"{(org.slug if org else 'unknown')}.local"
    # IdP usernames keep the asserted email as the local part if it matches the
    # org domain; otherwise the email becomes the full_username verbatim.
    local = email.split("@")[0].lower() if "@" in (email or "") else (display_name or external_sub).lower()
    full = email.lower() if email and "@" in email else f"{local}@{domain}"
    # Deduplicate: if a UserAccount with this full_username already exists in
    # the org but for a different external_sub, suffix the local part.
    if session.query(UserAccount).filter_by(full_username=full).first():
        full = f"{local}+{external_sub[:8]}@{domain}"
    user = UserAccount(
        id=secrets.token_hex(16),
        org_id=tenant_id,
        username=local,
        full_username=full,
        display_name=display_name or local,
        email=email,
        password_hash=None,
        role="user",
        auth_method=auth_method,
        external_sub=external_sub,
        oidc_provider_id=oidc_provider_id,
        saml_provider_id=saml_provider_id,
        is_active=True,
        invite_pending=False,
        last_login_at=datetime.utcnow(),
    )
    session.add(user)
    return user


def _effective_tenant_slug(claims: dict, requested: Optional[str], session: Session) -> str:
    """
    Resolve the tenant_slug to operate on.  platform_admin may override via
    the query param to act on any tenant; everyone else is forced to their
    own org's slug regardless of what the query param says.
    """
    if claims.get("role") == "platform_admin":
        return (requested or DEFAULT_TENANT_SLUG).strip() or DEFAULT_TENANT_SLUG
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Caller's org no longer exists")
    return org.slug


def _resolve_bearer_token(session: Session, token_hash: str):
    """Resolve a bearer token hash to (info_dict, Tenant). Returns (None, None)
    when the token is unknown, expired, or its org isn't active. Updates
    last_used_at as a side effect. Shared by the MCP token-validate endpoint
    and the internal wiki-write endpoint."""
    if not token_hash:
        return None, None

    token = session.query(ApiToken).filter_by(token_hash=token_hash).first()
    if token:
        if token.expires_at and datetime.utcnow() > token.expires_at:
            return None, None
        tenant = session.query(Tenant).filter_by(id=token.effective_tenant_id()).first()
        if tenant and tenant.lifecycle_status != "active":
            return None, None
        token.last_used_at = datetime.utcnow()
        session.commit()
        info = token.to_dict()
        info["valid"] = True
        info["tenant_slug"] = tenant.slug if tenant else DEFAULT_TENANT_SLUG
        # Which identity space this token's own id lives in. An ApiToken id is
        # a durable UUID, so a standalone one IS an agent account a card can
        # name as its reporter; an OAuth token's id is derived from the token
        # hash and changes at every login, so it can never be one. The caller
        # deriving a principal has to be able to tell those apart, and
        # guessing from the shape of an id is not a contract.
        info["principal_kind"] = "api"
        return info, tenant

    oauth_token = session.query(OAuthAccessToken).filter_by(token_hash=token_hash).first()
    if oauth_token:
        if oauth_token.expires_at and datetime.utcnow() > oauth_token.expires_at:
            return None, None
        tenant = session.query(Tenant).filter_by(id=oauth_token.effective_tenant_id()).first()
        if tenant and tenant.lifecycle_status != "active":
            return None, None
        oauth_token.last_used_at = datetime.utcnow()
        session.commit()
        info = oauth_token.to_validate_dict()
        info["valid"] = True
        info["tenant_slug"] = tenant.slug if tenant else DEFAULT_TENANT_SLUG
        info["principal_kind"] = "oauth"
        return info, tenant

    return None, None


def _board_stream_token(session: Session, raw: str):
    """Check a raw bearer token for the board stream.

    Returns (status, info): 200 with the token info when it may open the
    stream, 401 when the token is unknown, expired, revoked, or belongs to a
    deactivated user, 403 when it is valid but lacks board_read. Shared by the
    nginx gate (`/api/auth/check`) and the route itself, so the two cannot
    disagree about which tokens get through."""
    if not raw:
        return 401, None
    info, _tenant = _resolve_bearer_token(session, hashlib.sha256(raw.encode()).hexdigest())
    if not info or not info.get("valid"):
        return 401, None
    if info.get("user_id"):
        # A token minted for a person stops with that person. A standalone
        # agent token has no user and is judged on the token alone.
        user = session.query(UserAccount).filter_by(id=info["user_id"]).first()
        if not user or not user.is_active:
            return 401, None
    if not info.get("board_read"):
        return 403, None
    return 200, info


def _is_multi_org_mode() -> bool:
    """True when the deployment hosts many organisations (ELEFANT_MODE=multi-org)."""
    return deployment_mode.is_multi_org()


def _require_multi_org() -> None:
    if not _is_multi_org_mode():
        raise HTTPException(
            status_code=404,
            detail="Platform admin features require ELEFANT_MODE=multi-org",
        )


def _live_session_user(claims: dict, session: Session):
    """The user and org behind a decoded session cookie — or (None, None) if
    that session is no longer live.

    A signed, unexpired cookie is not the same as a valid session, and the two
    places that care used to disagree about it: the auth gate re-checked the
    user row while `/login` looked only at the signature. A cookie that
    decoded but failed the gate then bounced forever — gate 401 → `/login` →
    "you're logged in" → back to the page → 401 — and the operator could not
    even reach the login form to fix it. One test, used by both.

    The four ways a signed cookie stops being a live session:
      * the user row is gone;
      * the user was deactivated (offboarded) — SEC-04 takes effect on the
        next request, not at cookie expiry;
      * the session epoch moved on: "log out all sessions", a password
        change, a role change or deactivation bump `session_epoch`, so an
        older token is refused. Legacy tokens carry no epoch → 0, which
        matches the column default, so they keep working until a bump;
      * the org is no longer active, and the caller is not a platform admin.
    """
    uid = claims.get("uid")
    user = session.query(UserAccount).filter_by(id=uid).first() if uid else None
    if not user or not user.is_active:
        return None, None
    if int(claims.get("sepoch", 0)) != int(user.session_epoch or 0):
        return None, None
    org = session.query(Tenant).filter_by(id=user.org_id).first() if user.org_id else None
    if not (user.role == "platform_admin" or (org and org.lifecycle_status == "active")):
        return None, None
    return user, org
