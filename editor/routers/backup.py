"""
The editor's backup and restore web API.

`router` is the org-admin surface: import and export, and the memory and
wiki backups. `platform_router` is the platform surface: backup targets,
org backup schedules, full org snapshots, restore preview and the
supervised restore jobs. main.py includes each one where its routes used to
be declared, so the route order is unchanged. The machinery behind both is
in org_backup.py.

Moved out of main.py by CQ-04 phase 4; the paths are unchanged.
"""
import io
import json as _json
import logging
import secrets
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import backup_crypto
import restore_jobs
from auth import require_org_admin, require_platform_admin
from auth_helpers import _effective_tenant_slug, _require_multi_org
from config_db import (
    get_db_session, Tenant, UserAccount, OrgDomain,
    MemoryBackupConfig, WikiBackupConfig,
    OrgBackupS3Config, OrgBackupSSHTarget, OrgBackupSchedule,
    crypto,
)
from internal_service import EMBEDDER_URL, _internal_service_headers
from org_backup import (
    BackupNotConfigured, _restore_jobs,
    _create_memory_backup, _run_tenant_memory_restore_job,
    _get_tenant_backup_passcode, _set_tenant_backup_passcode,
    _wiki_backup_dir, _create_wiki_backup,
    _org_backup_dir, _get_platform_backup_passcode, _set_platform_backup_passcode,
    _resolve_org_backup_passcode,
    _get_s3_config_row, _s3_client, _list_s3_org_backups,
    _get_ssh_config_row, _test_ssh_connection,
    _get_org_schedule, _dispatch_org_backup, _restore_org_snapshot,
    _resolve_local_org_backup, _read_backup_manifest, _restore_staging_dir,
    _preview_org_bundle, _start_org_restore_job,
)
from storage import get_s3_storage
from wiki_storage import _active_org, _normalize_path, _s3, _s3_delete, _s3_read, _s3_write

log = logging.getLogger("editor")

router = APIRouter()
platform_router = APIRouter()


# ── Import / Export ────────────────────────────────────────────────────────

@router.post("/api/admin/import")
async def import_docs(
    request: Request,
    _claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """
    Import documents into the caller's org bucket.
    mode: "single" (.md) | "merge" (.zip into target_path) | "restore" (.zip replaces all)
    Accepts files up to 500MB.
    """
    try:
        # Read body manually to avoid Starlette's internal size limits
        from starlette.formparsers import MultiPartParser

        # Get the actual request body
        body = b""
        async for chunk in request.stream():
            body += chunk

        # Create async iterable that yields body chunks
        class AsyncBodyStream:
            def __init__(self, data):
                self.data = data
                self.offset = 0
                self.chunk_size = 1024 * 1024  # 1MB chunks

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.offset >= len(self.data):
                    raise StopAsyncIteration
                chunk = self.data[self.offset:self.offset + self.chunk_size]
                self.offset += self.chunk_size
                return chunk

        body_stream = AsyncBodyStream(body)
        parser = MultiPartParser(request.headers, body_stream)
        form_data = await parser.parse()

        mode = str(form_data.get("mode", "")).strip()
        target_path = str(form_data.get("target_path", "")).strip()
        file = form_data.get("file")

        if not mode or mode not in ("single", "merge", "restore"):
            raise HTTPException(status_code=400, detail=f"Invalid import mode: {mode}")

        if not file:
            raise HTTPException(status_code=400, detail="No file provided")

        data = await file.read() if hasattr(file, "read") else file
        if not data:
            raise HTTPException(status_code=400, detail="File is empty")

        filename = getattr(file, "filename", "upload") or "upload"
        filename = filename.lstrip("/")

        org = _active_org(request, session)
        target_norm = _normalize_path(target_path) if target_path.strip() else ""

        if mode == "single":
            if not filename.lower().endswith(".md"):
                raise HTTPException(status_code=400, detail="Single import requires a .md file")
            leaf = _normalize_path(Path(filename).name)
            rel = f"{target_norm}/{leaf}" if target_norm else leaf
            _s3_write(org, rel, data)
            return {"status": "imported", "count": 1, "files": [rel]}

        elif mode in ("merge", "restore"):
            if not filename.lower().endswith(".zip"):
                raise HTTPException(status_code=400, detail=f"ZIP import requires a .zip file (got {filename})")

            # Validate ZIP before making destructive changes
            try:
                test_zip = zipfile.ZipFile(io.BytesIO(data))
                test_zip.close()
            except zipfile.BadZipFile as e:
                raise HTTPException(status_code=400, detail=f"Invalid ZIP file: {str(e)}")

            if mode == "restore":
                # Drop every object in the org's bucket. Versioning leaves a
                # delete-marker so the prior content is still recoverable
                # until lifecycle rules expire it.
                for k in list(_s3().walk(org.slug)):
                    try:
                        _s3_delete(org, k)
                    except Exception as exc:
                        log.warning("Restore-mode delete failed: key=%s err=%s", k, exc)

            imported = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for member in zf.namelist():
                        # Skip directories, macOS metadata, and hidden files
                        if member.endswith("/") or "__MACOSX" in member or member.startswith("."):
                            continue

                        # In merge mode, only import .md files
                        if mode == "merge" and not member.lower().endswith(".md"):
                            continue

                        # Build destination key inside the org bucket. _normalize_path
                        # rejects traversal so a malicious ZIP can't escape the
                        # target prefix.
                        try:
                            member_rel = _normalize_path(member)
                        except HTTPException:
                            log.warning("Skipping %s — invalid path", member)
                            continue
                        rel = f"{target_norm}/{member_rel}" if (mode == "merge" and target_norm) else member_rel
                        _s3_write(org, rel, zf.read(member))
                        imported.append(rel)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"ZIP extraction failed: {str(e)}")

            if not imported:
                if mode == "merge":
                    raise HTTPException(status_code=400, detail="No .md files found in ZIP")
                else:
                    raise HTTPException(status_code=400, detail="ZIP is empty or contains no valid files")

            return {"status": "imported", "count": len(imported), "files": imported}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Import error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.get("/api/export/page")
