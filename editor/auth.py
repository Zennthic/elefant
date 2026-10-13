"""
Authentication, sessions, MFA, role guards, and invite flow.
─────────────────────────────────────────────────────────────
Centralises everything that used to live inline in main.py around the legacy
LocalCredential / User tables.  All user records now live in `UserAccount`
with a unified `role` column (platform_admin | org_admin | team_admin | user).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
import pyotp
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from config_db import (
    UserAccount, MfaCredential, InviteToken, Tenant, TeamMembership,
    GroupMembership, Team, Group,
    DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG, crypto, get_db_session,
)

SESSION_COOKIE = "elefant_session"
SESSION_TTL    = 86400          # 24h
MFA_PENDING_COOKIE = "elefant_mfa_pending"
MFA_PENDING_TTL    = 600        # 10 minutes to complete TOTP entry
INVITE_TTL_HOURS   = 72


def _load_session_secret() -> str:
    val = os.environ.get("SESSION_SECRET", "")
    if val:
        return val
    try:
        return Path("/run/secrets/elefant_session_secret").read_text().strip()
    except Exception:
        return ""


SESSION_SECRET = _load_session_secret()


# ── Username / domain helpers ──────────────────────────────────────────────

def parse_login(login: str) -> tuple[str, Optional[str]]:
    """
    Split a login string into (local_part, domain_or_None).
    Accepts either 'alice@acme.com' or just 'alice' (self-hosted single-org).
    """
    login = (login or "").strip()
    if "@" in login:
        local, _, domain = login.partition("@")
        return local.strip(), domain.strip().lower() or None
    return login, None


def lookup_user_by_login(session: Session, login: str) -> Optional[UserAccount]:
    """
    Resolve a login string to a UserAccount.

    1. Exact match on ``full_username`` — covers the canonical case.
    2. Verified-domain resolution — if ``alice@secondary.com`` and
       ``secondary.com`` is a verified ``OrgDomain``, look up the user
       by ``(org_id, username=alice)``.  Allows users to log in from
       any of their org's verified domains.
    3. Bare-username fallback — ``alice`` (no ``@``) maps to the
       default-org user.  Self-hosted single-org convenience only.

    Unverified domains never resolve (defence in depth — even if a row
    is created with an unverified-domain full_username, login is
    refused).
    """
    full = login.strip().lower()
    user = session.query(UserAccount).filter(
        UserAccount.full_username == full,
    ).first()
    if user:
        return user
    local, domain = parse_login(login)
    if domain:
        # Lazy import to avoid auth.py ↔ config_db circular at module load
        from config_db import OrgDomain
        od = session.query(OrgDomain).filter_by(domain=domain, verified=True).first()
        if not od:
            return None
        return session.query(UserAccount).filter_by(
            org_id=od.org_id, username=local,
        ).first()
    # Bare username — look it up against the default org
    default_org = session.query(Tenant).filter_by(id=DEFAULT_TENANT_ID).first()
    if not default_org:
        return None
    return session.query(UserAccount).filter_by(
        org_id=DEFAULT_TENANT_ID, username=local,
    ).first()


def build_full_username(local: str, org: Tenant) -> str:
    """Compose canonical full_username = local@<org domain>."""
    domain = (org.domain or f"{org.slug}.local").lower()
    return f"{local.strip().lower()}@{domain}"


# ── Password hashing ───────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# A real bcrypt hash at the same cost hash_password() uses, never matched by
# anything, so a check against it takes as long as checking a real account.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt()).decode()


def burn_password_check(plain: str) -> None:
    """Spend one bcrypt verify when there is no real hash to check.

    Without it an unknown username (or an IdP-only account) is rejected in
    microseconds while a real one takes a full bcrypt round, and that gap
    tells a caller which usernames exist (SEC-10).
    """
    try:
        bcrypt.checkpw((plain or "x").encode(), _DUMMY_HASH.encode())
    except Exception:
        pass


# ── Session JWT helpers ────────────────────────────────────────────────────

def make_session_token(user: UserAccount) -> str:
    payload = {
        "sub":     user.full_username,
        "uid":     user.id,
        "oid":     user.org_id,
        "role":    user.role,
        # Session-revocation epoch (SEC-04). A token stays valid only while its
        # epoch matches the user row; bumping the row's epoch kills every token
        # minted before it. Absent on legacy tokens → treated as 0 on decode.
        "sepoch":  getattr(user, "session_epoch", 0) or 0,
        "exp":     int(time.time()) + SESSION_TTL,
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> Optional[dict]:
    if not token or not SESSION_SECRET:
        return None
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        if "uid" not in payload:
            return None
        return {
            "sub":    payload.get("sub"),
            "uid":    payload["uid"],
            "oid":    payload.get("oid"),
            "role":   payload.get("role", "user"),
            "sepoch": payload.get("sepoch", 0),
        }
    except Exception:
        return None


def bump_session_epoch(user: UserAccount) -> None:
    """Invalidate all of this user's live session cookies (SEC-04).

    Increments the user's session epoch; the caller is responsible for
    committing. Any session JWT minted before this — carrying the old epoch —
    is rejected by /api/auth/check on its next request. Call on logout,
    password change, role change, and deactivation.
    """
    user.session_epoch = (getattr(user, "session_epoch", 0) or 0) + 1


def make_mfa_pending_token(user_id: str) -> str:
    payload = {
        "uid":   user_id,
        "stage": "mfa",
        "exp":   int(time.time()) + MFA_PENDING_TTL,
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def decode_mfa_pending_token(token: str) -> Optional[str]:
    if not token or not SESSION_SECRET:
        return None
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        if payload.get("stage") != "mfa":
            return None
        return payload.get("uid")
    except Exception:
        return None


# ── MFA helpers ────────────────────────────────────────────────────────────

def mfa_is_required(user: UserAccount, org: Optional[Tenant]) -> bool:
    """
    True when the user may not sign in without an enrolled second factor.

    A platform admin always needs MFA. Everybody else follows their org's
    mfa_policy: 'enforced' requires it, 'optional' leaves it to the user. An
    org or team admin with no org to read a policy from is held to MFA.
    """
    if user.role == "platform_admin":
        return True
    if org is None:
        return user.role in ("org_admin", "team_admin")
    return org.mfa_policy == "enforced"


def mfa_setup_offered(user: UserAccount, org: Optional[Tenant]) -> bool:
    """
    True when a sign-in should show the MFA setup page with a Skip option.

    Org and team admins in an org whose MFA is optional are offered setup on
    their first sign-in, since they are the accounts an org is onboarded with.
    They can skip it, and set it up later from their profile.
    """
    if mfa_is_required(user, org):
        return False
    return (user.role in ("org_admin", "team_admin")
            and user.last_login_at is None)


def get_or_create_mfa_secret(session: Session, user_id: str) -> MfaCredential:
    """Returns the user's MfaCredential, creating or re-keying one as needed.

    If the stored secret can't be decrypted (e.g. after a cross-deployment
    restore where the source crypto.key differs), a fresh secret is generated
    so the setup page always shows a valid QR code.
    """
    cred = session.query(MfaCredential).filter_by(user_id=user_id).first()
    if cred is None:
        secret = pyotp.random_base32()
        cred = MfaCredential(
            user_id=user_id,
            totp_secret_enc=crypto.encrypt(secret),
            is_enabled=False,
        )
        session.add(cred)
        session.commit()
    elif not crypto.decrypt(cred.totp_secret_enc or ""):
        # Secret is present but undecodable (wrong key or corrupt) — regenerate.
        cred.totp_secret_enc = crypto.encrypt(pyotp.random_base32())
        cred.is_enabled = False
        session.commit()
    return cred


def verify_totp(cred: MfaCredential, code: str) -> bool:
    if not cred or not code:
        return False
    secret = crypto.decrypt(cred.totp_secret_enc or "")
    if not secret:
        return False
    try:
        return pyotp.TOTP(secret).verify((code or "").strip(), valid_window=1)
    except Exception:
        return False


def totp_provisioning_uri(cred: MfaCredential, user: UserAccount, app_name: str) -> str:
    secret = crypto.decrypt(cred.totp_secret_enc or "")
    return pyotp.TOTP(secret).provisioning_uri(name=user.full_username, issuer_name=app_name)


# ── Role guards (FastAPI dependencies) ─────────────────────────────────────

class RequiresLogin(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def _claims_from_request(request: Request) -> Optional[dict]:
    return decode_session_token(request.cookies.get(SESSION_COOKIE, ""))


def require_session(request: Request) -> dict:
    """Returns the session claims dict; redirects to /login otherwise."""
    claims = _claims_from_request(request)
    if claims:
        return claims
    raise RequiresLogin(next_url=str(request.url))


def require_role(*allowed_roles: str):
    """Factory: dependency requiring caller's role to be in `allowed_roles`."""
    def _dep(request: Request) -> dict:
        claims = _claims_from_request(request)
        if not claims:
            raise RequiresLogin(next_url=str(request.url))
        if claims["role"] not in allowed_roles:
            raise HTTPException(status_code=403,
                                detail=f"Requires role: {' or '.join(allowed_roles)}")
        return claims
    return _dep


