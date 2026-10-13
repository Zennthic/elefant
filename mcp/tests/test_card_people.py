"""Card people — reporter and participants — over MCP.

The board is already majority agent-written, so this is the surface where the
attribute either works or is decorative. The properties worth holding, in the
order they bite:

  1. An agent knows WHO IT IS without being told. A token linked to a person
     acts as that person; a standalone token is its own agent account. Getting
     this wrong attributes every card to the wrong identity, silently.
  2. People are VISIBLE on read, by NAME and by reference. A name is what a
     conversation uses; the reference is what the next call has to pass back.
  3. Filing a card for somebody records both of them: they reported it, the
     agent is on it.
  4. Adding one participant does not disturb the others — the same in-place
     rule as labels and wiki links.
  5. A name that resolves to nobody is refused, and reading a card never fails
     because the directory was unreachable.
"""
import json

import httpx
import pytest
import respx


@pytest.fixture
def helpers():
    import main
    return main


_RW = {"user_id": "u1", "tenant_slug": "acme", "source": "claude",
       "principal_kind": "api", "id": "tok-1",
       "board_read": True, "board_write": True}
# A routine's own credential: an ApiToken linked to nobody.
_AGENT = {"user_id": None, "id": "tok-9", "principal_kind": "api",
          "tenant_slug": "acme", "source": "claude",
          "board_read": True, "board_write": True}

_CARD = {"title": "Network discovery tool", "slug": "netdisco",
         "column": "Backlog", "stage": "spark", "source": "claude",
         "aliases": [], "body": "an idea", "participants": []}

_SETTINGS = "http://test-editor:8080/api/internal/tenant-settings"
_PEOPLE = "http://test-editor:8080/api/internal/board/people"
_CARDS = "http://test-memory-api:8002/board/cards"

_JON = {"ref": "user:jon", "kind": "user", "display": "Jon Smith",
        "handle": "jon", "active": True, "known": True}
_BOT = {"ref": "agent:bot", "kind": "agent", "display": "Dispatch routine",
        "handle": "claude_code", "active": True, "known": True}


def _card(**over):
    c = dict(_CARD)
    c.update(over)
    return c


def _mock_settings():
    respx.get(_SETTINGS).mock(return_value=httpx.Response(200, json={"settings": {}}))


def _mock_people(people=None, resolved=None, errors=None):
    return respx.post(_PEOPLE).mock(return_value=httpx.Response(200, json={
        "people": people if people is not None else {"user:jon": _JON, "agent:bot": _BOT},
        "resolved": resolved or {},
        "errors": errors or {},
    }))


# ── who the agent is ───────────────────────────────────────────────────────

def test_a_token_linked_to_a_person_acts_as_that_person(helpers):
    """The "Claude working with Jon" case: the agent holds Jon's credential,
    so Jon is who acted and the card should say Jon."""
    assert helpers._actor_ref(_RW) == "user:u1"


def test_a_standalone_token_is_its_own_agent_account(helpers):
    """A routine that finds something and files a card on its own is the
    reporter — the case that has no human at all."""
    assert helpers._actor_ref(_AGENT) == "agent:tok-9"


def test_an_oauth_token_with_no_user_has_no_durable_identity(helpers):
    """Its id comes from the token hash and changes at every login, so minting
    a reference from it would scatter one actor across the board as a crowd of
    strangers. Naming nobody is the honest answer."""
    assert helpers._actor_ref(
        {"id": "abcd1234", "principal_kind": "oauth", "user_id": None}) == ""


def test_no_token_information_names_nobody(helpers):
    assert helpers._actor_ref({}) == ""
    assert helpers._actor_ref(None) == ""


# ── read side: people are visible, by name and by reference ────────────────

def test_fmt_card_names_the_reporter_and_the_participants(helpers):
    out = helpers._fmt_card(_card(
        reporter="user:jon", participants=["agent:bot"],
        people={"user:jon": _JON, "agent:bot": _BOT}))
    assert "Reporter: Jon Smith (`user:jon`)" in out
    assert "Participants: Dispatch routine (`agent:bot`, agent)" in out


def test_fmt_card_omits_the_people_lines_when_a_card_has_none(helpers):
    out = helpers._fmt_card(_card())
    assert "Reporter:" not in out
    assert "Participants:" not in out


def test_fmt_card_falls_back_to_the_reference_when_the_lookup_failed(helpers):
    """Worse to read, never wrong. A blank chip would say the card names
    nobody, which is a different and false claim."""
    out = helpers._fmt_card(_card(reporter="user:jon", people={}))
    # Once, as the reference — printing it twice would read like two facts
    # where there is one.
    assert "Reporter: `user:jon`" in out


def test_fmt_card_marks_somebody_who_has_left(helpers):
    gone = {"ref": "user:x", "kind": "user", "display": "Former member",
            "known": False, "active": False}
    out = helpers._fmt_card(_card(reporter="user:x", people={"user:x": gone}))
    assert "no longer on this board" in out


