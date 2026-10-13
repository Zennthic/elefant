"""
The editor's org admin web API.

`router` is the org admin console: local users, teams, groups, user and
group membership, memory sharing policies, and the wiki space and git
source ACLs. `settings_router` is tenant settings (the admin form and the
peer endpoints the MCP server and memory-api read), memory edit
notifications and the org setup wizard. main.py includes each one where
its routes used to be declared, so the route order is unchanged.

Moved out of main.py by CQ-04 phase 5; the paths are unchanged.
"""
import secrets
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from acl import auth_context_from_request
from auth import (
    SESSION_COOKIE, decode_session_token, bump_session_epoch, hash_password,
    generate_invite, password_reset_blocked, require_org_admin, require_team_admin,
)
from auth_helpers import APP_NAME, _STATIC, _editor_base_url, _effective_tenant_slug, _render
from config_db import (
    get_db_session, ApiToken, OAuthAccessToken,
    UserAccount, Team, Group, TeamMembership, GroupMembership, GroupTeam,
    InviteToken, MfaCredential,
    WikiSpaceACL, GitSourceACL, MemorySharingPolicy, MemoryEditNotification,
    Tenant, OrgDomain, TenantSettings,
)
from orgs import _slugify, validate_role_domain
from wiki_storage import (
    AI_WIKI_FOLDER_DEFAULT, _org_spaces_from_s3, _resolve_ai_wiki_folder, _sanitize_ai_folder,
)

router = APIRouter()
settings_router = APIRouter()


# ── Local user management (admin only) ───────────────────────────────────────

class UserCreateRequest(BaseModel):
    # Two-part username: local part + verified org domain
    local_part:    str
    domain:        str
    display_name:  Optional[str] = None
    # How to credential the new account
    creation_mode: str = "set_password"        # set_password | generate_password | invite
    password:      Optional[str] = None        # required only when creation_mode=set_password
    # Default True to match the UserAccount.can_oauth column default and the
    # invite/SSO/signup creation paths — a member added by an admin should be
    # able to connect an MCP client by default, not silently blocked from OAuth
    # (issue #87).
    can_oauth:     bool = True
    team_id:       Optional[str] = None
    # Role on create is always "user". Promotion requires login + MFA.

class UserUpdateRequest(BaseModel):
    password:  Optional[str]  = None
    role:      Optional[str]  = None
    can_oauth: Optional[bool] = None
    is_active: Optional[bool] = None
    domain:    Optional[str]  = None           # change which verified domain owns this login


_ROLES = {"platform_admin", "org_admin", "team_admin", "user"}
_ADMIN_ROLES = {"org_admin", "team_admin"}


def _team_admin_team_ids(user_id: str, session: Session) -> list[str]:
    """Team IDs where the given user is a team_admin membership."""
    rows = session.query(TeamMembership).filter_by(
        user_id=user_id, role="team_admin"
    ).all()
    return [m.team_id for m in rows]


def _users_query_for(claims: dict, session: Session):
    """Filter the UserAccount query by the caller's org / team context."""
    q = session.query(UserAccount)
    role = claims.get("role")
    if role == "platform_admin":
        return q.order_by(UserAccount.created_at)
    q = q.filter(UserAccount.org_id == claims.get("oid"))
    if role == "team_admin":
        # Team admins can only see users belonging to one of their teams,
        # plus themselves.  Other team admins / org admins are invisible.
        team_ids = _team_admin_team_ids(claims.get("uid") or "", session)
        if not team_ids:
            q = q.filter(UserAccount.id == (claims.get("uid") or ""))
        else:
            member_ids = {
                m.user_id for m in session.query(TeamMembership).filter(
                    TeamMembership.team_id.in_(team_ids)
                ).all()
            }
            member_ids.add(claims.get("uid") or "")
            q = q.filter(UserAccount.id.in_(member_ids))
    return q.order_by(UserAccount.created_at)


def _user_with_mfa_dict(user: UserAccount, session: Session) -> dict:
    """Serialise a UserAccount and indicate whether MFA is enabled."""
    mfa = session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first()
    d = user.to_dict()
    d["mfa_enabled"] = bool(mfa)
    return d


def _invalidate_user_logins(user: UserAccount, session: Session) -> dict:
    """Revoke persistent login artifacts for a single user.

    Used when an admin rotates a user's domain (the user's full_username login
    identifier changes, so old sessions / OAuth tokens must die).  Session
    cookies are stateless JWTs, so they are revoked by bumping the user's
    session epoch (SEC-04); /api/auth/check rejects any cookie carrying the
    stale epoch on its next request.  API and OAuth tokens are deleted outright.
    """
    bump_session_epoch(user)
    api_tokens = session.query(ApiToken).filter_by(
        user_id=user.id
    ).delete(synchronize_session=False)
    oauth_tokens = session.query(OAuthAccessToken).filter_by(
        user_id=user.id
    ).delete(synchronize_session=False)
    return {"api_tokens": api_tokens, "oauth_access_tokens": oauth_tokens}


def _generate_random_password(length: int = 16) -> str:
    """A reasonably-strong human-typeable password using URL-safe alphabet."""
    return secrets.token_urlsafe(length)[:length]


def _attach_invite_status(user: UserAccount, base: str, session: Session,
                          include_code: bool = False) -> dict:
    """Build the user list payload entry, enriched with invite status.

    invite_code is only included when include_code=True (e.g. the admin
    who just created the invite is reading their own creation response).
    The /api/admin/users list view used to return invite_code for every
    pending user in the org, which let any team_admin in the org take
    over any pending user's onboarding by walking to the invite URL with
    that code. Now the list endpoint hides the code; admins who need to
    surface it again must use a dedicated reveal endpoint that requires
    org_admin and audits the access.
    """
    d = _user_with_mfa_dict(user, session)
    if user.invite_pending:
        inv = session.query(InviteToken).filter(
            InviteToken.used_by == user.id,
            InviteToken.used_at.is_(None),
        ).order_by(InviteToken.expires_at.desc()).first()
        now = datetime.utcnow()
        if inv and inv.expires_at and inv.expires_at > now:
            d["invite_status"] = "pending"
            d["invite_url"] = f"{base}/invite/{inv.id}"
            if include_code:
                d["invite_code"] = inv.code
            d["invite_expires_at"] = inv.expires_at.isoformat()
            d["invite_token_id"] = inv.id
        else:
            d["invite_status"] = "expired"
            d["invite_token_id"] = inv.id if inv else None
    else:
        d["invite_status"] = None
    return d


