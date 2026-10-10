"""
The editor's git knowledge sources API.

`router` is the org admin's /api/admin/git-sources routes: list, create,
update, delete, sync, reindex and live sync status. `internal_router` is the
two peer endpoints the embedder calls to enforce git source ACLs and to back
the `list_sources` MCP tool. main.py includes each one where its routes used
to be declared, so the route order is unchanged.

Moved out of main.py by CQ-04 phase 6; the paths are unchanged.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from acl import user_accessible_git_sources
from auth import require_org_admin
from config_db import get_db_session, GitSource, Tenant, DEFAULT_TENANT_SLUG
from git_sources import (
    _git_source_or_404, _org_slug, _proxy_embedder,
    _norm_tool_availability, _norm_sync_interval,
    _valid_git_branch, _valid_git_repo_url,
)
from internal_service import EMBEDDER_URL, _internal_service_headers

router = APIRouter()
internal_router = APIRouter()


@router.get("/api/admin/git-sources")
def list_git_sources(
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    rows = (
        session.query(GitSource)
        .filter_by(org_id=claims["oid"])
        .order_by(GitSource.created_at)
        .all()
    )
    return {"sources": [r.to_dict() for r in rows]}


@router.post("/api/admin/git-sources", status_code=201)
def create_git_source(
    body: dict,
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    repo_url = _valid_git_repo_url(body.get("repo_url"))
    branch = _valid_git_branch(body.get("branch"))
    src = GitSource(
        org_id                = claims["oid"],
        name                  = ((body.get("name") or repo_url).strip())[:255],
        repo_url              = repo_url,
        branch                = branch,
        path_prefix           = ((body.get("path_prefix") or "").strip())[:512],
        sync_interval_minutes = _norm_sync_interval(body.get("sync_interval_minutes")),
        enabled               = bool(body.get("enabled", True)),
        tool_availability     = _norm_tool_availability(body.get("tool_availability")),
    )
    src.set_token((body.get("token") or "").strip())
    session.add(src)
    session.commit()
    session.refresh(src)
    return src.to_dict()


@router.put("/api/admin/git-sources/{source_id}")
def update_git_source(
    source_id: str,
    body: dict,
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    src = _git_source_or_404(session, source_id, claims["oid"])
    if "name" in body and body["name"] is not None:
        src.name = str(body["name"]).strip()[:255]
    if "repo_url" in body and body["repo_url"] is not None:
        src.repo_url = _valid_git_repo_url(body["repo_url"])
    if "branch" in body and body["branch"] is not None:
        src.branch = _valid_git_branch(body["branch"])
    if "path_prefix" in body and body["path_prefix"] is not None:
        src.path_prefix = str(body["path_prefix"]).strip()[:512]
    if "sync_interval_minutes" in body:
        src.sync_interval_minutes = _norm_sync_interval(body["sync_interval_minutes"])
    if "enabled" in body:
        src.enabled = bool(body["enabled"])
    if "tool_availability" in body:
        src.tool_availability = _norm_tool_availability(body["tool_availability"])
    # Empty token preserves the existing one (matches the "leave blank to
    # keep existing" hint in the admin form).
    if body.get("token"):
        src.set_token(str(body["token"]).strip())
    session.commit()
    session.refresh(src)
    return src.to_dict()


@router.delete("/api/admin/git-sources/{source_id}", status_code=204)
def delete_git_source(
    source_id: str,
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    src      = _git_source_or_404(session, source_id, claims["oid"])
    org_slug = _org_slug(session, src.org_id)
    session.delete(src)
    session.commit()
    # Tell embedder to drop chunks + on-disk clone. Pass org_slug so it can
    # find the per-org clone path (the row is already gone here, so the
    # embedder can't look it up itself).
    _proxy_embedder("DELETE", f"/api/git-sources/{source_id}?org_slug={org_slug}")


@router.post("/api/admin/git-sources/{source_id}/sync")
def sync_git_source(
    source_id: str,
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    _git_source_or_404(session, source_id, claims["oid"])
    _proxy_embedder("POST", f"/api/git-sources/{source_id}/sync")
    return {"status": "sync triggered", "id": source_id}


@router.post("/api/admin/git-sources/{source_id}/reindex")
def reindex_git_source(
    source_id: str,
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    """Force a full reindex of one git source — wipes its chunks and rebuilds
    them all from the current clone.  Use after a vector-dim change, or to
    recover from a partial/failed index."""
    _git_source_or_404(session, source_id, claims["oid"])
    _proxy_embedder("POST", f"/api/git-sources/{source_id}/reindex")
    return {"status": "reindex triggered", "id": source_id}


@router.get("/api/admin/git-sources/status")
def git_sources_status(
    session: Session = Depends(get_db_session),
    claims: dict = Depends(require_org_admin),
):
    """Proxy live sync state from the embedder, filtered to this admin's
    org so cross-org source IDs aren't even enumerable here."""
    import urllib.request as _urlreq, json as _json
    org_source_ids = {
        s.id for s in session.query(GitSource).filter_by(org_id=claims["oid"]).all()
    }
    try:
        _req = _urlreq.Request(
            f"{EMBEDDER_URL}/api/git-sources/status",
            headers=_internal_service_headers(),
        )
        with _urlreq.urlopen(_req, timeout=5) as r:
            all_states = _json.loads(r.read())
    except Exception:
        return {}
    return {sid: st for sid, st in all_states.items() if sid in org_source_ids}


