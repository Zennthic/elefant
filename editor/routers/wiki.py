"""
The wiki's session-authed web API: file CRUD and the tree, revisions and
write locks, public share links, the recycle bin, moves and directory
management, and images and card attachments.

Moved out of main.py by CQ-04 phase 2; the paths are unchanged.
"""
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from acl import auth_context_from_request, filter_readable_spaces
from config_db import (
    get_db_session, SoftDeletedPage, Tenant, WikiPageLock, WikiRevision, WikiShare,
)
from wiki_pages import (
    _assert_wiki_prefix_unlocked, _assert_wiki_unlocked, _check_space_access,
    _filter_tree_by_acl, _last_wiki_edit, _record_wiki_revision, _share_page_title,
    _user_label, _wiki_lock_for, _wiki_revisions_for,
)
from wiki_storage import (
    _active_org, _build_tree, _normalize_path, _resolve_ai_wiki_folder,
    _s3, _s3_delete, _s3_exists, _s3_read, _s3_write, _trigger_embedder_reindex,
)

log = logging.getLogger("editor")

router = APIRouter()


class FileBody(BaseModel):
    path: str
    content: str


class WikiRevertBody(BaseModel):
    path: str
    revision_id: int


class WikiLockBody(BaseModel):
    path: str
    locked: bool
    reason: Optional[str] = None


# ── Wiki sharing (public opaque links) ──────────────────────────────────────
# A WikiShare grants read-only public access to one page or folder subtree via
# an unguessable token in the URL. The public endpoints resolve the tenant +
# path from the share record server-side and never trust a caller-supplied
# tenant, so a link can only ever expose exactly the shared subtree.

def _share_lookup(session: Session, token: str) -> Optional["WikiShare"]:
    """Return a live (not revoked, not expired) share for this token, else None."""
    if not token:
        return None
    share = session.query(WikiShare).filter_by(token=token, revoked=False).first()
    if not share:
        return None
    if share.expires_at and share.expires_at < datetime.utcnow():
        return None
    return share


def _share_org(session: Session, share: "WikiShare") -> Tenant:
    org = session.query(Tenant).filter_by(id=share.org_id).first()
    if not org or org.lifecycle_status != "active":
        raise HTTPException(status_code=404, detail="This share link is no longer available")
    return org


def _share_root_dir(share: "WikiShare") -> str:
    """The directory a served asset must stay inside for this share."""
    if share.resource_type == "folder":
        return share.path.rstrip("/")
    return share.path.rsplit("/", 1)[0] if "/" in share.path else ""


def _share_allows_file(share: "WikiShare", req: str) -> bool:
    """Is this .md path inside the shared page/subtree?"""
    if share.resource_type == "page":
        return req == share.path
    root = share.path.rstrip("/")
    if not req.startswith(root + "/"):
        return False
    if not share.include_subfolders:
        return req.count("/") == root.count("/") + 1  # direct children only
    return True


def _share_allows_asset(share: "WikiShare", req: str) -> bool:
    """Is this asset (image) inside the shared subtree?  Scoped to the share
    root directory so a page's co-located images load without exposing the
    rest of the tenant."""
    root = _share_root_dir(share)
    if root == "":
        return "/" not in req  # root-level page → only top-level sibling assets
    return req == root or req.startswith(root + "/")


def _share_pages(session: Session, org: Tenant, share: "WikiShare") -> List[dict]:
    """Every .md page the share exposes, as [{path, title}]."""
    if share.resource_type == "page":
        return [{"path": share.path, "title": _share_page_title(share.path)}]
    root = share.path.rstrip("/")
    keys = [k for k in _s3().walk(org.slug, root)
            if k.startswith(root + "/") and k.lower().endswith(".md")]
    if not share.include_subfolders:
        depth = root.count("/") + 1
        keys = [k for k in keys if k.count("/") == depth]
    return [{"path": k, "title": _share_page_title(k)} for k in sorted(keys)]


class ShareCreate(BaseModel):
    path: str
    resource_type: str = "page"          # "page" | "folder"
    include_subfolders: bool = True
    expires_in_days: Optional[int] = None


