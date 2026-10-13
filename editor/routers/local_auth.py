"""
The editor's local (username and password) sign-in routes.

First-run /setup, /login and /logout, the forced password change after an
admin-issued one-time password, TOTP MFA (/mfa and /setup-mfa), the invite
flow (the org admin's /api/admin/invites and the invitee's /invite pages)
and the org admin's MFA reset for a user. main.py includes `router` where
these routes used to be declared, so the route order is unchanged.

Moved out of main.py by CQ-04 phase 7; the paths are unchanged.
"""
import io
import logging
import os
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import quote as _urlquote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import login_throttle
from auth import (
    SESSION_COOKIE, SESSION_TTL, SESSION_SECRET, MFA_PENDING_COOKIE,
    lookup_user_by_login, build_full_username,
    hash_password, verify_password, burn_password_check,
    make_session_token, decode_session_token, bump_session_epoch,
    make_mfa_pending_token, decode_mfa_pending_token,
    mfa_is_required, mfa_setup_offered, get_or_create_mfa_secret, verify_totp, totp_provisioning_uri,
    require_session, require_org_admin, require_team_admin,
    generate_invite, consume_invite, mfa_reset_blocked,
    needs_first_run, cookie_kwargs,
)
from auth_helpers import (
    APP_NAME, _editor_base_url, _request_ip, _render, _SafeHTML, _STATIC,
    _tenant_is_active, _is_multi_org_mode, _live_session_user,
)
from config_db import (
    get_db_session, ConfigEntry, UserAccount, Team, TeamMembership,
    InviteToken, MfaCredential, Tenant, OrgDomain, DEFAULT_TENANT_ID, crypto,
)
from orgs import validate_role_domain

log = logging.getLogger("editor")

router = APIRouter()


def _bootstrap_env_set() -> bool:
    """True when PLATFORM_ADMIN_USERNAME + _PASSWORD env vars are both populated."""
    return bool(
        os.environ.get("PLATFORM_ADMIN_USERNAME", "").strip() and
        os.environ.get("PLATFORM_ADMIN_PASSWORD", "")
    )


def _normalize_username_input(raw: str, allowed_domains: set[str]) -> str:
    """
    Accept either a bare local part or a 'local@domain' string where the
    domain matches one of ``allowed_domains`` (case-insensitive).  Returns
    the bare local part suitable for ``build_full_username`` / DB insert.
    Raises HTTPException(400) on a mismatched or stray domain.

    Fixes a usability trap where users typed their full email into the
    invite/setup form and ended up with ``jon@acme.com@acme.com`` because
    ``build_full_username`` blindly appended the org domain.
    """
    s = (raw or "").strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="Username is required")
    if "@" not in s:
        return s
    local, _, dom = s.partition("@")
    if not local:
        raise HTTPException(status_code=400, detail="Username cannot start with '@'.")
    allowed_lc = {d.lower() for d in allowed_domains if d}
    if not allowed_lc:
        raise HTTPException(status_code=400,
            detail="Enter just the local part of your username, without '@domain'.")
    if dom not in allowed_lc:
        allowed_str = ", ".join(sorted(allowed_lc))
        raise HTTPException(status_code=400, detail=(
            f"The domain '{dom}' is not associated with this organization.  "
            f"Use just '{local}' (no @suffix) or one of: {allowed_str}."
        ))
    return local


def _setup_role_label() -> str:
    """Copy hint shown on the /setup form depending on the deployment mode."""
    return "Platform Admin" if _is_multi_org_mode() else "Organisation Admin"


def _setup_org_domain(session: Session) -> str:
    """The autofill domain shown on /setup.  Empty string in multi-org mode
    (platform_admin has no domain) or when the default org has none."""
    if _is_multi_org_mode():
        return ""
    default_org = session.query(Tenant).filter_by(id=DEFAULT_TENANT_ID).first()
    return (default_org.domain or "") if default_org else ""


