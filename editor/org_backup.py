"""
Backup and restore machinery for the editor: per-tenant memory and wiki
backups, full org snapshots and their restore, the S3 and SSH upload
targets, the supervised restore job runners and the auto-backup scheduler
loops.

Plain functions, no routes: routers/backup.py serves them over HTTP, and
main.py starts the scheduler loops and settles interrupted restores on
startup. Moved out of main.py by CQ-04 phase 4; behaviour is unchanged.
"""
import json as _json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

import backup_crypto
import restore_jobs
from restore_jobs import RestoreCancelled, RestoreControl
from config_db import (
    get_db_session, ConfigEntry,
    ApiToken, OAuthClient, SoftDeletedPage, WikiRevision,
    UserAccount, Team, Group, TeamMembership, GroupMembership, GroupTeam,
    InviteToken, MfaCredential,
    WikiSpaceACL, GitSourceACL, GitSource, MemorySharingPolicy,
    Tenant, OrgDomain, OidcProvider, SamlProvider,
    MemoryBackupConfig, WikiBackupConfig, TenantSettings,
    OrgBackupS3Config, OrgBackupSSHTarget, OrgBackupSchedule,
    DEFAULT_TENANT_SLUG,
    crypto,
)
from internal_service import EMBEDDER_URL, MEMORY_API_URL, _internal_service_headers
from storage import get_s3_storage
from wiki_storage import _normalize_path, _s3_delete, _s3_write

log = logging.getLogger("editor")


# ── Memory backup/restore ──────────────────────────────────────────────────

def _create_memory_backup(tenant_slug: str) -> dict:
    """Snapshot the tenant's memories to /data/memories/backups/<slug>/."""
    passcode = _require_tenant_backup_passcode(tenant_slug)
    resp = httpx.get(
        f"{EMBEDDER_URL}/api/memories",
        params={"tenant_slug": tenant_slug, "limit": 0},
        headers=_internal_service_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    memories = resp.json().get("memories", [])

    backup_dir = Path("/data/memories/backups") / tenant_slug
    backup_dir.mkdir(parents=True, exist_ok=True)

    now       = datetime.utcnow()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"memories-backup-{timestamp}.json"

    backup_data = {
        "export_time": now.isoformat() + "Z",
        "tenant_slug": tenant_slug,
        "count":       len(memories),
        "memories":    memories,
    }
    with backup_file.open("w") as f:
        _json.dump(backup_data, f, indent=2)
    # Encrypt in place (same .json filename, AES-256-GCM contents).
    _enc_tmp = backup_file.with_name(backup_file.name + ".enc.tmp")
    try:
        backup_crypto.encrypt_file(backup_file, _enc_tmp, passcode)
        os.replace(_enc_tmp, backup_file)
    finally:
        _enc_tmp.unlink(missing_ok=True)

    log.info("Created memory backup: %s (%d memories, encrypted)", backup_file.name, len(memories))
    return {
        "filename":     backup_file.name,
        "id":           backup_file.stem,
        "memory_count": len(memories),
        "created_at":   backup_data["export_time"],
        "size_bytes":   backup_file.stat().st_size,
    }


def _prune_backups(directory: Path, glob: str, retention_days: int) -> None:
    """Delete backup files older than retention_days from directory."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    for old_file in directory.glob(glob):
        try:
            stat = old_file.stat()
            if datetime.fromtimestamp(stat.st_mtime) < cutoff:
                old_file.unlink()
                log.info("Pruned old backup: %s", old_file.name)
        except Exception as e:
            log.warning("Failed to prune %s: %s", old_file.name, e)


# Per-org MEMORY restore jobs (Memory Admin). Platform-level ORG restores are
# a different, supervised thing — see restore_jobs.registry.
_restore_jobs: dict = {}


def _run_tenant_memory_restore_job(job_id: str, tenant_slug: str, memories: list, mode: str) -> None:
    """Background worker for a per-org memory restore. Re-adding memories
    re-embeds each via the embedder (slow), so this runs off-request."""
    _restore_jobs[job_id] = {"status": "running", "tenant_slug": tenant_slug,
                             "started_at": datetime.utcnow().isoformat() + "Z"}
    restored_count = 0
    failures: list = []
    try:
        if mode == "replace":
            try:
                existing = httpx.get(f"{EMBEDDER_URL}/api/memories",
                                     params={"tenant_slug": tenant_slug, "limit": 0},
                                     headers=_internal_service_headers(),
                                     timeout=30).json()
                for mem in existing.get("memories", []):
                    httpx.delete(f"{EMBEDDER_URL}/api/memories/{mem['id']}",
                                 params={"tenant_slug": tenant_slug},
                                 headers=_internal_service_headers(),
                                 timeout=30)
            except Exception as exc:
                log.warning("Failed to delete existing memories: %s", exc)
        for mem in memories:
            try:
                resp = httpx.post(
                    f"{EMBEDDER_URL}/api/memories",
                    json={"content": mem.get("content"), "tags": mem.get("tags", []),
                          "type": mem.get("type", "fact"),
                          "source": mem.get("source") or "legacy", "tenant_slug": tenant_slug},
                    headers=_internal_service_headers(),
                    timeout=30,
                )
                resp.raise_for_status()
                restored_count += 1
            except Exception as exc:
                log.warning("Failed to restore memory %s: %s", mem.get("id"), exc)
                failures.append({"id": mem.get("id"), "error": str(exc)})
        live_count: Optional[int] = None
        try:
            r = httpx.get(f"{EMBEDDER_URL}/api/memories",
                          params={"tenant_slug": tenant_slug, "limit": 0},
                          headers=_internal_service_headers(),
                          timeout=30)
            r.raise_for_status()
            live_count = len(r.json().get("memories", []))
        except Exception as exc:
            log.warning("post-restore live count fetch failed: %s", exc)
        _restore_jobs[job_id] = {
            "status": "done", "tenant_slug": tenant_slug,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "result": {"status": "restored" if not failures else "partial",
                       "tenant_slug": tenant_slug, "restored": restored_count,
                       "total": len(memories), "failed": failures,
                       "live_count": live_count, "count": restored_count},
        }
    except Exception as exc:
        log.exception("tenant memory restore job %s failed", job_id)
        _restore_jobs[job_id] = {"status": "error", "tenant_slug": tenant_slug,
                                 "error": str(exc), "finished_at": datetime.utcnow().isoformat() + "Z"}


# ── Wiki backup/restore ────────────────────────────────────────────────────
# Mirrors the memory backup surface. Wiki backups capture the markdown
# tree (and the local git history when present) as a tar.gz archive so
# operators can run real BC/DR drills.

def _wiki_backup_dir(tenant_slug: str) -> Path:
    return Path("/data/wiki/backups") / tenant_slug


def _build_wiki_archive(tenant_slug: str, *, encrypt_passcode: Optional[str] = None) -> dict:
    """Snapshot the wiki to a tar.gz at /data/wiki/backups/<slug>/.

    Source of truth is the tenant's S3 bucket — every object is streamed
    into the archive verbatim. The on-disk /docs mirror is intentionally
    ignored so backups stay consistent regardless of mirror lag.

    If `encrypt_passcode` is given the archive is AES-256-GCM encrypted in
    place; otherwise it is written in the clear. The platform org snapshot
    calls this WITHOUT a passcode (it embeds the plaintext wiki and encrypts
    the whole bundle once with the platform passcode) — double-encrypting the
    wiki would make it un-restorable.
    Returns metadata about the archive. Raises Exception on failure.
    """
    import tarfile, io as _io
    backup_dir = _wiki_backup_dir(tenant_slug)
    backup_dir.mkdir(parents=True, exist_ok=True)

    now       = datetime.utcnow()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    archive   = backup_dir / f"wiki-backup-{timestamp}.tar.gz"

    file_count = 0
    s3 = get_s3_storage()
    if s3 is None:
        raise RuntimeError("Object storage is not configured")
    with tarfile.open(archive, "w:gz") as tf:
        for key in s3.walk(tenant_slug):
            try:
                data = s3.read(tenant_slug, key)
            except Exception as exc:
                log.warning("Backup skip: slug=%s key=%s err=%s", tenant_slug, key, exc)
                continue
            info = tarfile.TarInfo(name=key)
            info.size = len(data)
            info.mtime = int(now.timestamp())
            tf.addfile(info, _io.BytesIO(data))
            file_count += 1

    if encrypt_passcode:
        _enc_tmp = archive.with_name(archive.name + ".enc.tmp")
        try:
            backup_crypto.encrypt_file(archive, _enc_tmp, encrypt_passcode)
            os.replace(_enc_tmp, archive)
        finally:
            _enc_tmp.unlink(missing_ok=True)

    log.info("Created wiki archive: %s (%d files%s)", archive.name, file_count,
             ", encrypted" if encrypt_passcode else "")
    return {
        "filename":   archive.name,
        "id":         archive.name.removesuffix(".tar.gz"),
        "file_count": file_count,
        "created_at": now.isoformat() + "Z",
        "size_bytes": archive.stat().st_size,
    }


def _create_wiki_backup(tenant_slug: str) -> dict:
    """Per-org wiki backup: hard-enforced and encrypted with the org's own
    backup passcode."""
    passcode = _require_tenant_backup_passcode(tenant_slug)
    return _build_wiki_archive(tenant_slug, encrypt_passcode=passcode)


# ── Platform org-level backup helpers ─────────────────────────────────────
# Full org snapshots bundle the DB rows, Qdrant memories, and wiki files
# into a single tar.gz archive.  Optionally uploaded to S3-compatible
# storage if OrgBackupS3Config.enabled is True.

def _org_backup_dir(org_slug: str) -> Path:
    return Path("/data/org-backups") / org_slug


class BackupNotConfigured(Exception):
    """Raised when a backup/restore can't proceed because no passcode is set.
    Encryption is hard-enforced: backups never run, and encrypted backups
    never restore, without the passcode."""


_PLATFORM_BACKUP_PASSCODE_KEY = "platform_org_backup_passcode"


def _get_platform_backup_passcode(session: Session) -> Optional[str]:
    """The platform-global backup passcode (decrypted), or None if unset."""
    entry = session.query(ConfigEntry).filter_by(key=_PLATFORM_BACKUP_PASSCODE_KEY).first()
    if not entry:
        return None
    val = entry.get_value() or {}
    pc = (val.get("passcode") or "").strip()
    return pc or None


def _set_platform_backup_passcode(session: Session, passcode: str) -> None:
    entry = session.query(ConfigEntry).filter_by(key=_PLATFORM_BACKUP_PASSCODE_KEY).first()
    if entry is None:
        entry = ConfigEntry(key=_PLATFORM_BACKUP_PASSCODE_KEY)
        session.add(entry)
    entry.set_value({"passcode": passcode})


def _resolve_org_backup_passcode(org_id: str, session: Session) -> Optional[str]:
    """Effective passcode for a PLATFORM-level org backup: the per-org override
    (OrgBackupSchedule.passcode_enc) if set, otherwise the platform-global
    passcode. Independent from the per-org (tenant-admin) backup passcode."""
    sched = _get_org_schedule(org_id, session)
    if sched and sched.passcode_enc:
        pc = (crypto.decrypt(sched.passcode_enc) or "").strip()
        if pc:
            return pc
    return _get_platform_backup_passcode(session)


def _get_tenant_backup_passcode(tenant_slug: str, session: Session) -> Optional[str]:
    """The per-org (tenant-admin) backup passcode, decrypted, or None."""
    row = session.query(TenantSettings).filter_by(tenant_slug=tenant_slug).first()
    if not row or not row.backup_passcode_enc:
        return None
    pc = (crypto.decrypt(row.backup_passcode_enc) or "").strip()
    return pc or None


def _set_tenant_backup_passcode(tenant_slug: str, passcode: str, session: Session) -> None:
    row = session.query(TenantSettings).filter_by(tenant_slug=tenant_slug).first()
    if row is None:
        row = TenantSettings(tenant_slug=tenant_slug)
        session.add(row)
    row.backup_passcode_enc = crypto.encrypt(passcode) if passcode else None


def _require_tenant_backup_passcode(tenant_slug: str) -> str:
    """Resolve the per-org backup passcode in a short-lived session, raising
    BackupNotConfigured if unset (encryption is hard-enforced)."""
    from config_db import SessionLocal as _SL
    with _SL() as _s:
        pc = _get_tenant_backup_passcode(tenant_slug, _s)
    if not pc:
        raise BackupNotConfigured(
            "This org's backups are not configured: set a backup passcode in "
            "Settings -> Backup & Restore before backups can run.")
    return pc


def _row_to_dict(row) -> dict:
    """Serialize a SQLAlchemy model row to a plain dict with JSON-safe values."""
    from sqlalchemy import inspect as _sa_inspect
    out = {}
    for col in _sa_inspect(type(row)).mapper.column_attrs:
        val = getattr(row, col.key)
        if isinstance(val, datetime):
            val = val.isoformat() + "Z"
        out[col.key] = val
    return out


def _get_s3_config_row(session: Session) -> Optional["OrgBackupS3Config"]:
    return session.query(OrgBackupS3Config).first()


def _s3_client(cfg: "OrgBackupS3Config"):
    """Build a boto3 S3 client from OrgBackupS3Config."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 is not installed — S3 org backup unavailable")
    kwargs: dict = {
        "aws_access_key_id":     crypto.decrypt(cfg.access_key_enc or ""),
        "aws_secret_access_key": crypto.decrypt(cfg.secret_key_enc or ""),
        "region_name":           cfg.region or "us-east-1",
    }
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    return boto3.client("s3", **kwargs)