# ── Internal endpoints used by the embedder to enforce git source ACLs ────

@internal_router.get("/api/internal/git-source-access")
def internal_git_source_access(
    user_id: str = Query(...),
    org_id: str  = Query(...),
    source_ids: str = Query("", description="Comma-separated source IDs"),
    session: Session = Depends(get_db_session),
):
    """Returns the subset of source_ids the user has access to."""
    ids = {s.strip() for s in source_ids.split(",") if s.strip()}
    allowed = user_accessible_git_sources(session, user_id, org_id, ids)
    return {"allowed": sorted(allowed)}


@internal_router.get("/api/internal/git-sources")
def internal_git_sources(
    user_id: str = Query(...),
    org_id: str  = Query(...),
    session: Session = Depends(get_db_session),
):
    """Returns the git sources visible to the given user, with secrets stripped.

    Two layers of scoping:
      1. Source ownership — only sources owned by the caller's org are
         considered. A cross-org request returns nothing even if ACL rows
         from another org reference these sources.
      2. Intra-org ACL — within the org-owned set, GitSourceACL further
         restricts visibility to specific teams/groups/users when grant
         rows exist.

    Used by the embedder to back the `list_sources` MCP tool so agents can
    discover the perimeter of synced repos visible to their token.
    """
    org_sources = session.query(GitSource).filter_by(org_id=org_id).all()
    if not org_sources:
        return {"sources": []}
    org_source_ids = {s.id for s in org_sources}
    allowed = user_accessible_git_sources(session, user_id, org_id, org_source_ids)
    # org_slug is not decoration: clones live at /git-sources/<org_slug>/<id>/,
    # so every reader that touches the checkout needs it to find the thing.
    # Without it `source_tree` looked under /git-sources/default/<id>, missed,
    # fell through to the legacy /git-sources/<id>, missed again, and answered
    # "Source has not been synced yet" about a repo that was sitting right
    # there, while `_git_source_disk_state` reported "not cloned" and
    # list_sources showed files: 0 (issue #136). /api/internal/config has
    # carried the field all along; this endpoint is the one that forgot.
    slug_by_org = {t.id: t.slug for t in session.query(Tenant).all()}
    return {
        "sources": [
            {
                "id":          s.id,
                "name":        s.name,
                "repo_url":    s.repo_url,
                "branch":      s.branch or "main",
                "path_prefix": s.path_prefix or "",
                "enabled":     bool(s.enabled),
                "tool_availability": s.tool_availability or "on",
                "org_slug":    slug_by_org.get(s.org_id) or DEFAULT_TENANT_SLUG,
            }
            for s in org_sources if s.id in allowed
        ]
    }
