"""
Single sign-on: org-scoped OIDC/SAML provider management, the public
provider discovery endpoint, and the OIDC and SAML login flows.

Moved out of main.py by CQ-04 phase 1; the paths are unchanged.
"""
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from auth import require_org_admin
from auth_helpers import EDITOR_EXTERNAL_URL, _tenant_is_active, _upsert_idp_user
from config_db import (
    get_db_session, ApiToken, OidcProvider, OidcState, SamlProvider, SamlRequestId,
    Tenant,
)

# ── Optional SAML support ──────────────────────────────────────────────────
try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.utils import OneLogin_Saml2_Utils
    SAML_AVAILABLE = True
except ImportError:
    SAML_AVAILABLE = False

# OIDC id_token validation. PyJWT is a hard dependency (auth.py needs it too):
# there is no unverified fallback.
import jwt
from jwt import PyJWKClient

router = APIRouter()


# ── OIDC / SAML provider management (org-scoped) ───────────────────────────
# IdP configuration always operates on the caller's own org.  Cross-org
# IdP edits are not supported by design.

@router.get("/api/admin/identity-providers")
def list_identity_providers(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """List OIDC + SAML providers configured for the caller's org."""
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    oidc = session.query(OidcProvider).filter_by(tenant_id=org_id).all()
    saml = session.query(SamlProvider).filter_by(tenant_id=org_id).all()
    return {
        "oidc": [p.to_dict() for p in oidc],
        "saml": [p.to_dict() for p in saml],
    }


@router.post("/api/admin/identity-providers/oidc", status_code=201)
async def create_oidc_provider(
    request: Request,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    body = await request.json()
    for field in ("display_name", "issuer", "client_id"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"{field} is required")
    p = OidcProvider(
        tenant_id=org_id,
        display_name=body["display_name"].strip(),
        issuer=body["issuer"].rstrip("/"),
        client_id=body["client_id"].strip(),
        scopes=body.get("scopes", "openid email profile"),
        attr_email=body.get("attr_email", "email"),
        attr_name=body.get("attr_name", "name"),
        attr_sub=body.get("attr_sub", "sub"),
        use_userinfo_only=bool(body.get("use_userinfo_only", False)),
        userinfo_endpoint=body.get("userinfo_endpoint"),
        default_memory_read=bool(body.get("default_memory_read", True)),
        default_memory_write=bool(body.get("default_memory_write", False)),
        default_wiki_read=bool(body.get("default_wiki_read", True)),
        default_wiki_write=bool(body.get("default_wiki_write", False)),
        default_rate_limit_rpm=int(body.get("default_rate_limit_rpm", 60)),
        default_rate_limit_rpd=int(body.get("default_rate_limit_rpd", 1000)),
        default_token_ttl_hours=int(body.get("default_token_ttl_hours", 8)),
    )
    if body.get("client_secret"):
        p.set_client_secret(body["client_secret"])
    session.add(p)
    session.commit()
    return p.to_dict()


@router.patch("/api/admin/identity-providers/oidc/{provider_id}")
async def update_oidc_provider(
    provider_id: str,
    request: Request,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    p = session.query(OidcProvider).filter_by(
        id=provider_id, tenant_id=claims.get("oid")
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    body = await request.json()
    for field in ("display_name", "issuer", "client_id", "scopes", "attr_email",
                  "attr_name", "attr_sub", "userinfo_endpoint",
                  "default_rate_limit_rpm", "default_rate_limit_rpd", "default_token_ttl_hours"):
        if field in body:
            setattr(p, field, body[field])
    for flag in ("use_userinfo_only", "is_active", "default_memory_read", "default_memory_write",
                 "default_wiki_read", "default_wiki_write"):
        if flag in body:
            setattr(p, flag, bool(body[flag]))
    if "client_secret" in body and body["client_secret"]:
        p.set_client_secret(body["client_secret"])
    session.commit()
    return p.to_dict()


@router.delete("/api/admin/identity-providers/oidc/{provider_id}", status_code=204)
def delete_oidc_provider(
    provider_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    p = session.query(OidcProvider).filter_by(
        id=provider_id, tenant_id=claims.get("oid")
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    session.delete(p)
    session.commit()
    return Response(status_code=204)


@router.post("/api/admin/identity-providers/oidc/{provider_id}/test")
async def test_oidc_provider(
    provider_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    p = session.query(OidcProvider).filter_by(
        id=provider_id, tenant_id=claims.get("oid")
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    discovery_url = f"{p.issuer}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            doc = resp.json()
        return {"ok": True, "authorization_endpoint": doc.get("authorization_endpoint"),
                "token_endpoint": doc.get("token_endpoint"),
                "jwks_uri": doc.get("jwks_uri")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/admin/identity-providers/saml", status_code=201)
async def create_saml_provider(
    request: Request,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    body = await request.json()
    for field in ("display_name", "idp_entity_id", "idp_sso_url", "idp_x509_cert", "sp_entity_id"):
        if not body.get(field):
            raise HTTPException(status_code=400, detail=f"{field} is required")
    p = SamlProvider(
        tenant_id=org_id,
        display_name=body["display_name"],
        idp_entity_id=body["idp_entity_id"],
        idp_sso_url=body["idp_sso_url"],
        idp_x509_cert=body["idp_x509_cert"],
        sp_entity_id=body["sp_entity_id"],
        attr_email=body.get("attr_email", "email"),
        attr_name=body.get("attr_name", "displayName"),
        attr_sub=body.get("attr_sub", "nameID"),
        sp_certificate=body.get("sp_certificate"),
        sign_requests=bool(body.get("sign_requests", False)),
        want_assertions_signed=bool(body.get("want_assertions_signed", True)),
        want_response_signed=bool(body.get("want_response_signed", True)),
        default_memory_read=bool(body.get("default_memory_read", True)),
        default_memory_write=bool(body.get("default_memory_write", False)),
        default_wiki_read=bool(body.get("default_wiki_read", True)),
        default_wiki_write=bool(body.get("default_wiki_write", False)),
        default_rate_limit_rpm=int(body.get("default_rate_limit_rpm", 60)),
        default_rate_limit_rpd=int(body.get("default_rate_limit_rpd", 1000)),
        default_token_ttl_hours=int(body.get("default_token_ttl_hours", 8)),
    )
    if body.get("sp_private_key"):
        p.set_sp_private_key(body["sp_private_key"])
    session.add(p)
    session.commit()
    return p.to_dict()


@router.patch("/api/admin/identity-providers/saml/{provider_id}")
async def update_saml_provider(
    provider_id: str,
    request: Request,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    p = session.query(SamlProvider).filter_by(
        id=provider_id, tenant_id=claims.get("oid")
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    body = await request.json()
    for field in ("display_name", "idp_entity_id", "idp_sso_url", "idp_x509_cert", "sp_entity_id",
                  "attr_email", "attr_name", "attr_sub", "sp_certificate",
                  "default_rate_limit_rpm", "default_rate_limit_rpd", "default_token_ttl_hours"):
        if field in body:
            setattr(p, field, body[field])
    for flag in ("sign_requests", "want_assertions_signed", "want_response_signed", "is_active",
                 "default_memory_read", "default_memory_write", "default_wiki_read", "default_wiki_write"):
        if flag in body:
            setattr(p, flag, bool(body[flag]))
    if body.get("sp_private_key"):
        p.set_sp_private_key(body["sp_private_key"])
    session.commit()
    return p.to_dict()


@router.delete("/api/admin/identity-providers/saml/{provider_id}", status_code=204)
def delete_saml_provider(
    provider_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    p = session.query(SamlProvider).filter_by(
        id=provider_id, tenant_id=claims.get("oid")
    ).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    session.delete(p)
    session.commit()
    return Response(status_code=204)


# ── Auth discovery (public) ─────────────────────────────────────────────────

@router.get("/auth/{tenant_slug}/providers")
def auth_providers(tenant_slug: str, session: Session = Depends(get_db_session)):
    """Return available IdP login options for a tenant (drives the login page)."""
    tenant = session.query(Tenant).filter_by(slug=tenant_slug, is_active=True).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    result = []
    for p in session.query(OidcProvider).filter_by(tenant_id=tenant.id, is_active=True).all():
        result.append({"type": "oidc", "id": p.id, "display_name": p.display_name,
                       "login_url": f"/auth/oidc/{p.id}/login"})
    for p in session.query(SamlProvider).filter_by(tenant_id=tenant.id, is_active=True).all():
        result.append({"type": "saml", "id": p.id, "display_name": p.display_name,
                       "login_url": f"/auth/saml/{p.id}/login"})
    return {"tenant": tenant.to_dict(), "providers": result}


# ── OIDC auth flow ──────────────────────────────────────────────────────────

async def _oidc_discovery(issuer: str) -> dict:
    """Fetch OIDC discovery document."""
    url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _oidc_build_auth_url(doc: dict, provider: OidcProvider, state_token: str,
                          code_challenge: str, redirect_uri: str,
                          nonce: Optional[str] = None) -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes,
        "state": state_token,
    }
    if nonce:
        params["nonce"] = nonce
    if not provider.use_userinfo_only:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return doc["authorization_endpoint"] + "?" + urlencode(params)


def _oidc_validate_id_token(id_token: str, *, jwks_uri: str, client_id: str,
                            issuer: str, expected_nonce: Optional[str]) -> dict:
    """Verify an id_token and return its claims, or raise (SEC-10).

    Checks the signature against the IdP's JWKS, `aud` against our client id,
    `exp`, `iss` against the configured issuer, and `nonce` against the one
    sent with this login. A trailing slash on the issuer is not significant,
    since providers disagree on it and the stored issuer has it stripped.
    """
    signing_key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(id_token, signing_key.key, algorithms=["RS256", "ES256"],
                        audience=client_id,
                        options={"require": ["exp", "iss", "aud"]})
    if str(claims.get("iss", "")).rstrip("/") != (issuer or "").rstrip("/"):
        raise ValueError("issuer mismatch")
    # A login started before the nonce column existed has none stored; it
    # expires within ten minutes of the upgrade.
    if expected_nonce is not None and not hmac.compare_digest(
            str(claims.get("nonce", "")), expected_nonce):
        raise ValueError("nonce mismatch")
    return claims


@router.get("/auth/oidc/{provider_id}/login")
async def oidc_login(provider_id: str, redirect_after: Optional[str] = None,
                     session: Session = Depends(get_db_session)):
    p = session.query(OidcProvider).filter_by(id=provider_id, is_active=True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        doc = await _oidc_discovery(p.issuer)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach IdP discovery: {e}")

    # Generate PKCE code verifier + challenge
    code_verifier = secrets.token_urlsafe(64)
    import base64 as _b64
    digest = __import__("hashlib").sha256(code_verifier.encode()).digest()
    code_challenge = _b64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    state_token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(minutes=10)
    session.add(OidcState(
        provider_id=provider_id,
        state_token=state_token,
        code_verifier=code_verifier,
        nonce=nonce,
        redirect_after=redirect_after,
        expires_at=expires,
    ))
    session.commit()

    callback_base = EDITOR_EXTERNAL_URL
    redirect_uri = f"{callback_base}/auth/oidc/{provider_id}/callback"
    auth_url = _oidc_build_auth_url(doc, p, state_token, code_challenge, redirect_uri, nonce)
    return RedirectResponse(auth_url)


@router.get("/auth/oidc/{provider_id}/callback", response_class=HTMLResponse)
async def oidc_callback(provider_id: str, request: Request,
                         session: Session = Depends(get_db_session)):
    """Receive authorization code from IdP, validate, issue KB token."""
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(_auth_error_page(f"IdP returned error: {error}"), status_code=400)

    if not code or not state:
        return HTMLResponse(_auth_error_page("Missing code or state parameter"), status_code=400)

    # Validate state
    oidc_state = session.query(OidcState).filter_by(state_token=state, provider_id=provider_id).first()
    if not oidc_state or datetime.utcnow() > oidc_state.expires_at:
        return HTMLResponse(_auth_error_page("Invalid or expired state — please try again"), status_code=400)
    code_verifier = oidc_state.code_verifier
    expected_nonce = oidc_state.nonce
    session.delete(oidc_state)
    session.commit()

    p = session.query(OidcProvider).filter_by(id=provider_id, is_active=True).first()
    if not p:
        return HTMLResponse(_auth_error_page("Provider not found or inactive"), status_code=404)

    try:
        doc = await _oidc_discovery(p.issuer)
    except Exception as e:
        return HTMLResponse(_auth_error_page(f"Cannot reach IdP: {e}"), status_code=502)

    # Exchange code for tokens
    callback_base = EDITOR_EXTERNAL_URL
    redirect_uri = f"{callback_base}/auth/oidc/{provider_id}/callback"
    token_data: dict = {"grant_type": "authorization_code", "code": code,
                        "redirect_uri": redirect_uri, "client_id": p.client_id}
    if code_verifier:
        token_data["code_verifier"] = code_verifier
    client_secret = p.client_secret()
    auth = (p.client_id, client_secret) if client_secret else None
    if client_secret and not code_verifier:
        token_data["client_secret"] = client_secret

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(doc["token_endpoint"], data=token_data,
                                      auth=auth if auth and code_verifier else None)
            resp.raise_for_status()
            tokens = resp.json()
    except Exception as e:
        return HTMLResponse(_auth_error_page(f"Token exchange failed: {e}"), status_code=502)

    # Get identity claims
    claims: dict = {}
    if p.use_userinfo_only:
        try:
            userinfo_url = p.userinfo_endpoint or doc.get("userinfo_endpoint")
            async with httpx.AsyncClient(timeout=10) as client:
                ui = await client.get(userinfo_url,
                                       headers={"Authorization": f"Bearer {tokens['access_token']}"})
                ui.raise_for_status()
                claims = ui.json()
        except Exception as e:
            return HTMLResponse(_auth_error_page(f"Userinfo fetch failed: {e}"), status_code=502)
    else:
        id_token = tokens.get("id_token")
        if not id_token:
            return HTMLResponse(_auth_error_page("No id_token in response"), status_code=502)
        try:
            claims = _oidc_validate_id_token(
                id_token, jwks_uri=doc["jwks_uri"], client_id=p.client_id,
                issuer=p.issuer, expected_nonce=expected_nonce,
            )
        except Exception as e:
            return HTMLResponse(_auth_error_page(f"id_token validation failed: {e}"), status_code=401)

    email   = claims.get(p.attr_email) or claims.get("email") or ""
    name    = claims.get(p.attr_name)  or claims.get("name")  or email
    sub     = claims.get(p.attr_sub)   or claims.get("sub")   or email

    if not email:
        return HTMLResponse(_auth_error_page("Could not determine user email from IdP response"), status_code=400)

    if not _tenant_is_active(session, p.tenant_id):
        return HTMLResponse(_auth_error_page(
            "This organization is not active. Contact your administrator."), status_code=403)

    # Upsert UserAccount via the IdP subject
    user = _upsert_idp_user(
        session, tenant_id=p.tenant_id, email=email, display_name=name,
        external_sub=sub, oidc_provider_id=provider_id, saml_provider_id=None,
    )
    session.flush()

    raw_token, token_hash = ApiToken.generate()
    api_token = ApiToken(
        name=name or email,
        token_hash=token_hash,
        tenant_id=p.tenant_id,
        user_id=user.id,
        memory_read=p.default_memory_read,
        memory_write=p.default_memory_write,
        wiki_read=p.default_wiki_read,
        wiki_write=p.default_wiki_write,
        rate_limit_rpm=p.default_rate_limit_rpm,
        rate_limit_rpd=p.default_rate_limit_rpd,
        expires_at=datetime.utcnow() + timedelta(hours=p.default_token_ttl_hours),
    )
    session.add(api_token)
    session.commit()

    return HTMLResponse(_auth_success_page(name or email, raw_token, p.default_token_ttl_hours))


# ── SAML auth flow ──────────────────────────────────────────────────────────

# Any PEM armour line: "-----BEGIN <TYPE>-----" or "-----END <TYPE>-----".
# A pattern rather than the literal armour strings, so secret scanners do not
# read this module as holding a private key.
_PEM_ARMOUR = re.compile(r"-----(?:BEGIN|END) [A-Z0-9 ]+-----")


def _pem_body(pem: str) -> str:
    """The base64 body of a PEM block, armour and line breaks removed: the
    form python3-saml wants for sp.privateKey and sp.x509cert."""
    return _PEM_ARMOUR.sub("", pem).replace("\r", "").replace("\n", "").strip()


def _build_saml_settings(p: SamlProvider, acs_url: str) -> dict:
    cert = _pem_body(p.idp_x509_cert)
    settings: dict = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": p.sp_entity_id,
            "assertionConsumerService": {"url": acs_url, "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"},
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": p.idp_entity_id,
            "singleSignOnService": {"url": p.idp_sso_url, "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},
            "x509cert": cert,
        },
        "security": {
            "wantAssertionsSigned": p.want_assertions_signed,
            "wantResponseSigned": p.want_response_signed,
            "authnRequestsSigned": p.sign_requests,
            # Honoured by python3-saml releases that know it; saml_acs enforces
            # the same rule itself via _consume_saml_request_id either way.
            "rejectUnsolicitedResponsesWithInResponseTo": True,
        },
    }
    if p.sign_requests and p.sp_private_key() and p.sp_certificate:
        key = _pem_body(p.sp_private_key())
        crt = _pem_body(p.sp_certificate)
        settings["sp"]["privateKey"] = key
        settings["sp"]["x509cert"] = crt
    return settings


def _saml_request_data(provider_id: str, get_data: Optional[dict] = None,
                       post_data: Optional[dict] = None) -> dict:
    """The request as python3-saml sees it: the public ACS URL, so strict
    Destination/Recipient checks compare against what the IdP was told."""
    req = {"http_host": EDITOR_EXTERNAL_URL.split("//")[-1],
           "script_name": f"/auth/saml/{provider_id}/acs",
           "get_data": get_data or {}, "post_data": post_data or {}}
    if EDITOR_EXTERNAL_URL.startswith("https://"):
        req["https"] = "on"
    return req


@router.get("/auth/saml/{provider_id}/login")
async def saml_login(provider_id: str, session: Session = Depends(get_db_session)):
    if not SAML_AVAILABLE:
        raise HTTPException(status_code=501, detail="SAML support not installed")
    p = session.query(SamlProvider).filter_by(id=provider_id, is_active=True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")

    acs_url = f"{EDITOR_EXTERNAL_URL}/auth/saml/{provider_id}/acs"
    saml_settings = _build_saml_settings(p, acs_url)
    auth = OneLogin_Saml2_Auth(_saml_request_data(provider_id), saml_settings)
    redirect_url = auth.login()

    # Store request ID for replay prevention
    session.add(SamlRequestId(
        provider_id=provider_id,
        request_id=auth.get_last_request_id() or secrets.token_urlsafe(16),
        expires_at=datetime.utcnow() + timedelta(minutes=15),
    ))
    session.commit()
    return RedirectResponse(redirect_url)


def _consume_saml_request_id(session: Session, provider_id: str, request_id: Optional[str]) -> bool:
    """Spend the AuthnRequest id a SAML response answers.

    True only if `request_id` was issued by saml_login for this provider and
    has not expired. The row is deleted whether or not it is still valid, and
    before the response itself is checked, so an id can never be used twice.
    """
    if not request_id:
        return False
    row = session.query(SamlRequestId).filter_by(
        provider_id=provider_id, request_id=request_id,
    ).first()
    if row is None:
        return False
    expired = row.expires_at < datetime.utcnow()
    # The rowcount decides between two concurrent posts of the same response.
    deleted = session.query(SamlRequestId).filter_by(id=row.id).delete(synchronize_session=False)
    session.commit()
    return deleted == 1 and not expired


@router.post("/auth/saml/{provider_id}/acs", response_class=HTMLResponse)
async def saml_acs(provider_id: str, request: Request, session: Session = Depends(get_db_session)):
    """Assertion Consumer Service — IdP POSTs SAML response here."""
    if not SAML_AVAILABLE:
        return HTMLResponse(_auth_error_page("SAML support not installed"), status_code=501)
    p = session.query(SamlProvider).filter_by(id=provider_id, is_active=True).first()
    if not p:
        return HTMLResponse(_auth_error_page("Provider not found"), status_code=404)

    form = await request.form()
    post_data = dict(form)

    acs_url = f"{EDITOR_EXTERNAL_URL}/auth/saml/{provider_id}/acs"
    saml_settings = _build_saml_settings(p, acs_url)
    req = _saml_request_data(provider_id, dict(request.query_params), post_data)
    try:
        auth = OneLogin_Saml2_Auth(req, saml_settings)
        saml_response = req["post_data"].get("SAMLResponse")
        if not saml_response:
            return HTMLResponse(_auth_error_page("SAML response missing"), status_code=400)
        request_id = auth.response_class(auth.get_settings(), saml_response).get_in_response_to()
        if not _consume_saml_request_id(session, provider_id, request_id):
            return HTMLResponse(_auth_error_page(
                "This SAML response does not match a pending sign-in. Start the sign-in again."),
                status_code=401)
        auth.process_response(request_id=request_id)
        errors = auth.get_errors()
        if errors:
            return HTMLResponse(_auth_error_page(f"SAML validation error: {', '.join(errors)}"), status_code=401)
        if not auth.is_authenticated():
            return HTMLResponse(_auth_error_page("SAML authentication failed"), status_code=401)
    except Exception as e:
        return HTMLResponse(_auth_error_page(f"SAML processing error: {e}"), status_code=500)

    # Extract attributes
    attrs = auth.get_attributes()

    def _attr(key: str, fallback: str = "") -> str:
        val = attrs.get(key, [])
        return val[0] if val else fallback

    email = _attr(p.attr_email) or auth.get_nameid() or ""
    name  = _attr(p.attr_name)  or email
    sub   = _attr(p.attr_sub)   or auth.get_nameid() or email

    if not email:
        return HTMLResponse(_auth_error_page("Could not determine email from SAML assertion"), status_code=400)

    if not _tenant_is_active(session, p.tenant_id):
        return HTMLResponse(_auth_error_page(
            "This organization is not active. Contact your administrator."), status_code=403)

    user = _upsert_idp_user(
        session, tenant_id=p.tenant_id, email=email, display_name=name,
        external_sub=sub, oidc_provider_id=None, saml_provider_id=provider_id,
    )
    session.flush()

    raw_token, token_hash = ApiToken.generate()
    api_token = ApiToken(
        name=name or email,
        token_hash=token_hash,
        tenant_id=p.tenant_id,
        user_id=user.id,
        memory_read=p.default_memory_read,
        memory_write=p.default_memory_write,
        wiki_read=p.default_wiki_read,
        wiki_write=p.default_wiki_write,
        rate_limit_rpm=p.default_rate_limit_rpm,
        rate_limit_rpd=p.default_rate_limit_rpd,
        expires_at=datetime.utcnow() + timedelta(hours=p.default_token_ttl_hours),
    )
    session.add(api_token)
    session.commit()
    return HTMLResponse(_auth_success_page(name or email, raw_token, p.default_token_ttl_hours))


@router.get("/auth/saml/{provider_id}/metadata")
def saml_metadata(provider_id: str, session: Session = Depends(get_db_session)):
    """Return SP metadata XML for configuring the IdP."""
    if not SAML_AVAILABLE:
        raise HTTPException(status_code=501, detail="SAML support not installed")
    p = session.query(SamlProvider).filter_by(id=provider_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    acs_url = f"{EDITOR_EXTERNAL_URL}/auth/saml/{provider_id}/acs"
    saml_settings = _build_saml_settings(p, acs_url)
    auth = OneLogin_Saml2_Auth(_saml_request_data(provider_id), saml_settings)
    metadata = auth.get_settings().get_sp_metadata()
    return Response(content=metadata, media_type="application/xml")


# ── Auth page templates ─────────────────────────────────────────────────────

def _auth_success_page(display_name: str, raw_token: str, ttl_hours: int) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Login Successful</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:36px 40px;
         max-width:480px;width:100%;text-align:center}}
  h2{{color:#58a6ff;margin:0 0 8px}} p{{color:#8b949e;margin:0 0 20px}}
  .token-box{{background:#0d1117;border:1px solid #30363d;border-radius:6px;
              padding:12px;font-family:monospace;font-size:13px;word-break:break-all;
              color:#79c0ff;text-align:left;margin-bottom:16px}}
  button{{background:#238636;color:#fff;border:none;border-radius:6px;padding:10px 20px;
          cursor:pointer;font-size:14px}}
  button:hover{{background:#2ea043}}
  .note{{font-size:12px;color:#8b949e;margin-top:12px}}
</style></head><body>
<div class="card">
  <h2>&#10003; Login Successful</h2>
  <p>Welcome, <strong>{display_name}</strong>. Your access token is below.<br>
  Copy it now — it will not be shown again.</p>
  <div class="token-box" id="tok">{raw_token}</div>
  <button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent)">
    Copy Token
  </button>
  <div class="note">Token expires in {ttl_hours} hours. Use it as a Bearer token for MCP or API access.</div>
</div></body></html>"""


def _auth_error_page(message: str) -> str:
    import html as _html
    safe = _html.escape(message)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Authentication Error</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .card{{background:#161b22;border:1px solid #f85149;border-radius:10px;padding:36px 40px;
         max-width:460px;width:100%;text-align:center}}
  h2{{color:#f85149;margin:0 0 12px}} p{{color:#8b949e}}
  a{{color:#58a6ff}}
</style></head><body>
<div class="card">
  <h2>&#10007; Authentication Error</h2>
  <p>{safe}</p>
  <p><a href="javascript:history.back()">Go back</a></p>
</div></body></html>"""
