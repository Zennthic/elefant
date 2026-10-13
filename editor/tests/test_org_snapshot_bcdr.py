"""BCDR org snapshot v2 round-trip: the snapshot now captures boards + the hot
memory tier (via memory-api) and wiki history / recycle bin / tenant settings
(editor DB), and the restore re-applies them and preserves durable-memory
identity + provenance.

The editor-owned tables use the sqlite config DB. memory-api and the embedder
aren't running under the unit harness, so httpx is stubbed with a recorder.
"""
import json

import pytest


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import org_backup
    monkeypatch.setattr(org_backup, "_org_backup_dir", lambda slug: tmp_path / slug)

    calls = {"get": [], "post": []}
    export_payload = {"tables": {
        "board_columns": [{"id": "col-1", "tenant_id": "SRC", "name": "Spark",
                           "stage": "spark", "position": 1.0}],
        "cards": [{"id": "card-1", "tenant_id": "SRC", "column_id": "col-1",
                   "slug": "idea", "title": "An idea", "position": 1.0, "tags": ["note-to-self"]}],
        "memory_hot": [{"id": "hot-1", "tenant_id": "SRC", "content": "recent capture",
                        "dedup_hash": "z"}],
        "memory_revisions": [],
    }}
    mem_payload = {"memories": [{
        "id": "mem-123", "content": "a durable memory", "tags": ["owner:u1"],
        "type": "fact", "source": "claude", "created_at": "2026-05-26T12:00:00+00:00",
        "originated_session_ids": ["s1"], "compactor_score": 1.4,
    }]}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["get"].append((url, params))
        if "/internal/org-export" in url:
            return _Resp(200, export_payload)
        if "/api/memories" in url:
            return _Resp(200, mem_payload)
        return _Resp(200, {})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["post"].append((url, json))
        if "/internal/org-import" in url:
            return _Resp(200, {"restored": {"board_columns": 1, "cards": 1, "memory_hot": 1}})
        return _Resp(201, {})

    monkeypatch.setattr(org_backup.httpx, "get", fake_get)
    monkeypatch.setattr(org_backup.httpx, "post", fake_post)
    return calls


def test_org_snapshot_v2_captures_and_restores(stub, tmp_path):
    import org_backup
    from config_db import (SessionLocal, Tenant, SoftDeletedPage, WikiRevision,
                           TenantSettings, UserAccount, MfaCredential, crypto)

    # ── Seed a source org with wiki history, recycle bin, and a tenant setting ──
    s = SessionLocal()
    try:
        s.query(Tenant).filter(Tenant.id.in_(["SRC", "TGT"])).delete(synchronize_session=False)
        s.query(UserAccount).filter_by(id="user-1").delete()
        s.query(MfaCredential).filter_by(user_id="user-1").delete()
        s.add(Tenant(id="SRC", name="Src Org", slug="srcorg"))
        s.add(UserAccount(id="user-1", org_id="SRC", username="jon",
                          full_username="jon@srcorg", auth_method="local"))
        # An enrolled + enabled MFA credential whose TOTP secret is Fernet-encrypted
        # with this deployment's crypto.key.
        s.add(MfaCredential(user_id="user-1",
                            totp_secret_enc=crypto.encrypt("JBSWY3DPEHPK3PXP"),
                            is_enabled=True))
        s.add(SoftDeletedPage(tenant_id="SRC", original_path="AI/x.md", name="x", content="deleted"))
        s.add(WikiRevision(tenant_id="SRC", path="AI/x.md", content="old revision"))
        s.add(TenantSettings(tenant_slug="srcorg", board_nudge_chars=321))
        org_backup._set_platform_backup_passcode(s, "test-pass-1234")
        s.commit()
    finally:
        s.close()

    # ── Create the snapshot ──
    s = SessionLocal()
    try:
        meta = org_backup._create_org_snapshot("SRC", s)
    finally:
        s.close()
    bundle = (tmp_path / "srcorg") / meta["filename"]
    assert bundle.exists()

    # Decrypt + inspect the bundle contents.
    out = tmp_path / "extract"
    out.mkdir()
    import backup_crypto
    backup_crypto.extract_aes_zip(bundle, out, "test-pass-1234")
    manifest = json.loads((out / "manifest.json").read_text())
    db_dump = json.loads((out / "db.json").read_text())
    memapi = json.loads((out / "memapi.json").read_text())

    assert manifest["version"] == "2"
    assert manifest["card_count"] == 1
    # New editor-DB sections captured:
    assert len(db_dump["soft_deleted_pages"]) == 1
    assert len(db_dump["wiki_revisions"]) == 1
    assert db_dump["tenant_settings"][0]["board_nudge_chars"] == 321
    # memory-api Postgres data captured:
    assert memapi["cards"][0]["slug"] == "idea"
    assert memapi["memory_hot"][0]["content"] == "recent capture"
    # MFA is carried as a portable plaintext secret (key-specific ciphertext dropped)
    # so it can be re-encrypted with the restore target's crypto.key.
    mfa_dump = db_dump["mfa_credentials"]
    assert mfa_dump[0]["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert "totp_secret_enc" not in mfa_dump[0]

    # ── Wipe the org's editor-DB extras, then restore over the same org so we
    # prove the restore actually re-applies them (a real DR restores into the
    # same org id or a fresh deployment; both keep the source slug). ──
    s = SessionLocal()
    try:
        s.query(SoftDeletedPage).filter_by(tenant_id="SRC").delete()
        s.query(WikiRevision).filter_by(tenant_id="SRC").delete()
        s.query(TenantSettings).filter_by(tenant_slug="srcorg").delete()
        # Simulate the "lost MFA" post-restore state: disabled + an unusable secret.
        cred = s.query(MfaCredential).filter_by(user_id="user-1").first()
        cred.is_enabled = False
        cred.totp_secret_enc = crypto.encrypt("WRONGSECRET99999")
        s.commit()
    finally:
        s.close()
    s = SessionLocal()
    try:
        result = org_backup._restore_org_snapshot(bundle, "SRC", s, passcode="test-pass-1234")
        s.commit()
    finally:
        s.close()

    assert result["cards_restored"] == 1

    # memory-api import was called with the boards + the restored org slug.
    imports = [c for c in stub["post"] if "/internal/org-import" in c[0]]
    assert imports, "memory-api org-import was not called"
    assert imports[0][1]["tenant_slug"] == "srcorg"
    assert imports[0][1]["tables"]["cards"][0]["slug"] == "idea"

    # durable memory restored WITH its original id + created_at + provenance.
    mem_posts = [c for c in stub["post"] if c[0].endswith("/api/memories")]
    assert mem_posts, "memory restore POST was not called"
    body = mem_posts[0][1]
    assert body["id"] == "mem-123"
    assert body["created_at"] == "2026-05-26T12:00:00+00:00"
    assert body["originated_session_ids"] == ["s1"]

    # The wiped editor-DB extras came back under the org.
    s = SessionLocal()
    try:
        assert s.query(SoftDeletedPage).filter_by(tenant_id="SRC").count() == 1
        assert s.query(WikiRevision).filter_by(tenant_id="SRC").count() == 1
        ts = s.query(TenantSettings).filter_by(tenant_slug="srcorg").first()
        assert ts is not None and ts.board_nudge_chars == 321
        # MFA survived the restore: re-encrypted with this deployment's key and
        # left enabled — no forced re-enrollment.
        cred = s.query(MfaCredential).filter_by(user_id="user-1").first()
        assert cred is not None and cred.is_enabled is True
        assert crypto.decrypt(cred.totp_secret_enc) == "JBSWY3DPEHPK3PXP"
    finally:
        s.close()