def _render_setup(session: Session, error: str = "", status_code: int = 200):
    return HTMLResponse(_render(_STATIC / "setup.html", {
        "<!-- APP_NAME -->":   APP_NAME,
        "<!-- ROLE_LABEL -->": _setup_role_label(),
        "<!-- ORG_DOMAIN -->": _setup_org_domain(session),
        "<!-- ERROR -->":      error,
    }), status_code=status_code)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(session: Session = Depends(get_db_session)):
    if not needs_first_run(session):
        return RedirectResponse("/login", status_code=302)
    # In multi-org mode with bootstrap env vars set, /setup must NOT be reachable —
    # the env vars are the canonical first-admin path.  Surface the bootstrap
    # failure rather than silently letting someone create a different first user.
    if _is_multi_org_mode() and _bootstrap_env_set():
        return _render_setup(session, error=(
            "PLATFORM_ADMIN_USERNAME / _PASSWORD are set but no Platform Admin "
            "exists.  The bootstrap likely failed at startup — check the editor "
            "logs and retry.  /setup is intentionally locked while these env "
            "vars are present."
        ), status_code=503)
    return _render_setup(session)


_FIRST_RUN_CLAIM_KEY = "first_run_setup_claim"


def _claim_first_run(session: Session) -> bool:
    """Take the one-time first-admin slot inside the caller's transaction.

    needs_first_run() alone is check-then-act: two POSTs racing on a fresh
    install both see zero users and both create a first admin. The claim is a
    fixed-key config row, so the database's primary key admits exactly one; a
    concurrent claimant blocks until the winner commits and then fails. The
    caller must commit the claim together with the user it creates (SEC-10).

    Returns False when someone else already set this install up.
    """
    for _ in range(2):
        claim = ConfigEntry(key=_FIRST_RUN_CLAIM_KEY)
        claim.set_value({"claimed_at": datetime.utcnow().isoformat()})
        session.add(claim)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            if not needs_first_run(session):
                return False
            # A claim row with no users behind it is left over from an
            # install whose accounts were all removed; the claim and its user
            # always commit together, so it is not a setup in flight.
            session.query(ConfigEntry).filter_by(key=_FIRST_RUN_CLAIM_KEY).delete()
            session.commit()
            continue
        # Re-check under the claim: the users may have come from elsewhere.
        if needs_first_run(session):
            return True
        session.rollback()
        return False
    return False


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_db_session),
):
    """
    First-run admin bootstrap.  In multi-org mode creates a platform_admin
    (no org affiliation); otherwise creates the org_admin of the default Org.
    """
    if not needs_first_run(session):
        return RedirectResponse("/login", status_code=302)
    if _is_multi_org_mode() and _bootstrap_env_set():
        # See setup_page() — refuse the form rather than plant the wrong user.
        return _render_setup(session,
            error="Bootstrap env vars set; refusing to create a different first user.",
            status_code=503)
    if len(password) < 8:
        return _render_setup(session,
            error="Password must be at least 8 characters.", status_code=400)
    if _is_multi_org_mode():
        # Platform admin has no org affiliation, so any '@domain' the user
        # types is just a stray suffix — reject it with a clear message.
        try:
            local = _normalize_username_input(username, set())
        except HTTPException as exc:
            return _render_setup(session, error=exc.detail, status_code=exc.status_code)
        if not _claim_first_run(session):
            return RedirectResponse("/login", status_code=302)
        user = UserAccount(
            id=secrets.token_hex(16),
            org_id=None,
            username=local,
            full_username=local,
            display_name=local,
            password_hash=hash_password(password),
            role="platform_admin",
            auth_method="local",
            is_active=True,
        )
        session.add(user)
        session.commit()
        log.info("First-run setup created platform_admin: %s", local)
        return RedirectResponse("/login", status_code=302)

    # Single-org: first user is the default org's org_admin.
    default_org = session.query(Tenant).filter_by(id=DEFAULT_TENANT_ID).first()
    if not default_org:
        return _render_setup(session,
            error="Default organisation not initialised. Restart the editor.",
            status_code=500)
    try:
        local = _normalize_username_input(
            username, {default_org.domain} if default_org.domain else set()
        )
    except HTTPException as exc:
        return _render_setup(session, error=exc.detail, status_code=exc.status_code)
    full = build_full_username(local, default_org)
    if not _claim_first_run(session):
        return RedirectResponse("/login", status_code=302)
    user = UserAccount(
        id=secrets.token_hex(16),
        org_id=default_org.id,
        username=local,
        full_username=full,
        display_name=local,
        password_hash=hash_password(password),
        role="org_admin",
        auth_method="local",
        is_active=True,
    )
    session.add(user)
    session.commit()
    return RedirectResponse("/login", status_code=302)