def _s3_key(cfg: "OrgBackupS3Config", org_slug: str, filename: str) -> str:
    prefix = (cfg.prefix or "org-backups/").rstrip("/")
    return f"{prefix}/{org_slug}/{filename}"


def _upload_org_backup_to_s3(cfg: "OrgBackupS3Config", org_slug: str, local_path: Path) -> str:
    s3 = _s3_client(cfg)
    key = _s3_key(cfg, org_slug, local_path.name)
    s3.upload_file(str(local_path), cfg.bucket, key)
    log.info("Uploaded org backup to S3: s3://%s/%s", cfg.bucket, key)
    return key


def _download_from_s3(cfg: "OrgBackupS3Config", org_slug: str, filename: str, dest: Path) -> None:
    s3 = _s3_client(cfg)
    key = _s3_key(cfg, org_slug, filename)
    s3.download_file(cfg.bucket, key, str(dest))


def _list_s3_org_backups(cfg: "OrgBackupS3Config", org_slug: str) -> list[dict]:
    s3 = _s3_client(cfg)
    prefix = _s3_key(cfg, org_slug, "")
    paginator = s3.get_paginator("list_objects_v2")
    out = []
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if name.startswith("org-backup-") and name.endswith((".zip", ".tar.gz")):
                out.append({
                    "filename":   name,
                    "id":         name.removesuffix(".zip").removesuffix(".tar.gz"),
                    "size_bytes": obj["Size"],
                    "created_at": obj["LastModified"].isoformat(),
                    "source":     "s3",
                })
    return sorted(out, key=lambda x: x["filename"], reverse=True)


def _get_ssh_config_row(session: Session) -> Optional["OrgBackupSSHTarget"]:
    return session.query(OrgBackupSSHTarget).first()


def _upload_org_backup_ssh(cfg: "OrgBackupSSHTarget", org_slug: str, local_path: Path) -> str:
    """Upload a local backup archive to a remote host via SFTP (paramiko)."""
    import shlex
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko is not installed — install it with: pip install paramiko")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kw: dict = dict(
        hostname=cfg.host,
        port=cfg.port or 22,
        username=cfg.username,
    )
    if cfg.private_key_enc:
        import io as _io
        pem = crypto.decrypt(cfg.private_key_enc)
        for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                connect_kw["pkey"] = key_cls.from_private_key(_io.StringIO(pem))
                break
            except Exception:
                pass
    elif cfg.password_enc:
        connect_kw["password"] = crypto.decrypt(cfg.password_enc)

    client.connect(**connect_kw)
    try:
        remote_base = (cfg.remote_path or "/tmp/elefant-backups").rstrip("/")
        remote_dir  = f"{remote_base}/{org_slug}"
        _, _, _ = client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
        # small wait — exec_command is async; no output expected so we don't block on exit
        import time as _t; _t.sleep(0.3)
        sftp = client.open_sftp()
        remote_file = f"{remote_dir}/{local_path.name}"
        sftp.put(str(local_path), remote_file)
        sftp.close()
        log.info("SSH backup uploaded to %s:%s", cfg.host, remote_file)
        return remote_file
    finally:
        client.close()


