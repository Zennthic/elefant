"""The editor proxy carries a card's due date and its filters.

FastAPI drops a query param the route does not declare, and this proxy builds
its upstream body by hand, so a field the UI sends and the proxy forgets is
lost in SILENCE: the date picker would look as if it saved and the card would
never change. That is how `assignee` went missing on this same route, so it is
checked here alongside `due`.
"""
from routers import boards


def _capture(monkeypatch):
    seen = {}

    def fake_upstream(method, path, slug, *, params=None, json_body=None, **_):
        seen.update(method=method, path=path, params=params, json=json_body)
        return {"card": {}, "cards": []}

    monkeypatch.setattr(boards, "_board_upstream", fake_upstream)
    monkeypatch.setattr(boards, "_effective_tenant_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(boards, "_board_nudge_for", lambda *a, **k: 0)
    monkeypatch.setattr(boards, "_enrich_people", lambda data, *a, **k: data)
    return seen


def test_create_forwards_due_and_assignee(monkeypatch):
    seen = _capture(monkeypatch)
    boards.board_card_create(body={"title": "T", "due": "2026-11-27", "assignee": "jon"},
                             tenant_slug=None, claims={"uid": "u1"}, session=None)
    assert seen["json"]["due"] == "2026-11-27"
    assert seen["json"]["assignee"] == "jon"


def test_update_forwards_due_including_a_clear(monkeypatch):
    seen = _capture(monkeypatch)
    boards.board_card_update("c", body={"due": "2026-11-27"}, tenant_slug=None,
                             claims={}, session=None)
    assert seen["json"]["due"] == "2026-11-27"
    boards.board_card_update("c", body={"due": ""}, tenant_slug=None, claims={}, session=None)
    assert seen["json"]["due"] == ""


def test_update_leaves_due_and_assignee_alone_when_absent(monkeypatch):
    """None upstream means "not mentioned", so a title edit cannot clear
    either one."""
    seen = _capture(monkeypatch)
    boards.board_card_update("c", body={"title": "x"}, tenant_slug=None, claims={}, session=None)
    assert seen["json"]["due"] is None
    assert seen["json"]["assignee"] is None


def test_list_forwards_the_due_filters(monkeypatch):
    seen = _capture(monkeypatch)
    boards.board_cards_list(
        column=None, stage=None, tag=None, label=None, repo=None, reporter=None,
        assignee=None, participant=None, person=None, search=None,
        due_before="2026-11-30", due_after="2026-11-01", overdue=True,
        closed=False, archived=False, tenant_slug=None, claims={}, session=None)
    assert seen["params"]["due_before"] == "2026-11-30"
    assert seen["params"]["due_after"] == "2026-11-01"
    assert seen["params"]["overdue"] == "true"