def _safe_next(next_url: str) -> str:
    """Return next_url iff it is a relative path on this host; otherwise '/'.

    The previous next.startswith('/') checks accepted protocol-relative
    URLs like '//evil.com/path', which the browser then resolves to
    https://evil.com/path — a clean open-redirect after login.  Reject
    leading '//' and percent-encoded variants explicitly.
    """
    n = (next_url or "").strip()
    if not n.startswith("/"):
        return "/"
    if n.startswith("//") or n.startswith("/\\") or n.startswith("/%2f") or n.startswith("/%2F"):
        return "/"
    return n


def _login_html(error: str = "", next_url: str = "/", status_code: int = 200) -> HTMLResponse:
    """Render the login page, always substituting the hidden `next` field so the
    post-login destination survives (and the literal placeholder never leaks)."""
    return HTMLResponse(_render(_STATIC / "login.html", {
        "<!-- APP_NAME -->": APP_NAME,
        "<!-- ERROR -->": error,
        "<!-- NEXT -->": _safe_next(next_url),
    }), status_code=status_code)


def _throttled(template_render, retry_after: int) -> HTMLResponse:
    """A 429 for an attempt refused by the login throttle (SEC-08)."""
    minutes = max(1, -(-retry_after // 60))
    resp = template_render(
        f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp


@router.get("/login-redirect")
def login_redirect(request: Request):
    """nginx's 401 handler (@login_redirect) proxies here, passing the original
    request URI in X-Original-URI, so we can build the post-login `next` and
    percent-encode it in Python. nginx can't urlencode a variable, so its old
    `/login?next=$request_uri` truncated a multi-param URL at the first `&` —
    which silently dropped redirect_uri/code_challenge/state on the OAuth
    /oauth/authorize flow and dead-ended the login."""
    original = _safe_next(request.headers.get("X-Original-URI", "") or "/")
    return RedirectResponse(url=f"/login?next={_urlquote(original, safe='')}", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", session: Session = Depends(get_db_session)):
    # First-run: redirect to /setup
    if needs_first_run(session):
        return RedirectResponse("/setup", status_code=302)
    # If already logged in, go straight to destination — but "logged in" has
    # to mean what the auth gate means by it. Trusting the signature alone
    # sent anyone holding a stale-but-signed cookie into an endless bounce
    # between here and the page they asked for, with no way to reach this form
    # and log in again. A cookie the gate would refuse is cleared on the way
    # past, so the browser stops presenting it.
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if claims is not None:
        user, _org = _live_session_user(claims, session)
        if user is not None:
            return RedirectResponse(_safe_next(next), status_code=302)
    # Carry the post-login destination into the form's hidden field so the POST
    # handler can honour it (without this the form submitted the literal
    # "<!-- NEXT -->", _safe_next rejected it, and every login landed on /).
    resp = _login_html(next_url=next)
    if claims is not None:
        resp.delete_cookie(SESSION_COOKIE)
        resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    session: Session = Depends(get_db_session),
):
    """
    Local login.  Accepts 'alice' (resolved against the default Org) or
    'alice@acme.com' (resolved across orgs by domain).  On success, if MFA
    is required the user is bounced to /mfa to enter their TOTP code.
    """
    if not SESSION_SECRET:
        return _login_html("SESSION_SECRET is not configured. Set it in the environment.", next, 500)

    user = lookup_user_by_login(session, username)
    # Throttle before the password is checked, so a locked key is not an
    # oracle. An unknown name is keyed by what was typed, so it locks the same
    # way a real account does and the response says nothing about existence.
    ip = _request_ip(request)
    acct = f"u:{user.id}" if user else f"n:{username.strip().lower()[:320]}"
    wait = login_throttle.throttle.retry_after("login", acct, ip)
    if wait:
        return _throttled(lambda msg: _login_html(msg, next), wait)
    if not user or user.auth_method != "local" or not user.password_hash:
        # Same bcrypt cost as a real check, so response time does not say
        # whether the username exists (SEC-10).
        burn_password_check(password)
        login_throttle.throttle.record_failure("login", acct, ip)
        return _login_html("Invalid username or password.", next, 401)
    if not verify_password(password, user.password_hash):
        login_throttle.throttle.record_failure("login", acct, ip)
        return _login_html("Invalid username or password.", next, 401)
    login_throttle.throttle.record_success("login", acct, ip)
    if not user.is_active:
        return _login_html("This account has been deactivated.", next, 403)
    if user.invite_pending:
        return _login_html("Invite onboarding has not been completed.", next, 403)

    org = session.query(Tenant).filter_by(id=user.org_id).first() if user.org_id else None
    if org and org.lifecycle_status != "active" and user.role != "platform_admin":
        return _login_html("This organization is not active. Contact your administrator.", next, 403)
    dest = _safe_next(next)

    # MFA gate. An enrolled user always confirms a code, whatever the policy.
    cred = session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first()
    if cred is not None or mfa_is_required(user, org) or mfa_setup_offered(user, org):
        # Not enrolled → /setup-mfa (which offers Skip when MFA is optional);
        # enrolled → verify a code at /mfa. Both via a short-lived token.
        page = "/mfa" if cred is not None else "/setup-mfa"
        pending = make_mfa_pending_token(user.id)
        resp = RedirectResponse(url=f"{page}?next={_urlquote(dest, safe='')}", status_code=302)
        resp.set_cookie(MFA_PENDING_COOKIE, pending, max_age=600, **cookie_kwargs(request))
        return resp

    return _finish_login(request, session, user, org, dest)


def _finish_login(request: Request, session: Session, user: UserAccount,
                  org: Optional[Tenant], dest: str) -> RedirectResponse:
    """Issue the full session for a user who has passed every sign-in step."""
    user.last_login_at = datetime.utcnow()
    session.commit()
    token = make_session_token(user)
    # Org-admin first-login → push to setup wizard if their org isn't set up.
    if user.role == "org_admin" and org and not org.setup_complete and dest == "/":
        dest = "/setup-org"
    # Platform admins land on the platform console (the wiki SPA at / is
    # useless to them — they have no org).
    if user.role == "platform_admin" and dest == "/":
        dest = "/platform/admin"
    # Admin set a one-time password → force the user to change it before
    # they reach the destination.
    if user.must_change_password and not dest.startswith("/change-password"):
        dest = f"/change-password?next={_urlquote(dest, safe='')}"
    resp  = RedirectResponse(url=dest, status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, **cookie_kwargs(request))
    return resp


@router.get("/logout")
def logout(request: Request):
    """Log out THIS session: clear the cookie on this device and nothing else.

    That is what a logout button means to a user — signing out on the office
    laptop must not kill the phone. The session JWT is stateless, so a copy of
    the cookie captured before logout stays valid until it expires; ending every
    session is the deliberately separate action below (`/api/me/logout-all`),
    which is what you reach for when a device is lost. A password change and an
    admin deactivation still bump the epoch on their own, because there the
    whole point is to lock out whoever else is holding a session.
    """
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


# ── Forced password change (after admin-issued one-time password) ──────────

@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(
    request: Request,
    next: str = "/",
    _user: dict = Depends(require_session),
    session: Session = Depends(get_db_session),
):
    """Page shown to a user holding an admin-issued one-time password."""
    uid = _user.get("uid") if isinstance(_user, dict) else None
    user = session.query(UserAccount).filter_by(id=uid).first() if uid else None
    if not user:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_render(_STATIC / "change_password.html", {
        "<!-- APP_NAME -->": APP_NAME,
        "<!-- USERNAME -->": user.full_username or user.username or "",
        "<!-- NEXT -->":     _safe_next(next),
        "<!-- ERROR -->":    "",
        "<!-- FORCED -->":   "true" if user.must_change_password else "",
    }))


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    next: str = Form(default="/"),
    _user: dict = Depends(require_session),
    session: Session = Depends(get_db_session),
):
    uid = _user.get("uid") if isinstance(_user, dict) else None
    user = session.query(UserAccount).filter_by(id=uid).first() if uid else None
    if not user:
        return RedirectResponse("/login", status_code=302)

    def _err(msg: str, status: int = 400):
        return HTMLResponse(_render(_STATIC / "change_password.html", {
            "<!-- APP_NAME -->": APP_NAME,
            "<!-- USERNAME -->": user.full_username or user.username or "",
            "<!-- NEXT -->":     _safe_next(next),
            "<!-- ERROR -->":    msg,
            "<!-- FORCED -->":   "true" if user.must_change_password else "",
        }), status_code=status)

    if not user.password_hash or not verify_password(current_password, user.password_hash):
        return _err("Current password is incorrect.", status=401)
    if len(new_password) < 8:
        return _err("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _err("New password and confirmation do not match.")
    if new_password == current_password:
        return _err("New password must be different from current password.")

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.last_password_reset_at = datetime.utcnow()
    bump_session_epoch(user)   # SEC-04: a password change logs out other sessions
    session.commit()

    dest = _safe_next(next)
    return RedirectResponse(url=dest, status_code=302)


# ── MFA (TOTP) ─────────────────────────────────────────────────────────────

def _mfa_pending_user(request: Request, session: Session) -> Optional[UserAccount]:
    uid = decode_mfa_pending_token(request.cookies.get(MFA_PENDING_COOKIE, ""))
    if not uid:
        return None
    return session.query(UserAccount).filter_by(id=uid).first()


@router.get("/mfa", response_class=HTMLResponse)
def mfa_page(request: Request, next: str = "/", session: Session = Depends(get_db_session)):
    """Prompt the user for a 6-digit TOTP code mid-login."""
    user = _mfa_pending_user(request, session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_render(_STATIC / "mfa.html", {
        "<!-- APP_NAME -->": APP_NAME,
        "<!-- USERNAME -->": user.full_username,
        "<!-- NEXT -->":     next,
        "<!-- ERROR -->":    "",
    }))


@router.post("/mfa", response_class=HTMLResponse)
async def mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default="/"),
    session: Session = Depends(get_db_session),
):
    user = _mfa_pending_user(request, session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "platform_admin" and not _tenant_is_active(session, user.org_id):
        return _login_html("This organization is not active. Contact your administrator.", next, 403)
    def _mfa_html(msg: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(_render(_STATIC / "mfa.html", {
            "<!-- APP_NAME -->": APP_NAME,
            "<!-- USERNAME -->": user.full_username,
            "<!-- NEXT -->":     next,
            "<!-- ERROR -->":    msg,
        }), status_code=status)

    # A 6-digit code falls to guessing without a limit; check before verifying.
    ip = _request_ip(request)
    acct = f"u:{user.id}"
    wait = login_throttle.throttle.retry_after("mfa", acct, ip)
    if wait:
        return _throttled(_mfa_html, wait)
    cred = session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first()
    if not cred or not verify_totp(cred, code):
        login_throttle.throttle.record_failure("mfa", acct, ip)
        return _mfa_html("Invalid 6-digit code. Try again.", 401)
    login_throttle.throttle.record_success("mfa", acct, ip)

    cred.last_used_at = datetime.utcnow()
    user.last_login_at = datetime.utcnow()
    session.commit()
    dest = _safe_next(next)
    if user.role == "org_admin" and dest == "/":
        org = session.query(Tenant).filter_by(id=user.org_id).first()
        if org and not org.setup_complete:
            dest = "/setup-org"
    if user.role == "platform_admin" and dest == "/":
        dest = "/platform/admin"
    if user.must_change_password and not dest.startswith("/change-password"):
        dest = f"/change-password?next={_urlquote(dest, safe='')}"
    resp = RedirectResponse(url=dest, status_code=302)
    resp.set_cookie(SESSION_COOKIE, make_session_token(user),
                    max_age=SESSION_TTL, **cookie_kwargs(request))
    resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


@router.get("/setup-mfa", response_class=HTMLResponse)
def setup_mfa_page(request: Request, next: str = "/",
                   session: Session = Depends(get_db_session)):
    """Show QR code + setup key for first-time TOTP enrolment."""
    # Allow either a fully-authenticated user (post-login enrolment from the
    # profile page) or a user mid-login who hasn't enrolled yet.
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    user = None
    if claims:
        user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    full_session = user is not None
    if user is None:
        user = _mfa_pending_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=302)

    # Never re-expose the live TOTP secret for an already-enrolled user. The
    # mid-login "pending" cookie is granted on the password alone, so echoing
    # the stored secret here would let anyone holding the password derive valid
    # codes — collapsing MFA to a single factor. An enrolled user mid-login must
    # enter a code at /mfa; a fully-authenticated one is sent home. Losing a
    # device is recovered through an admin MFA reset, not by re-viewing here.
    already_enrolled = session.query(MfaCredential).filter_by(
        user_id=user.id, is_enabled=True).first() is not None
    if already_enrolled:
        dest = _safe_next(next)
        if full_session:
            return RedirectResponse(dest, status_code=302)
        return RedirectResponse(f"/mfa?next={_urlquote(dest, safe='')}", status_code=302)

    cred = get_or_create_mfa_secret(session, user.id)
    secret = crypto.decrypt(cred.totp_secret_enc or "")
    uri    = totp_provisioning_uri(cred, user, APP_NAME)

    # Render an inline SVG QR code via the qrcode library
    import qrcode  # local import — only needed here
    import qrcode.image.svg as _svg
    img = qrcode.make(uri, image_factory=_svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    # _render escapes every value, so the SVG has to be marked safe or it shows
    # as text. It is our own markup: the URI is drawn as path data, never text.
    # The XML declaration is dropped because it is meaningless inside HTML.
    qr_svg = buf.getvalue().decode()
    if qr_svg.startswith("<?xml"):
        qr_svg = qr_svg[qr_svg.index("?>") + 2:].lstrip()

    # MFA the org leaves optional can be skipped; the page says so and offers
    # the button. Otherwise the hidden block stays hidden.
    org = session.query(Tenant).filter_by(id=user.org_id).first() if user.org_id else None
    skippable = not mfa_is_required(user, org)

    return HTMLResponse(_render(_STATIC / "setup_mfa.html", {
        "<!-- APP_NAME -->":  APP_NAME,
        "<!-- USERNAME -->":  user.full_username,
        "<!-- QR_SVG -->":    _SafeHTML(qr_svg),
        "<!-- SETUP_KEY -->": secret,
        "<!-- NEXT -->":      next,
        "<!-- ERROR -->":     "",
        "<!-- SKIP_HIDDEN -->": "" if skippable else "hidden",
    }))


@router.post("/setup-mfa", response_class=HTMLResponse)
async def setup_mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default="/"),
    session: Session = Depends(get_db_session),
):
    """Confirm enrolment by verifying a code from the freshly-provisioned secret."""
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    user = None
    if claims:
        user = session.query(UserAccount).filter_by(id=claims["uid"]).first()
    if user is None:
        user = _mfa_pending_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=302)

    cred = session.query(MfaCredential).filter_by(user_id=user.id).first()
    if not cred or not verify_totp(cred, code):
        return RedirectResponse(url=f"/setup-mfa?next={_urlquote(next, safe='')}",
                                status_code=302)

    cred.is_enabled = True
    user.last_login_at = datetime.utcnow()
    session.commit()

    dest = _safe_next(next)
    resp = RedirectResponse(url=dest, status_code=302)
    # If the caller is mid-login, finalise the session now.
    if not claims:
        resp.set_cookie(SESSION_COOKIE, make_session_token(user),
                        max_age=SESSION_TTL, **cookie_kwargs(request))
        resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