@router.post("/api/wiki/share")
def create_wiki_share(body: ShareCreate, request: Request,
                      session: Session = Depends(get_db_session)):
    """Create a public share link for a page or folder. Requires write access
    to the space (publishing publicly is a deliberate, editor-level action)."""
    org = _active_org(request, session)
    rtype = body.resource_type if body.resource_type in ("page", "folder") else "page"
    path = _normalize_path(body.path)
    _check_space_access(request, session, path, write=True)

    if rtype == "page":
        if not path.lower().endswith(".md"):
            raise HTTPException(status_code=422, detail="A page share path must be a .md file")
        _s3_read(org, path)  # 404 if the page doesn't exist
    else:
        root = path.rstrip("/")
        has_page = any(k.startswith(root + "/") and k.lower().endswith(".md")
                       for k in _s3().walk(org.slug, root))
        if not has_page:
            raise HTTPException(status_code=404, detail="That folder has no pages to share")
        path = root

    ctx = auth_context_from_request(request)
    expires_at = None
    if body.expires_in_days and body.expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=int(body.expires_in_days))

    share = WikiShare(
        token=secrets.token_urlsafe(16),
        org_id=org.id,
        created_by=(ctx or {}).get("user_id"),
        resource_type=rtype,
        path=path,
        include_subfolders=bool(body.include_subfolders),
        audience="public",
        expires_at=expires_at,
    )
    session.add(share)
    session.commit()
    return share.to_dict()


@router.get("/api/wiki/shares")
def list_wiki_shares(request: Request, path: Optional[str] = Query(None),
                     session: Session = Depends(get_db_session)):
    """List this org's active shares, optionally filtered to one path."""
    org = _active_org(request, session)
    q = session.query(WikiShare).filter_by(org_id=org.id, revoked=False)
    if path:
        q = q.filter_by(path=_normalize_path(path))
    rows = q.order_by(WikiShare.created_at.desc()).all()
    return {"shares": [s.to_dict() for s in rows]}


@router.delete("/api/wiki/share/{share_id}")
def revoke_wiki_share(share_id: str, request: Request,
                      session: Session = Depends(get_db_session)):
    """Revoke a share (the link 404s immediately)."""
    org = _active_org(request, session)
    share = session.query(WikiShare).filter_by(id=share_id, org_id=org.id).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    share.revoked = True
    session.commit()
    return {"status": "revoked"}


_NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


@router.get("/api/public/share/{token}")
def public_share_meta(token: str, session: Session = Depends(get_db_session)):
    """Public: metadata + page list for a share link. No auth."""
    share = _share_lookup(session, token)
    if not share:
        raise HTTPException(status_code=404, detail="This share link is invalid or has expired")
    org = _share_org(session, share)
    pages = _share_pages(session, org, share)
    if not pages:
        raise HTTPException(status_code=404, detail="This share has no content")
    try:
        share.view_count = (share.view_count or 0) + 1
        share.last_viewed_at = datetime.utcnow()
        session.commit()
    except Exception:
        session.rollback()
    title = (_share_page_title(share.path) if share.resource_type == "page"
             else (share.path.rsplit("/", 1)[-1] or share.path))
    return JSONResponse({
        "type":     share.resource_type,
        "root":     share.path,
        "title":    title,
        "org_name": (getattr(org, "display_name", None) or org.name),
        "pages":    pages,
        "count":    len(pages),
    }, headers=_NOINDEX)


@router.get("/api/public/share/{token}/file")
def public_share_file(token: str, path: str = Query(...),
                      session: Session = Depends(get_db_session)):
    """Public: one markdown page's content, scoped to the shared subtree."""
    share = _share_lookup(session, token)
    if not share:
        raise HTTPException(status_code=404, detail="This share link is invalid or has expired")
    req = _normalize_path(path)
    if not req.lower().endswith(".md") or not _share_allows_file(share, req):
        raise HTTPException(status_code=404, detail="Not found")
    org = _share_org(session, share)
    data = _s3_read(org, req)
    return JSONResponse({
        "path":    req,
        "title":   _share_page_title(req),
        "content": data.decode("utf-8", errors="replace"),
    }, headers=_NOINDEX)


@router.get("/api/public/share/{token}/image")
def public_share_image(token: str, path: str = Query(...),
                       session: Session = Depends(get_db_session)):
    """Public: a binary asset referenced by a shared page, scoped to the
    shared subtree."""
    import mimetypes
    share = _share_lookup(session, token)
    if not share:
        raise HTTPException(status_code=404, detail="This share link is invalid or has expired")
    req = _normalize_path(path)
    if req.lower().endswith(".md") or not _share_allows_asset(share, req):
        raise HTTPException(status_code=404, detail="Not found")
    org = _share_org(session, share)
    data = _s3_read(org, req)
    ctype = mimetypes.guess_type(req)[0] or "application/octet-stream"
    headers = dict(_NOINDEX)
    headers["Cache-Control"] = "public, max-age=3600"
    return Response(content=data, media_type=ctype, headers=headers)


