"""Wiki page → board card: the reverse of a card's wiki thread.

A card can point at a wiki page two different ways — an explicit `wiki_links`
entry, or by convention (the page lives under <AI folder>/projects/<slug>/).
A reader of the page shouldn't have to know which was used, so this endpoint
resolves both and returns one list. It also must never be able to break a page
render: a board that's down yields no chip, not a 500.
"""
import pytest
from fastapi import HTTPException

import main
from served_routes import served_paths
from routers import boards
import internal_service


@pytest.fixture
def upstream(monkeypatch):
    """Record calls to memory-api and script their replies by path."""
    calls = []
    replies = {}

    def _fake(method, path, slug, *, params=None, json_body=None, timeout=15):
        calls.append((method, path, slug, params))
        r = replies.get(path)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        return r

    monkeypatch.setattr(boards, "_board_upstream", _fake)
    monkeypatch.setattr(boards, "_effective_tenant_slug", lambda c, t, s: "acme")
    monkeypatch.setattr(boards, "_resolve_ai_wiki_folder", lambda s, slug: "AI")
    return calls, replies


def _get(path):
    return boards.board_wiki_page_cards(path=path, tenant_slug=None, claims={}, session=None)


_NETDISCO = {"slug": "netdisco", "title": "Netdisco rollout", "column": "Building",
             "stage": "building", "color": "#6366F1", "archived": False}


def test_derived_page_finds_its_card(upstream):
    """A page under AI/projects/<slug>/ belongs to that card's thread — the
    folder name IS the reference, and nothing is stored on either side."""
    calls, replies = upstream
    replies["/board/cards/netdisco/status"] = dict(_NETDISCO)
    replies["/board/wiki-page-cards"] = {"cards": []}
    cards = _get("AI/projects/netdisco/notes.md")["cards"]
    assert [c["slug"] for c in cards] == ["netdisco"]
    assert cards[0]["via"] == "derived"
    assert cards[0]["column"] == "Building"


def test_derived_lookup_goes_through_aliases(upstream):
    """The folder keeps the card's OLD slug after a rename; memory-api resolves
    aliases, so the editor just asks for the folder name verbatim."""
    calls, replies = upstream
    replies["/board/cards/old-name/status"] = {**_NETDISCO, "slug": "netdisco"}
    replies["/board/wiki-page-cards"] = {"cards": []}
    cards = _get("AI/projects/old-name/index.md")["cards"]
    assert [c["slug"] for c in cards] == ["netdisco"]     # answers under the CURRENT slug
    assert ("GET", "/board/cards/old-name/status", "acme", None) in calls


def test_explicit_wiki_link_finds_its_card(upstream):
    """A page anywhere in the wiki, named by a card's wiki_links."""
    calls, replies = upstream
    replies["/board/wiki-page-cards"] = {"cards": [dict(_NETDISCO)]}
    cards = _get("runbooks/netdisco.md")["cards"]
    assert [c["slug"] for c in cards] == ["netdisco"]
    assert cards[0]["via"] == "linked"
    # Outside AI/projects/, there's no folder to derive from — don't guess.
    assert not any(p.endswith("/status") for _, p, _, _ in calls)


def test_same_card_both_ways_appears_once(upstream):
    """A card that both owns the folder AND links the page is one referrer, and
    the derived reading wins — that's the thread the page actually sits in."""
    calls, replies = upstream
    replies["/board/cards/netdisco/status"] = dict(_NETDISCO)
    replies["/board/wiki-page-cards"] = {"cards": [dict(_NETDISCO)]}
    cards = _get("AI/projects/netdisco/index.md")["cards"]
    assert len(cards) == 1
    assert cards[0]["via"] == "derived"


def test_derived_first_then_other_referrers(upstream):
    calls, replies = upstream
    replies["/board/cards/netdisco/status"] = dict(_NETDISCO)
    replies["/board/wiki-page-cards"] = {"cards": [
        {**_NETDISCO, "slug": "audit", "title": "Q3 audit"},
    ]}
    cards = _get("AI/projects/netdisco/index.md")["cards"]
    assert [c["slug"] for c in cards] == ["netdisco", "audit"]


def test_page_sitting_directly_in_projects_names_no_card(upstream):
    """AI/projects/README.md is in the projects folder, not in a card's folder."""
    calls, replies = upstream
    replies["/board/wiki-page-cards"] = {"cards": []}
    assert _get("AI/projects/README.md")["cards"] == []
    assert not any(p.endswith("/status") for _, p, _, _ in calls)