@router.post("/setup-mfa/skip")
async def setup_mfa_skip(
    request: Request,
    next: str = Form(default="/"),
    session: Session = Depends(get_db_session),
):
    """Carry on without enrolling, where the user's org leaves MFA optional.

    Mid-login this finishes the sign-in the password started; from the profile
    page it just goes back. Where MFA is required it is refused: the user is
    sent back to the setup page, and no session is issued.
    """
    dest = _safe_next(next)
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if claims:
        user, _org = _live_session_user(claims, session)
        if user is not None:
            return RedirectResponse(dest, status_code=302)
    user = _mfa_pending_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    org = session.query(Tenant).filter_by(id=user.org_id).first() if user.org_id else None
    if user.role != "platform_admin" and not _tenant_is_active(session, user.org_id):
        return _login_html("This organization is not active. Contact your administrator.", next, 403)
    if mfa_is_required(user, org):
        return RedirectResponse(f"/setup-mfa?next={_urlquote(dest, safe='')}", status_code=302)
    # An enrolled user cannot skip the code by coming in this way.
    if session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first():
        return RedirectResponse(f"/mfa?next={_urlquote(dest, safe='')}", status_code=302)
    resp = _finish_login(request, session, user, org, dest)
    resp.delete_cookie(MFA_PENDING_COOKIE)
    return resp


