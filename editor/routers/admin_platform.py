"""
The editor's platform admin web API, and the org's domains.

`legacy_router` is the 410 left where /api/admin/tenants used to be.
`router` is the platform admin console (multi-org mode): org lifecycle, the
platform-scoped domain routes and platform admin self-management, plus
the startup hooks that bootstrap the first platform admin and purge orgs
whose recycle-bin window has run out. `domains_router` is the org-scoped
domain CRUD and CNAME verification, and the legacy single-domain
endpoints. main.py includes each one where its routes used to be
declared, so the route order and the startup hook order are unchanged.
The domain helpers they share with main.py are in orgs.py.

Moved out of main.py by CQ-04 phase 5; the paths are unchanged.
"""
import logging
import os
import secrets
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from auth import (
    SESSION_COOKIE, decode_session_token, generate_invite, hash_password,
    require_org_admin, require_platform_admin, require_team_admin,
)
from auth_helpers import (
    APP_NAME, _STATIC, _editor_base_url, _render, _is_multi_org_mode, _require_multi_org,
)
from config_db import (
    get_db_session, ApiToken, OAuthClient, OAuthAccessToken, SoftDeletedPage,
    UserAccount, Team, Group, TeamMembership, GroupMembership, GroupTeam,
    InviteToken, MfaCredential,
    WikiSpaceACL, GitSourceACL, MemorySharingPolicy, MemoryEditNotification,
    Tenant, OrgDomain, OidcProvider, SamlProvider,
    MemoryBackupConfig, WikiBackupConfig,
    OrgBackupS3Config, OrgBackupSSHTarget, OrgBackupSchedule,
    DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG, tenant_docs_root,
)
from internal_service import EMBEDDER_URL, MEMORY_API_URL, _internal_service_headers
from org_backup import _get_s3_config_row, _get_ssh_config_row
from orgs import (
    _domain_to_dict, _run_cname_check, _slugify, _validate_domain_format,
    _verify_host, _verify_target, set_primary_domain,
)
from storage import get_s3_storage
from wiki_storage import _ensure_home_page

log = logging.getLogger("editor")

legacy_router = APIRouter()
router = APIRouter()
domains_router = APIRouter()


# ── Legacy /api/admin/tenants — gone in Phase 6 ────────────────────────────
# The Tenant model is now exposed only as Org via the platform admin
# console.  The legacy CRUD endpoints had no role guard and let any logged-in
# user create / modify tenants — fixed by removing them and pointing
# clients at /api/platform/orgs.