def test_fmt_card_marks_a_deactivated_member(helpers):
    off = dict(_JON, active=False)
    out = helpers._fmt_card(_card(reporter="user:jon", people={"user:jon": off}))
    assert "inactive" in out


def test_fmt_card_list_shows_the_reporter_by_name_only(helpers):
    """Scanning a board is "whose cards are these". References in a list would
    drown it."""
    out = helpers._fmt_card_list([_card(reporter="user:jon")], {"user:jon": _JON})
    assert "Jon Smith" in out
    assert "user:jon" not in out


def test_fmt_card_list_without_people_renders_cleanly(helpers):
    out = helpers._fmt_card_list([_card()])
    assert "`netdisco` — claude" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_get_resolves_the_people_on_the_card(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(reporter="user:jon")}))
    route = _mock_people()

    out = await helpers.card_get("netdisco", ctx=fake_ctx(token_info=_RW))
    assert "Jon Smith" in out
    assert json.loads(route.calls.last.request.content)["describe"] == ["user:jon"]


@respx.mock
@pytest.mark.asyncio
async def test_a_card_still_reads_when_the_directory_is_down(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(reporter="user:jon")}))
    respx.post(_PEOPLE).mock(side_effect=httpx.ConnectError("directory down"))

    out = await helpers.card_get("netdisco", ctx=fake_ctx(token_info=_RW))
    assert "netdisco" in out
    assert "user:jon" in out


@respx.mock
@pytest.mark.asyncio
async def test_the_people_lookup_is_one_call_for_a_whole_board(helpers, fake_ctx):
    respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": [
        _card(reporter="user:jon"),
        _card(slug="other", reporter="user:jon", participants=["agent:bot"]),
    ]}))
    route = _mock_people()

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)["describe"] == ["user:jon", "agent:bot"]


@respx.mock
@pytest.mark.asyncio
async def test_a_board_with_no_people_does_not_ask(helpers, fake_ctx):
    respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": [_card()]}))
    route = _mock_people()

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    assert route.call_count == 0