# ── Invite flow ────────────────────────────────────────────────────────────

@router.post("/api/admin/invites", status_code=201)
def create_invite(
    body: dict,
    request: Request,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """
    Create a one-time invite URL + code for a new user.  Org admins can issue
    invites at any role <= their own; team admins can only invite plain users.
    """
    target_role = (body.get("role") or "user").strip()
    target_email = (body.get("email") or "").strip() or None
    team_id = (body.get("team_id") or "").strip() or None

    valid_roles = {"user", "team_admin", "org_admin"}
    if target_role not in valid_roles:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(valid_roles)}")
    # team_admin role: can only invite plain users
    if claims["role"] == "team_admin" and target_role != "user":
        raise HTTPException(status_code=403,
                            detail="Team admins can only invite users with role='user'")
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    if team_id:
        team = session.query(Team).filter_by(id=team_id, org_id=org_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="team not found in this org")

    # If the invitee will join via a specific email's domain, validate that
    # domain against the role-domain rule before sealing the invite.  Skip
    # when no email is supplied (the invitee will land on the primary domain
    # by default at consume time).
    if target_email and "@" in target_email:
        invitee_domain = target_email.split("@", 1)[-1].strip().lower()
        org = session.query(Tenant).filter_by(id=org_id).first()
        if org:
            validate_role_domain(target_role, invitee_domain, org, session)

    invite = generate_invite(
        session, org_id=org_id, target_role=target_role,
        created_by=claims["uid"], target_email=target_email, team_id=team_id,
    )
    base = _editor_base_url(request).rsplit("/editor", 1)[0]  # strip /editor prefix
    return {
        **invite.to_dict(),
        "code": invite.code,
        "invite_url": f"{base}/invite/{invite.id}",
    }