def test_unknown_card_folder_is_not_an_error(upstream):
    """Someone can make AI/projects/whatever/ by hand. No card, no chip."""
    calls, replies = upstream
    replies["/board/wiki-page-cards"] = {"cards": []}
    assert _get("AI/projects/whatever/page.md")["cards"] == []


def test_board_outage_yields_no_chip_not_a_500(upstream):
    """The chip is decoration on someone's wiki page; it must never take the
    page down with it."""
    calls, replies = upstream
    replies["/board/cards/netdisco/status"] = dict(_NETDISCO)
    replies["/board/wiki-page-cards"] = HTTPException(status_code=502, detail="down")
    cards = _get("AI/projects/netdisco/index.md")["cards"]
    assert [c["slug"] for c in cards] == ["netdisco"]     # derived survives


def test_traversal_path_is_rejected_without_calling_upstream(upstream):
    """`..` and empty paths never reach the board at all."""
    calls, replies = upstream
    for bad in ("../../etc/passwd", "", "   ", "AI/../../x", "a\x00b"):
        assert _get(bad)["cards"] == []
    assert not calls


def test_leading_slash_is_normalised_not_rejected(upstream):
    """Paths here are tenant-relative wiki keys — a leading slash is spelling,
    not traversal, and normalising it matches every other path in the editor
    (and how _clean_links stores them on the card)."""
    calls, replies = upstream
    replies["/board/wiki-page-cards"] = {"cards": []}
    _get("/runbooks/netdisco.md")
    assert calls[-1][3] == {"path": "runbooks/netdisco.md"}


def test_ai_folder_override_is_honoured(monkeypatch, upstream):
    """The projects prefix follows the TENANT'S AI folder, not a constant."""
    calls, replies = upstream
    monkeypatch.setattr(boards, "_resolve_ai_wiki_folder", lambda s, slug: "Agents")
    replies["/board/cards/netdisco/status"] = dict(_NETDISCO)
    replies["/board/wiki-page-cards"] = {"cards": []}
    assert _get("Agents/projects/netdisco/index.md")["cards"][0]["slug"] == "netdisco"
    assert _get("AI/projects/netdisco/index.md")["cards"] == []


def test_folder_segment_is_url_quoted(upstream):
    """A folder name is arbitrary text off a wiki path. An unescaped '?' would
    turn the rest of the upstream URL into a query string and silently point
    the call at a different endpoint."""
    calls, replies = upstream
    replies["/board/wiki-page-cards"] = {"cards": []}
    _get("AI/projects/what? now/index.md")
    assert ("GET", "/board/cards/what%3F%20now/status", "acme", None) in calls


def test_route_registered():
    paths = served_paths(main.app)
    assert "/api/board/wiki-page-cards" in paths


# ── the peer-service twin (MCP get_wiki_page) ──────────────────────────────
# Same lookup, reached with the internal-service token instead of a session.
# It reveals which cards a tenant has, so it must be gated, and it must never
# be able to fail an agent's page read.

@pytest.fixture
def iclient():
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _tok():
    return internal_service.INTERNAL_SERVICE_TOKEN


_IBODY = {"tenant_slug": "acme", "path": "AI/projects/netdisco/index.md"}


def test_internal_requires_service_token(iclient):
    assert iclient.post("/api/internal/board/page-cards", json=_IBODY).status_code == 403


def test_internal_rejects_wrong_token(iclient):
    r = iclient.post("/api/internal/board/page-cards", json=_IBODY,
                     headers={"X-Internal-Service": "not-the-token"})
    assert r.status_code == 403


def test_internal_requires_tenant_and_path(iclient):
    for body in ({"tenant_slug": "acme"}, {"path": "a.md"}, {}):
        r = iclient.post("/api/internal/board/page-cards", json=body,
                         headers={"X-Internal-Service": _tok()})
        assert r.status_code == 400


def test_internal_returns_a_list_for_a_valid_call(iclient):
    r = iclient.post("/api/internal/board/page-cards", json=_IBODY,
                     headers={"X-Internal-Service": _tok()})
    assert r.status_code == 200
    assert isinstance(r.json()["cards"], list)


def test_internal_and_session_routes_share_one_lookup():
    """Two doors, one answer — the browser chip and the agent's back-link must
    never disagree about which card owns a page."""
    import inspect
    for fn in (boards.board_wiki_page_cards, boards.internal_board_page_cards):
        assert "_cards_referencing_page" in inspect.getsource(fn)
