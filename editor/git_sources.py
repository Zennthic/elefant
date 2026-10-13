"""
Git knowledge sources: the helpers behind the admin API in
routers/git_sources.py.

Looking a source up within the caller's org, telling the embedder to sync,
reindex or drop one, and holding an admin-supplied branch and repo URL to a
shape that git cannot read as an option.

Moved out of main.py by CQ-04 phase 6. Nothing here imports main.
"""
import logging
import re

from fastapi import HTTPException

from config_db import GitSource, Tenant, DEFAULT_TENANT_SLUG
from internal_service import EMBEDDER_URL, _internal_service_headers

log = logging.getLogger("editor")


def _git_source_or_404(session, source_id: str, org_id: str) -> GitSource:
    """Look up a source, scoped to the caller's org. 404 if it belongs to a
    different org or doesn't exist — orgs cannot even probe for each other's
    source IDs."""
    src = session.query(GitSource).filter_by(id=source_id, org_id=org_id).first()
    if src is None:
        raise HTTPException(status_code=404, detail="Git source not found")
    return src


def _org_slug(session, org_id: str) -> str:
    """Resolve an org_id to its slug for path/collection routing."""
    t = session.query(Tenant).filter_by(id=org_id).first()
    return t.slug if t else DEFAULT_TENANT_SLUG


def _proxy_embedder(method: str, path: str) -> None:
    """Fire-and-forget call to embedder. Silently ignores errors."""
    import urllib.request as _urlreq
    headers = _internal_service_headers()
    def _do():
        try:
            req = _urlreq.Request(
                f"{EMBEDDER_URL}{path}", data=b"", method=method, headers=headers,
            )
            with _urlreq.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            log.debug("Embedder proxy %s %s failed: %s", method, path, exc)
    import threading as _t
    _t.Thread(target=_do, daemon=True).start()


_TOOL_AVAILABILITY = ("on", "off", "admin_off")


def _norm_tool_availability(v) -> str:
    """Clamp to a valid tool-availability value; unknown/missing → 'on'."""
    return v if v in _TOOL_AVAILABILITY else "on"


def _norm_sync_interval(v) -> int:
    """Sync interval in minutes. None → 60 (default); 0 → no recurring sync
    (manual / once). Negatives clamp to 0. Note: must NOT use `or 60`, which
    would turn a legitimate 0 back into 60."""
    if v is None:
        return 60
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 60


# A git source's branch and URL end up as git arguments in the embedder. One
# that starts with `-` would be read as an option, and a scheme like `ext::`
# runs a command, so both are held to a plain shape here. The embedder checks
# again at the point of use, for rows written before this existed.
_GIT_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
_GIT_URL_RE = re.compile(
    r"(?:https|ssh)://(?:[^\s@/]+@)?[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?(?:/\S*)?"
    r"|[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*:\S+")


def _valid_git_branch(value) -> str:
    branch = str(value or "").strip() or "main"
    if (len(branch) > 255 or branch.startswith("-")
            or not _GIT_BRANCH_RE.fullmatch(branch)):
        raise HTTPException(
            status_code=422,
            detail="branch may contain only letters, digits, '.', '_', '/' and '-', "
                   "and must not start with '-'")
    return branch


def _valid_git_repo_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="repo_url is required")
    if (len(url) > 1024
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in url)
            or not _GIT_URL_RE.fullmatch(url)):
        raise HTTPException(
            status_code=422,
            detail="repo_url must be an https:// or ssh:// URL, or user@host:path")
    return url