# ── filtering ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_list_passes_the_people_filters_through(helpers, fake_ctx):
    route = respx.get(_CARDS).mock(
        return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(reporter="jon", participant="jan", person="bot",
                            ctx=fake_ctx(token_info=_RW))
    params = route.calls.last.request.url.params
    assert params["reporter"] == "jon"
    assert params["participant"] == "jan"
    assert params["person"] == "bot"


@respx.mock
@pytest.mark.asyncio
async def test_card_list_omits_the_people_filters_when_not_asked(helpers, fake_ctx):
    """An always-sent empty filter would be harmless today and a bug the day
    the backend starts treating "" as "cards with no reporter"."""
    route = respx.get(_CARDS).mock(
        return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    params = route.calls.last.request.url.params
    for field in ("reporter", "participant", "person"):
        assert field not in params


# ── create: filing a card for somebody records both of you ─────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_the_actor_so_the_reporter_can_default(helpers, fake_ctx):
    _mock_settings()
    _mock_people()
    route = respx.post(_CARDS).mock(return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", ctx=fake_ctx(token_info=_AGENT))
    body = json.loads(route.calls.last.request.content)
    assert body["actor"] == "agent:tok-9"
    assert body["reporter"] == ""


@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_an_explicit_reporter_and_participants(helpers, fake_ctx):
    _mock_settings()
    _mock_people()
    route = respx.post(_CARDS).mock(return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", reporter="jon",
                           participants=["jan"], ctx=fake_ctx(token_info=_AGENT))
    body = json.loads(route.calls.last.request.content)
    assert body["reporter"] == "jon"
    assert body["participants"] == ["jan"]
    # The actor rides along regardless, so the store can put the agent on the
    # card it just filed for somebody else.
    assert body["actor"] == "agent:tok-9"


@respx.mock
@pytest.mark.asyncio
async def test_note_to_self_records_who_noted_it(helpers, fake_ctx):
    """A note to self has a self."""
    _mock_settings()
    _mock_people()
    route = respx.post(_CARDS).mock(return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.note_to_self("check the fan curve", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["actor"] == "user:u1"


# ── card_reporter ──────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_reporter_sets_the_reporter(helpers, fake_ctx):
    _mock_settings()
    _mock_people()
    route = respx.patch(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(reporter="user:jon")}))

    out = await helpers.card_reporter("netdisco", "jon", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["reporter"] == "jon"
    assert "Jon Smith" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_reporter_touches_nothing_else(helpers, fake_ctx):
    """The surgical tools send ONE field. Sending the whole card would let a
    reporter change silently revert a title somebody edited in between."""
    _mock_settings()
    _mock_people()
    route = respx.patch(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(reporter="user:jon")}))

    await helpers.card_reporter("netdisco", "jon", ctx=fake_ctx(token_info=_RW))
    assert set(json.loads(route.calls.last.request.content)) == {"tenant_slug", "reporter"}


@respx.mock
@pytest.mark.asyncio
async def test_card_reporter_needs_somebody_to_name(helpers, fake_ctx):
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_reporter("netdisco", "", ctx=fake_ctx(token_info=_RW))
    assert "Nothing changed" in out
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_card_reporter_surfaces_the_stores_refusal(helpers, fake_ctx):
    respx.patch(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        400, json={"detail": "no member matches 'ghost'. Known accounts: Jon Smith."}))

    out = await helpers.card_reporter("netdisco", "ghost", ctx=fake_ctx(token_info=_RW))
    assert "Known accounts: Jon Smith." in out


@pytest.mark.asyncio
async def test_card_reporter_requires_board_write(helpers, fake_ctx):
    out = await helpers.card_reporter(
        "netdisco", "jon", ctx=fake_ctx(token_info={"board_read": True}))
    assert "board_write" in out


# ── card_participant ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_participant_adds_without_disturbing_the_others(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jan"])}))
    _mock_settings()
    _mock_people(resolved={"jon": "user:jon"})
    route = respx.patch(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jan", "user:jon"])}))

    out = await helpers.card_participant("netdisco", "jon", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["participants"] == [
        "user:jan", "user:jon"]
    assert "Added" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_participant_removes_by_name(helpers, fake_ctx):
    """The typed name is resolved BEFORE comparing. Comparing "jon" against a
    card holding `user:jon` would report nothing to remove while Jon sat there
    in plain sight."""
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jon", "user:jan"])}))
    _mock_settings()
    _mock_people(resolved={"jon": "user:jon"})
    route = respx.patch(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jan"])}))

    out = await helpers.card_participant("netdisco", "jon", remove=True,
                                         ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["participants"] == ["user:jan"]
    assert "Removed" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_participant_adding_somebody_already_on_it_is_a_no_op(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jon"])}))
    _mock_people(resolved={"jon": "user:jon"})
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_participant("netdisco", "jon", ctx=fake_ctx(token_info=_RW))
    assert "already a participant" in out
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_card_participant_removing_somebody_not_on_it_says_so(helpers, fake_ctx):
    """Rather than pretending it did something."""
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card(participants=["user:jan"])}))
    _mock_people(resolved={"jon": "user:jon"})
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_participant("netdisco", "jon", remove=True,
                                         ctx=fake_ctx(token_info=_RW))
    assert "nothing to remove" in out
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_card_participant_refuses_a_name_that_resolves_to_nobody(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(return_value=httpx.Response(
        200, json={"card": _card()}))
    _mock_people(resolved={}, errors={"ghost": "no member matches 'ghost'. Known accounts: Jon Smith."})
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_participant("netdisco", "ghost", ctx=fake_ctx(token_info=_RW))
    assert "Known accounts: Jon Smith." in out
    # Nothing was written on the way to finding that out.
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_card_participant_on_a_missing_card(helpers, fake_ctx):
    respx.get(f"{_CARDS}/nope").mock(return_value=httpx.Response(
        404, json={"detail": "No card matches 'nope'"}))

    out = await helpers.card_participant("nope", "jon", ctx=fake_ctx(token_info=_RW))
    assert "No card matches" in out


@pytest.mark.asyncio
async def test_card_participant_requires_board_write(helpers, fake_ctx):
    out = await helpers.card_participant(
        "netdisco", "jon", ctx=fake_ctx(token_info={"board_read": True}))
    assert "board_write" in out


# ── card_update ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_update_omits_people_when_the_caller_did_not_pass_them(helpers, fake_ctx):
    """Otherwise every title edit would strip who raised the card."""
    _mock_settings()
    _mock_people()
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", title="New title", ctx=fake_ctx(token_info=_RW))
    body = json.loads(route.calls.last.request.content)
    assert "reporter" not in body
    assert "participants" not in body


@respx.mock
@pytest.mark.asyncio
async def test_card_update_can_clear_the_participants_deliberately(helpers, fake_ctx):
    _mock_settings()
    _mock_people()
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", participants=[], ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["participants"] == []


# ── resolution helper ──────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_resolve_person_returns_the_reference(helpers):
    _mock_people(resolved={"jon": "user:jon"})
    assert await helpers._resolve_person("acme", "jon") == ("user:jon", None)


@respx.mock
@pytest.mark.asyncio
async def test_resolve_person_returns_the_directorys_own_wording(helpers):
    """A second phrasing invented here would drift from the store's."""
    _mock_people(resolved={}, errors={"ghost": "no member matches 'ghost'."})
    ref, why = await helpers._resolve_person("acme", "ghost")
    assert ref is None
    assert why == "no member matches 'ghost'."


@respx.mock
@pytest.mark.asyncio
async def test_resolve_person_when_the_directory_is_unreachable(helpers):
    respx.post(_PEOPLE).mock(side_effect=httpx.ConnectError("down"))
    ref, why = await helpers._resolve_person("acme", "jon")
    assert ref is None
    assert "nothing was changed" in why