@router.get("/api/admin/invites")
def list_invites(
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    org_id = claims.get("oid")
    rows = session.query(InviteToken).filter_by(org_id=org_id).order_by(
        InviteToken.expires_at.desc()
    ).all()
    return {"invites": [r.to_dict() for r in rows]}


@router.delete("/api/admin/invites/{invite_id}")
def revoke_invite(
    invite_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    inv = session.query(InviteToken).filter_by(id=invite_id, org_id=claims.get("oid")).first()
    if not inv:
        raise HTTPException(status_code=404, detail="invite not found")
    if inv.used_at is not None:
        raise HTTPException(status_code=409, detail="invite already used")
    session.delete(inv)
    session.commit()
    return {"ok": True}


@router.get("/invite/{invite_id}", response_class=HTMLResponse)
def invite_landing(
    invite_id: str,
    session: Session = Depends(get_db_session),
):
    """Public landing page for invite onboarding."""
    inv = session.query(InviteToken).filter_by(id=invite_id).first()
    error = ""
    org_name = ""
    org_domain = ""
    full_username = ""
    if not inv:
        error = "This invite is invalid."
    elif inv.used_at is not None:
        error = "This invite has already been used."
    elif inv.expires_at and inv.expires_at < datetime.utcnow():
        error = "This invite has expired."
    else:
        org = session.query(Tenant).filter_by(id=inv.org_id).first()
        org_name = (org.display_name or org.name) if org else ""
        # Primary domain becomes the autofill suffix on the username field.
        # Even though build_full_username always uses primary, the form
        # autofill helps password managers capture the canonical login.
        org_domain = (org.domain or "") if org else ""
        # If the invite is linked to a pre-created UserAccount, surface the
        # admin-assigned full username so the form can display it read-only.
        if inv.used_by:
            pre = session.query(UserAccount).filter_by(id=inv.used_by).first()
            if pre:
                full_username = pre.full_username or ""
    return HTMLResponse(_render(_STATIC / "invite.html", {
        "<!-- APP_NAME -->":     APP_NAME,
        "<!-- ORG_NAME -->":     org_name,
        "<!-- ORG_DOMAIN -->":   org_domain,
        "<!-- INVITE_ID -->":    invite_id,
        "<!-- EMAIL -->":        (inv.target_email or "") if inv else "",
        "<!-- ROLE -->":         (inv.target_role or "") if inv else "",
        "<!-- FULL_USERNAME -->": full_username,
        "<!-- ERROR -->":        error,
    }))


@router.post("/invite/{invite_id}", response_class=HTMLResponse)
async def invite_complete(
    invite_id: str,
    request: Request,
    code: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(default=""),
    username: str = Form(default=""),
    session: Session = Depends(get_db_session),
):
    """Complete an invite — either by activating a pre-created account
    (new flow, used_by set at issuance) or by creating a fresh UserAccount
    (legacy flow, no pre-created row)."""
    def _render_invite(error: str, status_code: int = 400, *,
                       org_obj: Optional[Tenant] = None,
                       pre_user: Optional[UserAccount] = None):
        full_username = pre_user.full_username if pre_user else ""
        return HTMLResponse(_render(_STATIC / "invite.html", {
            "<!-- APP_NAME -->":     APP_NAME,
            "<!-- ORG_NAME -->":     (org_obj.display_name or org_obj.name) if org_obj else "",
            "<!-- ORG_DOMAIN -->":   (org_obj.domain or "") if org_obj else "",
            "<!-- INVITE_ID -->":    invite_id,
            "<!-- EMAIL -->":        (inv.target_email or "") if inv else "",
            "<!-- ROLE -->":         (inv.target_role or "") if inv else "",
            "<!-- FULL_USERNAME -->": full_username,
            "<!-- ERROR -->":        error,
        }), status_code=status_code)

    inv = consume_invite(session, invite_id, code)
    if not inv:
        return _render_invite("Invalid or expired invite.")

    org = session.query(Tenant).filter_by(id=inv.org_id).first()
    if not org:
        raise HTTPException(status_code=500, detail="invite references unknown org")

    if org.lifecycle_status != "active":
        return _render_invite(
            "This organization is no longer accepting new members.",
            status_code=403, org_obj=org,
        )

    if len(password) < 8:
        # Pull the pre-created user (if any) for the re-render
        pre = session.query(UserAccount).filter_by(id=inv.used_by).first() if inv.used_by else None
        return _render_invite("Password must be at least 8 characters.",
                              org_obj=org, pre_user=pre)

    # ── New flow: invite has a pre-created UserAccount linked via used_by ──
    if inv.used_by:
        user = session.query(UserAccount).filter_by(id=inv.used_by).first()
        if not user:
            return _render_invite("Account not found for this invite.",
                                  status_code=500, org_obj=org)
        if not user.invite_pending:
            return _render_invite("This account has already completed onboarding.",
                                  status_code=409, org_obj=org)
        user.password_hash = hash_password(password)
        if display_name.strip():
            user.display_name = display_name.strip()
        user.invite_pending = False
        user.must_change_password = False
        inv.used_at = datetime.utcnow()
        session.commit()
        return RedirectResponse("/login", status_code=302)

    # ── Legacy flow: no pre-created user, create one from the form ──
    org_verified_domains = {d.domain for d in session.query(OrgDomain).filter_by(
        org_id=org.id, verified=True
    ).all()}
    if org.domain:
        org_verified_domains.add(org.domain)
    try:
        local = _normalize_username_input(username, org_verified_domains)
    except HTTPException as exc:
        return _render_invite(exc.detail, status_code=exc.status_code, org_obj=org)
    full = build_full_username(local, org)

    if session.query(UserAccount).filter_by(full_username=full).first():
        return _render_invite("Username already taken in this organisation.",
                              status_code=409, org_obj=org)

    user = UserAccount(
        id=secrets.token_hex(16),
        org_id=org.id,
        username=local,
        full_username=full,
        display_name=display_name.strip() or local,
        email=inv.target_email,
        password_hash=hash_password(password),
        role=inv.target_role,
        auth_method="local",
        is_active=True,
        invite_pending=False,
    )
    session.add(user)
    if inv.team_id:
        session.add(TeamMembership(user_id=user.id, team_id=inv.team_id, role="member"))
    inv.used_at = datetime.utcnow()
    inv.used_by = user.id
    session.commit()

    return RedirectResponse("/login", status_code=302)


@router.post("/api/admin/users/{user_id}/mfa-reset")
def admin_reset_user_mfa(
    user_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Org admin can reset a user's MFA enrolment (e.g. after device loss)."""
    target = session.query(UserAccount).filter_by(id=user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Same-org check (platform admin bypasses)
    if claims["role"] != "platform_admin" and target.org_id != claims.get("oid"):
        raise HTTPException(status_code=403, detail="Cross-org MFA reset is not permitted")
    # Privilege-concentration guard: an org_admin / team_admin must not be
    # able to strip MFA off a peer (or higher) admin and then exploit a
    # stolen password. Only a platform_admin may reset MFA for an
    # admin-tier account. Self-reset is also disallowed here — use the
    # normal /setup-mfa re-enrolment flow instead of the admin path.
    if claims["role"] != "platform_admin":
        if target.id == claims.get("uid"):
            raise HTTPException(status_code=403,
                                detail="Use the MFA setup flow to re-enrol your own device")
        if target.role in ("platform_admin", "org_admin", "team_admin"):
            raise HTTPException(status_code=403,
                                detail="Only a platform admin can reset MFA for an admin account")
    if mfa_reset_blocked(target):
        raise HTTPException(status_code=409,
                            detail="Cannot reset MFA within 1 hour of a password reset")
    cred = session.query(MfaCredential).filter_by(user_id=user_id).first()
    if cred:
        cred.is_enabled = False
        cred.last_reset_at = datetime.utcnow()
        session.commit()
    return {"ok": True}