@legacy_router.api_route("/api/admin/tenants{rest:path}",
                         methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
def legacy_tenants_gone(rest: str = ""):
    raise HTTPException(
        status_code=410,
        detail="Tenant CRUD has moved.  Platform admins manage Orgs at "
               "/api/platform/orgs; per-org IdP config lives at "
               "/api/admin/identity-providers (org-scoped to the caller).",
    )



# ── Platform Admin (multi-org mode only) ────────────────────────────────────
# Platform Admins are global super-admins that exist outside any single Org.
# They manage Org lifecycle (create, offboard, MFA policy, domain ownership).
# Bootstrap: env-driven on first run via PLATFORM_ADMIN_USERNAME / _PASSWORD.


# Startup hook, registered on the app by main.py where this router is included.
# Not a `router.on_event` hook: FastAPI 0.142 runs a router's own startup handlers
# twice once the router is included (copied onto the app AND run by the
# router's merged lifespan). test_route_table.py holds the router to none.
def bootstrap_platform_admin() -> None:
    """
    On first boot in multi-org mode, create a Platform Admin from env vars if one
    doesn't already exist.  This is the only way to get into a fresh multi-org
    deployment — there's no /setup page for the platform admin.
    """
    if not _is_multi_org_mode():
        return
    pa_user = os.environ.get("PLATFORM_ADMIN_USERNAME", "").strip().lower()
    pa_pass = os.environ.get("PLATFORM_ADMIN_PASSWORD", "")
    if not pa_user or not pa_pass:
        log.warning(
            "Multi-org mode is active but PLATFORM_ADMIN_USERNAME / "
            "PLATFORM_ADMIN_PASSWORD are not set — no platform admin will exist."
        )
        return
    try:
        from config_db import SessionLocal as _SL, _init_engine
        _init_engine()
        with _SL() as session:
            existing = session.query(UserAccount).filter_by(
                role="platform_admin", username=pa_user
            ).first()
            if existing:
                return
            user = UserAccount(
                id=secrets.token_hex(16),
                org_id=None,
                username=pa_user,
                full_username=pa_user,  # platform admins use bare usernames
                display_name=pa_user,
                password_hash=hash_password(pa_pass),
                role="platform_admin",
                auth_method="local",
                is_active=True,
            )
            session.add(user)
            session.commit()
            log.info("Bootstrapped platform admin: %s", pa_user)
    except Exception as exc:
        log.warning("Platform admin bootstrap failed: %s", exc)


@router.get("/platform/admin", response_class=HTMLResponse)
def platform_admin_page(request: Request, session: Session = Depends(get_db_session)):
    _require_multi_org()
    claims = decode_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if not claims or claims.get("role") != "platform_admin":
        return RedirectResponse("/login?next=/platform/admin", status_code=302)
    return HTMLResponse(_render(_STATIC / "platform_admin.html", {
        "<!-- APP_NAME -->": APP_NAME,
    }))


@router.get("/api/platform/orgs")
def platform_list_orgs(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    rows = session.query(Tenant).order_by(Tenant.name).all()
    out = []
    for o in rows:
        admins = session.query(UserAccount).filter_by(
            org_id=o.id, role="org_admin"
        ).count()
        users = session.query(UserAccount).filter_by(org_id=o.id).count()
        teams = session.query(Team).filter_by(org_id=o.id).count()
        out.append({**o.to_dict(), "admin_count": admins,
                    "user_count": users, "team_count": teams})
    return {"orgs": out}


@router.post("/api/platform/orgs", status_code=201)
def platform_create_org(
    body: dict,
    request: Request,
    claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    name = (body.get("name") or "").strip()
    slug = _slugify(body.get("slug") or name)
    domain = (body.get("domain") or f"{slug}.local").strip().lower()
    max_teams = int(body.get("max_teams") or 5)
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if slug == DEFAULT_TENANT_SLUG:
        raise HTTPException(status_code=422, detail="'default' is reserved")
    if session.query(Tenant).filter_by(slug=slug).first():
        raise HTTPException(status_code=409, detail=f"slug '{slug}' already exists")

    primary_token = secrets.token_hex(8)
    org = Tenant(
        id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        display_name=body.get("display_name") or name,
        domain=domain,
        domain_verified=False,
        domain_verify_token=primary_token,
        mfa_policy=body.get("mfa_policy") or "enforced",
        max_teams=max_teams,
        setup_complete=False,
        saas_plan=body.get("saas_plan"),
        is_active=True,
    )
    session.add(org)
    session.flush()  # need org.id before inserting OrgDomain

    # Seed the primary OrgDomain row alongside the Tenant.  CNAME
    # verification happens later via /api/admin/org/domains/{id}/verify.
    session.add(OrgDomain(
        org_id=org.id,
        domain=domain,
        is_primary=True,
        verified=False,
        verify_token=primary_token,
        verified_at=None,
    ))

    # New orgs default to encrypted 24h auto-backups to every configured
    # target. Backups won't actually run until a passcode (platform-global or
    # per-org override) is set — hard-enforced encryption.
    _s3 = _get_s3_config_row(session)
    _ssh = _get_ssh_config_row(session)
    session.add(OrgBackupSchedule(
        org_id=org.id,
        enabled=True,
        interval_hours=24,
        retention_days=30,
        use_local=True,
        use_s3=bool(_s3 and _s3.enabled and _s3.bucket),
        use_ssh=bool(_ssh and _ssh.enabled and _ssh.host),
    ))

    session.commit()
    session.refresh(org)

    # Eagerly provision a per-org S3 bucket when the platform is configured for
    # object storage. The org's storage_backend flag is updated to "s3" so
    # later PRs can route file CRUD through the S3 path. Failures are logged
    # and non-fatal — the org still exists on the legacy /docs path.
    s3 = get_s3_storage()
    if s3 is not None:
        try:
            s3.provision_wiki(org.slug, seed_welcome=False)
            org.storage_backend = "s3"
            session.commit()
            _ensure_home_page(org)
            log.info("Provisioned S3 bucket for org %s (slug=%s)", org.id, org.slug)
        except Exception as exc:
            log.error("Failed to provision S3 bucket for org %s: %s", org.slug, exc)

    # Generate an invite for the first org admin
    invite = generate_invite(
        session,
        org_id=org.id,
        target_role="org_admin",
        created_by=claims["uid"],
        target_email=body.get("admin_email"),
    )
    base = _editor_base_url(request).rsplit("/editor", 1)[0]
    return {
        "org": org.to_dict(),
        "invite": {
            "id": invite.id,
            "code": invite.code,
            "url_path": f"/invite/{invite.id}",
            "invite_url": f"{base}/invite/{invite.id}",
            "expires_at": invite.expires_at.isoformat(),
        },
    }


@router.patch("/api/platform/orgs/{org_id}")
def platform_update_org(
    org_id: str,
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    for key in ("name", "display_name", "domain", "saas_plan"):
        if key in body and body[key] is not None:
            setattr(org, key, body[key])
    if "domain" in body:
        org.domain_verified = False
        org.domain_verify_token = secrets.token_urlsafe(16)
    if "mfa_policy" in body and body["mfa_policy"] in ("enforced", "optional"):
        org.mfa_policy = body["mfa_policy"]
    if "max_teams" in body:
        org.max_teams = int(body["max_teams"])
    if "is_active" in body:
        org.is_active = bool(body["is_active"])
    session.commit()
    return org.to_dict()


@router.delete("/api/platform/orgs/{org_id}", status_code=204)
def platform_offboard_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Soft-offboard: deactivate the org but keep its data for a grace period."""
    _require_multi_org()
    if org_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Cannot delete the default org")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    org.is_active = False
    org.lifecycle_status = "offboarded"
    _invalidate_org_logins(org, session)
    session.commit()
    return Response(status_code=204)


# ── Org lifecycle helpers ───────────────────────────────────────────────────

def _invalidate_org_logins(org: "Tenant", session: "Session") -> dict:
    """Revoke all persistent login artifacts scoped to this org.

    Deletes ApiToken and OAuthAccessToken rows for the org.  Stateless JWT
    session cookies cannot be revoked server-side; they are rejected on the
    next request by the /api/auth/check gate.  Returns a summary dict.
    """
    api_tokens = session.query(ApiToken).filter_by(
        tenant_id=org.id
    ).delete(synchronize_session=False)
    oauth_tokens = session.query(OAuthAccessToken).filter_by(
        tenant_id=org.id
    ).delete(synchronize_session=False)
    return {"api_tokens": api_tokens, "oauth_access_tokens": oauth_tokens}


def _purge_org_data(org: "Tenant", session: "Session") -> dict:
    """
    Permanently delete an org and all its data.  Removes:
      - All related rows in config DB (cascading through FKs manually)
      - Wiki filesystem tree for the org
      - Local backup archives
      - Qdrant collections (via embedder) — best effort
      - Memory-api events (via memory-api) — best effort
      - Per-org S3 bucket (drains all object versions, then deletes the
        bucket itself) — best effort, only for storage_backend='s3' orgs
    Returns a summary dict of what was purged.
    """
    slug = org.slug
    org_id = org.id
    # storage_backend is captured before the org row is deleted below;
    # the S3 teardown happens after the DB cascade so that any in-flight
    # mirror writes from the editor have already been rejected.
    storage_backend = org.storage_backend or "legacy"
    summary: dict = {"db_rows": 0, "wiki_files": False, "backups": False,
                     "qdrant": False, "memory_api": False, "s3_bucket": False}

    # ── Config DB cascade ────────────────────────────────────────────────
    def _del(model, **filters):
        n = session.query(model).filter_by(**filters).delete(synchronize_session=False)
        summary["db_rows"] += n

    # Tables that reference org_id / tenant_id directly
    _del(SoftDeletedPage,       tenant_id=org_id)
    _del(WikiSpaceACL,          org_id=org_id)
    _del(GitSourceACL,          org_id=org_id)
    _del(MemorySharingPolicy,   org_id=org_id)
    _del(OrgBackupSchedule,     org_id=org_id)
    _del(OrgBackupS3Config,     org_id=org_id)
    _del(OrgBackupSSHTarget,    org_id=org_id)
    _del(MemoryBackupConfig,    tenant_id=slug)
    _del(WikiBackupConfig,      tenant_id=slug)
    _del(InviteToken,           org_id=org_id)
    _del(OidcProvider,          tenant_id=org_id)
    _del(SamlProvider,          tenant_id=org_id)
    _del(ApiToken,              tenant_id=org_id)
    _del(OAuthAccessToken,      tenant_id=org_id)
    _del(OAuthClient,           tenant_id=org_id)
    _del(OrgDomain,             org_id=org_id)

    # Users and their sub-records
    user_ids = [u.id for u in session.query(UserAccount.id).filter_by(org_id=org_id)]
    if user_ids:
        session.query(MfaCredential).filter(
            MfaCredential.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        session.query(MemoryEditNotification).filter(
            MemoryEditNotification.original_author_id.in_(user_ids)
        ).delete(synchronize_session=False)
        summary["db_rows"] += len(user_ids)

        # Membership tables
        team_ids = [t.id for t in session.query(Team.id).filter_by(org_id=org_id)]
        group_ids = [g.id for g in session.query(Group.id).filter_by(org_id=org_id)]
        if team_ids:
            session.query(TeamMembership).filter(
                TeamMembership.team_id.in_(team_ids)
            ).delete(synchronize_session=False)
        if group_ids:
            session.query(GroupMembership).filter(
                GroupMembership.group_id.in_(group_ids)
            ).delete(synchronize_session=False)
            session.query(GroupTeam).filter(
                GroupTeam.group_id.in_(group_ids)
            ).delete(synchronize_session=False)

    _del(Group,       org_id=org_id)
    _del(Team,        org_id=org_id)
    _del(UserAccount, org_id=org_id)
    session.delete(org)
    session.commit()

    # ── Wiki filesystem ──────────────────────────────────────────────────
    try:
        wiki_root = tenant_docs_root(slug)
        if wiki_root.exists():
            shutil.rmtree(wiki_root)
            summary["wiki_files"] = True
    except Exception as exc:
        log.warning("purge_org(%s): wiki rmtree failed: %s", slug, exc)

    # ── Local backup archives ────────────────────────────────────────────
    try:
        backup_dir = Path("/data/org-backups") / slug
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
            summary["backups"] = True
    except Exception as exc:
        log.warning("purge_org(%s): backup rmtree failed: %s", slug, exc)

    # ── Qdrant collections (embedder) ────────────────────────────────────
    try:
        import httpx as _httpx
        resp = _httpx.delete(
            f"{EMBEDDER_URL}/api/tenant/{slug}/collections",
            headers=_internal_service_headers(),
            timeout=15,
        )
        summary["qdrant"] = resp.status_code < 400
    except Exception as exc:
        log.warning("purge_org(%s): embedder purge failed: %s", slug, exc)

    # ── Memory-api events ────────────────────────────────────────────────
    try:
        import httpx as _httpx
        resp = _httpx.delete(
            f"{MEMORY_API_URL}/tenant/{slug}",
            headers=_internal_service_headers(),
            timeout=15,
        )
        summary["memory_api"] = resp.status_code < 400
    except Exception as exc:
        log.warning("purge_org(%s): memory-api purge failed: %s", slug, exc)

    # ── Per-org S3 bucket ────────────────────────────────────────────────
    # Only s3-backed orgs ever provisioned a bucket. deprovision_org drains
    # every object version + delete-marker before dropping the bucket so a
    # subsequent re-create with the same slug would not collide on stale
    # state. Routine PITR for live orgs is handled by S3 bucket versioning
    # (enabled at provision time) + operator-managed lifecycle rules.
    if storage_backend == "s3":
        s3 = get_s3_storage()
        if s3 is not None:
            try:
                s3.deprovision_org(slug)
                summary["s3_bucket"] = True
            except Exception as exc:
                log.warning("purge_org(%s): S3 bucket deprovision failed: %s", slug, exc)
        else:
            log.warning("purge_org(%s): org is s3-backed but get_s3_storage() returned None — "
                        "bucket left in place; remove manually via S3 console", slug)

    log.info("Purged org slug=%s: %s", slug, summary)
    return summary


@router.post("/api/platform/orgs/{org_id}/recycle", status_code=200)
def platform_recycle_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Move an offboarded or archived org to the Recycle Bin (90-day TTL)."""
    _require_multi_org()
    if org_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Cannot delete the default org")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.lifecycle_status not in ("offboarded", "archived"):
        raise HTTPException(status_code=400,
                            detail="Only offboarded or archived orgs can be moved to Recycle Bin")
    org.lifecycle_status = "recycled"
    org.is_active = False
    org.recycled_at = datetime.utcnow()
    _invalidate_org_logins(org, session)
    session.commit()
    return org.to_dict()


@router.post("/api/platform/orgs/{org_id}/archive", status_code=200)
def platform_archive_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Move an offboarded org to the Archived Orgs section."""
    _require_multi_org()
    if org_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Cannot archive the default org")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.lifecycle_status != "offboarded":
        raise HTTPException(status_code=400, detail="Only offboarded orgs can be archived")
    org.lifecycle_status = "archived"
    org.is_active = False
    _invalidate_org_logins(org, session)
    session.commit()
    return org.to_dict()


@router.post("/api/platform/orgs/{org_id}/restore", status_code=200)
def platform_restore_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Restore a recycled or archived org back to offboarded status."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.lifecycle_status not in ("recycled", "archived"):
        raise HTTPException(status_code=400,
                            detail="Only recycled or archived orgs can be restored")
    org.lifecycle_status = "offboarded"
    org.is_active = False
    org.recycled_at = None
    session.commit()
    return org.to_dict()


@router.post("/api/platform/orgs/{org_id}/reactivate", status_code=200)
def platform_reactivate_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Reactivate an offboarded org, returning it to active status."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.lifecycle_status != "offboarded":
        raise HTTPException(status_code=400,
                            detail="Only offboarded orgs can be reactivated")
    org.lifecycle_status = "active"
    org.is_active = True
    session.commit()
    return org.to_dict()


@router.delete("/api/platform/orgs/{org_id}/purge", status_code=200)
def platform_purge_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Permanently and irreversibly delete a recycled or archived org and all its data."""
    _require_multi_org()
    if org_id == DEFAULT_TENANT_ID:
        raise HTTPException(status_code=400, detail="Cannot purge the default org")
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if org.lifecycle_status not in ("recycled", "archived"):
        raise HTTPException(status_code=400,
                            detail="Only recycled or archived orgs can be permanently deleted")
    slug = org.slug
    summary = _purge_org_data(org, session)
    return {"purged": True, "slug": slug, "summary": summary}


# Startup hook, registered on the app by main.py where this router is included.
# Not a `router.on_event` hook: FastAPI 0.142 runs a router's own startup handlers
# twice once the router is included (copied onto the app AND run by the
# router's merged lifespan). test_route_table.py holds the router to none.
def _auto_purge_expired_recycle_bin() -> None:
    """Purge orgs in the Recycle Bin that have been there for more than 90 days."""
    if not _is_multi_org_mode():
        return
    try:
        from config_db import SessionLocal as _SL, _init_engine
        _init_engine()
        cutoff = datetime.utcnow() - timedelta(days=90)
        with _SL() as session:
            expired = session.query(Tenant).filter(
                Tenant.lifecycle_status == "recycled",
                Tenant.recycled_at <= cutoff,
            ).all()
            for org in expired:
                log.info("Auto-purging expired recycle-bin org: %s (recycled %s)",
                         org.slug, org.recycled_at)
                try:
                    _purge_org_data(org, session)
                except Exception as exc:
                    log.error("Auto-purge failed for org %s: %s", org.slug, exc)
    except Exception as exc:
        log.warning("_auto_purge_expired_recycle_bin failed: %s", exc)


@router.post("/api/platform/orgs/{org_id}/invite-admin")
def platform_invite_org_admin(
    org_id: str,
    body: dict,
    request: Request,
    claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    invite = generate_invite(
        session, org_id=org.id, target_role="org_admin",
        created_by=claims["uid"], target_email=body.get("email"),
    )
    base = _editor_base_url(request).rsplit("/editor", 1)[0]
    return {"invite": {
        "id": invite.id, "code": invite.code,
        "url_path": f"/invite/{invite.id}",
        "invite_url": f"{base}/invite/{invite.id}",
        "expires_at": invite.expires_at.isoformat(),
    }}


# ── Platform-scoped domain management ──────────────────────────────────────
# Platform admins manage every Org's domains.  These mirror the org-scoped
# endpoints in /api/admin/org/domains but accept an explicit org_id in the
# path instead of deriving it from the caller's claims.

@router.get("/api/platform/orgs/{org_id}/domains")
def platform_list_org_domains(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    rows = session.query(OrgDomain).filter_by(org_id=org_id).order_by(
        OrgDomain.is_primary.desc(), OrgDomain.created_at
    ).all()
    return {"domains": [_domain_to_dict(d) for d in rows]}


@router.post("/api/platform/orgs/{org_id}/domains", status_code=201)
def platform_add_org_domain(
    org_id: str,
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    domain = _validate_domain_format(body.get("domain") or "")
    existing = session.query(OrgDomain).filter_by(domain=domain).first()
    if existing:
        raise HTTPException(status_code=409, detail="Domain already claimed by another org")
    has_primary = session.query(OrgDomain).filter_by(org_id=org_id, is_primary=True).first()
    row = OrgDomain(
        org_id=org_id,
        domain=domain,
        is_primary=not has_primary,
        verified=False,
        verify_token=secrets.token_hex(8),
        verified_at=None,
    )
    session.add(row)
    # Keep Tenant.domain in sync if this is the org's first (and therefore
    # primary) domain.  Otherwise leave it pointing at whatever already
    # holds the primary flag.
    if not has_primary:
        org.domain = domain
        org.domain_verify_token = row.verify_token
        org.domain_verified = False
    session.commit()
    session.refresh(row)
    return _domain_to_dict(row)


@router.post("/api/platform/orgs/{org_id}/domains/{domain_id}/verify")
def platform_verify_org_domain(
    org_id: str,
    domain_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    row = session.query(OrgDomain).filter_by(id=domain_id, org_id=org_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")
    host     = _verify_host(row.domain)
    expected = _verify_target(row.verify_token)
    matched, found, err = _run_cname_check(host, expected)
    payload = {
        "verified": matched, "found": found,
        "expected_host": host, "expected_target": expected,
    }
    if matched:
        row.verified = True
        row.verified_at = datetime.utcnow()
        if row.is_primary:
            org = session.query(Tenant).filter_by(id=row.org_id).first()
            if org:
                org.domain_verified = True
        session.commit()
    elif err:
        payload["error"] = err
    else:
        payload["error"] = "CNAME record not found or target did not match"
    return payload


@router.post("/api/platform/orgs/{org_id}/domains/{domain_id}/promote")
def platform_promote_org_domain(
    org_id: str,
    domain_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    target = set_primary_domain(session, org, domain_id)
    session.commit()
    return _domain_to_dict(target)


@router.delete("/api/platform/orgs/{org_id}/domains/{domain_id}", status_code=204)
def platform_delete_org_domain(
    org_id: str,
    domain_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    row = session.query(OrgDomain).filter_by(id=domain_id, org_id=org_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")
    if row.is_primary:
        raise HTTPException(status_code=400,
            detail="Cannot delete the primary domain.  Promote another first.")
    suffix = f"@{row.domain}"
    user_count = session.query(UserAccount).filter(
        UserAccount.org_id == row.org_id,
        UserAccount.full_username.endswith(suffix),
    ).count()
    if user_count:
        raise HTTPException(status_code=400,
            detail=f"{user_count} user(s) live on this domain.  Move or delete them first.")
    session.delete(row)
    session.commit()
    return Response(status_code=204)


# ── Platform admin self-management ──────────────────────────────────────────
# Lets an existing platform_admin add or remove others.  The UI lives on
# /platform/admin (platform_admin.html, "Platform admins" card).

@router.get("/api/platform/admins")
def platform_list_admins(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    rows = session.query(UserAccount).filter_by(role="platform_admin").order_by(
        UserAccount.created_at
    ).all()
    return {"admins": [u.to_dict() for u in rows]}


@router.post("/api/platform/admins", status_code=201)
def platform_create_admin(
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    display_name = (body.get("display_name") or "").strip() or username
    if not username:
        raise HTTPException(status_code=422, detail="username is required")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if session.query(UserAccount).filter_by(full_username=username).first():
        raise HTTPException(status_code=409, detail=f"Username '{username}' already exists")
    user = UserAccount(
        id=secrets.token_hex(16),
        org_id=None,
        username=username,
        full_username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        role="platform_admin",
        auth_method="local",
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.to_dict()


@router.delete("/api/platform/admins/{user_id}", status_code=204)
def platform_delete_admin(
    user_id: str,
    claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    _require_multi_org()
    if user_id == claims.get("uid"):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    target = session.query(UserAccount).filter_by(id=user_id, role="platform_admin").first()
    if not target:
        raise HTTPException(status_code=404, detail="Platform admin not found")
    remaining = session.query(UserAccount).filter_by(role="platform_admin").count()
    if remaining <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last platform admin")
    session.delete(target)
    session.commit()
    return Response(status_code=204)


@router.patch("/api/platform/users/{user_id}/mfa-override")
def platform_user_mfa_override(
    user_id: str,
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Only platform admins can disable MFA for an admin-tier user."""
    _require_multi_org()
    target = session.query(UserAccount).filter_by(id=user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    cred = session.query(MfaCredential).filter_by(user_id=user_id).first()
    enabled = bool(body.get("enabled", False))
    if cred:
        cred.is_enabled = enabled
        cred.last_reset_at = datetime.utcnow()
    session.commit()
    # Disabling MFA on another platform_admin is the highest-blast-radius
    # MFA action in the system (a compromised platform_admin could sweep
    # every peer's MFA off before anyone notices). Emit a loud audit line
    # so the action is at least visible in the logs. A future hardening
    # step is a two-person rule for cross-platform_admin MFA changes.
    if target.role == "platform_admin" and not enabled:
        log.warning(
            "AUDIT: platform_admin %s disabled MFA for platform_admin %s",
            _claims.get("uid"), user_id,
        )
    return {"user_id": user_id, "mfa_enabled": enabled}



# ── Org-scoped domain CRUD (multi-domain, CNAME verification) ──────────────

@domains_router.get("/api/admin/org/domains")
def list_org_domains(
    claims: dict = Depends(require_team_admin),
    session: Session = Depends(get_db_session),
):
    """List the caller's org's domains (primary first).
    Read-only access is open to team admins so they can populate the
    domain dropdown when creating users; write access remains org_admin-only."""
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    rows = session.query(OrgDomain).filter_by(org_id=org_id).order_by(
        OrgDomain.is_primary.desc(), OrgDomain.created_at
    ).all()
    return {"domains": [_domain_to_dict(d) for d in rows]}


@domains_router.post("/api/admin/org/domains", status_code=201)
def add_org_domain(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """
    Claim an additional domain for the caller's org.  Returns CNAME
    instructions; the customer must add the record then call /verify.
    """
    org_id = claims.get("oid")
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    domain = _validate_domain_format(body.get("domain") or "")
    # Cross-org uniqueness: refuse if any other org has claimed it.
    existing = session.query(OrgDomain).filter_by(domain=domain).first()
    if existing:
        raise HTTPException(status_code=409,
            detail="Domain already claimed.  If this is your domain and the claim "
                   "is stale, contact Zennthic support.")
    # First domain on a brand-new org becomes primary automatically.
    has_primary = session.query(OrgDomain).filter_by(org_id=org_id, is_primary=True).first()
    row = OrgDomain(
        org_id=org_id,
        domain=domain,
        is_primary=not has_primary,
        verified=False,
        verify_token=secrets.token_hex(8),
        verified_at=None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _domain_to_dict(row)


@domains_router.post("/api/admin/org/domains/{domain_id}/verify")
def verify_org_domain_cname(
    domain_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Run the CNAME lookup and flip ``verified`` if the target matches."""
    row = session.query(OrgDomain).filter_by(
        id=domain_id, org_id=claims.get("oid")
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")
    host     = _verify_host(row.domain)
    expected = _verify_target(row.verify_token)
    matched, found, err = _run_cname_check(host, expected)
    payload = {
        "verified": matched, "found": found,
        "expected_host": host, "expected_target": expected,
    }
    if matched:
        row.verified = True
        row.verified_at = datetime.utcnow()
        # If this is the primary, also keep Tenant.domain_verified in sync.
        if row.is_primary:
            org = session.query(Tenant).filter_by(id=row.org_id).first()
            if org:
                org.domain_verified = True
        session.commit()
    elif err:
        payload["error"] = err
    else:
        payload["error"] = "CNAME record not found or target did not match"
    return payload


@domains_router.post("/api/admin/org/domains/{domain_id}/promote")
def promote_org_domain(
    domain_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Promote a verified secondary domain to primary."""
    org = session.query(Tenant).filter_by(id=claims.get("oid")).first()
    if not org:
        raise HTTPException(status_code=400, detail="Caller has no org context")
    target = set_primary_domain(session, org, domain_id)
    session.commit()
    return _domain_to_dict(target)


@domains_router.delete("/api/admin/org/domains/{domain_id}", status_code=204)
def delete_org_domain(
    domain_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Delete a non-primary domain.  Refuses if any user lives on it."""
    row = session.query(OrgDomain).filter_by(
        id=domain_id, org_id=claims.get("oid")
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")
    if row.is_primary:
        raise HTTPException(status_code=400,
            detail="Cannot delete the primary domain.  Promote another first.")
    # Refuse if any user has full_username on this domain
    suffix = f"@{row.domain}"
    user_count = session.query(UserAccount).filter(
        UserAccount.org_id == row.org_id,
        UserAccount.full_username.endswith(suffix),
    ).count()
    if user_count:
        raise HTTPException(status_code=400, detail=(
            f"{user_count} user(s) live on this domain.  Move or delete them first."
        ))
    session.delete(row)
    session.commit()
    return Response(status_code=204)


# ── Legacy single-domain endpoints (Phase 5; kept as thin wrappers) ────────
# These pre-Phase-7 endpoints worked on Tenant.domain only.  They now read
# / write the primary OrgDomain row so callers using the old paths keep
# working.  They will be removed once the admin UI is fully migrated.

@domains_router.get("/api/admin/org/domain-verify-instructions")
def domain_verify_instructions(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    org = session.query(Tenant).filter_by(id=claims.get("oid")).first()
    if not org or not org.domain:
        raise HTTPException(status_code=400, detail="Org has no domain configured")
    primary = session.query(OrgDomain).filter_by(org_id=org.id, is_primary=True).first()
    if not primary:
        raise HTTPException(status_code=500, detail="Org has no primary OrgDomain row")
    return {
        "domain":         primary.domain,
        "verified":       bool(primary.verified),
        "verify_host":    _verify_host(primary.domain),
        "verify_target":  _verify_target(primary.verify_token),
    }


@domains_router.post("/api/admin/org/verify-domain")
def verify_org_domain(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Verify the *primary* domain.  Use /api/admin/org/domains/{id}/verify
    to verify any specific domain (primary or secondary)."""
    primary = session.query(OrgDomain).filter_by(
        org_id=claims.get("oid"), is_primary=True
    ).first()
    if not primary:
        raise HTTPException(status_code=400, detail="Org has no primary domain")
    return verify_org_domain_cname(primary.id, claims, session)