@router.get("/api/tree")
def get_tree(request: Request, session: Session = Depends(get_db_session)):
    org = _active_org(request, session)
    tree = _build_tree(org)
    ai_folder = _resolve_ai_wiki_folder(session, org.slug)
    ctx = auth_context_from_request(request)
    if not ctx or ctx["role"] in ("platform_admin", "org_admin"):
        tree["ai_folder"] = ai_folder
        return tree
    # Derive the org's "spaces" (top-level dirs) from the tree we just
    # built rather than the filesystem — same answer, one S3 listing.
    org_spaces = {
        c["name"] for c in tree.get("children", [])
        if c.get("type") == "directory"
    }
    allowed = filter_readable_spaces(session, ctx["user_id"], ctx["org_id"], org_spaces)
    filtered = _filter_tree_by_acl(tree, allowed)
    filtered["ai_folder"] = ai_folder
    return filtered


@router.get("/api/file")
def get_file(path: str = Query(...), request: Request = None,
             session: Session = Depends(get_db_session)):
    _check_space_access(request, session, path, write=False)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    data = _s3_read(org, rel)
    lock = _wiki_lock_for(session, org.id, rel)
    return {"path": path, "content": data.decode("utf-8"),
            "last_edit": _last_wiki_edit(session, org.id, rel),
            "lock": lock.to_dict() if lock else {"locked": False}}