def _test_ssh_connection(cfg: "OrgBackupSSHTarget") -> dict:
    """Return {success, message} after attempting an SSH connection."""
    try:
        import paramiko
    except ImportError:
        return {"success": False, "message": "paramiko is not installed on this server"}
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kw: dict = dict(hostname=cfg.host, port=cfg.port or 22, username=cfg.username, timeout=10)
        if cfg.private_key_enc:
            import io as _io
            pem = crypto.decrypt(cfg.private_key_enc)
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey):
                try:
                    connect_kw["pkey"] = key_cls.from_private_key(_io.StringIO(pem))
                    break
                except Exception:
                    pass
        elif cfg.password_enc:
            connect_kw["password"] = crypto.decrypt(cfg.password_enc)
        client.connect(**connect_kw)
        client.close()
        return {"success": True, "message": f"Connected to {cfg.host}:{cfg.port or 22} as {cfg.username}"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def _prune_org_backups(backup_dir: Path, retention_days: int) -> None:
    """Delete org backup archives older than retention_days."""
    if retention_days <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    candidates = [
        f for f in backup_dir.iterdir()
        if f.name.startswith("org-backup-") and f.name.endswith((".zip", ".tar.gz"))
    ]
    for f in candidates:
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                f.with_name(f.name + ".meta.json").unlink(missing_ok=True)
                log.info("Pruned old org backup: %s", f.name)
        except Exception as exc:
            log.warning("Failed to prune org backup %s: %s", f.name, exc)


def _get_org_schedule(org_id: str, session: Session) -> Optional["OrgBackupSchedule"]:
    return session.query(OrgBackupSchedule).filter_by(org_id=org_id).first()


def _dispatch_org_backup(org_id: str, session: Session) -> dict:
    """Create a full org snapshot and dispatch it to all configured targets.

    Reads the org's OrgBackupSchedule to determine which targets are enabled.
    Falls back to local-only when no schedule row exists.
    """
    schedule = _get_org_schedule(org_id, session)
    use_local      = schedule.use_local      if schedule else True
    use_s3         = schedule.use_s3         if schedule else False
    use_ssh        = schedule.use_ssh        if schedule else False
    retention_days = schedule.retention_days if schedule else 30

    info = _create_org_snapshot(org_id, session)
    org_slug   = info["org_slug"]
    bundle_path = _org_backup_dir(org_slug) / info["filename"]

    # ── S3 upload ────────────────────────────────────────────────────────────
    if use_s3:
        try:
            s3_cfg = _get_s3_config_row(session)
            if s3_cfg and s3_cfg.enabled and s3_cfg.bucket:
                info["s3_key"] = _upload_org_backup_to_s3(s3_cfg, org_slug, bundle_path)
        except Exception as exc:
            log.warning("S3 upload failed for org %s: %s", org_slug, exc)
            info["s3_error"] = str(exc)

    # ── SSH upload ───────────────────────────────────────────────────────────
    if use_ssh:
        try:
            ssh_cfg = _get_ssh_config_row(session)
            if ssh_cfg and ssh_cfg.enabled and ssh_cfg.host:
                info["ssh_path"] = _upload_org_backup_ssh(ssh_cfg, org_slug, bundle_path)
        except Exception as exc:
            log.warning("SSH upload failed for org %s: %s", org_slug, exc)
            info["ssh_error"] = str(exc)

    # ── Local retention ───────────────────────────────────────────────────────
    if not use_local and bundle_path.exists():
        # Local was just a staging area — remove it now that remote targets are done
        bundle_path.unlink(missing_ok=True)
        bundle_path.with_name(bundle_path.name + ".meta.json").unlink(missing_ok=True)
    else:
        _prune_org_backups(_org_backup_dir(org_slug), retention_days)

    # ── Update schedule timestamp ─────────────────────────────────────────────
    if schedule:
        schedule.last_backup_at = datetime.utcnow()
        session.commit()

    return info


def _create_org_snapshot(org_id: str, session: Session) -> dict:
    """Create a full org backup bundle (DB + memories + wiki).

    Saves to /data/org-backups/<slug>/org-backup-<timestamp>.zip — an AES-256
    encrypted (passcode-protected) zip, openable on the desktop — and
    optionally uploads to S3 if OrgBackupS3Config.enabled is True.
    Returns backup metadata dict.
    """
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise ValueError(f"Org {org_id} not found")

    # Hard-enforced encryption: refuse to produce an in-the-clear org snapshot.
    passcode = _resolve_org_backup_passcode(org_id, session)
    if not passcode:
        raise BackupNotConfigured(
            "Org backups are not configured: set a platform backup passcode "
            "(or a per-org override) before backups can run.")

    backup_dir = _org_backup_dir(org.slug)
    backup_dir.mkdir(parents=True, exist_ok=True)

    now       = datetime.utcnow()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    bundle_name = f"org-backup-{timestamp}.zip"
    bundle_path = backup_dir / bundle_name

    # ── Collect all org-scoped DB rows ──────────────────────────────────────
    team_ids  = [t.id for t in session.query(Team).filter_by(org_id=org_id).all()]
    group_ids = [g.id for g in session.query(Group).filter_by(org_id=org_id).all()]

    db_dump: dict = {
        "org":               _row_to_dict(org),
        "domains":           [_row_to_dict(r) for r in session.query(OrgDomain).filter_by(org_id=org_id).all()],
        "oidc_providers":    [_row_to_dict(r) for r in session.query(OidcProvider).filter_by(tenant_id=org_id).all()],
        "saml_providers":    [_row_to_dict(r) for r in session.query(SamlProvider).filter_by(tenant_id=org_id).all()],
        "oauth_clients":     [_row_to_dict(r) for r in session.query(OAuthClient).filter_by(tenant_id=org_id).all()],
        "api_tokens":        [_row_to_dict(r) for r in session.query(ApiToken).filter_by(tenant_id=org_id).all()],
        "users":             [_row_to_dict(r) for r in session.query(UserAccount).filter_by(org_id=org_id).all()],
        "teams":             [_row_to_dict(r) for r in session.query(Team).filter_by(org_id=org_id).all()],
        "groups":            [_row_to_dict(r) for r in session.query(Group).filter_by(org_id=org_id).all()],
        "team_memberships":  [_row_to_dict(r) for r in (
            session.query(TeamMembership).filter(TeamMembership.team_id.in_(team_ids)).all()
            if team_ids else []
        )],
        "group_memberships": [_row_to_dict(r) for r in (
            session.query(GroupMembership).filter(GroupMembership.group_id.in_(group_ids)).all()
            if group_ids else []
        )],
        "group_teams":       [_row_to_dict(r) for r in (
            session.query(GroupTeam).filter(GroupTeam.group_id.in_(group_ids)).all()
            if group_ids else []
        )],
        "invite_tokens":     [_row_to_dict(r) for r in session.query(InviteToken).filter_by(org_id=org_id).all()],
        "wiki_space_acls":   [_row_to_dict(r) for r in session.query(WikiSpaceACL).filter_by(org_id=org_id).all()],
        "git_sources":       [_row_to_dict(r) for r in session.query(GitSource).filter_by(org_id=org_id).all()],
        "git_source_acls":   [_row_to_dict(r) for r in session.query(GitSourceACL).filter_by(org_id=org_id).all()],
        "memory_sharing":    [_row_to_dict(r) for r in session.query(MemorySharingPolicy).filter_by(org_id=org_id).all()],
        "memory_backup_cfg": [_row_to_dict(r) for r in session.query(MemoryBackupConfig).filter_by(tenant_id=org.slug).all()],
        "wiki_backup_cfg":   [_row_to_dict(r) for r in session.query(WikiBackupConfig).filter_by(tenant_id=org.slug).all()],
    }

    user_ids = [u["id"] for u in db_dump["users"]]
    # MFA credentials: the stored TOTP secret is Fernet-encrypted with THIS
    # deployment's crypto.key, which a restore target won't have. Carry the
    # decrypted base32 secret alongside so MFA survives a cross-deployment
    # restore (safe — the whole bundle is passcode-encrypted at rest). We drop
    # the key-specific ciphertext; the restore re-encrypts with the target key.
    _mfa_rows = []
    for r in (session.query(MfaCredential).filter(MfaCredential.user_id.in_(user_ids)).all()
              if user_ids else []):
        d = _row_to_dict(r)
        d["totp_secret"] = crypto.decrypt(r.totp_secret_enc or "")
        d.pop("totp_secret_enc", None)
        _mfa_rows.append(d)
    db_dump["mfa_credentials"] = _mfa_rows
    # Wiki history, recycle bin, and per-tenant settings (BCDR: previously the
    # snapshot restored current wiki content via S3 but not its history/recycle
    # bin, and lost tenant settings like board_nudge_chars / ai_wiki_folder).
    db_dump["soft_deleted_pages"] = [_row_to_dict(r) for r in session.query(SoftDeletedPage).filter_by(tenant_id=org_id).all()]
    db_dump["wiki_revisions"]     = [_row_to_dict(r) for r in session.query(WikiRevision).filter_by(tenant_id=org_id).all()]
    db_dump["tenant_settings"]    = [_row_to_dict(r) for r in session.query(TenantSettings).filter_by(tenant_slug=org.slug).all()]

    # ── Fetch memories from embedder ─────────────────────────────────────────
    memories: list = []
    try:
        resp = httpx.get(
            f"{EMBEDDER_URL}/api/memories",
            params={"tenant_slug": org.slug, "limit": 0},
            headers=_internal_service_headers(),
            timeout=60,
        )
        if resp.status_code == 200:
            memories = resp.json().get("memories", [])
    except Exception as exc:
        log.warning("Could not fetch memories for org snapshot %s: %s", org.slug, exc)

    # ── Fetch memory-api Postgres data (boards + hot tier + revisions) ───────
    # These live in memory-api's DB, which the snapshot can't read directly, so
    # pull them through the internal org-export endpoint.
    memapi: dict = {}
    try:
        resp = httpx.get(
            f"{MEMORY_API_URL}/internal/org-export",
            params={"tenant_slug": org.slug},
            headers=_internal_service_headers(),
            timeout=60,
        )
        if resp.status_code == 200:
            memapi = resp.json().get("tables", {}) or {}
    except Exception as exc:
        log.warning("Could not fetch memory-api data for org snapshot %s: %s", org.slug, exc)

    # ── Create wiki archive ──────────────────────────────────────────────────
    wiki_info: dict = {}
    wiki_archive_path: Optional[Path] = None
    try:
        # Plaintext wiki archive — the whole org bundle is encrypted once with
        # the platform passcode below, so this must NOT be per-org-encrypted
        # (and must not require the per-org passcode).
        wiki_info = _build_wiki_archive(org.slug)
        wiki_archive_path = _wiki_backup_dir(org.slug) / wiki_info["filename"]
    except Exception as exc:
        log.warning("Wiki archive skipped during org snapshot for %s: %s", org.slug, exc)

    # ── Bundle everything ────────────────────────────────────────────────────
    manifest = {
        "version":        "2",   # v2 adds memapi.json + wiki history/settings in db.json
        "created_at":     now.isoformat() + "Z",
        "org_id":         org.id,
        "org_slug":       org.slug,
        "org_name":       org.display_name or org.name,
        "user_count":     len(db_dump["users"]),
        "memory_count":   len(memories),
        "card_count":     len(memapi.get("cards", [])),
        "board_column_count": len(memapi.get("board_columns", [])),
        "hot_memory_count":   len(memapi.get("memory_hot", [])),
        "wiki_file_count": wiki_info.get("file_count", 0),
    }

    # Write everything into a single AES-256 password .zip so the bundle is
    # encrypted at rest yet still openable on the desktop (Keka / The
    # Unarchiver / 7-Zip / Windows) with the passcode — the members are the
    # backup components so an operator can browse them after unlocking.
    items: list = [
        ("manifest.json", _json.dumps(manifest, indent=2).encode()),
        ("db.json",       _json.dumps(db_dump, indent=2).encode()),
        ("memories.json", _json.dumps({"count": len(memories), "memories": memories}, indent=2).encode()),
        ("memapi.json",   _json.dumps(memapi, indent=2).encode()),
    ]
    if wiki_archive_path and wiki_archive_path.exists():
        items.append(("wiki.tar.gz", wiki_archive_path))
    backup_crypto.write_aes_zip(bundle_path, items, passcode)

    # Plaintext sidecar (counts only, no data) so the backup list can show
    # users/memories/wiki counts without the passcode — the manifest inside
    # the bundle is now encrypted and unreadable to the list view.
    try:
        bundle_path.with_name(bundle_name + ".meta.json").write_text(_json.dumps(manifest))
    except Exception as exc:
        log.warning("Could not write backup sidecar manifest for %s: %s", bundle_name, exc)

    size_bytes = bundle_path.stat().st_size
    log.info("Created org snapshot: %s (%.1f MB, encrypted)", bundle_name, size_bytes / 1024 / 1024)

    return {
        "id":              bundle_name.removesuffix(".zip"),
        "filename":        bundle_name,
        "created_at":      manifest["created_at"],
        "size_bytes":      size_bytes,
        "org_slug":        org.slug,
        "org_name":        manifest["org_name"],
        "user_count":      manifest["user_count"],
        "memory_count":    manifest["memory_count"],
        "wiki_file_count": manifest["wiki_file_count"],
    }


def _restore_org_snapshot(bundle_path: Path, target_org_id: Optional[str], session: Session,
                          passcode: Optional[str] = None,
                          ctl: Optional[RestoreControl] = None,
                          plan: Optional[restore_jobs.RestorePlan] = None) -> dict:
    """Restore an org from a snapshot bundle.

    If target_org_id is set, the restore merges into (and overwrites) that
    org.  Otherwise the org from the manifest is used (creating it if it
    doesn't exist).  Returns a summary dict.

    Handles three bundle formats: an AES-256 password .zip (current), a legacy
    AES-256-GCM container (magic header), and a plaintext .tar.gz (oldest). The
    first two require `passcode`. Whatever the format, the components
    (manifest.json/db.json/memories.json/wiki.tar.gz) are extracted into a
    temp dir and the rest of the restore is format-agnostic.

    `ctl` is the operator's handle on a long-running restore: it collects
    progress and is the thing a pause or a cancel acts through. Every item
    loop advances it, which is also where a cancel takes effect. Default is a
    detached control — never paused, never cancelled — so there is one code
    path whether or not anybody is watching.

    The editor-DB half commits as a single transaction, so a cancel before
    `mark_committed()` leaves the target org untouched and one after it leaves
    the org partly restored. That distinction is reported, not glossed.

    `plan` selects which areas to put back and how (see RestorePlan). Default
    is everything, merged, embedding as it goes — the whole-org restore this
    function has always done. An area left out of the plan is not touched at
    all; an area set to `replace` ends up as the backup had it, which means
    deleting what the backup does not contain.
    """
    import tarfile as _tarfile

    plan = plan or restore_jobs.RestorePlan()
    ctl = ctl or RestoreControl(plan=plan)
    _dec_tmp: Optional[Path] = None
    tmp_dir = Path(f"/tmp/org-restore-{secrets.token_hex(8)}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        ctl.begin("preparing", total=1)
        if backup_crypto.is_zip(bundle_path):
            # AES-256 password zip (current format).
            if not passcode:
                raise BackupNotConfigured(
                    "This backup is encrypted — a passcode is required to restore it.")
            try:
                backup_crypto.extract_aes_zip(bundle_path, tmp_dir, passcode)
            except backup_crypto.BackupCryptoError as exc:
                raise ValueError(str(exc))
        else:
            # tar.gz — optionally wrapped in the legacy AES-256-GCM container.
            tar_src = bundle_path
            if backup_crypto.is_encrypted(bundle_path):
                if not passcode:
                    raise BackupNotConfigured(
                        "This backup is encrypted — a passcode is required to restore it.")
                _dec_tmp = Path(f"/tmp/org-restore-dec-{secrets.token_hex(8)}.tar.gz")
                try:
                    backup_crypto.decrypt_file(bundle_path, _dec_tmp, passcode)
                except backup_crypto.BackupCryptoError as exc:
                    _dec_tmp.unlink(missing_ok=True)
                    raise ValueError(str(exc))
                tar_src = _dec_tmp
            with _tarfile.open(tar_src, "r:gz") as tf:
                tf.extractall(tmp_dir)

        manifest_path = tmp_dir / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("Archive is missing manifest.json — not a valid org backup")

        manifest   = _json.loads(manifest_path.read_text())
        db_dump    = _json.loads((tmp_dir / "db.json").read_text())
        mem_path   = tmp_dir / "memories.json"
        memories   = _json.loads(mem_path.read_text()).get("memories", []) if mem_path.exists() else []
        # memory-api Postgres data (boards + hot tier + revisions) — v2 bundles.
        # Absent in v1 bundles, so default to empty (backward compatible).
        memapi_path = tmp_dir / "memapi.json"
        memapi      = _json.loads(memapi_path.read_text()) if memapi_path.exists() else {}

        src_org_id = manifest["org_id"]
        org_slug   = manifest["org_slug"]
        ctl.advance(1)

        # The editor-DB rows split cleanly between two areas, and the split is
        # what lets a wiki-only restore put page history back without also
        # reverting everybody's account.
        _IDENTITY_SECTIONS = (
            "domains", "oidc_providers", "saml_providers", "oauth_clients",
            "api_tokens", "users", "teams", "groups", "team_memberships",
            "group_memberships", "group_teams", "invite_tokens",
            "mfa_credentials", "wiki_space_acls", "git_sources",
            "git_source_acls", "memory_sharing", "memory_backup_cfg",
            "wiki_backup_cfg", "tenant_settings",
        )
        _WIKI_DB_SECTIONS = ("soft_deleted_pages", "wiki_revisions")

        do_identity = plan.includes("identity")
        do_wiki     = plan.includes("wiki")
        _db_sections = ((_IDENTITY_SECTIONS if do_identity else ())
                        + (_WIKI_DB_SECTIONS if do_wiki else ()))
        ctl.begin("database",
                  total=sum(len(db_dump.get(k) or []) for k in _db_sections))

        # ── Determine / create the target org ───────────────────────────────
        if target_org_id:
            org = session.query(Tenant).filter_by(id=target_org_id).first()
            if not org:
                raise ValueError(f"Target org {target_org_id} not found")
            actual_org_id = org.id
        else:
            org = session.query(Tenant).filter_by(id=src_org_id).first()
            if org is None:
                org = session.query(Tenant).filter_by(slug=org_slug).first()
            actual_org_id = org.id if org else src_org_id

        # Update or create the Tenant record. A restore that was not asked to
        # touch identity must leave the org's own row alone — otherwise
        # "just put the wiki back" quietly reverts the org's name, domain and
        # policy too. A missing org is the exception: there is nothing to
        # restore INTO until it exists.
        org_data = db_dump["org"]
        if org is None:
            t = Tenant()
            for k, v in org_data.items():
                if k.endswith("_at") and v:
                    try:
                        v = datetime.fromisoformat(v.rstrip("Z"))
                    except ValueError:
                        v = None
                if hasattr(t, k):
                    setattr(t, k, v)
            t.id = actual_org_id
            session.add(t)
            session.flush()
        elif do_identity:
            for k, v in org_data.items():
                if k in ("id",):
                    continue
                if k.endswith("_at") and v:
                    try:
                        v = datetime.fromisoformat(v.rstrip("Z"))
                    except ValueError:
                        continue
                if hasattr(org, k):
                    setattr(org, k, v)
            session.flush()

        # ── Helper: delete all existing rows for this org, then re-insert ──
        # (safe re-import strategy — avoids primary-key conflicts on restore)

        def _parse_dt(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.rstrip("Z"))
            except (ValueError, AttributeError):
                return None

        def _restore_rows(model, rows: list, id_remap: dict = None,
                          extra_fields: dict = None, area: str = "identity"):
            """Delete existing rows then re-insert from dump.

            Uses the model's actual primary key columns to locate existing
            rows, so this works for both id-keyed tables and junction tables
            with composite keys (e.g. TeamMembership, GroupTeam).

            `area` is which part of the plan this table belongs to; a table
            whose area was not selected is skipped entirely. Deciding it here,
            once, rather than at each of the twenty call sites is the whole
            point — a table added later and left unguarded would otherwise be
            restored by a plan that never asked for it.
            """
            if not plan.includes(area):
                return
            pk_cols = [c.name for c in model.__mapper__.primary_key]
            for row_data in rows:
                ctl.advance()
                data = dict(row_data)
                # Remap org_id if the org got a new ID
                if "org_id" in data and data["org_id"] == src_org_id:
                    data["org_id"] = actual_org_id
                if "tenant_id" in data and data["tenant_id"] == src_org_id:
                    data["tenant_id"] = actual_org_id
                for k, v in list(data.items()):
                    if k.endswith("_at") and isinstance(v, str):
                        data[k] = _parse_dt(v)
                if id_remap and "id" in data and data["id"] in id_remap:
                    data["id"] = id_remap[data["id"]]
                if extra_fields:
                    data.update(extra_fields)
                pk_filter = {c: data.get(c) for c in pk_cols if c in data}
                obj = session.query(model).filter_by(**pk_filter).first() if pk_filter else None
                if obj:
                    for k, v in data.items():
                        if hasattr(obj, k):
                            setattr(obj, k, v)
                else:
                    obj = model(**{k: v for k, v in data.items() if hasattr(model, k)})
                    session.add(obj)

        # Restore org-scoped tables
        _restore_rows(OrgDomain,      db_dump.get("domains", []))
        _restore_rows(OidcProvider,   db_dump.get("oidc_providers", []))
        _restore_rows(SamlProvider,   db_dump.get("saml_providers", []))
        _restore_rows(OAuthClient,    db_dump.get("oauth_clients", []))
        _restore_rows(ApiToken,       db_dump.get("api_tokens", []))
        _restore_rows(UserAccount,    db_dump.get("users", []))
        # Diagnostic: surface the auth-critical state of restored users so a
        # post-restore login failure can be pinned (reverted password vs.
        # lookup/identity mismatch). full_username is the login identity, not a
        # secret; the password hash itself is never logged.
        _ru = db_dump.get("users", []) if do_identity else []
        log.info(
            "Org restore %s: %d user(s) — local_auth=%d active=%d with_password=%d; "
            "full_usernames=%s; org slug=%s domain=%s",
            actual_org_id, len(_ru),
            sum(1 for u in _ru if (u.get("auth_method") or "local") == "local"),
            sum(1 for u in _ru if u.get("is_active", True)),
            sum(1 for u in _ru if u.get("password_hash")),
            [u.get("full_username") for u in _ru[:8]],
            org_data.get("slug"), org_data.get("domain"),
        )
        _restore_rows(Team,           db_dump.get("teams", []))
        _restore_rows(Group,          db_dump.get("groups", []))
        _restore_rows(TeamMembership, db_dump.get("team_memberships", []))
        _restore_rows(GroupMembership, db_dump.get("group_memberships", []))
        _restore_rows(GroupTeam,      db_dump.get("group_teams", []))
        _restore_rows(InviteToken,    db_dump.get("invite_tokens", []))
        # MFA credentials: re-encrypt the portable base32 secret carried in the
        # snapshot with THIS deployment's crypto.key, so MFA keeps working after
        # a cross-deployment restore instead of forcing everyone to re-enroll.
        # Legacy (pre-portable) backups have no `totp_secret` — keep the row but
        # force re-enrollment, since the source ciphertext is unusable here.
        for row in (db_dump.get("mfa_credentials", []) if do_identity else []):
            ctl.advance()
            data = dict(row)
            uid = data.get("user_id")
            if not uid:
                continue
            plaintext = (data.get("totp_secret") or "").strip()
            if plaintext:
                enc, enabled = crypto.encrypt(plaintext), bool(data.get("is_enabled"))
            else:
                enc, enabled = (data.get("totp_secret_enc") or ""), False
                if not enc:
                    continue
            obj = session.query(MfaCredential).filter_by(user_id=uid).first()
            if obj is None:
                obj = MfaCredential(user_id=uid)
                session.add(obj)
            obj.totp_secret_enc = enc
            obj.is_enabled = enabled
            _cd = _parse_dt(data.get("created_at"))
            if _cd:
                obj.created_at = _cd
            obj.last_used_at  = _parse_dt(data.get("last_used_at"))
            obj.last_reset_at = _parse_dt(data.get("last_reset_at"))
        _restore_rows(WikiSpaceACL,   db_dump.get("wiki_space_acls", []))
        _restore_rows(GitSource,      db_dump.get("git_sources", []))
        _restore_rows(GitSourceACL,   db_dump.get("git_source_acls", []))
        _restore_rows(MemorySharingPolicy, db_dump.get("memory_sharing", []))
        # Backup configs use tenant_id = slug, not org_id
        for row in (db_dump.get("memory_backup_cfg", []) if do_identity else []):
            ctl.advance()
            r = dict(row)
            for k, v in list(r.items()):
                if k.endswith("_at") and isinstance(v, str):
                    r[k] = _parse_dt(v)
            obj = session.query(MemoryBackupConfig).filter_by(tenant_id=r.get("tenant_id")).first()
            if obj:
                for k, v in r.items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
            else:
                session.add(MemoryBackupConfig(**{k: v for k, v in r.items() if hasattr(MemoryBackupConfig, k)}))
        for row in (db_dump.get("wiki_backup_cfg", []) if do_identity else []):
            ctl.advance()
            r = dict(row)
            for k, v in list(r.items()):
                if k.endswith("_at") and isinstance(v, str):
                    r[k] = _parse_dt(v)
            obj = session.query(WikiBackupConfig).filter_by(tenant_id=r.get("tenant_id")).first()
            if obj:
                for k, v in r.items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
            else:
                session.add(WikiBackupConfig(**{k: v for k, v in r.items() if hasattr(WikiBackupConfig, k)}))

        # Recycle bin (uuid PK) and per-tenant settings (slug PK) upsert cleanly.
        _restore_rows(SoftDeletedPage, db_dump.get("soft_deleted_pages", []), area="wiki")
        _restore_rows(TenantSettings,  db_dump.get("tenant_settings", []))
        # WikiRevision has an integer autoincrement PK that isn't stable across
        # deployments (restoring source ids could clobber another org's rows), so
        # clear this org's history and re-insert without the id.
        if do_wiki:
            session.query(WikiRevision).filter_by(tenant_id=actual_org_id).delete()
        for row in (db_dump.get("wiki_revisions", []) if do_wiki else []):
            ctl.advance()
            r = {k: v for k, v in row.items() if k != "id"}
            if r.get("tenant_id") == src_org_id:
                r["tenant_id"] = actual_org_id
            for k, v in list(r.items()):
                if k.endswith("_at") and isinstance(v, str):
                    r[k] = _parse_dt(v)
            session.add(WikiRevision(**{k: v for k, v in r.items() if hasattr(WikiRevision, k)}))

        # Last chance to walk away clean: everything above lives in one
        # uncommitted transaction, so a cancel before this line rolls the
        # target org back to exactly where it was.
        ctl.checkpoint()
        session.commit()
        ctl.mark_committed()

        # ── Restore memories ─────────────────────────────────────────────────
        restored_memories = 0
        memories_cleared  = 0
        deferred_embeds   = 0
        if not plan.includes("memories"):
            memories = []
        ctl.begin("memories", total=len(memories))
        if plan.replaces("memories"):
            # Point-in-time: the corpus must BE the backup, so whatever is not
            # in the backup goes first. Done before the inserts rather than by
            # diffing afterwards — a half-written restore that then deleted
            # "strays" would delete the memories it had not got to yet.
            try:
                resp = httpx.post(
                    f"{EMBEDDER_URL}/api/internal/memories/clear",
                    params={"tenant_slug": org_slug},
                    headers=_internal_service_headers(),
                    timeout=120,
                )
                if resp.status_code == 200:
                    memories_cleared = resp.json().get("deleted", 0)
                else:
                    raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                # Carrying on would merge into a corpus the operator asked to
                # have replaced, and report it as a point-in-time restore.
                raise RuntimeError(
                    f"Could not clear existing memories for a point-in-time "
                    f"restore: {exc}") from exc
        if memories:
            for mem in memories:
                ctl.advance()
                try:
                    body = {
                        "content":     mem.get("content"),
                        "tags":        mem.get("tags", []),
                        "type":        mem.get("type", "fact"),
                        "source":      mem.get("source") or "legacy",
                        "tenant_slug": org_slug,
                        # Deferred: store now, embed later. The content lives
                        # in the payload either way, so nothing is lost — but
                        # semantic search cannot reach these until the pending
                        # pass runs, which is why the result says how many.
                        "embed":       plan.embed_memories,
                    }
                    # Restore mode: preserve the memory's original identity,
                    # timestamps, attribution, and provenance so the graph, the
                    # session grouping, and any references survive the restore.
                    for f in ("id", "created_at", "updated_at", "author_id",
                              "originated_from_hot_ids", "originated_session_ids",
                              "compactor_score", "promoted_at", "oldest_captured_at"):
                        if mem.get(f) is not None:
                            body[f] = mem[f]
                    httpx.post(
                        f"{EMBEDDER_URL}/api/memories",
                        json=body,
                        headers=_internal_service_headers(),
                        timeout=30,
                    )
                    restored_memories += 1
                    if not plan.embed_memories:
                        deferred_embeds += 1
                except Exception as exc:
                    log.warning("Failed to restore memory during org restore: %s", exc)

        # ── Restore memory-api Postgres data (boards + hot tier + revisions) ──
        memapi_restored: dict = {}
        if not plan.includes("boards"):
            memapi = {}
        ctl.begin("boards", total=1)
        if memapi:
            try:
                resp = httpx.post(
                    f"{MEMORY_API_URL}/internal/org-import",
                    json={"tenant_slug": org_slug, "tables": memapi},
                    headers=_internal_service_headers(),
                    timeout=120,
                )
                if resp.status_code == 200:
                    memapi_restored = resp.json().get("restored", {})
                else:
                    log.warning("memory-api org-import failed (%s): %s",
                                resp.status_code, resp.text[:200])
            except Exception as exc:
                log.warning("Could not restore memory-api data during org restore: %s", exc)
        ctl.advance()

        # ── Restore wiki ─────────────────────────────────────────────────────
        # Wiki content lives in the org's S3 bucket. Extract the archive
        # streaming into S3 — we never re-create a /docs working tree.
        wiki_files_restored = 0
        wiki_files_removed  = 0
        wiki_error: Optional[str] = None
        wiki_archive = tmp_dir / "wiki.tar.gz"
        restored_org = session.query(Tenant).filter_by(id=actual_org_id).first()
        ctl.begin("wiki")
        if plan.includes("wiki") and wiki_archive.exists() and restored_org is not None:
            try:
                s3 = get_s3_storage()
                if s3 is None:
                    raise RuntimeError("Object storage is not configured")
                # Make sure the destination bucket exists before writing.
                s3.provision_wiki(restored_org.slug, seed_welcome=False)
                # For a point-in-time restore, list what is live before writing
                # anything: one consistent snapshot of "what was here", taken
                # while nothing is changing it. The strays are then that set
                # minus what the bundle actually restored — never the tar's
                # member list, which would also delete a page the write step
                # skipped or failed on.
                live_before = set()
                if plan.replaces("wiki"):
                    live_before = {_normalize_path(k) for k in s3.walk(restored_org.slug)}
                restored_keys = set()
                with _tarfile.open(wiki_archive, "r:gz") as tf:
                    members = [m for m in tf.getmembers() if m.isfile()]
                    ctl.begin("wiki", total=len(members) + (1 if plan.replaces("wiki") else 0))
                    for member in members:
                        ctl.advance()
                        key = member.name.lstrip("./")
                        try:
                            key = _normalize_path(key)
                        except HTTPException:
                            continue
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        _s3_write(restored_org, key, f.read())
                        restored_keys.add(key)
                        wiki_files_restored += 1
                if plan.replaces("wiki"):
                    # Every page the backup does not contain. This is the half
                    # that makes it a point-in-time restore rather than an
                    # overwrite — and the half that loses work, which is why
                    # the count comes back and the preview says so first.
                    ctl.advance()
                    for key in sorted(live_before - restored_keys):
                        try:
                            _s3_delete(restored_org, key)
                            wiki_files_removed += 1
                        except Exception as exc:
                            log.warning("Wiki point-in-time restore: could not "
                                        "remove %s: %s", key, exc)
            except RestoreCancelled:
                raise
            except Exception as exc:
                # Fail safe, and say so. Anything that goes wrong here aborts
                # the phase BEFORE the point-in-time deletions, so a wiki that
                # could not be fully written is never also pruned — a half
                # restore beats a half restore with the other half deleted.
                # It used to be logged and nowhere else; with `replace` on the
                # table, an operator being told "restored" after a partial
                # wiki pass is a materially worse thing to be told.
                wiki_error = str(exc)
                log.warning("Wiki restore failed during org restore: %s", exc)
        ctl.finish()

        return {
            "status":            "restored",
            "org_id":            actual_org_id,
            "org_slug":          org_slug,
            "org_name":          manifest.get("org_name"),
            "plan":              plan.to_dict(),
            "memories_restored": restored_memories,
            "memories_cleared":  memories_cleared,
            # Restored but not yet searchable — the operator has to be told,
            # and told in a number rather than a footnote.
            "embeddings_pending": deferred_embeds,
            "cards_restored":    memapi_restored.get("cards", 0),
            "columns_restored":  memapi_restored.get("board_columns", 0),
            "hot_restored":      memapi_restored.get("memory_hot", 0),
            "wiki_restored":     bool(wiki_files_restored),
            "wiki_files_restored": wiki_files_restored,
            "wiki_files_removed":  wiki_files_removed,
            "wiki_error":          wiki_error,
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp_dir, ignore_errors=True)
        if _dec_tmp is not None:
            _dec_tmp.unlink(missing_ok=True)


# ── Auto-backup scheduler ──────────────────────────────────────────────────

_DEFAULT_BACKUP_INTERVAL_HOURS = 24


_DEFAULT_BACKUP_RETENTION_DAYS = 30


# When an org's backup attempt fails (most commonly: no backup passcode
# configured), back off this long before retrying instead of re-attempting —
# and re-warning — every 60s loop tick. Orgs that simply never configured a
# passcode were otherwise flooding logs and burning CPU once per minute.
_BACKUP_FAIL_BACKOFF_HOURS = 1


def _backoff_last_check(now, interval_hours):
    """Return a `last_check` timestamp that defers the next attempt by
    `_BACKUP_FAIL_BACKOFF_HOURS` (clamped so it can't push retries past the
    normal interval or, for sub-hour intervals, into the future)."""
    defer = min(_BACKUP_FAIL_BACKOFF_HOURS, interval_hours)
    return now - timedelta(hours=interval_hours) + timedelta(hours=defer)


def _run_auto_backup_loop():
    """Background task: periodically backup memories for active tenants.

    Tenants without an explicit MemoryBackupConfig row are treated as
    enabled=True with default interval/retention.
    """
    import time as _time
    last_check: dict = {}

    while True:
        try:
            _time.sleep(60)
            session = get_db_session().__next__()
            try:
                configs_by_tenant = {
                    c.tenant_id: c for c in session.query(MemoryBackupConfig).all()
                }
                tenants = session.query(Tenant).filter_by(is_active=True).all()
                if not any(t.slug == DEFAULT_TENANT_SLUG for t in tenants):
                    tenants = list(tenants) + [type("_T", (), {"slug": DEFAULT_TENANT_SLUG})()]

                for tenant in tenants:
                    tenant_slug = tenant.slug
                    cfg = configs_by_tenant.get(tenant_slug)
                    if cfg is not None and not cfg.enabled:
                        continue
                    interval_hours = cfg.interval_hours if cfg else _DEFAULT_BACKUP_INTERVAL_HOURS
                    retention_days = cfg.retention_days if cfg else _DEFAULT_BACKUP_RETENTION_DAYS

                    now = datetime.utcnow()
                    last = last_check.get(tenant_slug, now - timedelta(hours=interval_hours + 1))
                    if (now - last).total_seconds() / 3600 < interval_hours:
                        continue

                    try:
                        _create_memory_backup(tenant_slug)
                        if cfg:
                            cfg.last_backup_at = now
                            session.commit()
                        _prune_backups(
                            Path("/data/memories/backups") / tenant_slug,
                            "memories-backup-*.json",
                            retention_days,
                        )
                        last_check[tenant_slug] = now
                    except Exception as exc:
                        log.warning("Memory auto-backup failed for %s: %s", tenant_slug, exc)
                        session.rollback()
                        last_check[tenant_slug] = _backoff_last_check(now, interval_hours)
            finally:
                session.close()
        except Exception as exc:
            log.warning("Memory auto-backup loop error: %s", exc)


def _run_wiki_backup_loop():
    """Background task: periodically backup wiki for active tenants."""
    import time as _time
    last_check: dict = {}

    while True:
        try:
            _time.sleep(60)
            session = get_db_session().__next__()
            try:
                configs_by_tenant = {
                    c.tenant_id: c for c in session.query(WikiBackupConfig).all()
                }
                tenants = session.query(Tenant).filter_by(is_active=True).all()
                if not any(t.slug == DEFAULT_TENANT_SLUG for t in tenants):
                    tenants = list(tenants) + [type("_T", (), {"slug": DEFAULT_TENANT_SLUG})()]

                for tenant in tenants:
                    tenant_slug = tenant.slug
                    cfg = configs_by_tenant.get(tenant_slug)
                    if cfg is not None and not cfg.enabled:
                        continue
                    interval_hours = cfg.interval_hours if cfg else _DEFAULT_BACKUP_INTERVAL_HOURS
                    retention_days = cfg.retention_days if cfg else _DEFAULT_BACKUP_RETENTION_DAYS

                    now = datetime.utcnow()
                    last = last_check.get(tenant_slug, now - timedelta(hours=interval_hours + 1))
                    if (now - last).total_seconds() / 3600 < interval_hours:
                        continue

                    try:
                        _create_wiki_backup(tenant_slug)
                        if cfg:
                            cfg.last_backup_at = now
                            session.commit()
                        _prune_backups(
                            _wiki_backup_dir(tenant_slug),
                            "wiki-backup-*.tar.gz",
                            retention_days,
                        )
                        last_check[tenant_slug] = now
                    except Exception as exc:
                        log.warning("Wiki auto-backup failed for %s: %s", tenant_slug, exc)
                        session.rollback()
                        last_check[tenant_slug] = _backoff_last_check(now, interval_hours)
            finally:
                session.close()
        except Exception as exc:
            log.warning("Wiki auto-backup loop error: %s", exc)


def _run_org_backup_loop():
    """Background task: periodically run full org snapshots per OrgBackupSchedule."""
    import time as _time
    last_check: dict = {}

    while True:
        try:
            _time.sleep(60)
            session = get_db_session().__next__()
            try:
                schedules = {
                    s.org_id: s for s in session.query(OrgBackupSchedule).filter_by(enabled=True).all()
                }
                orgs = session.query(Tenant).filter_by(is_active=True).all()
                for org in orgs:
                    sched = schedules.get(org.id)
                    if sched is None:
                        continue  # no schedule configured — skip (manual-only for this org)
                    now = datetime.utcnow()
                    last = last_check.get(org.id, now - timedelta(hours=sched.interval_hours + 1))
                    if (now - last).total_seconds() / 3600 < sched.interval_hours:
                        continue
                    try:
                        _dispatch_org_backup(org.id, session)
                        last_check[org.id] = now
                    except Exception as exc:
                        log.warning("Org auto-backup failed for %s: %s", org.slug, exc)
                        session.rollback()
            finally:
                session.close()
        except Exception as exc:
            log.warning("Org auto-backup loop error: %s", exc)


# ── Platform org backup / restore ──────────────────────────────────────────

def _resolve_local_org_backup(org_slug: str, backup_id: str, session: Session) -> Optional[Path]:
    """Locate a backup file for `backup_id` (extension-agnostic), pulling it
    from S3 if it isn't present locally. Tries the current `.zip` format first,
    then the legacy `.tar.gz`. Returns the local Path or None."""
    backup_dir = _org_backup_dir(org_slug)
    for ext in (".zip", ".tar.gz"):
        p = backup_dir / f"{backup_id}{ext}"
        if p.exists():
            return p
    try:
        s3_cfg = _get_s3_config_row(session)
        if s3_cfg and s3_cfg.enabled and s3_cfg.bucket:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for ext in (".zip", ".tar.gz"):
                fn = f"{backup_id}{ext}"
                dest = backup_dir / fn
                try:
                    _download_from_s3(s3_cfg, org_slug, fn, dest)
                except Exception:
                    dest.unlink(missing_ok=True)
                    continue
                if dest.exists():
                    return dest
    except Exception as exc:
        log.warning("S3 lookup for backup %s/%s failed: %s", org_slug, backup_id, exc)
    return None


def _read_backup_manifest(bundle_path: Path, passcode: Optional[str]) -> dict:
    """Read manifest.json from any backup format (AES-256 zip / legacy GCM /
    plaintext tar.gz). Encrypted formats require `passcode`. Raises
    BackupCryptoError/BackupNotConfigured/ValueError on failure."""
    if backup_crypto.is_zip(bundle_path):
        if not passcode:
            raise BackupNotConfigured(
                "This backup is encrypted — a passcode is required to import it.")
        return _json.loads(backup_crypto.read_aes_zip_member(bundle_path, "manifest.json", passcode))

    import tarfile as _tarfile
    src = bundle_path
    _dec: Optional[Path] = None
    try:
        if backup_crypto.is_encrypted(bundle_path):
            if not passcode:
                raise BackupNotConfigured(
                    "This backup is encrypted — a passcode is required to import it.")
            _dec = Path(f"/tmp/org-import-peek-{secrets.token_hex(8)}.tar.gz")
            backup_crypto.decrypt_file(bundle_path, _dec, passcode)
            src = _dec
        with _tarfile.open(src, "r:gz") as tf:
            mf = tf.extractfile("manifest.json")
            if mf is None:
                raise ValueError("manifest.json not found in archive")
            return _json.loads(mf.read())
    finally:
        if _dec is not None:
            _dec.unlink(missing_ok=True)


# Uploaded bundles awaiting the operator's go-ahead. They are full org
# backups, so they are swept on a timer rather than left lying around when
# somebody previews a restore and then closes the tab. Overridable so tests
# (and anyone mounting /data read-only) don't stage into the live volume.
_RESTORE_STAGING_DIR = Path(
    os.environ.get("RESTORE_STAGING_DIR", "/data/restore-staging"))


def _restore_staging_dir() -> Path:
    _RESTORE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    return _RESTORE_STAGING_DIR


# How big each area is — in the bundle, and live right now. A v1 bundle
# predates boards, so its card count is absent rather than zero, and None has
# to survive all the way to the screen instead of being rendered as "0 cards".
def _bundle_board_count(manifest: dict):
    cards = manifest.get("card_count")
    cols  = manifest.get("board_column_count")
    if cards is None and cols is None:
        return None
    return (cards or 0) + (cols or 0) + (manifest.get("hot_memory_count") or 0)


_AREA_BUNDLE_COUNT = {
    "identity": lambda m: m.get("user_count"),
    "wiki":     lambda m: m.get("wiki_file_count"),
    "boards":   _bundle_board_count,
    "memories": lambda m: m.get("memory_count"),
}


def _live_wiki_file_count(org: Tenant) -> Optional[int]:
    """Pages in the org's bucket today. Best-effort: object storage being
    unreachable must not block a preview of the other three areas."""
    try:
        s3 = get_s3_storage()
        if s3 is None:
            return None
        return sum(1 for _ in s3.walk(org.slug))
    except Exception as exc:
        log.warning("restore preview: could not count live wiki files for %s: %s",
                    org.slug, exc)
        return None


def _live_memory_count(org: Tenant) -> Optional[int]:
    try:
        resp = httpx.get(f"{EMBEDDER_URL}/api/memories",
                         params={"tenant_slug": org.slug, "limit": 0},
                         headers=_internal_service_headers(), timeout=8)
        if resp.status_code == 200:
            return len(resp.json().get("memories", []))
    except Exception as exc:
        log.warning("restore preview: could not count live memories for %s: %s",
                    org.slug, exc)
    return None


def _live_board_count(org: Tenant) -> Optional[int]:
    try:
        resp = httpx.get(f"{MEMORY_API_URL}/internal/org-export",
                         params={"tenant_slug": org.slug},
                         headers=_internal_service_headers(), timeout=8)
        if resp.status_code == 200:
            tables = resp.json().get("tables", {}) or {}
            return sum(len(tables.get(k) or [])
                       for k in ("cards", "board_columns", "memory_hot"))
    except Exception as exc:
        log.warning("restore preview: could not count live board rows for %s: %s",
                    org.slug, exc)
    return None


_AREA_LIVE_COUNT = {
    "identity": lambda org, s: s.query(UserAccount).filter_by(org_id=org.id).count(),
    "wiki":     lambda org, s: _live_wiki_file_count(org),
    "boards":   lambda org, s: _live_board_count(org),
    "memories": lambda org, s: _live_memory_count(org),
}


def _preview_org_bundle(bundle_path: Path, passcode: Optional[str],
                        target_org: Optional[Tenant], session: Session) -> dict:
    """What is in this bundle, and what restoring it would do to `target_org`.

    Reads the manifest only — nothing in the org is touched. (A legacy GCM
    bundle is decrypted to a temp file to get at its manifest and the temp
    file is removed again; current .zip bundles are read in place.)
    `target_org` is None for an import that will create the org, which is the
    one case where a restore destroys nothing.

    The `effects` list is the part that matters: a restore merges most tables
    but deletes the target's wiki history outright, and an operator is
    entitled to know that before saying yes rather than after.
    """
    manifest = _read_backup_manifest(bundle_path, passcode)
    src_id   = manifest.get("org_id")
    src_slug = manifest.get("org_slug")
    if not src_id or not src_slug:
        raise ValueError("Manifest is missing org_id or org_slug")

    same_org = bool(target_org) and (src_id == target_org.id)
    warnings = []
    if target_org is None:
        warnings.append(
            f"No org with slug '{src_slug}' exists here yet — it will be created "
            "from the backup's manifest, so nothing existing is overwritten.")
    elif not same_org:
        warnings.append(
            f"This backup is from a DIFFERENT org ({src_slug}). Restoring it "
            f"into {target_org.slug} moves one org's data into another — check "
            "this is the BC/DR test you meant to run.")

    # What each area is worth restoring, and what restoring it would cost.
    # Counts come from the manifest (what's in the bundle) alongside what is
    # live now, so "replace" can be judged rather than guessed at.
    areas = []
    for area in restore_jobs.AREAS:
        areas.append({
            "area":       area,
            "label":      restore_jobs.AREA_LABEL[area],
            "modes":      list(restore_jobs.AREA_MODES[area]),
            "in_bundle":  _AREA_BUNDLE_COUNT[area](manifest),
            "live_now":   (_AREA_LIVE_COUNT[area](target_org, session)
                           if target_org is not None else None),
            # Creating an org means writing its row, so identity is not
            # optional on an import — say so rather than failing later.
            "required":   area == "identity" and target_org is None,
        })

    try:
        size_bytes = bundle_path.stat().st_size
    except OSError:
        size_bytes = None
    # Whether starting this restore will need the passcode again. The passcode
    # is held in memory only, so the start call has to be able to tell "none
    # was needed" from "the one we had is gone".
    encrypted = backup_crypto.is_zip(bundle_path) or backup_crypto.is_encrypted(bundle_path)

    target = None
    if target_org is not None:
        target = {
            "org_id":   target_org.id,
            "org_slug": target_org.slug,
            "org_name": target_org.display_name or target_org.name,
            "user_count":        session.query(UserAccount).filter_by(org_id=target_org.id).count(),
            "wiki_revisions":    session.query(WikiRevision).filter_by(tenant_id=target_org.id).count(),
            "recycle_bin_pages": session.query(SoftDeletedPage).filter_by(tenant_id=target_org.id).count(),
        }

    return {
        "bundle": {
            "version":            manifest.get("version", "1"),
            "created_at":         manifest.get("created_at"),
            "org_id":             src_id,
            "org_slug":           src_slug,
            "org_name":           manifest.get("org_name"),
            "size_bytes":         size_bytes,
            "user_count":         manifest.get("user_count"),
            "memory_count":       manifest.get("memory_count"),
            "wiki_file_count":    manifest.get("wiki_file_count"),
            # v1 bundles predate boards; absent is not the same as zero.
            "card_count":         manifest.get("card_count"),
            "board_column_count": manifest.get("board_column_count"),
            "hot_memory_count":   manifest.get("hot_memory_count"),
        },
        "target":        target,
        "creates_org":   target_org is None,
        "same_org":      same_org,
        "encrypted":     bool(encrypted),
        "warnings":      warnings,
        "areas":         areas,
        # The wording for every area-and-mode combination, so the review
        # screen describes a plan with the same sentences the job records.
        "area_effects":  restore_jobs.AREA_EFFECT,
        "embed_effects": restore_jobs.EMBED_EFFECT,
        # What a plan that changes nothing about the defaults would do.
        "effects":       restore_jobs.RestorePlan().effects(),
    }


# ── Supervised org-restore jobs ──────────────────────────────────────────────
# A full org restore re-embeds every memory via the embedder, which can take
# minutes and blow past proxy timeouts (observed: ~2.5 min -> 504 while the
# restore actually succeeded). It runs off-request, reports progress, and can
# be paused or cancelled; see restore_jobs.py for the job model.


def _run_org_restore_job(job_id: str, target_org_id: Optional[str], bundle_path: Path,
                         passcode: Optional[str], cleanup: bool,
                         ctl: RestoreControl,
                         plan: Optional[restore_jobs.RestorePlan] = None) -> None:
    """Worker thread for one org restore.

    `target_org_id` is None for an import that creates the org from the
    bundle's own manifest; the job row still carries an org_id so the UI can
    find the job by org either way.
    """
    from config_db import SessionLocal as _SL
    reg = restore_jobs.registry
    reg.update(job_id, status="running", started_at=datetime.utcnow(), error=None)
    try:
        with _SL() as _s:
            result = _restore_org_snapshot(bundle_path, target_org_id, _s,
                                           passcode=passcode, ctl=ctl, plan=plan)
        reg.progress(job_id, ctl.snapshot(), force=True)
        reg.update(job_id, status="done", result=result,
                   finished_at=datetime.utcnow(),
                   # An import that created the org may have landed on a
                   # different id than the manifest's; record where it went.
                   org_id=result.get("org_id") or target_org_id,
                   org_slug=result.get("org_slug"))
    except RestoreCancelled:
        # Say which cancel this was. Before the database commit the org is
        # untouched; after it, some of the restore has already landed and
        # pretending otherwise would send an operator away from a half-org.
        partial = ctl.touched_org
        reg.progress(job_id, ctl.snapshot(), force=True)
        reg.update(
            job_id, status="cancelled", finished_at=datetime.utcnow(),
            error=("Cancelled after the database had been restored — this org "
                   "is PARTIALLY restored. Re-run the restore to finish it."
                   if partial else
                   "Cancelled before anything was written — this org is unchanged."),
        )
        log.warning("Org restore job %s cancelled (partial=%s)", job_id, partial)
    except Exception as exc:
        log.exception("Org restore job %s failed", job_id)
        reg.progress(job_id, ctl.snapshot(), force=True)
        reg.update(job_id, status="error", error=str(exc),
                   finished_at=datetime.utcnow())
    finally:
        if cleanup:
            try:
                bundle_path.unlink(missing_ok=True)
            except Exception:
                pass
        reg.update(job_id, staged_path=None)
        reg.detach(job_id)


def _start_org_restore_job(job_id: str, target_org_id: Optional[str], bundle_path: Path,
                           passcode: Optional[str], cleanup: bool,
                           plan: Optional[restore_jobs.RestorePlan] = None) -> None:
    """Spawn the worker, with its control already registered.

    Attaching before the thread starts is what makes the queued window
    cancellable: a cancel that arrived while the thread was still being
    scheduled would otherwise fall through to the staged-job path and delete
    the bundle out from under the worker about to open it.
    """
    import threading
    reg = restore_jobs.registry
    ctl = RestoreControl(on_update=lambda snap: reg.progress(job_id, snap), plan=plan)
    reg.attach(job_id, ctl)
    threading.Thread(
        target=_run_org_restore_job,
        args=(job_id, target_org_id, bundle_path, passcode, cleanup, ctl, plan),
        daemon=True,
    ).start()
