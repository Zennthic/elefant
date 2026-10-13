"""Card people: reporter and participants as resolved references.

A card's people are RESOLVED accounts, not free text. memory-api stores them
and owns two things this module covers: the shape of what it stores, and the
fact that it never stores one it has not had confirmed by the directory —
which lives in the editor, so the store leaves the database to ask.

The two defaulting rules are here too, because they are the specification
rather than an implementation detail: the reporter defaults to whoever created
the card, and an actor who is not the reporter goes on the card as a
participant.

Pure logic plus SQL-string assertions, in the style of the other board tests.
The resolver is exercised against a stubbed editor, because its whole job is
what happens when the answer is no.
"""
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import main
from conftest import card_insert_lists


# ── reference shape ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["user:abc", "agent:abc", "USER:abc", " user:abc "])
def test_principal_references_are_recognised(value):
    assert main._is_principal_ref(value) is True


@pytest.mark.parametrize("value", ["", None, "jon", "user:", "team:abc", "user:a/b"])
def test_everything_else_is_not_a_principal_reference(value):
    assert main._is_principal_ref(value) is False


def test_participants_are_a_set_not_a_list():
    """One person named twice is one participant. Storing them twice would
    show a duplicate chip and count them twice in any future rollup."""
    assert main._dedup_refs(["user:a", "user:a", "user:b"]) == ["user:a", "user:b"]
    assert main._dedup_refs(["user:A", "user:a"]) == ["user:A"]
    assert main._dedup_refs(["", None, " user:a "]) == ["user:a"]
    assert main._dedup_refs(None) == []


# ── the write gate ─────────────────────────────────────────────────────────