@router.get("/api/admin/users")
def list_users(
    request: Request,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    users = _users_query_for(claims, session).all()
    base = _editor_base_url(request).rsplit("/editor", 1)[0]
    return {"users": [_attach_invite_status(u, base, session) for u in users]}


@router.post("/api/admin/users", status_code=201)
def create_user(
    body: UserCreateRequest,
    request: Request,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """
    Create a user.  One of three creation modes:
      - set_password:      admin types a password the user knows
      - generate_password: server generates a one-time password, returned once,
                           user is forced to change it on first login
      - invite:            no password yet; an invite link + code is returned,
                           user completes onboarding via /invite/{id}

    Role is always set to 'user' on creation.  Promotion to team_admin or
    org_admin requires the user to have logged in at least once and enrolled
    MFA, and is done later via PATCH /api/admin/users/{id}.
    """
    creation_mode = (body.creation_mode or "set_password").strip()
    if creation_mode not in {"set_password", "generate_password", "invite"}:
        raise HTTPException(status_code=400,
                            detail="creation_mode must be one of: set_password, generate_password, invite")

    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="caller has no org context")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=500, detail="org not found")

    # Validate local part + domain
    local_part = (body.local_part or "").strip().lower()
    if not local_part or "@" in local_part or "/" in local_part or " " in local_part:
        raise HTTPException(status_code=400,
                            detail="local_part must be the part of the email before the @, with no spaces")
    domain = (body.domain or "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain is required")
    # Domain must be a verified domain on this org
    org_verified_domains = {d.domain for d in session.query(OrgDomain).filter_by(
        org_id=org.id, verified=True
    ).all()}
    if org.domain:
        org_verified_domains.add(org.domain)
    if domain not in org_verified_domains:
        raise HTTPException(status_code=400,
                            detail=f"'{domain}' is not a verified domain for this org")

    full = f"{local_part}@{domain}"
    if session.query(UserAccount).filter_by(full_username=full).first():
        raise HTTPException(status_code=409, detail=f"Username '{full}' already exists")

    # Team scope: team_admin callers can only create users in one of their teams
    team_id = (body.team_id or "").strip() or None
    if team_id:
        team = session.query(Team).filter_by(id=team_id, org_id=org_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="team not found in this org")
    if claims.get("role") == "team_admin":
        admin_teams = _team_admin_team_ids(claims.get("uid") or "", session)
        if not team_id:
            raise HTTPException(status_code=400,
                                detail="Team admins must assign the new user to one of their teams")
        if team_id not in admin_teams:
            raise HTTPException(status_code=403,
                                detail="You can only add users to teams you administer")

    # Build account skeleton — role pinned to "user" regardless of input
    display = (body.display_name or "").strip() or local_part
    user = UserAccount(
        id=secrets.token_hex(16),
        org_id=org_id,
        username=local_part,
        full_username=full,
        display_name=display,
        role="user",
        auth_method="local",
        is_active=True,
        can_oauth=bool(body.can_oauth),
    )

    generated_password: Optional[str] = None
    invite_url: Optional[str] = None
    invite_code: Optional[str] = None

    if creation_mode == "set_password":
        if not body.password or len(body.password) < 8:
            raise HTTPException(status_code=400,
                                detail="Password must be at least 8 characters")
        user.password_hash = hash_password(body.password)
        user.invite_pending = False
    elif creation_mode == "generate_password":
        generated_password = _generate_random_password(16)
        user.password_hash = hash_password(generated_password)
        user.must_change_password = True
        user.invite_pending = False
    elif creation_mode == "invite":
        # No password yet — user will set it via /invite/{id}
        user.password_hash = None
        user.invite_pending = True

    session.add(user)
    # Team membership at creation time (regardless of mode)
    if team_id:
        session.add(TeamMembership(user_id=user.id, team_id=team_id, role="member"))
    session.flush()

    if creation_mode == "invite":
        invite = generate_invite(
            session, org_id=org_id, target_role="user",
            created_by=claims["uid"], target_email=None, team_id=team_id,
        )
        invite.used_by = user.id  # link the pre-created user
        session.add(invite)
        base = _editor_base_url(request).rsplit("/editor", 1)[0]
        invite_url = f"{base}/invite/{invite.id}"
        invite_code = invite.code

    session.commit()
    session.refresh(user)

    base = _editor_base_url(request).rsplit("/editor", 1)[0]
    resp = _attach_invite_status(user, base, session)
    if generated_password:
        resp["generated_password"] = generated_password
    if invite_url:
        resp["invite_url"] = invite_url
        resp["invite_code"] = invite_code
    return resp


def _can_promote(user: UserAccount, session: Session) -> tuple[bool, str]:
    """Check whether a user is eligible for promotion to team/org admin.

    Returns (eligible, reason). Eligibility requires that the user has logged
    in at least once AND has MFA enrolled.  If MFA enrolment is enforced for
    the user's role-target by org policy, the check is on the actual
    MfaCredential row; otherwise we still require it for any admin tier.
    """
    if not user.last_login_at:
        return False, "User has not logged in yet — cannot promote to admin role"
    mfa = session.query(MfaCredential).filter_by(user_id=user.id, is_enabled=True).first()
    if not mfa:
        return False, "User has not enrolled MFA — cannot promote to admin role"
    return True, ""