def export_page(path: str = Query(...), request: Request = None,
                session: Session = Depends(get_db_session)):
    org = _active_org(request, session)
    rel = _normalize_path(path)
    data = _s3_read(org, rel)
    leaf = Path(rel).name
    return Response(
        content=data,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{leaf}"'},
    )


@router.get("/api/export/kb")
def export_kb(request: Request = None, session: Session = Depends(get_db_session)):
    """Stream a ZIP of every object in the org's bucket. Skips placeholder
    `.keep` markers and other dotfiles to match the previous filesystem
    export behavior."""
    org = _active_org(request, session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        try:
            for key in _s3().walk(org.slug):
                leaf = key.split("/")[-1]
                if leaf.startswith("."):
                    continue
                try:
                    data = _s3().read(org.slug, key)
                except Exception as exc:
                    log.warning("Export skip: key=%s err=%s", key, exc)
                    continue
                zf.writestr(key, data)
        except Exception as exc:
            log.error("Export listing failed: org=%s err=%s", org.slug, exc)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="knowledge-base.zip"'},
    )


# ── Memory backup/restore ──────────────────────────────────────────────────

class MemoryBackupConfigRequest(BaseModel):
    enabled: bool
    interval_hours: int = 24
    retention_days: int = 30


class WikiBackupConfigRequest(BaseModel):
    enabled: bool
    interval_hours: int = 24
    retention_days: int = 30