def _stub_editor(monkeypatch, payload=None, status=200, boom=False):
    """Stand in for the editor's resolver. Returns the captured request body."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        if boom:
            raise RuntimeError("editor unreachable")
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(main.httpx, "post", fake_post)
    return captured


def test_resolve_people_asks_the_editor_and_returns_canonical_refs(monkeypatch):
    captured = _stub_editor(monkeypatch, {"resolved": {"jon": "user:1"}, "errors": {}})
    assert main._resolve_people("acme", ["jon"]) == {"jon": "user:1"}
    assert captured["json"] == {"tenant_slug": "acme", "resolve": ["jon"]}
    assert captured["url"].endswith("/api/internal/board/people")


def test_resolve_people_asks_once_for_the_whole_card(monkeypatch):
    """A card with a reporter and three participants is one round-trip, not
    four."""
    captured = _stub_editor(monkeypatch, {
        "resolved": {"a": "user:a", "b": "user:b", "c": "user:c"}, "errors": {}})
    main._resolve_people("acme", ["a", "b", "c", "a"])
    assert captured["json"]["resolve"] == ["a", "b", "c"]


def test_resolve_people_skips_the_call_when_there_is_nobody(monkeypatch):
    captured = _stub_editor(monkeypatch, {"resolved": {}, "errors": {}})
    assert main._resolve_people("acme", []) == {}
    assert main._resolve_people("acme", ["", None]) == {}
    assert captured == {}


def test_an_unresolvable_name_is_a_400_carrying_the_directory_message(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {}, "errors": {"ghost": "no member matches 'ghost'. Known accounts: Jon Smith."}})
    with pytest.raises(HTTPException) as exc:
        main._resolve_people("acme", ["ghost"])
    assert exc.value.status_code == 400
    # The store must not reword the directory's refusal — the useful half is
    # the list of accounts that DO exist, and a second wording would drift.
    assert "Known accounts: Jon Smith." in exc.value.detail


def test_an_unreachable_directory_refuses_the_write_rather_than_storing(monkeypatch):
    """Fail closed. A card stored with a person nothing can resolve is the
    free-text failure this field exists to remove, wearing a colon."""
    _stub_editor(monkeypatch, boom=True)
    with pytest.raises(HTTPException) as exc:
        main._resolve_people("acme", ["jon"])
    assert exc.value.status_code == 502
    assert "not written" in exc.value.detail


def test_a_silently_missing_answer_is_still_a_refusal(monkeypatch):
    """The directory answering with neither a resolution nor an error must not
    be read as consent."""
    _stub_editor(monkeypatch, {"resolved": {}, "errors": {}})
    with pytest.raises(HTTPException) as exc:
        main._resolve_people("acme", ["jon"])
    assert exc.value.status_code == 400


# ── filter values are lenient ──────────────────────────────────────────────

def test_a_reference_filter_never_leaves_the_database(monkeypatch):
    """The board UI's chips carry references and it polls every few seconds,
    so the hot path must not make a network call per poll."""
    captured = _stub_editor(monkeypatch, {"resolved": {}, "errors": {}})
    assert main._match_person("acme", "user:1") == "user:1"
    assert captured == {}


def test_a_typed_filter_is_resolved(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {"jon": "user:1"}, "errors": {}})
    assert main._match_person("acme", "jon") == "user:1"


def test_an_unresolvable_filter_matches_nothing_instead_of_erroring(monkeypatch):
    """Same call as the repo filter: a filter that finds nothing is a fine
    answer to a stale filter in a URL, where a 400 would break the board that
    merely has one."""
    _stub_editor(monkeypatch, {"resolved": {}, "errors": {"ghost": "no match"}})
    assert main._match_person("acme", "ghost") == "ghost"


def test_an_unreachable_directory_does_not_break_reading_the_board(monkeypatch):
    _stub_editor(monkeypatch, boom=True)
    assert main._match_person("acme", "jon") == "jon"


# ── defaulting rules ───────────────────────────────────────────────────────

def _req(**kw):
    return main.CardAddRequest(title="Test", **kw)


def test_reporter_defaults_to_whoever_created_the_card(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {"user:agent-1": "user:agent-1"}, "errors": {}})
    reporter, _assignee, participants = main._people_for_new_card("acme", _req(actor="user:agent-1"))
    assert reporter == "user:agent-1"
    # Nobody else is involved, so nobody is added as a participant.
    assert participants == []


def test_an_explicit_reporter_wins_over_the_actor(monkeypatch):
    """The common agent case: the agent holds the credential, the human raised
    the thing, and the card should say the human."""
    _stub_editor(monkeypatch, {"resolved": {"jon": "user:jon", "agent:bot": "agent:bot"},
                               "errors": {}})
    reporter, _assignee, participants = main._people_for_new_card(
        "acme", _req(actor="agent:bot", reporter="jon"))
    assert reporter == "user:jon"
    # …and the agent goes on the card, so it records both who wanted it and
    # who wrote it.
    assert participants == ["agent:bot"]


def test_an_actor_who_is_the_reporter_is_not_also_a_participant(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {"user:jon": "user:jon", "jon": "user:jon"},
                               "errors": {}})
    reporter, _assignee, participants = main._people_for_new_card(
        "acme", _req(actor="user:jon", reporter="jon"))
    assert reporter == "user:jon"
    assert participants == []


def test_an_actor_already_named_as_a_participant_is_not_added_twice(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {"agent:bot": "agent:bot", "jon": "user:jon"},
                               "errors": {}})
    reporter, _assignee, participants = main._people_for_new_card(
        "acme", _req(actor="agent:bot", reporter="jon", participants=["agent:bot"]))
    assert participants == ["agent:bot"]


def test_a_card_with_no_actor_and_no_reporter_names_nobody(monkeypatch):
    """An OAuth session with no user has no durable identity to attribute to.
    Naming nobody is honest; inventing one would scatter one actor across the
    board as a crowd of strangers."""
    captured = _stub_editor(monkeypatch, {"resolved": {}, "errors": {}})
    reporter, _assignee, participants = main._people_for_new_card("acme", _req())
    assert reporter == ""
    assert participants == []
    assert captured == {}


def test_participants_are_resolved_and_deduplicated(monkeypatch):
    _stub_editor(monkeypatch, {"resolved": {"jon": "user:jon", "user:jon": "user:jon",
                                            "jan": "user:jan"}, "errors": {}})
    _reporter, _assignee, participants = main._people_for_new_card(
        "acme", _req(actor="user:jon", participants=["jan", "user:jon", "jon"]))
    # jan, then jon once — named twice under two spellings, stored once.
    assert participants == ["user:jan", "user:jon"]


# ── request shape ──────────────────────────────────────────────────────────

def test_card_add_request_defaults_people_to_empty():
    req = main.CardAddRequest(title="Test")
    assert req.reporter == ""
    assert req.participants == []
    assert req.actor == ""


def test_card_update_request_people_default_to_none():
    """None leaves them alone, so editing a card's title cannot strip who
    raised it. [] and "" are deliberate clears."""
    req = main.CardUpdateRequest()
    assert req.reporter is None
    assert req.participants is None


# ── schema and SQL ─────────────────────────────────────────────────────────

def test_cards_table_gains_the_people_columns_on_startup():
    """This repo has no migration runner; the ADD COLUMN IF NOT EXISTS is it."""
    src = inspect.getsource(main._ensure_schema)
    assert "ADD COLUMN IF NOT EXISTS reporter TEXT NOT NULL DEFAULT ''" in src
    assert "ADD COLUMN IF NOT EXISTS participants TEXT[] NOT NULL DEFAULT '{}'" in src


def test_there_is_no_people_backfill():
    """`source` names a KIND of client ("ui", "claude"), not an account.
    Deriving a reporter from it would invent an identity and attribute every
    agent-written card to one fictional person."""
    src = inspect.getsource(main._ensure_schema)
    assert "_backfill_card_people" not in src
    assert not hasattr(main, "_backfill_card_people")


def test_card_add_inserts_both_people_columns():
    cols, values = card_insert_lists(inspect.getsource(main.card_add))
    assert "reporter" in cols and ":reporter" in values
    assert "participants" in cols and ":participants" in values


def test_card_add_response_carries_the_people():
    """card_add hand-builds its response dict rather than going through
    _card_dict, so it is the one place a field can go missing on create."""
    src = inspect.getsource(main.card_add)
    assert '"reporter": reporter' in src
    assert '"participants": participants' in src


def test_card_dict_serialises_the_people():
    src = inspect.getsource(main._card_dict)
    assert '"reporter":     row["reporter"] or ""' in src
    assert '"participants": list(row["participants"] or [])' in src


def test_card_update_leaves_people_alone_unless_asked():
    src = inspect.getsource(main.card_update)
    assert ("if req.reporter is not None or req.assignee is not None "
            "or req.participants is not None:") in src
    assert "sets.append(\"reporter = :reporter\")" in src
    assert "sets.append(\"assignee = :assignee\")" in src
    assert "sets.append(\"participants = :participants\")" in src


def test_card_update_resolves_a_whole_edit_before_writing_any_of_it():
    """A bad participant must fail the edit, not land the reporter and then
    refuse — an edit that half-applied would be worse to recover from than one
    that did not apply."""
    src = inspect.getsource(main.card_update)
    resolve_at = src.index("_resolve_people(")
    assert resolve_at < src.index('sets.append("reporter = :reporter")')
    assert resolve_at < src.index('sets.append("participants = :participants")')


@pytest.mark.parametrize("fn", ["card_add", "card_update"])
def test_people_are_resolved_before_the_database_connection_is_opened(fn):
    """Resolution is an HTTP call to the editor. Made INSIDE the transaction it
    would be held across the tenant's advisory lock — which _ensure_seed_columns
    takes and only commit releases — so one slow directory lookup would stall
    every board endpoint for that tenant. Creating a card must not be able to
    lock the board.
    """
    src = inspect.getsource(getattr(main, fn))
    connect_at = src.index("engine.connect()")
    resolve_at = min(src.index(name) for name in ("_resolve_people(", "_people_for_new_card(")
                     if name in src)
    assert resolve_at < connect_at, (
        f"{fn} resolves people after opening the connection, holding a "
        f"transaction open across a call to another service.")


def test_card_list_filters_on_reporter_participant_and_either():
    src = inspect.getsource(main.card_list)
    assert "lower(c.reporter) = lower(:reporter)" in src
    assert "unnest(c.participants) p " in src
    assert "lower(p) = lower(:participant)" in src
    # `person` is the one an actual question maps to: "what is Jon involved in".
    assert "lower(c.reporter) = lower(:person)" in src
    assert "lower(p) = lower(:person)" in src


def test_people_filters_go_through_the_lenient_matcher():
    src = inspect.getsource(main.card_list)
    for field in ("reporter", "participant", "person"):
        assert f'params["{field}"] = _match_person(slug, {field})' in src


# ── disaster recovery ──────────────────────────────────────────────────────

def test_bcdr_restore_carries_the_people():
    """The restore INSERT enumerates every card column by hand, so a new one
    that isn't added here is silently dropped on restore — the kind of loss
    nobody notices until they need the backup."""
    src = inspect.getsource(main._bcdr_insert_cards)
    cols, values = card_insert_lists(src)
    assert "reporter" in cols and ":reporter" in values
    assert "participants" in cols
    assert "CAST(:participants AS text[])" in values
    assert '"reporter": r.get("reporter") or ""' in src
    assert '"participants": r.get("participants") or []' in src


def test_restore_does_not_revalidate_people_against_the_directory():
    """A restore is history, not a new write. Refusing a card because the
    person who raised it has since left would edit the past to make it pass
    today's gate."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert "_resolve_people" not in src