@router.put("/api/file")
def save_file(body: FileBody, request: Request,
              session: Session = Depends(get_db_session)):
    _check_space_access(request, session, body.path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(body.path)
    _assert_wiki_unlocked(session, org.id, rel, "saved")
    _s3_write(org, rel, body.content.encode("utf-8"))
    _trigger_embedder_reindex(body.path, org=org)
    _uid = (auth_context_from_request(request) or {}).get("user_id")
    _record_wiki_revision(session, org, rel, body.content.encode("utf-8"),
                          edited_by_id=_uid, edited_by_name=_user_label(session, _uid),
                          source="editor", created=False)
    # Hand back the new attribution so the page header updates on save rather
    # than showing the previous editor until the next full load.
    return {"status": "saved", "path": body.path,
            "last_edit": _last_wiki_edit(session, org.id, rel)}


@router.post("/api/file")
def create_file(body: FileBody, request: Request,
                session: Session = Depends(get_db_session)):
    _check_space_access(request, session, body.path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(body.path)
    _assert_wiki_unlocked(session, org.id, rel, "created over")
    if _s3_exists(org, rel):
        raise HTTPException(status_code=409, detail="File already exists")
    _s3_write(org, rel, body.content.encode("utf-8"))
    _trigger_embedder_reindex(body.path, org=org)
    _uid = (auth_context_from_request(request) or {}).get("user_id")
    _record_wiki_revision(session, org, rel, body.content.encode("utf-8"),
                          edited_by_id=_uid, edited_by_name=_user_label(session, _uid),
                          source="editor", created=True)
    return {"status": "created", "path": body.path}


# ── Wiki page version history — session/cookie-authed web API ───────────────
# Session-authed counterparts of the token-authed /api/internal/wiki/* endpoints
# (those are for the MCP tools). These back the memory-ui History panel. Same
# WikiRevision data; auth via the wiki-space ACL (_check_space_access) and the
# caller's org (_active_org), exactly like GET/PUT /api/file.

@router.get("/api/wiki/history")
def wiki_history(path: str = Query(...), request: Request = None,
                 session: Session = Depends(get_db_session)):
    _check_space_access(request, session, path, write=False)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    rows = _wiki_revisions_for(session, org.id, rel, 200)
    return {"path": rel, "count": len(rows),
            "revisions": [r.to_dict(include_content=False) for r in rows]}


@router.get("/api/wiki/revision")
def wiki_revision(path: str = Query(...), id: int = Query(...),
                  request: Request = None, session: Session = Depends(get_db_session)):
    _check_space_access(request, session, path, write=False)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    row = (session.query(WikiRevision)
           .filter_by(id=id, tenant_id=org.id, path=rel).first())
    if row is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return {"path": rel, "revision": row.to_dict(include_content=True)}


@router.post("/api/wiki/revert")
def wiki_revert(body: WikiRevertBody, request: Request,
                session: Session = Depends(get_db_session)):
    """Restore a wiki page to a previous revision's content. Non-destructive:
    writes the old content forward as a new revision (history stays append-only)
    and re-triggers the embedder reindex. Web write-access is the wiki-space
    ACL — no AI-folder restriction here (that confinement is only for AI agents
    on the token-authed path)."""
    _check_space_access(request, session, body.path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(body.path)
    _assert_wiki_unlocked(session, org.id, rel, "reverted")
    row = (session.query(WikiRevision)
           .filter_by(id=body.revision_id, tenant_id=org.id, path=rel).first())
    if row is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    content_bytes = (row.content or "").encode("utf-8")
    _s3_write(org, rel, content_bytes)
    _trigger_embedder_reindex(rel, org=org)
    _uid = (auth_context_from_request(request) or {}).get("user_id")
    _record_wiki_revision(session, org, rel, content_bytes,
                          edited_by_id=_uid, edited_by_name=_user_label(session, _uid),
                          source="editor", message=f"Reverted to revision {body.revision_id}",
                          created=False)
    return {"status": "reverted", "path": rel, "revision_id": body.revision_id,
            "restored_from": row.edited_at.isoformat() if row.edited_at else None}


# ── Wiki page write locks — session/cookie-authed web API ──────────────────
# Deliberately web-only. There is no token-authed counterpart under
# /api/internal/wiki/* and no MCP tool, so an AI agent can neither lock a page
# nor unlock one it is blocked by — which is what "only a human can lock and
# unlock" means in practice. Enforcement of the lock itself lives in
# _assert_wiki_unlocked() and covers both paths.

@router.get("/api/wiki/lock")
def wiki_lock_state(path: str = Query(...), request: Request = None,
                    session: Session = Depends(get_db_session)):
    """Current lock state for a page. Readable by anyone who can read the page."""
    _check_space_access(request, session, path, write=False)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    lock = _wiki_lock_for(session, org.id, rel)
    return lock.to_dict() if lock else {"locked": False, "path": rel}


@router.post("/api/wiki/lock")
def wiki_set_lock(body: WikiLockBody, request: Request,
                  session: Session = Depends(get_db_session)):
    """Lock or unlock a page for writing.

    Requires web write access to the page's space — the same bar as editing
    it. Locking an already-locked page is a no-op that keeps the original
    holder and timestamp, so a second click never quietly reassigns the lock;
    unlocking an unlocked page is a no-op too. Both are idempotent on purpose:
    the UI reflects server state, and a double submit must not flip it.
    """
    _check_space_access(request, session, body.path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(body.path)
    existing = _wiki_lock_for(session, org.id, rel)

    if not body.locked:
        if existing is not None:
            session.delete(existing)
            session.commit()
        return {"locked": False, "path": rel}

    if existing is not None:
        return existing.to_dict()

    uid = (auth_context_from_request(request) or {}).get("user_id")
    lock = WikiPageLock(
        tenant_id=org.id,
        path=rel,
        locked_by_id=uid,
        locked_by_name=_user_label(session, uid),
        reason=(body.reason or None),
    )
    session.add(lock)
    session.commit()
    session.refresh(lock)
    return lock.to_dict()


@router.delete("/api/file")
def delete_file(path: str = Query(...), request: Request = None,
                session: Session = Depends(get_db_session)):
    _check_space_access(request, session, path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    _assert_wiki_unlocked(session, org.id, rel, "deleted")
    if not _s3_exists(org, rel):
        raise HTTPException(status_code=404, detail="File not found")
    _s3_delete(org, rel)
    _trigger_embedder_reindex(path, org=org, deleted=True)
    return {"status": "deleted", "path": path}


@router.post("/api/mkdir")
def make_dir(path: str = Query(...), request: Request = None,
             session: Session = Depends(get_db_session)):
    """S3 has no directories — write a `.keep` placeholder under the
    requested prefix so the tree listing shows the empty folder."""
    _check_space_access(request, session, path, write=True)
    org = _active_org(request, session)
    rel = _normalize_path(path)
    keep = f"{rel}/.keep"
    if not _s3_exists(org, keep):
        _s3_write(org, keep, b"")
    return {"status": "created", "path": path}


# Startup hook, registered on the app by main.py where this router is included.
# Not a `router.on_event` hook: FastAPI 0.142 runs a router's own startup handlers
# twice once the router is included (copied onto the app AND run by the
# router's merged lifespan). test_route_table.py holds the router to none.
def purge_old_recycle_bin():
    """Auto-purge recycled pages older than 30 days on startup."""
    try:
        from sqlalchemy.orm import Session as _Session
        from config_db import SessionLocal as _SL, _init_engine
        _init_engine()
        with _SL() as session:
            cutoff = datetime.utcnow() - timedelta(days=30)
            old = session.query(SoftDeletedPage).filter(
                SoftDeletedPage.status == "recycled",
                SoftDeletedPage.deleted_at < cutoff,
            ).all()
            for item in old:
                session.delete(item)
            session.commit()
            if old:
                log.info("Purged %d expired recycle-bin items", len(old))
    except Exception as exc:
        log.warning("Recycle-bin purge failed: %s", exc)


# ── Pydantic models for new endpoints ─────────────────────────────────────

class MoveFileBody(BaseModel):
    from_path: str
    to_path: str

class RenameDirBody(BaseModel):
    from_path: str
    to_path: str


def _tenant_id_from_request(
    request: Request,
    session: Session = Depends(get_db_session),
) -> str:
    """
    Resolve the tenant ID for the current request from the authenticated
    session org — never from a client-supplied header.

    Previously this trusted an `X-Tenant-Slug` request header (which nginx
    does not set), then fell back to "the first active tenant". Both were
    cross-tenant hazards: a caller could name any org's slug, and absent the
    header every org collapsed onto one shared tenant. `_active_org` derives
    the org from the auth_request-injected `X-Auth-Org-Id`, identical to every
    other editor route.
    """
    return _active_org(request, session).id


# ── Soft-delete / Recycle bin ──────────────────────────────────────────────

@router.post("/api/recycle")
def recycle_file(
    request: Request,
    path: str = Query(...),
    session: Session = Depends(get_db_session),
    tenant_id: str = Depends(_tenant_id_from_request),
):
    """Move a file to the recycle bin (soft delete)."""
    org = _active_org(request, session)
    rel = _normalize_path(path)
    if not _s3_exists(org, rel):
        raise HTTPException(status_code=404, detail="File not found")
    content = _s3_read(org, rel).decode("utf-8", errors="replace")
    entry = SoftDeletedPage(
        original_path=rel,
        name=Path(rel).name,
        content=content,
        tenant_id=tenant_id,
    )
    session.add(entry)
    session.commit()
    _s3_delete(org, rel)
    _trigger_embedder_reindex(rel, org=org, deleted=True)
    return {"status": "recycled", "id": entry.id}


@router.get("/api/recycle-bin")
def list_recycle_bin(
    session: Session = Depends(get_db_session),
    tenant_id: str = Depends(_tenant_id_from_request),
):
    items = (
        session.query(SoftDeletedPage)
        .filter_by(tenant_id=tenant_id)
        .order_by(SoftDeletedPage.deleted_at.desc())
        .all()
    )
    return {"items": [i.to_dict() for i in items]}


@router.post("/api/recycle-bin/{item_id}/restore")
def restore_page(
    item_id: str,
    request: Request,
    session: Session = Depends(get_db_session),
    tenant_id: str = Depends(_tenant_id_from_request),
):
    item = session.query(SoftDeletedPage).filter_by(id=item_id, tenant_id=tenant_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    org = _active_org(request, session)
    rel = _normalize_path(item.original_path)
    _s3_write(org, rel, item.content.encode("utf-8"))
    _trigger_embedder_reindex(item.original_path, org=org)
    session.delete(item)
    session.commit()
    return {"status": "restored", "path": item.original_path}


@router.post("/api/recycle-bin/{item_id}/archive")
def archive_page(
    item_id: str,
    session: Session = Depends(get_db_session),
    tenant_id: str = Depends(_tenant_id_from_request),
):
    item = session.query(SoftDeletedPage).filter_by(id=item_id, tenant_id=tenant_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    item.status = "archived"
    session.commit()
    return {"status": "archived"}


@router.delete("/api/recycle-bin/{item_id}")
def permanent_delete(
    item_id: str,
    session: Session = Depends(get_db_session),
    tenant_id: str = Depends(_tenant_id_from_request),
):
    item = session.query(SoftDeletedPage).filter_by(id=item_id, tenant_id=tenant_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(item)
    session.commit()
    return {"status": "deleted"}


# ── Move file ──────────────────────────────────────────────────────────────

@router.post("/api/move-file")
def move_file(body: MoveFileBody, request: Request,
              session: Session = Depends(get_db_session)):
    org = _active_org(request, session)
    src = _normalize_path(body.from_path)
    dst = _normalize_path(body.to_path)
    _assert_wiki_unlocked(session, org.id, src, "moved")
    if not _s3_exists(org, src):
        raise HTTPException(status_code=404, detail="Source not found")
    if _s3_exists(org, dst):
        raise HTTPException(status_code=409, detail="Destination already exists")
    # S3 has no atomic rename — emulate via copy-then-delete. If the
    # delete fails the source remains and the operation is replayable.
    data = _s3_read(org, src)
    _s3_write(org, dst, data)
    _s3_delete(org, src)
    _trigger_embedder_reindex(src, org=org, deleted=True)
    _trigger_embedder_reindex(dst, org=org)
    return {"status": "moved", "from": body.from_path, "to": body.to_path}


# ── Directory management ───────────────────────────────────────────────────

@router.post("/api/rename-dir")
def rename_dir(body: RenameDirBody, request: Request,
               session: Session = Depends(get_db_session)):
    """S3 has no directories — emulate rename by copy-then-delete on
    every key under the src prefix. Non-atomic; on partial failure the
    operation is replayable."""
    org = _active_org(request, session)
    src = _normalize_path(body.from_path)
    dst = _normalize_path(body.to_path)
    src_prefix = src + "/"
    dst_prefix = dst + "/"
    _assert_wiki_prefix_unlocked(session, org.id, src, "renamed")
    all_keys = list(_s3().walk(org.slug))
    src_keys = [k for k in all_keys if k.startswith(src_prefix)]
    if not src_keys:
        raise HTTPException(status_code=404, detail="Directory not found")
    if any(k.startswith(dst_prefix) for k in all_keys):
        raise HTTPException(status_code=409, detail="Destination already exists")
    for k in src_keys:
        new_key = dst_prefix + k[len(src_prefix):]
        data = _s3().read(org.slug, k)
        _s3_write(org, new_key, data)
        _s3_delete(org, k)
    _trigger_embedder_reindex(org=org)
    return {"status": "renamed", "from": body.from_path, "to": body.to_path}


@router.delete("/api/directory")
def delete_directory(request: Request, path: str = Query(...),
                     session: Session = Depends(get_db_session)):
    """Recycle every .md file under the directory prefix and drop the
    rest. Non-md placeholders (.keep, assets) are deleted outright."""
    org = _active_org(request, session)
    rel = _normalize_path(path)
    prefix = rel + "/"
    _assert_wiki_prefix_unlocked(session, org.id, rel, "deleted")
    keys = [k for k in _s3().walk(org.slug) if k.startswith(prefix)]
    if not keys:
        raise HTTPException(status_code=404, detail="Directory not found")
    recycled = []
    for k in sorted(keys):
        if k.endswith(".md"):
            try:
                content = _s3().read(org.slug, k).decode("utf-8", errors="replace")
            except Exception:
                content = ""
            entry = SoftDeletedPage(
                original_path=k,
                name=Path(k).name,
                content=content,
                tenant_id=org.id,
            )
            session.add(entry)
            recycled.append(k)
        _s3_delete(org, k)
    session.commit()
    _trigger_embedder_reindex(org=org)
    return {"status": "deleted", "recycled_pages": recycled}


@router.get("/api/image")
def get_image(path: str = Query(...), request: Request = None,
              session: Session = Depends(get_db_session)):
    """Serve a binary asset straight from S3."""
    import mimetypes
    org = _active_org(request, session)
    rel = _normalize_path(path)
    data = _s3_read(org, rel)
    mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    return Response(content=data, media_type=mime)


# A pasted screenshot on a card is the case this exists for — the bug report
# that produced it was a screenshot pasted into a chat window, because there
# was nowhere on the card to put it. Cards keep their own key prefix so a
# card's assets are identifiable as such (and collectable later) rather than
# mixed into the wiki's flat assets/images/.
_CARD_PREFIX_RE = re.compile(r"^cards/[a-z0-9][a-z0-9._-]{0,100}$")
# 10MB. Big enough for any screenshot, small enough that a mis-paste of a video
# is refused in a way somebody can read rather than hanging.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# SVG is deliberately NOT here. It is a script-capable document that this
# route serves as image/svg+xml from our own origin, so accepting one is
# accepting stored XSS from anybody who can paste into a card.
_IMAGE_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp",
}
_IMAGE_EXTS = set(_IMAGE_EXT_BY_MIME.values()) | {".jpeg"}


@router.post("/api/upload-image")
async def upload_image(request: Request, file: UploadFile = File(...),
                       prefix: str = Form(""),
                       session: Session = Depends(get_db_session)):
    """Accept a pasted/uploaded image and save it under assets/images/, or
    under a card's own prefix when one is given."""
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in _IMAGE_EXTS:
        ext = _IMAGE_EXT_BY_MIME.get((file.content_type or "").lower(), "")
    # Refusing by name rather than silently storing it as a .png: an upload
    # that "worked" and then renders as a broken image is the worst outcome,
    # because nothing says what went wrong or where.
    if not ext:
        raise HTTPException(
            status_code=400,
            detail=(f"{file.filename or 'That file'} is not an image this board "
                    "accepts. PNG, JPEG, GIF or WebP."))
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"That image is {len(data) // (1024 * 1024)}MB. The limit is "
                    f"{MAX_IMAGE_BYTES // (1024 * 1024)}MB — link to it instead, "
                    "or scale it down."))

    folder = "assets/images"
    if prefix:
        # An allowlisted shape, not a caller-chosen path. _normalize_path below
        # stops traversal, but a free-form prefix would still let a caller
        # scatter objects anywhere in the org's bucket.
        if not _CARD_PREFIX_RE.match(prefix):
            raise HTTPException(status_code=400, detail="Invalid upload prefix")
        folder = prefix

    name = f"paste-{int(time.time())}-{secrets.token_hex(4)}{ext}"
    org = _active_org(request, session)
    rel = _normalize_path(f"{folder}/{name}")
    _s3_write(org, rel, data)
    return {"path": rel}


# 25MB for an arbitrary attachment — a log, a config, a PDF. Same reasoning as
# the image cap: large enough to be useful, small enough to fail readably.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _attachment_display_name(key: str) -> str:
    """The original filename, recovered from the stored key.

    The key carries it — `<ts>-<rand>-<safe name>` — so the list on a card
    needs no second place to remember what a file was called, and there is no
    way for the two to disagree.
    """
    base = key.rsplit("/", 1)[-1]
    parts = base.split("-", 2)
    return parts[2] if len(parts) == 3 else base


@router.post("/api/upload-file")
async def upload_file(request: Request, file: UploadFile = File(...),
                      prefix: str = Form(...),
                      session: Session = Depends(get_db_session)):
    """Accept an arbitrary attachment for a card."""
    if not _CARD_PREFIX_RE.match(prefix or ""):
        raise HTTPException(status_code=400, detail="Invalid upload prefix")
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"That file is {len(data) // (1024 * 1024)}MB. The limit is "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB."))
    safe = _SAFE_NAME_RE.sub("_", Path(file.filename or "file").name).strip("._") or "file"
    safe = safe[:80]
    org = _active_org(request, session)
    rel = _normalize_path(f"{prefix}/{int(time.time())}-{secrets.token_hex(4)}-{safe}")
    _s3_write(org, rel, data)
    return {"path": rel, "name": _attachment_display_name(rel), "size": len(data)}


@router.get("/api/attachment")
def get_attachment(path: str = Query(...), request: Request = None,
                   session: Session = Depends(get_db_session)):
    """Serve a card attachment as a DOWNLOAD, never as a document.

    Unlike /api/image this accepts arbitrary uploaded content, so it must not
    be possible to navigate to one and have the browser run it. An uploaded
    .html or .svg served inline from our own origin is stored XSS against
    everybody who can see the board. Three things prevent that, and all three
    are needed: a neutral content type, `nosniff` so the browser does not
    second-guess it, and an attachment disposition so it is saved rather than
    rendered.
    """
    org = _active_org(request, session)
    rel = _normalize_path(path)
    data = _s3_read(org, rel)
    name = _attachment_display_name(rel)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