# Convenience dependencies used by route decorators
require_platform_admin = require_role("platform_admin")
require_org_admin      = require_role("platform_admin", "org_admin")
require_team_admin     = require_role("platform_admin", "org_admin", "team_admin")
# Any authenticated user (member tier). The board is member-facing, wiki-tier
# capability (board_read defaults on; boards ride along with the wiki grant),
# so their session-authed routes use this rather than the org-admin gate.
require_authenticated  = require_role("platform_admin", "org_admin", "team_admin", "user")


def get_user(claims: dict, session: Session) -> Optional[UserAccount]:
    return session.query(UserAccount).filter_by(id=claims["uid"]).first()


# ── Invite tokens ──────────────────────────────────────────────────────────

def generate_invite(session: Session, *, org_id: str, target_role: str,
                    created_by: str, target_email: Optional[str] = None,
                    team_id: Optional[str] = None) -> InviteToken:
    """Create a new invite token, persist it, and return it."""
    invite = InviteToken(
        code=secrets.token_urlsafe(8)[:12].upper().replace("-", "X").replace("_", "Y"),
        org_id=org_id,
        target_role=target_role,
        target_email=target_email,
        team_id=team_id,
        created_by=created_by,
        expires_at=datetime.utcnow() + timedelta(hours=INVITE_TTL_HOURS),
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)
    return invite