@router.patch("/api/admin/users/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdateRequest,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    user = _users_query_for(claims, session).filter(UserAccount.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    is_self = user.id == claims.get("uid")
    caller_role = claims.get("role")

    # Team admins can only mutate role=user rows that they administer.
    # Promotion / demotion of other admins is org_admin/platform_admin only.
    if caller_role == "team_admin":
        if user.role in _ADMIN_ROLES and not is_self:
            raise HTTPException(status_code=403,
                                detail="Team admins cannot modify other admins")
        if body.role and body.role in _ADMIN_ROLES and body.role != user.role:
            raise HTTPException(status_code=403,
                                detail="Team admins cannot promote users to admin roles")
        if body.domain is not None:
            raise HTTPException(status_code=403,
                                detail="Only org admins can change a user's domain")

    if body.role is not None:
        if body.role not in _ROLES or body.role == "platform_admin":
            raise HTTPException(status_code=400, detail="invalid role")
        if is_self and body.role != user.role:
            raise HTTPException(status_code=400, detail="Cannot change your own role")
        # Promotion gate: enforce positive control before granting admin role
        if body.role in _ADMIN_ROLES and user.role not in _ADMIN_ROLES:
            ok, reason = _can_promote(user, session)
            if not ok:
                raise HTTPException(status_code=400, detail=reason)
        if body.role == "org_admin":
            # org_admin must live on the primary domain.  Block role-up if not.
            org = session.query(Tenant).filter_by(id=user.org_id).first()
            user_domain = (user.full_username or "").split("@", 1)[-1]
            validate_role_domain("org_admin", user_domain, org, session)
        user.role = body.role
    if body.password is not None:
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        if password_reset_blocked(user, session):
            raise HTTPException(status_code=409,
                detail="Cannot reset password within 1 hour of an MFA reset")
        user.password_hash = hash_password(body.password)
        user.last_password_reset_at = datetime.utcnow()
        # If admin set a new password for the user, treat it as one-time
        # again unless the admin is the user themselves.
        if not is_self:
            user.must_change_password = True
    if body.can_oauth is not None:
        user.can_oauth = body.can_oauth
    if body.is_active is not None:
        if is_self and not body.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
        user.is_active = body.is_active
    if body.domain is not None:
        new_domain = (body.domain or "").strip().lower()
        if not new_domain:
            raise HTTPException(status_code=400, detail="domain cannot be empty")
        org = session.query(Tenant).filter_by(id=user.org_id).first()
        org_verified_domains = {d.domain for d in session.query(OrgDomain).filter_by(
            org_id=user.org_id, verified=True
        ).all()}
        if org and org.domain:
            org_verified_domains.add(org.domain)
        if new_domain not in org_verified_domains:
            raise HTTPException(status_code=400,
                                detail=f"'{new_domain}' is not a verified domain for this org")
        # org_admin must live on the primary domain regardless of request
        if user.role == "org_admin":
            validate_role_domain("org_admin", new_domain, org, session)
        new_full = f"{user.username}@{new_domain}"
        if new_full != user.full_username:
            clash = session.query(UserAccount).filter(
                UserAccount.full_username == new_full,
                UserAccount.id != user.id,
            ).first()
            if clash:
                raise HTTPException(status_code=409,
                                    detail=f"Username '{new_full}' already exists")
            user.full_username = new_full
            _invalidate_user_logins(user, session)
    # A role change (demotion), password reset, or deactivation by an admin must
    # take effect on the target's live sessions immediately, not at cookie
    # expiry (SEC-04). Bump the epoch when any of those fields was supplied.
    if any(v is not None for v in (body.role, body.password, body.is_active)):
        bump_session_epoch(user)
    session.commit()
    session.refresh(user)
    return _user_with_mfa_dict(user, session)


@router.post("/api/admin/users/{user_id}/resend-invite")
def resend_user_invite(
    user_id: str,
    request: Request,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """Issue a fresh invite for a pending-invite user (expires any prior one)."""
    user = _users_query_for(claims, session).filter(UserAccount.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.invite_pending:
        raise HTTPException(status_code=400,
                            detail="User has already accepted an invite — use 'Reset password' instead")

    # Expire any open invite(s) for this user
    session.query(InviteToken).filter(
        InviteToken.used_by == user.id,
        InviteToken.used_at.is_(None),
    ).update({"expires_at": datetime.utcnow()}, synchronize_session=False)

    # Locate the user's primary team membership (if any) so the new invite
    # can re-attach them on completion.
    membership = session.query(TeamMembership).filter_by(user_id=user.id).first()
    team_id = membership.team_id if membership else None

    invite = generate_invite(
        session, org_id=user.org_id, target_role="user",
        created_by=claims["uid"], target_email=user.email, team_id=team_id,
    )
    invite.used_by = user.id
    session.commit()
    session.refresh(invite)
    base = _editor_base_url(request).rsplit("/editor", 1)[0]
    return {
        "invite_url": f"{base}/invite/{invite.id}",
        "invite_code": invite.code,
        "invite_expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }


@router.delete("/api/admin/users/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    user = _users_query_for(claims, session).filter(UserAccount.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == claims.get("uid"):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if user.role == "org_admin":
        remaining = _users_query_for(claims, session).filter(UserAccount.role == "org_admin").count()
        if remaining <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last org admin")
    session.delete(user)
    session.commit()
    return Response(status_code=204)


# ── Teams ──────────────────────────────────────────────────────────────────


@router.get("/api/admin/teams")
def list_teams(
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """List teams in caller's org.
    org_admin / platform_admin see every team; team_admin sees only the
    teams they administer (used to scope the user-creation form)."""
    q = session.query(Team).filter_by(org_id=claims.get("oid"))
    if claims.get("role") == "team_admin":
        admin_team_ids = _team_admin_team_ids(claims.get("uid") or "", session)
        if not admin_team_ids:
            return {"teams": []}
        q = q.filter(Team.id.in_(admin_team_ids))
    rows = q.order_by(Team.name).all()
    return {"teams": [t.to_dict() for t in rows]}


def _apply_team_membership_set(
    session: Session, team_id: str, org_id: str,
    admin_user_ids: list[str], member_user_ids: list[str],
) -> None:
    """
    Replace the team's TeamMembership rows with the supplied sets.
    ``admin_user_ids`` users must already have role='team_admin' (or higher).
    ``member_user_ids`` users may be any role.  Users supplied in both lists
    are kept as team_admin (the higher membership-role of the two).
    """
    admin_ids  = {u for u in (admin_user_ids or []) if u}
    member_ids = {u for u in (member_user_ids or []) if u} - admin_ids
    all_ids = admin_ids | member_ids
    if all_ids:
        # Sanity: every user must belong to this org
        ok = session.query(UserAccount).filter(
            UserAccount.id.in_(all_ids), UserAccount.org_id == org_id,
        ).count()
        if ok != len(all_ids):
            raise HTTPException(status_code=404, detail="One or more user_ids not found in this org")
    # Validate that each admin candidate actually has the team_admin role
    if admin_ids:
        admin_role_ok = session.query(UserAccount).filter(
            UserAccount.id.in_(admin_ids),
            UserAccount.org_id == org_id,
            UserAccount.role.in_(("team_admin", "org_admin", "platform_admin")),
        ).count()
        if admin_role_ok != len(admin_ids):
            raise HTTPException(status_code=400, detail=(
                "All team admins must already have role='team_admin'.  "
                "Promote them first via the Users tab."
            ))
    session.query(TeamMembership).filter_by(team_id=team_id).delete(synchronize_session=False)
    for uid in admin_ids:
        session.add(TeamMembership(user_id=uid, team_id=team_id, role="team_admin"))
    for uid in member_ids:
        session.add(TeamMembership(user_id=uid, team_id=team_id, role="member"))


@router.post("/api/admin/teams", status_code=201)
def create_team(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    org_id = claims.get("oid")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=400, detail="org not found")
    current = session.query(Team).filter_by(org_id=org_id).count()
    if current >= (org.max_teams or 5):
        raise HTTPException(status_code=409, detail=f"Team limit reached ({org.max_teams})")
    slug = _slugify(body.get("slug") or name)
    if session.query(Team).filter_by(org_id=org_id, slug=slug).first():
        raise HTTPException(status_code=409, detail=f"Team slug '{slug}' already exists")
    team = Team(org_id=org_id, name=name, slug=slug)
    session.add(team)
    session.flush()
    # Optional initial admin / member assignments
    admin_ids  = body.get("admin_user_ids")  or []
    member_ids = body.get("member_user_ids") or []
    if admin_ids or member_ids:
        _apply_team_membership_set(session, team.id, org_id, admin_ids, member_ids)
    session.commit()
    session.refresh(team)
    return team.to_dict()


@router.patch("/api/admin/teams/{team_id}")
def update_team(
    team_id: str,
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if "name" in body and body["name"]:
        team.name = body["name"].strip()
    if "slug" in body and body["slug"]:
        team.slug = _slugify(body["slug"])
    # Optional bulk membership replacement.  Only triggers when at least
    # one of the keys is present so existing PATCH callers that only rename
    # the team aren't surprised.
    if "admin_user_ids" in body or "member_user_ids" in body:
        _apply_team_membership_set(
            session, team.id, claims.get("oid"),
            body.get("admin_user_ids")  or [],
            body.get("member_user_ids") or [],
        )
    session.commit()
    session.refresh(team)
    return team.to_dict()


@router.delete("/api/admin/teams/{team_id}", status_code=204)
def delete_team(
    team_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    # Cascade-delete groups, memberships, sharing policies
    session.query(GroupMembership).filter(
        GroupMembership.group_id.in_(
            session.query(Group.id).filter_by(team_id=team_id)
        )
    ).delete(synchronize_session=False)
    session.query(Group).filter_by(team_id=team_id).delete(synchronize_session=False)
    session.query(TeamMembership).filter_by(team_id=team_id).delete(synchronize_session=False)
    session.query(MemorySharingPolicy).filter_by(
        org_id=claims.get("oid"), scope_type="team", scope_id=team_id
    ).delete(synchronize_session=False)
    session.delete(team)
    session.commit()
    return Response(status_code=204)


@router.get("/api/admin/teams/{team_id}/members")
def list_team_members(
    team_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = session.query(TeamMembership, UserAccount).join(
        UserAccount, UserAccount.id == TeamMembership.user_id
    ).filter(TeamMembership.team_id == team_id).all()
    return {"members": [{
        **u.to_dict(),
        "team_role": tm.role,
        "joined_at": tm.joined_at.isoformat() if tm.joined_at else None,
    } for tm, u in rows]}


@router.post("/api/admin/teams/{team_id}/members", status_code=201)
def add_team_member(
    team_id: str,
    body: dict,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    user_id = (body.get("user_id") or "").strip()
    role    = (body.get("role") or "member").strip()
    if role not in {"member", "team_admin"}:
        raise HTTPException(status_code=422, detail="role must be member or team_admin")
    user = session.query(UserAccount).filter_by(id=user_id, org_id=claims.get("oid")).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found in this org")
    existing = session.query(TeamMembership).filter_by(team_id=team_id, user_id=user_id).first()
    if existing:
        existing.role = role
    else:
        session.add(TeamMembership(team_id=team_id, user_id=user_id, role=role))
    session.commit()
    return {"ok": True}


@router.delete("/api/admin/teams/{team_id}/members/{user_id}", status_code=204)
def remove_team_member(
    team_id: str,
    user_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    session.query(TeamMembership).filter_by(team_id=team_id, user_id=user_id).delete()
    session.commit()
    return Response(status_code=204)


# ── Groups ─────────────────────────────────────────────────────────────────

def _can_admin_group(group: Group, claims: dict, session: Session) -> bool:
    """org_admin / platform_admin → always True.  team_admin → True if at
    least one of the group's teams is a team they admin."""
    role = claims.get("role")
    if role in ("platform_admin", "org_admin"):
        return True
    if role != "team_admin":
        return False
    admin_team_ids = set(_team_admin_team_ids(claims.get("uid") or "", session))
    if not admin_team_ids:
        return False
    group_team_ids = {gt.team_id for gt in session.query(GroupTeam).filter_by(
        group_id=group.id
    ).all()}
    return bool(admin_team_ids & group_team_ids)


def _group_to_dict_with_teams(g: Group, session: Session) -> dict:
    """Group serializer including the list of teams it belongs to."""
    team_ids = [gt.team_id for gt in session.query(GroupTeam).filter_by(
        group_id=g.id
    ).all()]
    teams = session.query(Team).filter(Team.id.in_(team_ids)).all() if team_ids else []
    member_count = session.query(GroupMembership).filter_by(group_id=g.id).count()
    out = g.to_dict()
    out["teams"] = [{"id": t.id, "name": t.name, "slug": t.slug} for t in teams]
    out["member_count"] = member_count
    return out


@router.get("/api/admin/groups")
def list_org_groups(
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """
    Org-wide group listing.  org_admin / platform_admin see every group in
    the caller's org.  team_admin sees only groups belonging to a team they
    admin.  Each entry includes the list of teams the group is a member of.
    """
    rows = session.query(Group).filter_by(org_id=claims.get("oid")).order_by(Group.name).all()
    if claims.get("role") == "team_admin":
        admin_team_ids = set(_team_admin_team_ids(claims.get("uid") or "", session))
        if not admin_team_ids:
            return {"groups": []}
        # Filter to groups that share at least one team with admin_team_ids
        scoped_ids = {
            gt.group_id for gt in session.query(GroupTeam).filter(
                GroupTeam.team_id.in_(admin_team_ids)
            ).all()
        }
        rows = [g for g in rows if g.id in scoped_ids]
    return {"groups": [_group_to_dict_with_teams(g, session) for g in rows]}


@router.post("/api/admin/groups", status_code=201)
def create_group_org(
    body: dict,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """
    Create a group with one or more team assignments.  Body:
      { name, slug?, team_ids: [..] }

    org_admin / platform_admin can assign to any team in the org.  team_admin
    can only assign to teams they admin (and must supply at least one).
    """
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="caller has no org context")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    team_ids = body.get("team_ids") or []
    if isinstance(team_ids, str):
        team_ids = [team_ids]
    team_ids = [t for t in (team_ids or []) if t]
    if not team_ids:
        raise HTTPException(status_code=422, detail="At least one team_id is required")

    # Validate every team belongs to caller's org and (for team_admin) is in admin_team_ids.
    teams = session.query(Team).filter(
        Team.id.in_(team_ids), Team.org_id == org_id,
    ).all()
    if len(teams) != len(set(team_ids)):
        raise HTTPException(status_code=404, detail="One or more team_ids not found in this org")
    if claims.get("role") == "team_admin":
        admin_team_ids = set(_team_admin_team_ids(claims.get("uid") or "", session))
        unauth = set(team_ids) - admin_team_ids
        if unauth:
            raise HTTPException(status_code=403,
                detail="You can only create groups in teams you administer")

    slug = _slugify(body.get("slug") or name)
    # Slugs unique per (team, org).  We use the first team's id for the
    # legacy uq_groups_team_slug constraint and a global org-level slug
    # uniqueness check via name.
    primary_team_id = team_ids[0]
    if session.query(Group).filter_by(org_id=org_id, slug=slug).first():
        raise HTTPException(status_code=409, detail=f"Group slug '{slug}' already exists in this org")

    group = Group(team_id=primary_team_id, org_id=org_id, name=name, slug=slug)
    session.add(group)
    session.flush()  # get group.id
    for tid in team_ids:
        session.add(GroupTeam(group_id=group.id, team_id=tid, org_id=org_id))
    session.commit()
    session.refresh(group)
    return _group_to_dict_with_teams(group, session)


@router.patch("/api/admin/groups/{group_id}")
def update_group(
    group_id: str,
    body: dict,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """Rename a group and/or change which teams it belongs to."""
    group = session.query(Group).filter_by(id=group_id, org_id=claims.get("oid")).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not _can_admin_group(group, claims, session):
        raise HTTPException(status_code=403, detail="Not allowed for this group")

    if "name" in body and body["name"]:
        group.name = body["name"].strip()
    if "slug" in body and body["slug"]:
        group.slug = _slugify(body["slug"])

    if "team_ids" in body:
        team_ids = body.get("team_ids") or []
        if isinstance(team_ids, str):
            team_ids = [team_ids]
        team_ids = [t for t in team_ids if t]
        if not team_ids:
            raise HTTPException(status_code=422, detail="A group must belong to at least one team")
        teams = session.query(Team).filter(
            Team.id.in_(team_ids), Team.org_id == claims.get("oid"),
        ).all()
        if len(teams) != len(set(team_ids)):
            raise HTTPException(status_code=404, detail="One or more team_ids not found in this org")
        # team_admin: every team in the new set must be admin'd by caller
        # OR already in the existing set (so they can't lock themselves out
        # but can't reach across to teams they don't admin either).
        if claims.get("role") == "team_admin":
            admin_team_ids = set(_team_admin_team_ids(claims.get("uid") or "", session))
            existing_team_ids = {gt.team_id for gt in session.query(GroupTeam).filter_by(
                group_id=group.id
            ).all()}
            unauth = set(team_ids) - admin_team_ids - existing_team_ids
            if unauth:
                raise HTTPException(status_code=403,
                    detail="You cannot assign this group to teams you don't administer")
        # Replace the set
        session.query(GroupTeam).filter_by(group_id=group.id).delete(synchronize_session=False)
        for tid in team_ids:
            session.add(GroupTeam(group_id=group.id, team_id=tid, org_id=claims.get("oid")))
        # Keep Group.team_id cache pointing at the first remaining team
        group.team_id = team_ids[0]

    session.commit()
    session.refresh(group)
    return _group_to_dict_with_teams(group, session)


@router.get("/api/admin/teams/{team_id}/groups")
def list_groups(
    team_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """List groups belonging to a specific team (any group with a row in
    group_teams for that team)."""
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    group_ids = [gt.group_id for gt in session.query(GroupTeam).filter_by(
        team_id=team_id
    ).all()]
    rows = session.query(Group).filter(Group.id.in_(group_ids)).order_by(Group.name).all() if group_ids else []
    return {"groups": [_group_to_dict_with_teams(g, session) for g in rows]}


@router.post("/api/admin/teams/{team_id}/groups", status_code=201)
def create_group(
    team_id: str,
    body: dict,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """Legacy single-team create_group — internally delegates to the
    multi-team /api/admin/groups path.  Kept so existing UI hooks keep
    working."""
    team = session.query(Team).filter_by(id=team_id, org_id=claims.get("oid")).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    body = dict(body or {})
    body["team_ids"] = [team_id]
    return create_group_org(body, claims, session)


@router.delete("/api/admin/groups/{group_id}", status_code=204)
def delete_group(
    group_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    group = session.query(Group).filter_by(id=group_id, org_id=claims.get("oid")).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not _can_admin_group(group, claims, session):
        raise HTTPException(status_code=403, detail="Not allowed for this group")
    session.query(GroupMembership).filter_by(group_id=group_id).delete(synchronize_session=False)
    session.query(GroupTeam).filter_by(group_id=group_id).delete(synchronize_session=False)
    session.query(MemorySharingPolicy).filter_by(
        org_id=claims.get("oid"), scope_type="group", scope_id=group_id
    ).delete(synchronize_session=False)
    session.delete(group)
    session.commit()
    return Response(status_code=204)


# ── User ↔ Group membership (alternate path; lets the user-edit modal
#    add/remove group memberships without needing to know group ids). ────
@router.get("/api/admin/users/{user_id}/groups")
def list_user_groups(
    user_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    user = session.query(UserAccount).filter_by(id=user_id, org_id=claims.get("oid")).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    group_ids = [gm.group_id for gm in session.query(GroupMembership).filter_by(
        user_id=user_id
    ).all()]
    groups = session.query(Group).filter(
        Group.id.in_(group_ids), Group.org_id == claims.get("oid"),
    ).order_by(Group.name).all() if group_ids else []
    return {"groups": [_group_to_dict_with_teams(g, session) for g in groups]}


@router.put("/api/admin/users/{user_id}/groups")
def set_user_groups(
    user_id: str,
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """
    Replace the user's group membership set.  Body: ``{ group_ids: [..] }``.
    org_admin only — the user-edit modal lives behind the same surface and
    team_admin shouldn't be able to move a user across team boundaries via
    this path.
    """
    user = session.query(UserAccount).filter_by(id=user_id, org_id=claims.get("oid")).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    raw = body.get("group_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    new_ids = {gid for gid in raw if gid}
    # All targeted groups must belong to caller's org
    if new_ids:
        ok = session.query(Group).filter(
            Group.id.in_(new_ids), Group.org_id == claims.get("oid"),
        ).count()
        if ok != len(new_ids):
            raise HTTPException(status_code=404, detail="One or more group_ids not found")
    # Replace the set atomically
    session.query(GroupMembership).filter_by(user_id=user_id).delete(synchronize_session=False)
    for gid in new_ids:
        session.add(GroupMembership(user_id=user_id, group_id=gid))
    session.commit()
    return {"ok": True, "group_ids": sorted(new_ids)}


@router.get("/api/admin/groups/{group_id}/members")
def list_group_members(
    group_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    group = session.query(Group).filter_by(id=group_id, org_id=claims.get("oid")).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    rows = session.query(GroupMembership, UserAccount).join(
        UserAccount, UserAccount.id == GroupMembership.user_id
    ).filter(GroupMembership.group_id == group_id).all()
    return {"members": [u.to_dict() for _, u in rows]}


@router.post("/api/admin/groups/{group_id}/members", status_code=201)
def add_group_member(
    group_id: str,
    body: dict,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    group = session.query(Group).filter_by(id=group_id, org_id=claims.get("oid")).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    user_id = (body.get("user_id") or "").strip()
    user = session.query(UserAccount).filter_by(id=user_id, org_id=claims.get("oid")).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found in this org")
    if not session.query(GroupMembership).filter_by(group_id=group_id, user_id=user_id).first():
        session.add(GroupMembership(group_id=group_id, user_id=user_id))
        session.commit()
    return {"ok": True}


@router.delete("/api/admin/groups/{group_id}/members/{user_id}", status_code=204)
def remove_group_member(
    group_id: str,
    user_id: str,
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    group = session.query(Group).filter_by(id=group_id, org_id=claims.get("oid")).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    session.query(GroupMembership).filter_by(group_id=group_id, user_id=user_id).delete()
    session.commit()
    return Response(status_code=204)


# ── Memory sharing policies ────────────────────────────────────────────────

@router.get("/api/admin/memory-sharing")
def list_memory_sharing(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    rows = session.query(MemorySharingPolicy).filter_by(org_id=claims.get("oid")).all()
    return {"policies": [r.to_dict() for r in rows]}


@router.post("/api/admin/memory-sharing", status_code=201)
def upsert_memory_sharing(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    scope_type = (body.get("scope_type") or "").strip()
    scope_id   = (body.get("scope_id") or "").strip()
    share_mode = (body.get("share_mode") or "individual").strip()
    if scope_type not in {"team", "group"}:
        raise HTTPException(status_code=422, detail="scope_type must be team or group")
    if share_mode not in {"individual", "shared"}:
        raise HTTPException(status_code=422, detail="share_mode must be individual or shared")
    org_id = claims.get("oid")
    pol = session.query(MemorySharingPolicy).filter_by(
        org_id=org_id, scope_type=scope_type, scope_id=scope_id
    ).first()
    if pol:
        pol.share_mode = share_mode
    else:
        pol = MemorySharingPolicy(org_id=org_id, scope_type=scope_type,
                                  scope_id=scope_id, share_mode=share_mode)
        session.add(pol)
    session.commit()
    session.refresh(pol)
    return pol.to_dict()


# ── Wiki space ACL admin routes ────────────────────────────────────────────

@router.get("/api/admin/wiki-spaces")
def list_wiki_spaces(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """List the org's wiki spaces (top-level dirs) and their ACL rows."""
    org_id = claims.get("oid")
    org = session.query(Tenant).filter_by(id=org_id).first()
    spaces = sorted(_org_spaces_from_s3(org)) if org else []
    rows = session.query(WikiSpaceACL).filter_by(org_id=org_id).all()
    by_space: Dict[str, list] = {s: [] for s in spaces}
    for r in rows:
        by_space.setdefault(r.space_path, []).append(r.to_dict())
    return {"spaces": [{"path": s, "acls": by_space.get(s, [])} for s in spaces]}


@router.post("/api/admin/wiki-spaces/acl", status_code=201)
def grant_wiki_space_acl(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    space  = (body.get("space_path") or "").strip().strip("/")
    g_type = (body.get("grantee_type") or "").strip()
    g_id   = (body.get("grantee_id") or "").strip() or None
    access = (body.get("access") or "reader").strip()
    if g_type not in {"org", "team", "group", "user"}:
        raise HTTPException(status_code=422, detail="invalid grantee_type")
    if access not in {"reader", "writer"}:
        raise HTTPException(status_code=422, detail="access must be reader or writer")
    if not space:
        raise HTTPException(status_code=422, detail="space_path required")
    acl = WikiSpaceACL(org_id=claims.get("oid"), space_path=space,
                       grantee_type=g_type, grantee_id=g_id, access=access)
    session.add(acl)
    session.commit()
    session.refresh(acl)
    return acl.to_dict()


@router.delete("/api/admin/wiki-spaces/acl/{acl_id}", status_code=204)
def revoke_wiki_space_acl(
    acl_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    row = session.query(WikiSpaceACL).filter_by(id=acl_id, org_id=claims.get("oid")).first()
    if not row:
        raise HTTPException(status_code=404, detail="ACL not found")
    session.delete(row)
    session.commit()
    return Response(status_code=204)


# ── Git source ACL admin routes ────────────────────────────────────────────

@router.get("/api/admin/git-sources/{source_id}/acls")
def list_git_source_acls(
    source_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    rows = session.query(GitSourceACL).filter_by(
        source_id=source_id, org_id=claims.get("oid")
    ).all()
    return {"acls": [r.to_dict() for r in rows]}


@router.post("/api/admin/git-sources/{source_id}/acls", status_code=201)
def grant_git_source_acl(
    source_id: str,
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    g_type = (body.get("grantee_type") or "").strip()
    g_id   = (body.get("grantee_id") or "").strip() or None
    if g_type not in {"org", "team", "group", "user"}:
        raise HTTPException(status_code=422, detail="invalid grantee_type")
    acl = GitSourceACL(source_id=source_id, org_id=claims.get("oid"),
                       grantee_type=g_type, grantee_id=g_id)
    session.add(acl)
    session.commit()
    session.refresh(acl)
    return acl.to_dict()


@router.delete("/api/admin/git-sources/acl/{acl_id}", status_code=204)
def revoke_git_source_acl(
    acl_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    row = session.query(GitSourceACL).filter_by(id=acl_id, org_id=claims.get("oid")).first()
    if not row:
        raise HTTPException(status_code=404, detail="ACL not found")
    session.delete(row)
    session.commit()
    return Response(status_code=204)



# ── Tenant settings (programmatic, non-semantic) ───────────────────────────
# Real stored config values (booleans / numbers), distinct from the embedding
# memory store. Read at session start by the MCP get_user_settings tool and the
# kb://settings resource, and by the memory-api compactor for per-tenant
# hot-tier thresholds. Keyed by tenant slug; a NULL field means "inherit the
# system default" (defaults themselves are never persisted here).

@settings_router.get("/api/internal/tenant-settings")
def internal_tenant_settings(
    tenant_slug: str = Query(...),
    session: Session = Depends(get_db_session),
):
    """Per-tenant settings for the MCP server and the compactor. Internal
    network only. Returns stored values with NULLs where unset; the caller
    applies its own system defaults."""
    row = session.query(TenantSettings).filter_by(tenant_slug=tenant_slug).first()
    return {
        "tenant_slug": tenant_slug,
        "settings": row.to_dict() if row else TenantSettings.empty_dict(),
        # Resolved (concrete) AI-writable folder — used by the MCP wiki_write
        # tool to validate paths and guide the agent.
        "ai_wiki_folder": _resolve_ai_wiki_folder(session, tenant_slug),
    }


@settings_router.post("/api/internal/tenant-settings")
def internal_tenant_settings_write(
    body: dict,
    session: Session = Depends(get_db_session),
):
    """Internal write for the MCP set_user_settings tool. Body:
    {"tenant_slug": str, "patch": {field: value, ...}}. Internal network only;
    the MCP server is the trusted caller and passes its own validated
    tenant_slug. A field set to null clears the override (system default)."""
    tenant_slug = (body or {}).get("tenant_slug")
    if not tenant_slug:
        raise HTTPException(status_code=422, detail="tenant_slug is required")
    patch = _coerce_tenant_settings((body or {}).get("patch") or {})
    if not patch:
        raise HTTPException(status_code=422, detail="no recognised settings in patch")
    row = session.query(TenantSettings).filter_by(tenant_slug=tenant_slug).first()
    if not row:
        row = TenantSettings(tenant_slug=tenant_slug)
        session.add(row)
    for key, val in patch.items():
        setattr(row, key, val)
    session.commit()
    return {"ok": True, "tenant_slug": tenant_slug, "settings": row.to_dict()}


def _coerce_tenant_settings(body: dict) -> dict:
    """Validate an incoming settings patch. Only known keys are accepted; a
    value of None clears the override (reverts to the system default)."""
    out: dict = {}
    if "second_brain_mode" in body:
        v = body["second_brain_mode"]
        if v is not None and not isinstance(v, bool):
            raise HTTPException(status_code=422, detail="second_brain_mode must be a boolean or null")
        out["second_brain_mode"] = v
    for key in ("hot_tier_promote_score", "hot_tier_drop_score",
                "hot_tier_drop_age_days", "hot_tier_cluster_threshold"):
        if key in body:
            v = body[key]
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise HTTPException(status_code=422, detail=f"{key} must be a number or null")
                v = float(v)
                if v < 0:
                    raise HTTPException(status_code=422, detail=f"{key} must be >= 0")
            out[key] = v
    if "board_nudge_chars" in body:
        v = body["board_nudge_chars"]
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise HTTPException(status_code=422, detail="board_nudge_chars must be an integer or null")
            v = int(v)
            if v < 0:
                raise HTTPException(status_code=422, detail="board_nudge_chars must be >= 0")
        out["board_nudge_chars"] = v
    return out


@settings_router.get("/api/admin/tenant-settings")
def tenant_settings_get_admin(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """This org's programmatic settings, for the admin Settings UI."""
    slug = _effective_tenant_slug(claims, None, session)
    row = session.query(TenantSettings).filter_by(tenant_slug=slug).first()
    return {
        "tenant_slug": slug,
        "settings": row.to_dict() if row else TenantSettings.empty_dict(),
        "ai_wiki_folder": _resolve_ai_wiki_folder(session, slug),
        "ai_wiki_folder_default": AI_WIKI_FOLDER_DEFAULT,
    }


@settings_router.put("/api/admin/tenant-settings")
def tenant_settings_put_admin(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Upsert this org's settings. Only recognised keys are applied; a key set
    to null clears the override so the system default applies again. Tenant is
    resolved server-side from claims, so an admin can't write another org's."""
    patch = _coerce_tenant_settings(body)
    slug = _effective_tenant_slug(claims, None, session)
    row = session.query(TenantSettings).filter_by(tenant_slug=slug).first()
    if not row:
        row = TenantSettings(tenant_slug=slug)
        session.add(row)
    for key, val in patch.items():
        setattr(row, key, val)
    # ai_wiki_folder is admin-only (NOT part of _coerce_tenant_settings, so the
    # agent settings path can never set it). Handle it here explicitly.
    if "ai_wiki_folder" in (body or {}):
        row.ai_wiki_folder = _sanitize_ai_folder(body["ai_wiki_folder"])
    session.commit()
    return {
        "ok": True, "tenant_slug": slug,
        "settings": row.to_dict(),
        "ai_wiki_folder": _resolve_ai_wiki_folder(session, slug),
    }


# ── Memory edit notifications ──────────────────────────────────────────────

@settings_router.get("/api/notifications")
def list_notifications(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Return memory-edit notifications for the current user."""
    ctx = auth_context_from_request(request)
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")
    rows = session.query(MemoryEditNotification).filter_by(
        original_author_id=ctx["user_id"], seen=False,
    ).order_by(MemoryEditNotification.edited_at.desc()).all()
    return {"notifications": [r.to_dict() for r in rows]}


@settings_router.post("/api/notifications/{notification_id}/seen")
def mark_notification_seen(
    notification_id: str,
    request: Request,
    session: Session = Depends(get_db_session),
):
    ctx = auth_context_from_request(request)
    if not ctx:
        raise HTTPException(status_code=401, detail="Authentication required")
    row = session.query(MemoryEditNotification).filter_by(id=notification_id).first()
    if not row or row.original_author_id != ctx["user_id"]:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.seen = True
    session.commit()
    return {"ok": True}


@settings_router.post("/api/internal/memory-edit-notify")
def internal_memory_edit_notify(
    body: dict,
    session: Session = Depends(get_db_session),
):
    """Embedder posts here when one user edits another user's memory."""
    notif = MemoryEditNotification(
        memory_id=body.get("memory_id") or "",
        original_author_id=body.get("original_author_id") or "",
        edited_by_id=body.get("edited_by_id") or "",
    )
    session.add(notif)
    session.commit()
    return {"ok": True}


# ── Org Setup Wizard ───────────────────────────────────────────────────────

@settings_router.get("/setup-org", response_class=HTMLResponse)
def setup_org_page(request: Request, session: Session = Depends(get_db_session)):
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims or claims.get("role") not in ("platform_admin", "org_admin"):
        return RedirectResponse("/login", status_code=302)
    org = session.query(Tenant).filter_by(id=claims.get("oid")).first()
    if org and org.setup_complete:
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_render(_STATIC / "setup_org.html", {
        "<!-- APP_NAME -->": APP_NAME,
    }))


@settings_router.get("/api/org")
def get_my_org(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    org = session.query(Tenant).filter_by(id=claims.get("oid")).first()
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    return org.to_dict()


@settings_router.patch("/api/org")
def update_my_org(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    org = session.query(Tenant).filter_by(id=claims.get("oid")).first()
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    if "display_name" in body and body["display_name"]:
        org.display_name = body["display_name"].strip()
    if "domain" in body and body["domain"]:
        org.domain = body["domain"].strip().lower()
        org.domain_verified = False  # any change resets verification
        org.domain_verify_token = secrets.token_urlsafe(16)
    if "mfa_policy" in body and body["mfa_policy"] in ("enforced", "optional"):
        org.mfa_policy = body["mfa_policy"]
    if "setup_complete" in body:
        org.setup_complete = bool(body["setup_complete"])
    session.commit()
    return org.to_dict()
