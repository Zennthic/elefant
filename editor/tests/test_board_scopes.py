"""Boards: board_read/board_write token scopes and the board_nudge_chars
tenant setting are wired through the token models, the OAuth validate path,
and the settings-patch validator."""
import pytest
from fastapi import HTTPException

import main
from served_routes import served_paths
from routers import boards, org_admin, tokens
import config_db


def test_token_create_defaults_board_read_on_write_off():
    # Same posture as memory/wiki: read on, write off (least privilege).
    req = tokens.TokenCreateRequest(name="agent")
    assert req.board_read is True
    assert req.board_write is False


def test_api_token_to_dict_carries_board_scopes():
    t = config_db.ApiToken(name="agent", token_hash="h" * 64,
                           board_read=True, board_write=True)
    d = t.to_dict()
    assert d["board_read"] is True
    assert d["board_write"] is True


def test_oauth_token_board_scopes_mirror_wiki():
    # Over an OAuth session, boards ride along with the wiki grant.
    tok = config_db.OAuthAccessToken(
        token_hash="a" * 64, client_id="c", client_name="claude",
        wiki_read=True, wiki_write=False,
    )
    d = tok.to_validate_dict()
    assert d["board_read"] == tok.wiki_read
    assert d["board_write"] == tok.wiki_write
    assert d["board_read"] is True and d["board_write"] is False


def test_tenant_settings_dicts_include_board_nudge():
    assert "board_nudge_chars" in config_db.TenantSettings.empty_dict()
    assert "board_nudge_chars" in config_db.TenantSettings.SETTABLE
    ts = config_db.TenantSettings(tenant_slug="default", board_nudge_chars=120)
    assert ts.to_dict()["board_nudge_chars"] == 120


def test_board_proxy_routes_registered_and_member_dep_exists():
    import auth
    # Boards are member-tier: the any-authenticated dependency must exist.
    assert callable(auth.require_authenticated)
    paths = served_paths(main.app)
    for p in ("/api/board/columns", "/api/board/cards",
              "/api/board/cards/{card_slug}/move", "/api/board/columns/{column_id}"):
        assert p in paths, f"missing board route {p}"


def test_card_wiki_slugs_sanitizes():
    # slug + aliases → clean single-segment slugs, deduped, path-unsafe dropped.
    assert boards._card_wiki_slugs("Foo", ["foo", "bar", "", "a/b", "..", "x\x00y"]) == ["foo", "bar"]
    assert boards._card_wiki_slugs("only", None) == ["only"]


def test_board_wiki_routes_registered():
    paths = served_paths(main.app)
    assert "/api/board/cards/{card_slug}/wiki" in paths
    assert "/api/board/cards/{card_slug}/unarchive" in paths


def test_board_upstream_drops_none_params(monkeypatch):
    # Regression: httpx serialises None params as empty query values (archived=),
    # which memory-api's `archived: bool` 422s on. None params must be dropped.
    captured = {}

    class _FakeResp:
        status_code = 200
        content = b'{"ok": true}'

        def json(self):
            return {"ok": True}

    def _fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        captured["params"] = params
        return _FakeResp()

    monkeypatch.setattr(main.httpx, "request", _fake_request)
    boards._board_upstream("GET", "/board/cards", "zennthic",
                         params={"column": None, "stage": None, "archived": None})
    assert "archived" not in captured["params"]
    assert "column" not in captured["params"]
    assert captured["params"]["tenant_slug"] == "zennthic"


def test_coerce_tenant_settings_board_nudge_chars():
    assert org_admin._coerce_tenant_settings({"board_nudge_chars": 500}) == {"board_nudge_chars": 500}
    # None clears the override; a float coerces to int.
    assert org_admin._coerce_tenant_settings({"board_nudge_chars": None}) == {"board_nudge_chars": None}
    assert org_admin._coerce_tenant_settings({"board_nudge_chars": 300.0}) == {"board_nudge_chars": 300}
    for bad in (-5, "lots", True):
        with pytest.raises(HTTPException):
            org_admin._coerce_tenant_settings({"board_nudge_chars": bad})
