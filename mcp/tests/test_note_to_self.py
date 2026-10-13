"""note_to_self: the "note to self" quick-capture card tool.

Covers the pure title/body split, the board_write gate, and that a capture
posts a card labelled `note-to-self` to memory-api.
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
       "board_read": True, "board_write": True}


# ── pure title/body split ──────────────────────────────────────────────────

def test_note_title_body_short_line_is_title_only(helpers):
    assert helpers._note_title_body("call the dentist") == ("call the dentist", "")


def test_note_title_body_multiline_keeps_lead_and_full_body(helpers):
    title, body = helpers._note_title_body("Ship the thing\nremember to bump the version")
    assert title == "Ship the thing"
    assert body == "Ship the thing\nremember to bump the version"


def test_note_title_body_long_single_line_truncates_title_keeps_body(helpers):
    note = "x " * 120  # ~240 chars, single line
    title, body = helpers._note_title_body(note)
    assert title.endswith("…")
    assert len(title) <= 121
    assert body == note.strip()


def test_note_title_body_empty(helpers):
    assert helpers._note_title_body("   ") == ("", "")


# ── tool behaviour ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_to_self_requires_board_write(helpers, fake_ctx):
    out = await helpers.note_to_self("idea", ctx=fake_ctx(token_info={"board_write": False}))
    assert "board_write" in out.lower()


@pytest.mark.asyncio
async def test_note_to_self_empty_note(helpers, fake_ctx):
    out = await helpers.note_to_self("   ", ctx=fake_ctx(token_info=_RW))
    assert "empty" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_note_to_self_posts_labelled_card(helpers, fake_ctx):
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.post("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(201, json={"card": {
            "title": "call the dentist", "slug": "call-the-dentist",
            "column": "Backlog", "stage": "spark", "source": "claude"}}))

    out = await helpers.note_to_self("call the dentist", ctx=fake_ctx(token_info=_RW))
    assert "Noted" in out
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["title"] == "call the dentist"
    assert sent["body"] == ""
    # The marker is a LABEL now — cards carry one classification field. What
    # keeps it from being lost is that the board reserves it on the write path,
    # not that it sits in a column an agent's label edit cannot reach.
    assert sent["labels"] == ["note-to-self"]
    assert "tags" not in sent
    assert sent["source"] == "claude"
