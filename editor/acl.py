"""
Access-control helpers for wiki spaces, git sources, and memory scopes.

Default-allow policy: when no ACL rows exist for a resource, the entire Org
can access it (backward-compatible — existing wiki content stays visible
after the upgrade).  ACL rows make a resource restricted: only listed
grantees can access.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Optional, Set

from sqlalchemy.orm import Session

from config_db import (
    UserAccount, Tenant, Team, Group, TeamMembership, GroupMembership,
    WikiSpaceACL, GitSourceACL, MemorySharingPolicy,
)


def auth_context_from_request(request) -> Optional[dict]:
    """
    Extract the authenticated identity from the X-Auth-* headers that nginx
    forwards.  Returns {"user_id", "org_id", "role"} or None for unauthenticated.
    """
    user_id = request.headers.get("x-auth-user-id") or ""
    org_id  = request.headers.get("x-auth-org-id")  or ""
    role    = request.headers.get("x-auth-role")    or "user"
    if not user_id:
        return None
    return {"user_id": user_id, "org_id": org_id or None, "role": role}


def user_team_ids(session: Session, user_id: str) -> Set[str]:
    rows = session.query(TeamMembership.team_id).filter(
        TeamMembership.user_id == user_id
    ).all()
    return {r[0] for r in rows}


def user_group_ids(session: Session, user_id: str) -> Set[str]:
    rows = session.query(GroupMembership.group_id).filter(
        GroupMembership.user_id == user_id
    ).all()
    return {r[0] for r in rows}


# ── Wiki spaces ────────────────────────────────────────────────────────────

def space_of_path(rel_path: str) -> str:
    """Return the top-level directory ('space') from a relative wiki path."""
    p = PurePosixPath(rel_path.strip("/"))
    return p.parts[0] if p.parts else ""


def list_org_spaces(docs_root) -> Set[str]:
    """All immediate subdirectories of the org's docs root are wiki spaces."""
    if not docs_root or not docs_root.exists():
        return set()
    return {p.name for p in docs_root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def _user_grantee_ids(session: Session, user_id: str) -> dict:
    """Bundle of grantee IDs used in ACL filters for this user."""
    return {
        "user":   {user_id},
        "team":   user_team_ids(session, user_id),
        "group":  user_group_ids(session, user_id),
    }


def user_can_access_space(session: Session, user_id: str, org_id: str,
                           space: str, *, write: bool = False) -> bool:
    """
    True iff the user can read (or write, if write=True) the given top-level
    wiki space.  Default-allow when no ACL rows exist for that space.
    """
    if not space:
        return False
    rows = session.query(WikiSpaceACL).filter(
        WikiSpaceACL.org_id == org_id,
        WikiSpaceACL.space_path == space,
    ).all()
    if not rows:
        return True   # default-allow

    grantees = _user_grantee_ids(session, user_id)
    needed = "writer" if write else "reader"
    for row in rows:
        if row.access != needed and not (write is False and row.access == "writer"):
            # writer access satisfies reader requests
            continue
        if row.grantee_type == "org" and row.grantee_id is None:
            return True
        if row.grantee_type in grantees and row.grantee_id in grantees[row.grantee_type]:
            return True
    return False


def filter_readable_spaces(session: Session, user_id: str, org_id: str,
                            spaces: Set[str]) -> Set[str]:
    return {s for s in spaces if user_can_access_space(session, user_id, org_id, s)}


# ── Git sources ────────────────────────────────────────────────────────────

def user_accessible_git_sources(session: Session, user_id: str, org_id: str,
                                 all_source_ids: Set[str]) -> Set[str]:
    """
    Return the subset of source_ids the given user is allowed to see.
    Default-allow when no ACL rows exist for a source.
    """
    if not all_source_ids:
        return set()
    rows = session.query(GitSourceACL).filter(
        GitSourceACL.org_id == org_id,
        GitSourceACL.source_id.in_(all_source_ids),
    ).all()
    restricted_sources = {r.source_id for r in rows}
    out = set(all_source_ids) - restricted_sources

    grantees = _user_grantee_ids(session, user_id)
    for row in rows:
        if row.grantee_type == "org" and row.grantee_id is None:
            out.add(row.source_id)
        elif row.grantee_type in grantees and row.grantee_id in grantees[row.grantee_type]:
            out.add(row.source_id)
    return out


# ── Memory sharing ─────────────────────────────────────────────────────────

def memory_visibility_filter(session: Session, user_id: str, org_id: str) -> dict:
    """
    Resolve the user's effective memory-visibility scope.

    Returns {"share_mode", "scope_user_ids"}:
      share_mode = "individual" → see only own memories
      share_mode = "shared"     → see all memories from the team/group pool

    Without any MemorySharingPolicy rows, default to 'individual'.
    """
    team_ids = user_team_ids(session, user_id)
    group_ids = user_group_ids(session, user_id)

    # Group-level policy wins over team-level (more specific).
    for gid in group_ids:
        pol = session.query(MemorySharingPolicy).filter_by(
            org_id=org_id, scope_type="group", scope_id=gid,
        ).first()
        if pol and pol.share_mode == "shared":
            members = {r[0] for r in session.query(GroupMembership.user_id).filter(
                GroupMembership.group_id == gid).all()}
            return {"share_mode": "shared", "scope_user_ids": members}
    for tid in team_ids:
        pol = session.query(MemorySharingPolicy).filter_by(
            org_id=org_id, scope_type="team", scope_id=tid,
        ).first()
        if pol and pol.share_mode == "shared":
            members = {r[0] for r in session.query(TeamMembership.user_id).filter(
                TeamMembership.team_id == tid).all()}
            return {"share_mode": "shared", "scope_user_ids": members}

    return {"share_mode": "individual", "scope_user_ids": {user_id}}