@router.get("/api/admin/memories/export")
def export_memories(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Export all memories as JSON (org-scoped; downloaded inline)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        resp = httpx.get(
            f"{EMBEDDER_URL}/api/memories",
            params={"tenant_slug": tenant_slug, "limit": 0},
            headers=_internal_service_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch memories: {exc}")

    memories = data.get("memories", [])
    export_data = {
        "export_time": datetime.utcnow().isoformat() + "Z",
        "tenant_slug": tenant_slug,
        "count": len(memories),
        "memories": memories,
    }

    return StreamingResponse(
        iter([_json.dumps(export_data, indent=2)]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="memories-export-{tenant_slug}-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.json"'},
    )


@router.get("/api/admin/memories/auto-backup")
def get_auto_backup_config(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Get automatic memory backup configuration (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    config = session.query(MemoryBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if config is None:
        return {
            "enabled": True,
            "interval_hours": 24,
            "retention_days": 30,
            "last_backup_at": None,
        }
    return {
        "enabled": config.enabled,
        "interval_hours": config.interval_hours,
        "retention_days": config.retention_days,
        "last_backup_at": config.last_backup_at.isoformat() if config.last_backup_at else None,
    }


@router.post("/api/admin/memories/auto-backup")
def configure_auto_backup(
    body: MemoryBackupConfigRequest,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Configure automatic memory backups (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    config = session.query(MemoryBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if config is None:
        config = MemoryBackupConfig(
            tenant_id=tenant_slug,
            enabled=body.enabled,
            interval_hours=body.interval_hours,
            retention_days=body.retention_days,
        )
        session.add(config)
    else:
        config.enabled = body.enabled
        config.interval_hours = body.interval_hours
        config.retention_days = body.retention_days

    session.commit()
    return {"status": "saved"}


@router.post("/api/admin/memories/backup-now")
def backup_memories_now(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Trigger an immediate memory backup; returns the new backup metadata."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        info = _create_memory_backup(tenant_slug)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Backup failed: {exc}")
    cfg = session.query(MemoryBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if cfg:
        cfg.last_backup_at = datetime.utcnow()
        session.commit()
    return {"status": "created", **info}


@router.get("/api/admin/backup-passcode")
def get_tenant_backup_passcode_status(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Whether this org's backup passcode is set (never returns the value)."""
    slug = _effective_tenant_slug(claims, None, session)
    return {"set": bool(_get_tenant_backup_passcode(slug, session))}


@router.post("/api/admin/backup-passcode")
def set_tenant_backup_passcode_endpoint(
    body: dict,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Set this org's backup passcode (Fernet-encrypted at rest). Encrypts the
    org's own memory + wiki backups; independent of the platform passcode."""
    slug = _effective_tenant_slug(claims, None, session)
    pc = (body.get("passcode") or "").strip()
    if not pc:
        raise HTTPException(status_code=422, detail="passcode is required")
    _set_tenant_backup_passcode(slug, pc, session)
    session.commit()
    return {"set": True}


@router.get("/api/admin/backup-status")
def get_tenant_backup_status(
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Backup configuration status for this org — drives the 'backups not
    configured' warning banner. Backups are hard-enforced-encrypted, so a
    missing passcode means nothing gets backed up."""
    slug = _effective_tenant_slug(claims, None, session)
    passcode_set = bool(_get_tenant_backup_passcode(slug, session))
    mem  = session.query(MemoryBackupConfig).filter_by(tenant_id=slug).first()
    wiki = session.query(WikiBackupConfig).filter_by(tenant_id=slug).first()
    mem_on  = bool(mem and mem.enabled)
    wiki_on = bool(wiki and wiki.enabled)
    return {
        "passcode_set":          passcode_set,
        "memory_backup_enabled": mem_on,
        "wiki_backup_enabled":   wiki_on,
        "configured":            passcode_set and (mem_on or wiki_on),
    }


@router.get("/api/admin/memories/backups")
def list_memory_backups(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """List available memory backup files (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    backup_dir = Path("/data/memories/backups") / tenant_slug
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not backup_dir.exists():
        return {"backups": []}

    backups = []
    for f in sorted(backup_dir.glob("memories-backup-*.json"), reverse=True):
        try:
            stat = f.stat()
            if backup_crypto.is_encrypted(f):
                # Encrypted — counts/export_time aren't readable without the passcode.
                backups.append({
                    "id": f.stem, "filename": f.name, "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "memory_count": None, "encrypted": True, "tenant_slug": tenant_slug,
                })
                continue
            with f.open() as fp:
                data = _json.load(fp)
            backups.append({
                "id": f.stem,
                "filename": f.name,
                "size_bytes": stat.st_size,
                "created_at": data.get("export_time"),
                "memory_count": data.get("count", 0),
                "encrypted": False,
                "tenant_slug": tenant_slug,
            })
        except Exception:
            pass

    return {"backups": backups}


@router.get("/api/admin/memories/backups/{backup_id}/download")
def download_memory_backup(
    backup_id: str,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Stream a specific memory backup file by ID (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    # Strict id validation — must look like a memories-backup-* stem so a
    # caller can't traverse out of the backup directory.
    if "/" in backup_id or ".." in backup_id or not backup_id.startswith("memories-backup-"):
        raise HTTPException(status_code=400, detail="Invalid backup id")
    backup_path = Path("/data/memories/backups") / tenant_slug / f"{backup_id}.json"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    def _iter():
        with backup_path.open("rb") as fp:
            while chunk := fp.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{backup_path.name}"'},
    )


@router.post("/api/admin/memories/backups/upload")
async def upload_memory_backup(
    file: UploadFile = File(...),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Upload a memory backup .json file into this org's backup directory.

    After upload, the backup appears in /api/admin/memories/backups and can
    be restored via /api/admin/memories/restore with the returned id.
    """
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Only .json backup files are accepted")

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # 100MB
        raise HTTPException(status_code=400, detail="Backup file too large (100MB max)")

    backup_dir = Path("/data/memories/backups") / tenant_slug
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"memories-backup-{timestamp}-upload.json"

    # Encrypted backup: can't validate JSON without the passcode — save the
    # bytes verbatim; restore decrypts and validates with the passcode.
    if backup_crypto.is_encrypted_bytes(content):
        target.write_bytes(content)
        stat = target.stat()
        log.info("Uploaded encrypted memory backup: %s (tenant=%s)", target.name, tenant_slug)
        return {"id": target.stem, "filename": target.name, "size_bytes": stat.st_size,
                "memory_count": None, "encrypted": True}

    try:
        data = _json.loads(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        raise HTTPException(status_code=400, detail="Backup must contain a 'memories' list")

    saved = {
        "export_time": data.get("export_time") or datetime.utcnow().isoformat() + "Z",
        "tenant_slug": tenant_slug,
        "count": len(data["memories"]),
        "memories": data["memories"],
    }
    with target.open("w") as f:
        _json.dump(saved, f, indent=2)

    stat = target.stat()
    log.info("Uploaded memory backup: %s (%d memories, tenant=%s)",
             target.name, saved["count"], tenant_slug)
    return {
        "id": target.stem,
        "filename": target.name,
        "size_bytes": stat.st_size,
        "created_at": saved["export_time"],
        "memory_count": saved["count"],
        "tenant_slug": tenant_slug,
    }


@router.get("/api/admin/restore-jobs/{job_id}")
def get_tenant_restore_job(
    job_id: str,
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Poll a per-org restore job. Scoped to the caller's tenant so one org
    can't read another's job."""
    slug = _effective_tenant_slug(claims, None, session)
    job = _restore_jobs.get(job_id)
    if not job or job.get("tenant_slug") != slug:
        raise HTTPException(status_code=404, detail="Unknown restore job")
    return job


@router.post("/api/admin/memories/restore")
def restore_memories(
    backup_id: str = Form(...),
    mode: str = Form("replace"),
    passcode: Optional[str] = Form(None),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Restore memories from a backup (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    if mode not in ("replace", "merge"):
        raise HTTPException(status_code=400, detail="Invalid mode; must be 'replace' or 'merge'")

    backup_path = Path("/data/memories/backups") / tenant_slug / f"{backup_id}.json"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    src_path = backup_path
    _dec_tmp: Optional[Path] = None
    if backup_crypto.is_encrypted(backup_path):
        eff = (passcode or "").strip() or _get_tenant_backup_passcode(tenant_slug, session)
        if not eff:
            raise HTTPException(status_code=400,
                                detail="This backup is encrypted — a passcode is required to restore it.")
        _dec_tmp = Path(f"/tmp/mem-restore-dec-{secrets.token_hex(8)}.json")
        try:
            backup_crypto.decrypt_file(backup_path, _dec_tmp, eff)
        except backup_crypto.BackupCryptoError as exc:
            _dec_tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc))
        src_path = _dec_tmp
    try:
        with src_path.open() as f:
            backup_data = _json.load(f)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read backup: {exc}")
    finally:
        if _dec_tmp is not None:
            _dec_tmp.unlink(missing_ok=True)

    memories = backup_data.get("memories", [])
    # Re-adding memories re-embeds each one (slow) — run off-request so it can't
    # 504 at the proxy; the UI polls /api/admin/restore-jobs/{job_id}.
    import threading
    job_id = secrets.token_hex(8)
    _restore_jobs[job_id] = {"status": "queued", "tenant_slug": tenant_slug}
    threading.Thread(
        target=_run_tenant_memory_restore_job,
        args=(job_id, tenant_slug, memories, mode),
        daemon=True,
    ).start()
    return JSONResponse({"status": "started", "job_id": job_id, "total": len(memories)}, status_code=202)


# ── Wiki backup/restore ────────────────────────────────────────────────────

@router.get("/api/admin/wiki/auto-backup")
def get_wiki_backup_config(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Get automatic wiki backup configuration (org-scoped). Defaults to enabled=True."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    config = session.query(WikiBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if config is None:
        return {
            "enabled":         True,
            "interval_hours":  24,
            "retention_days":  30,
            "last_backup_at":  None,
        }
    return {
        "enabled":         config.enabled,
        "interval_hours":  config.interval_hours,
        "retention_days":  config.retention_days,
        "last_backup_at":  config.last_backup_at.isoformat() if config.last_backup_at else None,
    }


@router.post("/api/admin/wiki/auto-backup")
def configure_wiki_backup(
    body: WikiBackupConfigRequest,
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Configure automatic wiki backups (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    config = session.query(WikiBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if config is None:
        config = WikiBackupConfig(
            tenant_id=tenant_slug,
            enabled=body.enabled,
            interval_hours=body.interval_hours,
            retention_days=body.retention_days,
        )
        session.add(config)
    else:
        config.enabled        = body.enabled
        config.interval_hours = body.interval_hours
        config.retention_days = body.retention_days
    session.commit()
    return {"status": "saved"}


@router.post("/api/admin/wiki/backup-now")
def backup_wiki_now(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Trigger an immediate wiki backup; returns the new backup metadata."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        info = _create_wiki_backup(tenant_slug)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Backup failed: {exc}")
    cfg = session.query(WikiBackupConfig).filter_by(tenant_id=tenant_slug).first()
    if cfg:
        cfg.last_backup_at = datetime.utcnow()
        session.commit()
    return {"status": "created", **info}


@router.get("/api/admin/wiki/backups")
def list_wiki_backups(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """List available wiki backup archives (org-scoped)."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    backup_dir = _wiki_backup_dir(tenant_slug)
    backup_dir.mkdir(parents=True, exist_ok=True)

    backups = []
    for f in sorted(backup_dir.glob("wiki-backup-*.tar.gz"), reverse=True):
        try:
            stat = f.stat()
            backups.append({
                "id":          f.name.removesuffix(".tar.gz"),
                "filename":    f.name,
                "size_bytes":  stat.st_size,
                "created_at":  datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                "tenant_slug": tenant_slug,
            })
        except Exception:
            pass
    return {"backups": backups}


@router.get("/api/admin/wiki/export")
def export_wiki(
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Stream a fresh wiki tar.gz for the caller's org. The archive is also
    saved to /data/wiki/backups/<slug>/ so it's listed alongside scheduled
    backups."""
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    try:
        info = _create_wiki_backup(tenant_slug)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Wiki export failed: {exc}")
    archive = _wiki_backup_dir(tenant_slug) / info["filename"]

    def _iter_file():
        with archive.open("rb") as fp:
            while chunk := fp.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{info["filename"]}"'},
    )


@router.post("/api/admin/wiki/restore")
def restore_wiki(
    backup_id: str = Form(...),
    mode: str = Form("merge"),
    passcode: Optional[str] = Form(None),
    tenant_slug: Optional[str] = Query(None),
    claims: dict = Depends(require_org_admin),
    session: Session = Depends(get_db_session),
):
    """Restore wiki from a backup archive (org-scoped).

    Modes:
      - merge:   write every archived object into the bucket, overwriting
                 colliding keys, keeping everything else
      - replace: drop every object in the bucket first (versioning leaves
                 the prior content recoverable), then write the archive
    """
    import tarfile
    tenant_slug = _effective_tenant_slug(claims, tenant_slug, session)
    if mode not in ("replace", "merge"):
        raise HTTPException(status_code=400, detail="Invalid mode; must be 'replace' or 'merge'")

    backup_path = _wiki_backup_dir(tenant_slug) / f"{backup_id}.tar.gz"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    org = session.query(Tenant).filter_by(slug=tenant_slug).first()
    if not org:
        raise HTTPException(status_code=404, detail="Tenant not found")

    s3 = get_s3_storage()
    if s3 is None:
        raise HTTPException(status_code=503, detail="Object storage is not configured")

    if mode == "replace":
        for key in list(s3.walk(tenant_slug)):
            try:
                _s3_delete(org, key)
            except Exception as exc:
                log.warning("Replace-mode delete failed: key=%s err=%s", key, exc)

    src_path = backup_path
    _dec_tmp: Optional[Path] = None
    if backup_crypto.is_encrypted(backup_path):
        eff = (passcode or "").strip() or _get_tenant_backup_passcode(tenant_slug, session)
        if not eff:
            raise HTTPException(status_code=400,
                                detail="This backup is encrypted — a passcode is required to restore it.")
        _dec_tmp = Path(f"/tmp/wiki-restore-dec-{secrets.token_hex(8)}.tar.gz")
        try:
            backup_crypto.decrypt_file(backup_path, _dec_tmp, eff)
        except backup_crypto.BackupCryptoError as exc:
            _dec_tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc))
        src_path = _dec_tmp

    try:
        with tarfile.open(src_path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                # Strip any leading "./" the tar writer may have prepended
                key = member.name.lstrip("./")
                try:
                    key = _normalize_path(key)
                except HTTPException:
                    log.warning("Restore skip invalid path: %s", member.name)
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                _s3_write(org, key, f.read())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to extract backup: {exc}")
    finally:
        if _dec_tmp is not None:
            _dec_tmp.unlink(missing_ok=True)

    return {"status": "restored", "backup_id": backup_id, "mode": mode}


# ── Platform org backup / restore ──────────────────────────────────────────

@platform_router.get("/api/platform/backup-config")
def get_platform_backup_config(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Return the platform S3-compatible org backup configuration (masked)."""
    _require_multi_org()
    cfg = _get_s3_config_row(session)
    if cfg is None:
        return {"enabled": False, "endpoint_url": None, "access_key": "",
                "bucket": None, "region": "us-east-1", "prefix": "org-backups/"}
    return cfg.to_dict(masked=True)


@platform_router.post("/api/platform/backup-config")
def save_platform_backup_config(
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Save (upsert) the platform S3-compatible org backup configuration."""
    _require_multi_org()
    cfg = _get_s3_config_row(session)
    if cfg is None:
        cfg = OrgBackupS3Config()
        session.add(cfg)
    cfg.enabled      = bool(body.get("enabled", False))
    cfg.endpoint_url = (body.get("endpoint_url") or "").strip() or None
    cfg.bucket       = (body.get("bucket") or "").strip() or None
    cfg.region       = (body.get("region") or "us-east-1").strip()
    cfg.prefix       = (body.get("prefix") or "org-backups/").strip()
    # Only overwrite keys if the caller sends a non-masked value
    ak = (body.get("access_key") or "").strip()
    sk = (body.get("secret_key") or "").strip()
    if ak and ak != "***":
        cfg.access_key_enc = crypto.encrypt(ak)
    if sk and sk != "***":
        cfg.secret_key_enc = crypto.encrypt(sk)
    session.commit()
    return cfg.to_dict(masked=True)


@platform_router.get("/api/platform/backup-passcode")
def get_platform_backup_passcode_status(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Whether the platform-global backup passcode is set (never returns it)."""
    _require_multi_org()
    return {"set": bool(_get_platform_backup_passcode(session))}


@platform_router.post("/api/platform/backup-passcode")
def set_platform_backup_passcode_endpoint(
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Set the platform-global backup passcode used to encrypt platform org
    backups (Fernet-encrypted at rest). Default for every org unless that org
    has a per-org override on its backup schedule."""
    _require_multi_org()
    pc = (body.get("passcode") or "").strip()
    if not pc:
        raise HTTPException(status_code=422, detail="passcode is required")
    _set_platform_backup_passcode(session, pc)
    session.commit()
    return {"set": True}


@platform_router.get("/api/platform/backup-status")
def get_platform_backup_status(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Platform backup configuration status — drives the platform-admin
    warning banner. The global passcode is the hard gate for encrypted org
    backups."""
    _require_multi_org()
    s3  = _get_s3_config_row(session)
    ssh = _get_ssh_config_row(session)
    return {
        "global_passcode_set": bool(_get_platform_backup_passcode(session)),
        "s3_configured":       bool(s3 and s3.enabled and s3.bucket),
        "ssh_configured":      bool(ssh and ssh.enabled and ssh.host),
        "enabled_schedules":   session.query(OrgBackupSchedule).filter_by(enabled=True).count(),
        "configured":          bool(_get_platform_backup_passcode(session)),
    }


@platform_router.post("/api/platform/backup-config/test")
def test_platform_backup_config(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Test the S3 connection by listing the configured bucket."""
    _require_multi_org()
    cfg = _get_s3_config_row(session)
    if not cfg or not cfg.enabled or not cfg.bucket:
        raise HTTPException(status_code=400, detail="S3 backup not configured or disabled")
    try:
        s3 = _s3_client(cfg)
        s3.head_bucket(Bucket=cfg.bucket)
        return {"success": True, "message": f"Bucket '{cfg.bucket}' is accessible"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@platform_router.get("/api/platform/ssh-target")
def get_platform_ssh_target(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Return the platform SSH backup target config (masked)."""
    _require_multi_org()
    cfg = _get_ssh_config_row(session)
    if cfg is None:
        return {"enabled": False, "host": None, "port": 22, "username": None,
                "remote_path": None, "auth_method": "none", "password": "", "private_key": ""}
    return cfg.to_dict(masked=True)


@platform_router.post("/api/platform/ssh-target")
def save_platform_ssh_target(
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Save (upsert) the platform SSH backup target."""
    _require_multi_org()
    cfg = _get_ssh_config_row(session)
    if cfg is None:
        cfg = OrgBackupSSHTarget()
        session.add(cfg)
    cfg.enabled     = bool(body.get("enabled", False))
    cfg.host        = (body.get("host") or "").strip() or None
    cfg.port        = int(body.get("port") or 22)
    cfg.username    = (body.get("username") or "").strip() or None
    cfg.remote_path = (body.get("remote_path") or "").strip() or None
    pw = (body.get("password") or "").strip()
    pk = (body.get("private_key") or "").strip()
    if pw and pw != "***":
        cfg.password_enc = crypto.encrypt(pw)
        cfg.private_key_enc = None   # mutual exclusive: prefer key if both provided
    if pk and pk != "***":
        cfg.private_key_enc = crypto.encrypt(pk)
        cfg.password_enc = None
    if not pw and not pk:
        # caller explicitly cleared credentials — honour it
        if body.get("clear_auth"):
            cfg.password_enc = None
            cfg.private_key_enc = None
    session.commit()
    return cfg.to_dict(masked=True)


@platform_router.post("/api/platform/ssh-target/test")
def test_platform_ssh_target(
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Test SSH connectivity to the configured target."""
    _require_multi_org()
    cfg = _get_ssh_config_row(session)
    if not cfg or not cfg.enabled or not cfg.host:
        raise HTTPException(status_code=400, detail="SSH target not configured or disabled")
    return _test_ssh_connection(cfg)


@platform_router.get("/api/platform/orgs/{org_id}/backup-schedule")
def get_org_backup_schedule(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Return the backup schedule for an org (defaults when not configured)."""
    _require_multi_org()
    if not session.query(Tenant).filter_by(id=org_id).first():
        raise HTTPException(status_code=404, detail="Org not found")
    sched = _get_org_schedule(org_id, session)
    global_set = bool(_get_platform_backup_passcode(session))
    if sched is None:
        return {"org_id": org_id, "enabled": False, "interval_hours": 24,
                "retention_days": 30, "use_local": True, "use_s3": False,
                "use_ssh": False, "last_backup_at": None,
                "passcode_override_set": False, "global_passcode_set": global_set}
    return {**sched.to_dict(),
            "passcode_override_set": bool(sched.passcode_enc),
            "global_passcode_set": global_set}


@platform_router.post("/api/platform/orgs/{org_id}/backup-schedule")
def save_org_backup_schedule(
    org_id: str,
    body: dict,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Create or update the backup schedule for an org."""
    _require_multi_org()
    if not session.query(Tenant).filter_by(id=org_id).first():
        raise HTTPException(status_code=404, detail="Org not found")
    sched = _get_org_schedule(org_id, session)
    if sched is None:
        sched = OrgBackupSchedule(org_id=org_id)
        session.add(sched)
    sched.enabled        = bool(body.get("enabled", True))
    sched.interval_hours = max(1, int(body.get("interval_hours") or 24))
    sched.retention_days = max(0, int(body.get("retention_days") or 30))
    sched.use_local      = bool(body.get("use_local", True))
    sched.use_s3         = bool(body.get("use_s3", False))
    sched.use_ssh        = bool(body.get("use_ssh", False))
    # Per-org passcode override: a non-empty value overrides the platform
    # global passcode for this org's backups; empty/blank clears the override.
    if "passcode" in body:
        pc = (body.get("passcode") or "").strip()
        sched.passcode_enc = crypto.encrypt(pc) if pc else None
    session.commit()
    return {**sched.to_dict(), "passcode_override_set": bool(sched.passcode_enc)}


@platform_router.post("/api/platform/orgs/{org_id}/backup", status_code=202)
def platform_backup_org(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Trigger an immediate full org snapshot dispatched to all configured targets."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    try:
        info = _dispatch_org_backup(org_id, session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Org snapshot failed: {exc}")
    return {"status": "created", **info}


@platform_router.get("/api/platform/orgs/{org_id}/backups")
def platform_list_org_backups(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """List available org backup archives (local + S3) for the given org."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    backup_dir = _org_backup_dir(org.slug)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Local archives
    local: list[dict] = []
    backups = sorted(
        (f for f in backup_dir.iterdir()
         if f.name.startswith("org-backup-") and f.name.endswith((".zip", ".tar.gz"))),
        key=lambda p: p.name, reverse=True,
    )
    for f in backups:
        try:
            stat = f.stat()
            # Prefer the plaintext sidecar manifest (counts only) — the bundle
            # itself is encrypted, so we can't read manifest.json from it. Fall
            # back to opening the tar for older, unencrypted backups.
            meta: dict = {}
            sidecar = f.with_name(f.name + ".meta.json")
            if sidecar.exists():
                try:
                    meta = _json.loads(sidecar.read_text())
                except Exception:
                    pass
            elif f.name.endswith(".tar.gz"):
                import tarfile as _tf
                try:
                    with _tf.open(f, "r:gz") as arc:
                        m = arc.getmember("manifest.json")
                        meta = _json.loads(arc.extractfile(m).read())
                except Exception:
                    pass
            local.append({
                "id":             f.name.removesuffix(".zip").removesuffix(".tar.gz"),
                "filename":       f.name,
                "size_bytes":     stat.st_size,
                "created_at":     meta.get("created_at") or (
                    datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z"
                ),
                "user_count":     meta.get("user_count"),
                "memory_count":   meta.get("memory_count"),
                "wiki_file_count": meta.get("wiki_file_count"),
                "source":         "local",
            })
        except Exception:
            pass

    # S3 archives (deduplicated against local by filename)
    local_names = {b["filename"] for b in local}
    s3_only: list[dict] = []
    try:
        s3_cfg = _get_s3_config_row(session)
        if s3_cfg and s3_cfg.enabled and s3_cfg.bucket:
            for b in _list_s3_org_backups(s3_cfg, org.slug):
                if b["filename"] not in local_names:
                    s3_only.append(b)
    except Exception as exc:
        log.warning("Could not list S3 org backups for %s: %s", org.slug, exc)

    return {"backups": local + s3_only, "org_slug": org.slug}


@platform_router.get("/api/platform/orgs/{org_id}/backups/{backup_id}/download")
def platform_download_org_backup(
    org_id: str,
    backup_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Stream a local org backup archive for download.  Fetches from S3 first
    if the local file is missing and S3 is configured."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    local_path = _resolve_local_org_backup(org.slug, backup_id, session)
    if local_path is None:
        raise HTTPException(status_code=404, detail="Backup not found")

    filename = local_path.name
    media_type = "application/zip" if filename.endswith(".zip") else "application/gzip"

    def _iter_file():
        with local_path.open("rb") as fp:
            while chunk := fp.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@platform_router.post("/api/platform/orgs/restore-from-upload")
async def platform_restore_org_from_upload(
    file: UploadFile = File(...),
    overwrite_existing: bool = Form(False),
    passcode: Optional[str] = Form(None),
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Import an entire Org from a backup bundle, creating it if it does not
    yet exist on this platform.

    Use this when migrating an Org from another deployment (e.g. staging → prod).
    The org_id, slug, users, teams, groups, memories, wiki, and configuration
    are restored from the bundle's manifest. Encrypted bundles require the
    `passcode` that produced them.

    Refuses by default if an org with the same id or slug already exists on
    this platform — use the per-org `POST /api/platform/orgs/{id}/restore`
    endpoint to overwrite an existing org.  Set `overwrite_existing=true` to
    bypass this check and merge into the existing org.
    """
    _require_multi_org()

    tmp_path = Path(f"/tmp/org-import-{secrets.token_hex(8)}")
    try:
        try:
            contents = await file.read()
            tmp_path.write_bytes(contents)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to receive upload: {exc}")

        # Peek at the manifest before invoking the restore, so we can refuse
        # cleanly with a 409 instead of silently merging into an existing org.
        try:
            manifest = _read_backup_manifest(tmp_path, passcode)
        except BackupNotConfigured as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except backup_crypto.BackupCryptoError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail=f"Not a valid org backup bundle: {exc}")

        src_org_id = manifest.get("org_id")
        src_slug   = manifest.get("org_slug")
        if not src_org_id or not src_slug:
            raise HTTPException(status_code=400,
                                detail="Manifest is missing org_id or org_slug")

        existing = session.query(Tenant).filter(
            (Tenant.id == src_org_id) | (Tenant.slug == src_slug)
        ).first()
        if existing and not overwrite_existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An org already exists on this platform with id='{src_org_id}' "
                    f"or slug='{src_slug}' (existing org: '{existing.slug}'). "
                    "Use the per-org Restore action to overwrite it, or retry "
                    "with overwrite_existing=true."
                ),
            )

        try:
            result = _restore_org_snapshot(tmp_path, None, session, passcode=passcode)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Import failed: {exc}")
        return result
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ── Supervised org-restore jobs ──────────────────────────────────────────────
# The job runners live in org_backup.py; see restore_jobs.py for the job model.

@platform_router.get("/api/platform/restore-jobs")
def list_org_restore_jobs(
    org_id: Optional[str] = Query(None),
    active: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    _claims: dict = Depends(require_platform_admin),
):
    """Org restore jobs, newest first.

    This is how a browser that refreshed mid-restore finds its way back: the
    job id it was handed is gone, but the org it was restoring is not.
    """
    _require_multi_org()
    return {"jobs": restore_jobs.registry.list(org_id=org_id, limit=limit,
                                               active_only=active)}


@platform_router.get("/api/platform/restore-jobs/{job_id}")
def get_org_restore_job(job_id: str, _claims: dict = Depends(require_platform_admin)):
    """Poll one org restore. `status` is staged|queued|running|paused|
    cancelling|cancelled|done|error, alongside phase, percent and ETA."""
    _require_multi_org()
    job = restore_jobs.registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown restore job")
    return job


@platform_router.get("/api/platform/orgs/{org_id}/pending-embeddings")
def get_org_pending_embeddings(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """How much of this org's memory corpus is restored but not yet embedded.

    A deferred restore trades searchability for speed, and this is the debt it
    leaves. Until it reaches zero those memories are readable but invisible to
    semantic search, which is a thing an operator should be able to see rather
    than remember.
    """
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    try:
        resp = httpx.get(f"{EMBEDDER_URL}/api/memories/embed-pending",
                         params={"tenant_slug": org.slug},
                         headers=_internal_service_headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"Could not reach the embedder: {exc}")


@platform_router.post("/api/platform/orgs/{org_id}/pending-embeddings", status_code=202)
def start_org_pending_embeddings(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Embed what a deferred restore left behind. Runs in the embedder, off
    request; poll the GET. Safe to call twice — it only picks up what is still
    flagged, so a retry resumes rather than redoing."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    try:
        resp = httpx.post(f"{EMBEDDER_URL}/api/memories/embed-pending",
                          params={"tenant_slug": org.slug},
                          headers=_internal_service_headers(), timeout=30)
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"Could not reach the embedder: {exc}")


@platform_router.post("/api/platform/restore-jobs/{job_id}/pause")
def pause_org_restore_job(job_id: str, _claims: dict = Depends(require_platform_admin)):
    """Hold the restore at the next item boundary.

    Between items, not mid-item: an embedding call that is already in flight
    finishes. That is the granularity the operator wants anyway — the point
    of pausing is to stop consuming the machine, and a half-written item
    would be a worse thing to own than a one-item delay.
    """
    _require_multi_org()
    if not restore_jobs.registry.pause(job_id):
        raise HTTPException(status_code=409,
                            detail="That restore is not running on this instance.")
    return restore_jobs.registry.get(job_id)


@platform_router.post("/api/platform/restore-jobs/{job_id}/resume")
def resume_org_restore_job(job_id: str, _claims: dict = Depends(require_platform_admin)):
    """Pick up a paused restore where it stopped."""
    _require_multi_org()
    if not restore_jobs.registry.resume(job_id):
        raise HTTPException(status_code=409,
                            detail="That restore is not running on this instance.")
    return restore_jobs.registry.get(job_id)


@platform_router.post("/api/platform/restore-jobs/{job_id}/cancel")
def cancel_org_restore_job(job_id: str, _claims: dict = Depends(require_platform_admin)):
    """Stop a restore, or discard one that was previewed but never started."""
    _require_multi_org()
    if not restore_jobs.registry.cancel(job_id):
        job = restore_jobs.registry.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown restore job")
        raise HTTPException(
            status_code=409,
            detail=f"That restore is already {job['status']} — nothing to cancel.")
    return restore_jobs.registry.get(job_id)


@platform_router.get("/api/platform/orgs/{org_id}/login-debug")
def platform_org_login_debug(
    org_id: str,
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Diagnostic: the login-relevant state of an org's users + domains, to
    pinpoint why login fails after a restore. No secrets (password hashes are
    reported only as a boolean)."""
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    domains = session.query(OrgDomain).filter_by(org_id=org_id).all()
    users = session.query(UserAccount).filter_by(org_id=org_id).all()
    return {
        "org": {
            "id": org.id, "slug": org.slug, "domain": org.domain,
            "domain_verified": getattr(org, "domain_verified", None),
            "setup_complete": getattr(org, "setup_complete", None),
            "lifecycle_status": getattr(org, "lifecycle_status", None),
            "is_active": org.is_active,
        },
        "org_domains": [
            {"domain": d.domain, "verified": d.verified, "is_primary": d.is_primary}
            for d in domains
        ],
        "users": [
            {"username": u.username, "full_username": u.full_username,
             "auth_method": u.auth_method, "is_active": u.is_active,
             "has_password": bool(u.password_hash), "role": u.role,
             "invite_pending": getattr(u, "invite_pending", None)}
            for u in users
        ],
    }


async def _stage_restore_source(
    org: Tenant, backup_id: Optional[str], file: Optional[UploadFile],
    passcode: Optional[str], session: Session, *, persist_upload: bool,
) -> tuple:
    """Resolve a restore request down to (bundle_path, passcode, label, staged).

    `staged` is the path this call created and therefore owns — a stored
    backup is the org's own file and must never be deleted by a restore.
    """
    if backup_id:
        local_path = _resolve_local_org_backup(org.slug, backup_id, session)
        if local_path is None:
            raise HTTPException(status_code=404, detail="Backup file not found")
        # Stored backups: use the supplied passcode, else the configured one.
        return (local_path,
                passcode or _resolve_org_backup_passcode(org.id, session),
                backup_id, None)
    if file:
        # A previewed upload has to outlive the request that carried it, so it
        # is staged under /data rather than written to a per-request temp file.
        parent = _restore_staging_dir() if persist_upload else Path("/tmp")
        staged = parent / f"org-restore-upload-{secrets.token_hex(8)}"
        try:
            contents = await file.read()
            staged.write_bytes(contents)
        except Exception as exc:
            staged.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Failed to receive upload: {exc}")
        # Uploaded backups: only the operator-supplied passcode can decrypt them.
        return staged, passcode, (file.filename or "uploaded bundle"), staged
    raise HTTPException(status_code=422, detail="Provide either backup_id or a file upload")


@platform_router.post("/api/platform/orgs/{org_id}/restore-preview")
async def platform_preview_org_restore(
    org_id: str,
    backup_id: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    passcode: Optional[str] = Form(None),
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Analyse a bundle and show what restoring it would do — without doing it.

    Returns a job in `staged` state: the bundle is held, the passcode is held
    in memory only, and nothing happens to the org until the operator POSTs
    to .../start. Cancelling the job instead discards the staged bundle.
    """
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    bundle_path, eff_passcode, label, staged = await _stage_restore_source(
        org, backup_id, file, passcode, session, persist_upload=True)
    try:
        preview = _preview_org_bundle(bundle_path, eff_passcode, org, session)
    except BackupNotConfigured as exc:
        if staged:
            staged.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except backup_crypto.BackupCryptoError as exc:
        if staged:
            staged.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if staged:
            staged.unlink(missing_ok=True)
        raise HTTPException(status_code=400,
                            detail=f"Not a valid org backup bundle: {exc}")

    job_id = secrets.token_hex(8)
    restore_jobs.registry.create(
        job_id, org.id, org_slug=org.slug, source_label=label,
        staged_path=staged, preview=preview, passcode=eff_passcode,
        status="staged",
    )
    # `backup_id` is how the start call finds a stored backup again; the
    # staged path covers uploads.
    if staged is None:
        restore_jobs.registry.update(job_id, source_label=backup_id)
    return {"job_id": job_id, "status": "staged", "preview": preview}


@platform_router.post("/api/platform/orgs/import-preview")
async def platform_preview_org_import(
    file: UploadFile = File(...),
    passcode: Optional[str] = Form(None),
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Analyse a bundle for import — bringing an org onto this platform.

    The same staged-job flow as a per-org restore, and the same reason for it:
    an import re-embeds every memory, so it cannot run inside the request. It
    also answers the question the old flow only asked after failing, by saying
    up front whether the org already exists here and will be overwritten.
    """
    _require_multi_org()
    staged = _restore_staging_dir() / f"org-import-{secrets.token_hex(8)}"
    try:
        staged.write_bytes(await file.read())
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Failed to receive upload: {exc}")

    try:
        manifest = _read_backup_manifest(staged, passcode)
        src_id   = manifest.get("org_id")
        src_slug = manifest.get("org_slug")
        if not src_id or not src_slug:
            raise ValueError("Manifest is missing org_id or org_slug")
        existing = session.query(Tenant).filter(
            (Tenant.id == src_id) | (Tenant.slug == src_slug)).first()
        preview = _preview_org_bundle(staged, passcode, existing, session)
    except BackupNotConfigured as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except backup_crypto.BackupCryptoError as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise HTTPException(status_code=400,
                            detail=f"Not a valid org backup bundle: {exc}")

    job_id = secrets.token_hex(8)
    restore_jobs.registry.create(
        job_id, existing.id if existing else src_id,
        org_slug=src_slug, source_label=file.filename or "uploaded bundle",
        staged_path=staged, preview=preview, passcode=passcode, status="staged",
    )
    return {"job_id": job_id, "status": "staged", "preview": preview}


@platform_router.post("/api/platform/restore-jobs/{job_id}/start", status_code=202)
def start_org_restore_job(
    job_id: str,
    body: Optional[dict] = Body(None),
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Run a previewed restore. This is the click that changes data.

    The body is the plan — which areas to restore, each area's mode, and
    whether to embed memories now or later. It is validated here rather than
    at preview time because this is the call that acts on it, and it is
    recorded on the job so what was asked for outlives the browser that asked.
    """
    _require_multi_org()
    reg = restore_jobs.registry
    job = reg.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown restore job")
    if job["status"] != "staged":
        raise HTTPException(status_code=409,
                            detail=f"That restore is already {job['status']}.")

    # An import whose org does not exist here yet has no target to merge into;
    # the restore creates it from the bundle's own manifest.
    creates_org = bool((job.get("preview") or {}).get("creates_org"))
    org = session.query(Tenant).filter_by(id=job["org_id"]).first()
    if not creates_org and not org:
        raise HTTPException(status_code=404, detail="Org not found")

    try:
        plan = restore_jobs.RestorePlan.parse((body or {}).get("plan"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if creates_org and not plan.includes("identity"):
        raise HTTPException(
            status_code=422,
            detail="Creating an org means writing its users and settings, so "
                   "the identity area cannot be left out of an import.")

    staged = reg.staged_path(job_id)
    if staged is not None:
        bundle_path, cleanup = staged, True
    elif org is not None:
        bundle_path = _resolve_local_org_backup(org.slug, job["source_label"] or "", session)
        if bundle_path is None:
            raise HTTPException(status_code=404, detail="Backup file is no longer available")
        cleanup = False
    else:
        raise HTTPException(status_code=410,
                            detail="The staged bundle for this restore is gone — preview it again.")

    passcode = reg.passcode(job_id)
    if passcode is None and (job.get("preview") or {}).get("encrypted"):
        # The passcode lives in memory only, so a restart loses it while the
        # staged bundle survives. Say that, rather than failing mid-decrypt.
        raise HTTPException(
            status_code=409,
            detail="The passcode for this staged restore was lost when the "
                   "service restarted. Preview the bundle again to re-enter it.")
    reg.update(job_id, status="queued", plan=plan.to_dict())
    _start_org_restore_job(job_id, None if creates_org else org.id,
                           bundle_path, passcode, cleanup, plan=plan)
    return JSONResponse({"status": "started", "job_id": job_id,
                         "plan": plan.to_dict()}, status_code=202)


@platform_router.post("/api/platform/orgs/{org_id}/restore-from-backup")
async def platform_restore_org_from_backup(
    org_id: str,
    backup_id: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    passcode: Optional[str] = Form(None),
    _claims: dict = Depends(require_platform_admin),
    session: Session = Depends(get_db_session),
):
    """Restore an org from a backup (point-in-time recovery into a LIVE org).

    Either supply `backup_id` (a local/S3 backup already listed for this org)
    OR upload a `file` (tar.gz bundle from any source). Distinct path from the
    org-lifecycle `/restore` (un-recycle) endpoint — they previously collided
    on the same route, which shadowed this handler behind the lifecycle guard
    ("Only recycled or archived orgs can be restored") and blocked recovery
    for active orgs.

    This is the one-shot path: it starts immediately, without the preview
    step the UI uses. It is kept for CLI and automation callers, and it
    creates the same tracked job, so a scripted restore gets the same
    progress, pause and cancel as one driven from the browser.
    """
    _require_multi_org()
    org = session.query(Tenant).filter_by(id=org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    bundle_path, eff_passcode, label, staged = await _stage_restore_source(
        org, backup_id, file, passcode, session, persist_upload=False)

    # Run the (slow, re-embedding) restore off-request so it can't 504 at the
    # proxy. The UI polls /api/platform/restore-jobs/{job_id}.
    job_id = secrets.token_hex(8)
    restore_jobs.registry.create(job_id, org.id, org_slug=org.slug,
                                 source_label=label, status="queued")
    _start_org_restore_job(job_id, org.id, bundle_path, eff_passcode,
                           staged is not None)
    return JSONResponse({"status": "started", "job_id": job_id}, status_code=202)