def consume_invite(session: Session, invite_id: str, code: str) -> Optional[InviteToken]:
    """
    Return the invite if it is unused, unexpired, and the code matches.
    Does NOT mark it as used — the caller does that after creating the user.

    Uses hmac.compare_digest for the code equality so the comparison is
    constant-time — previously a plain '!=' would have leaked the code
    one character at a time via timing if the endpoint were attackable
    without rate-limiting. Defensive, not a clean exploit on its own,
    but the right primitive on a security-relevant secret check.
    """
    import hmac as _hmac
    invite = session.query(InviteToken).filter_by(id=invite_id).first()
    if not invite or invite.used_at is not None:
        return None
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        return None
    presented = (code or "").strip().upper()
    stored    = (invite.code or "").upper()
    if not presented or not _hmac.compare_digest(presented, stored):
        return None
    return invite


# ── Password / MFA reset cooldown ──────────────────────────────────────────

def password_reset_blocked(user: UserAccount, session: Session) -> bool:
    """
    Per spec: password reset and MFA reset cannot both happen within 1 hour.
    Returns True if the most recent MFA reset was less than 1 hour ago.
    """
    cred = session.query(MfaCredential).filter_by(user_id=user.id).first()
    if cred and cred.last_reset_at and (datetime.utcnow() - cred.last_reset_at) < timedelta(hours=1):
        return True
    return False


def mfa_reset_blocked(user: UserAccount) -> bool:
    """True if the password was reset less than 1 hour ago."""
    if user.last_password_reset_at and (datetime.utcnow() - user.last_password_reset_at) < timedelta(hours=1):
        return True
    return False


# ── First-run / bootstrap ──────────────────────────────────────────────────

def needs_first_run(session: Session) -> bool:
    """Returns True when no UserAccount exists yet (fresh install)."""
    return session.query(UserAccount).count() == 0


def cookie_kwargs(request: Request) -> dict:
    """Session-cookie attributes.

    samesite=lax: the browser sends the cookie on same-site requests and
    on top-level cross-site GETs (so SSO/OAuth redirect-back navigations
    keep working) but NOT on cross-site POST/PUT/DELETE. This blocks the
    primary CSRF vector — a malicious site auto-submitting a form to
    /oauth/authorize or /api/me/tokens with the victim's cookie attached
    — at the browser level. The earlier samesite=none policy let those
    cross-site requests carry the cookie, leaving the consent and admin
    endpoints exposed since the editor has no CSRF token in its forms.

    The CSRF middleware in main.py is a defence-in-depth Origin/Referer
    check for state-changing requests so a same-site XSS or a
    browser-implementation quirk (older browser lax-but-actually-none,
    method-override tricks) still has to clear a second gate.
    """
    is_tls = request.headers.get("x-forwarded-proto", "http").lower() == "https"
    return dict(httponly=True, samesite="lax", secure=is_tls)
