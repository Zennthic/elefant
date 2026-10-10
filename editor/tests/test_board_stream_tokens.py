"""The board stream opened with an API token (issue #150).

A service such as an automated reviewer reaches Elefant with an API token, not a browser
session, and wants to hear a card move the moment it happens. These tests drive
the real route and the real nginx gate (`/api/auth/check`) with real token rows;
only memory-api's stream is faked, and the fake records which tenant it was
asked for, which is the fact tenant isolation turns on.
"""
import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    return TestClient(main.app)


class _FakeUpstream:
    """Stands in for memory-api's /board/stream. Records the tenant asked for."""
    asked = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, params=None, headers=None):
        _FakeUpstream.asked.append(params["tenant_slug"])
        return _FakeResponse()


class _FakeResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_raw(self):
        yield b"event: hello\ndata: {}\n\n"


@pytest.fixture(autouse=True)
def fake_upstream(monkeypatch):
    _FakeUpstream.asked = []
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeUpstream)
    return _FakeUpstream


def _tenant():
    from config_db import get_db_session, Tenant
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"bst-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    db.close()
    return oid, slug


def _token(tenant_id, *, board_read=True, user_id=None):
    from config_db import get_db_session, ApiToken
    raw = "elf_" + secrets.token_urlsafe(24)
    db = next(get_db_session())
    row = ApiToken(name="agent", token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                   tenant_id=tenant_id, user_id=user_id, board_read=board_read)
    db.add(row)
    db.commit()
    tid = row.id
    db.close()
    return raw, tid


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


# ── The route ─────────────────────────────────────────────────────────────────

def test_a_board_read_token_opens_its_own_tenants_stream(client, fake_upstream):
    oid, slug = _tenant()
    raw, _ = _token(oid)
    r = client.get("/api/board/stream", headers=_bearer(raw))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: hello" in r.text
    assert fake_upstream.asked == [slug]


def test_a_token_without_board_read_is_refused_before_the_stream_opens(client, fake_upstream):
    oid, _ = _tenant()
    raw, _ = _token(oid, board_read=False)
    r = client.get("/api/board/stream", headers=_bearer(raw))
    assert r.status_code == 403
    assert fake_upstream.asked == []


def test_a_revoked_token_is_refused(client, fake_upstream):
    from config_db import get_db_session, ApiToken
    oid, _ = _tenant()
    raw, tid = _token(oid)
    assert client.get("/api/board/stream", headers=_bearer(raw)).status_code == 200
    db = next(get_db_session())
    db.delete(db.get(ApiToken, tid))
    db.commit()
    db.close()
    fake_upstream.asked = []
    r = client.get("/api/board/stream", headers=_bearer(raw))
    assert r.status_code == 401
    assert fake_upstream.asked == []


def test_an_unknown_token_is_refused(client, fake_upstream):
    r = client.get("/api/board/stream", headers=_bearer("elf_not-a-real-token"))
    assert r.status_code == 401
    assert fake_upstream.asked == []


def test_a_token_of_a_deactivated_user_is_refused(client, fake_upstream):
    from config_db import get_db_session, UserAccount
    oid, slug = _tenant()
    uid = secrets.token_hex(8)
    db = next(get_db_session())
    db.add(UserAccount(id=uid, org_id=oid, username="gone",
                       full_username=f"gone@{slug}.test", role="user",
                       is_active=False, auth_method="local"))
    db.commit()
    db.close()
    raw, _ = _token(oid, user_id=uid)
    assert client.get("/api/board/stream", headers=_bearer(raw)).status_code == 401
    assert fake_upstream.asked == []


def test_tenant_slug_cannot_widen_a_token(client, fake_upstream):
    """The tenant comes from the token. Naming another tenant is refused rather
    than quietly ignored, so a caller that thinks it is watching another board
    finds out instead of watching its own."""
    oid, slug = _tenant()
    _other_oid, other = _tenant()
    raw, _ = _token(oid)
    r = client.get("/api/board/stream", params={"tenant_slug": other}, headers=_bearer(raw))
    assert r.status_code == 403
    assert fake_upstream.asked == []
    # Repeating its own tenant is fine.
    r = client.get("/api/board/stream", params={"tenant_slug": slug}, headers=_bearer(raw))
    assert r.status_code == 200
    assert fake_upstream.asked == [slug]


def test_two_tenants_tokens_each_stream_only_their_own(client, fake_upstream):
    a_oid, a = _tenant()
    b_oid, b = _tenant()
    raw_a, _ = _token(a_oid)
    raw_b, _ = _token(b_oid)
    client.get("/api/board/stream", headers=_bearer(raw_a))
    client.get("/api/board/stream", headers=_bearer(raw_b))
    assert fake_upstream.asked == [a, b]


def test_the_session_cookie_path_still_works(client, fake_upstream):
    from config_db import get_db_session, UserAccount
    from auth import make_session_token, SESSION_COOKIE
    oid, slug = _tenant()
    uid = secrets.token_hex(8)
    db = next(get_db_session())
    db.add(UserAccount(id=uid, org_id=oid, username="dana",
                       full_username=f"dana@{slug}.test", role="user",
                       is_active=True, auth_method="local"))
    db.commit()
    cookie = make_session_token(db.get(UserAccount, uid))
    db.close()
    r = client.get("/api/board/stream", cookies={SESSION_COOKIE: cookie})
    assert r.status_code == 200
    assert fake_upstream.asked == [slug]


def test_no_credentials_still_goes_to_the_session_gate(client, fake_upstream):
    r = client.get("/api/board/stream", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401)
    assert fake_upstream.asked == []


# ── The nginx gate ────────────────────────────────────────────────────────────

def _gate(client, raw, path="/api/board/stream"):
    return client.get("/api/auth/check",
                      headers={**_bearer(raw), "X-Original-URI": path})


def test_the_gate_lets_a_board_read_token_reach_the_stream(client):
    oid, slug = _tenant()
    raw, _ = _token(oid)
    r = _gate(client, raw)
    assert r.status_code == 200
    assert r.headers["X-Auth-Org-Slug"] == slug


def test_the_gate_answers_403_for_a_token_without_board_read(client):
    oid, _ = _tenant()
    raw, _ = _token(oid, board_read=False)
    assert _gate(client, raw).status_code == 403


def test_the_gate_answers_401_for_an_unknown_token(client):
    assert _gate(client, "elf_nope").status_code == 401


def test_the_gate_still_refuses_a_board_token_everywhere_else(client):
    """Only the stream opens to a token. The rest of the board API, where card
    content lives, stays behind the session cookie."""
    oid, _ = _tenant()
    raw, _ = _token(oid)
    assert _gate(client, raw, "/api/board/cards").status_code == 401
    assert _gate(client, raw, "/api/board/stream/../cards").status_code == 401
