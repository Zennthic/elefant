"""wiki_edit / wiki_move / wiki_delete — the agent-facing surface.

The card that asked for these named the reason: wiki_write replaces a page
wholesale, so an agent changing one line of a long page regenerated the whole
document and quietly dropped content; and with no move or delete, a hierarchy
was immutable once created and every reorganisation left strays.

The editor holds the security boundary — these cover what the MCP layer owns:
the scope check, path handling, what gets sent upstream, and whether the reply
tells the agent something it can act on.
"""
import json

import httpx
import pytest
import respx

EDITOR = "http://test-editor:8080"
_RW = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": True}
_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": False}


@pytest.fixture
def helpers():
    import main
    return main


@pytest.fixture
def folder():
    """Every tool asks the editor for the tenant's AI folder first."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EDITOR}/api/internal/tenant-settings").mock(
            return_value=httpx.Response(200, json={"settings": {"ai_wiki_folder": "AI"}}))
        # An editor without the edit preview: wiki_edit goes straight to /edit.
        # The preview path itself is in test_page_diagram_validation.py.
        mock.post(f"{EDITOR}/api/internal/wiki/edit/preview").mock(
            return_value=httpx.Response(404, json={"detail": "Not Found"}))
        yield mock


def _sent(route):
    return json.loads(route.calls.last.request.content)


# ── scope ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_three_require_wiki_write(helpers, fake_ctx):
    ctx = fake_ctx(token_info=_RO)
    for out in (
        await helpers.wiki_edit("AI/a.md", "append", content="x", ctx=ctx),
        await helpers.wiki_move("AI/a.md", "AI/b.md", ctx=ctx),
        await helpers.wiki_delete("AI/a.md", ctx=ctx),
    ):
        assert "wiki_write" in out.lower()


# ── path handling ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_bare_filename_is_placed_under_the_ai_folder(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/notes.md",
                                               "bytes_before": 10, "bytes_after": 14}))
    await helpers.wiki_edit("notes.md", "append", content="x",
                            ctx=fake_ctx(token_info=_RW))
    assert _sent(route)["path"] == "AI/notes.md"


@pytest.mark.asyncio
async def test_traversal_is_refused_without_calling_the_editor(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/delete").mock(
        return_value=httpx.Response(200, json={}))
    out = await helpers.wiki_delete("AI/../../etc/passwd", ctx=fake_ctx(token_info=_RW))
    assert "traversal" in out.lower()
    assert not route.called


@pytest.mark.asyncio
async def test_move_normalises_the_source_but_not_the_destination(helpers, fake_ctx, folder):
    """The source is still placed helpfully; the destination is not.

    Auto-placing a destination is what turned "move this out of AI/" into "move
    it somewhere else inside AI/" and reported success.
    """
    route = folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "page", "from": "AI/a.md",
                                               "to": "AI/b.md", "moved": 1}))
    await helpers.wiki_move("a.md", "AI/b.md", ctx=fake_ctx(token_info=_RW))
    body = _sent(route)
    assert body["from_path"] == "AI/a.md" and body["to_path"] == "AI/b.md"


@pytest.mark.asyncio
async def test_a_move_wholly_inside_the_ai_folder_is_unaffected(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "folder", "from": "AI/projects/old",
                                               "to": "AI/archive/old", "moved": 2}))
    out = await helpers.wiki_move("AI/projects/old", "AI/archive/old",
                                  ctx=fake_ctx(token_info=_RW))
    assert _sent(route)["to_path"] == "AI/archive/old"
    assert "2 page(s)" in out


# ── wiki_edit ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edit_forwards_every_mode_argument(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 100, "bytes_after": 90}))
    await helpers.wiki_edit("AI/a.md", "replace_section", content="new body",
                            heading="Rollback", old="", ctx=fake_ctx(token_info=_RW))
    body = _sent(route)
    assert body["mode"] == "replace_section"
    assert body["heading"] == "Rollback"
    assert body["content"] == "new body"


@pytest.mark.asyncio
async def test_edit_reports_the_size_delta(helpers, fake_ctx, folder):
    """A shrinking page is the signal that an edit removed more than intended."""
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 500, "bytes_after": 480}))
    out = await helpers.wiki_edit("AI/a.md", "replace", old="a", content="b",
                                  ctx=fake_ctx(token_info=_RW))
    assert "-20 bytes" in out


@pytest.mark.asyncio
async def test_edit_says_so_when_nothing_changed(helpers, fake_ctx, folder):
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": False, "path": "AI/a.md"}))
    out = await helpers.wiki_edit("AI/a.md", "append", content="x",
                                  ctx=fake_ctx(token_info=_RW))
    assert "no change" in out.lower()


@pytest.mark.asyncio
async def test_edit_relays_the_editors_reason_for_refusing(helpers, fake_ctx, folder):
    """An ambiguous heading is something the agent can fix — but only if it's
    told which one and why."""
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(409, json={
            "detail": "Heading 'Setup' appears 2 times — it must be unique"}))
    out = await helpers.wiki_edit("AI/a.md", "replace_section", heading="Setup",
                                  content="x", ctx=fake_ctx(token_info=_RW))
    assert "appears 2 times" in out


# ── wiki_move ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_move_reports_a_folder_move_by_page_count(helpers, fake_ctx, folder):
    folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "folder", "from": "AI/x",
                                               "to": "AI/y", "moved": 7}))
    out = await helpers.wiki_move("AI/x", "AI/y", ctx=fake_ctx(token_info=_RW))
    assert "7 page(s)" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("dest,what", [
    ("standards/x.md", "a page into a real folder"),
    ("promoted-x.md", "a bare filename"),
    ("standards/scratch-boundary", "a folder into a real folder"),
])
async def test_a_destination_outside_the_ai_folder_is_refused_and_nothing_moves(
        helpers, fake_ctx, folder, dest, what):
    """The defect: these were silently rewritten to a path inside AI/ and
    reported as success, so the request, the effect and the report diverged.

    The refusal happens HERE, before the editor is called at all — which is
    also how "nothing was moved" is guaranteed rather than hoped for. A folder
    move that got halfway before hitting the boundary would be worse than
    either outcome.
    """
    route = folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "page", "moved": 1}))
    out = await helpers.wiki_move("AI/scratch/x.md", dest, ctx=fake_ctx(token_info=_RW))

    assert not route.called, f"{what}: the move was attempted rather than refused"
    assert "✅" not in out, f"{what}: a refusal reported as success"
    # It has to read as a DECISION rather than a malfunction: name the boundary
    # and say why.
    assert "Refused" in out
    assert "AI" in out and "promoting" in out
    assert "Nothing was moved" in out


@pytest.mark.asyncio
async def test_a_bare_destination_says_what_to_pass_instead(helpers, fake_ctx, folder):
    """On a write a bare name is obvious shorthand; on a move, where a full
    source path was just given, it is ambiguous. Refusing it is only helpful if
    the error says what the caller should have written."""
    folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "page", "moved": 1}))
    out = await helpers.wiki_move("AI/scratch/x.md", "promoted-x.md",
                                  ctx=fake_ctx(token_info=_RW))
    assert "AI/promoted-x.md" in out


@pytest.mark.asyncio
async def test_path_traversal_in_a_destination_is_still_refused(helpers, fake_ctx, folder):
    """This already worked, and is the refusal shape the boundary deny matches."""
    route = folder.post(f"{EDITOR}/api/internal/wiki/move").mock(
        return_value=httpx.Response(200, json={"kind": "page", "moved": 1}))
    out = await helpers.wiki_move("AI/x.md", "../promoted-x.md", ctx=fake_ctx(token_info=_RW))
    assert not route.called
    assert "traversal" in out.lower()


# ── wiki_delete ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_defaults_to_non_recursive(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/delete").mock(
        return_value=httpx.Response(200, json={"kind": "page", "path": "AI/a.md",
                                               "recoverable": True}))
    await helpers.wiki_delete("AI/a.md", ctx=fake_ctx(token_info=_RW))
    assert _sent(route)["recursive"] is False


@pytest.mark.asyncio
async def test_delete_mentions_the_recycle_bin(helpers, fake_ctx, folder):
    """The agent should know this is recoverable — and so should the human it
    is reporting to."""
    folder.post(f"{EDITOR}/api/internal/wiki/delete").mock(
        return_value=httpx.Response(200, json={"kind": "page", "path": "AI/a.md",
                                               "recoverable": True}))
    out = await helpers.wiki_delete("AI/a.md", ctx=fake_ctx(token_info=_RW))
    assert "recycle bin" in out.lower()


@pytest.mark.asyncio
async def test_deleting_a_folder_without_recursive_relays_the_refusal(helpers, fake_ctx, folder):
    folder.post(f"{EDITOR}/api/internal/wiki/delete").mock(
        return_value=httpx.Response(409, json={
            "detail": "'AI/notes' is a folder holding 4 item(s). Pass recursive=true"}))
    out = await helpers.wiki_delete("AI/notes", ctx=fake_ctx(token_info=_RW))
    assert "recursive=true" in out


# ── discoverability ────────────────────────────────────────────────────────

def test_instructions_steer_edits_away_from_whole_page_rewrites(helpers):
    """The gap this closes was never that the capability was missing — it was
    that an agent had no way to know about it."""
    text = helpers.mcp.instructions
    assert "wiki_edit" in text
    assert "wiki_move" in text and "wiki_delete" in text
    assert "silently lost" in text


def test_the_tools_tell_an_agent_when_not_to_use_wiki_write(helpers):
    doc = helpers.wiki_edit.__doc__ or ""
    assert "Prefer this over `wiki_write`" in doc


def test_move_docstring_states_the_promotion_boundary(helpers):
    doc = (helpers.wiki_move.__doc__ or "").lower()
    # The docstring has to say REFUSED rather than merely "must be inside":
    # an agent reading it should know a destination outside AI/ is an error it
    # will be told about, not a path that quietly becomes something else.
    assert "refused" in doc
    assert "promoting" in doc
    # …and that a bare name is not shorthand here, unlike on wiki_write.
    assert "bare name" in doc


# ── empty content must not read as "replace with nothing" ──────────────────
# Field report: `replace` with the replacement missing deleted the anchor and
# reported success with a negative byte delta. The editor now refuses it; these
# cover the agent-facing half — that the refusal reaches the agent intelligibly,
# and that a big loss is called out rather than tucked into a ✅ line.

@respx.mock
@pytest.mark.asyncio
async def test_the_empty_content_refusal_reaches_the_agent(helpers, fake_ctx, folder):
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(422, json={
            "detail": "mode='replace' needs non-empty `content` — the text to "
                      "put there. Empty content would have deleted the snippet "
                      "in `old` instead. If removal IS what you want, say so: "
                      "mode='delete' with `old` set to the exact text to remove."}))
    out = await helpers.wiki_edit("AI/a.md", "replace", old="## Heading",
                                  content="", ctx=fake_ctx(token_info=_RW))
    assert "mode='delete'" in out, "the agent needs the deliberate alternative"
    assert "non-empty" in out


@respx.mock
@pytest.mark.asyncio
async def test_delete_mode_is_forwarded(helpers, fake_ctx, folder):
    route = folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 500, "bytes_after": 480}))
    await helpers.wiki_edit("AI/a.md", "delete", old="drop me",
                            ctx=fake_ctx(token_info=_RW))
    body = _sent(route)
    assert body["mode"] == "delete" and body["old"] == "drop me"


@respx.mock
@pytest.mark.asyncio
async def test_a_large_loss_is_flagged_with_the_way_back(helpers, fake_ctx, folder):
    """A ✅ quoting only a negative delta is what let a page be emptied an edit
    at a time. Say how much of it went, and how to undo it."""
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 4000, "bytes_after": 200}))
    out = await helpers.wiki_edit("AI/a.md", "delete", old="most of it",
                                  ctx=fake_ctx(token_info=_RW))
    assert "⚠️" in out
    assert "3800 of 4000" in out
    assert "wiki_revert" in out


@respx.mock
@pytest.mark.asyncio
async def test_an_ordinary_edit_is_not_nagged_about(helpers, fake_ctx, folder):
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 4000, "bytes_after": 3900}))
    out = await helpers.wiki_edit("AI/a.md", "replace", old="a", content="b",
                                  ctx=fake_ctx(token_info=_RW))
    assert "⚠️" not in out


@respx.mock
@pytest.mark.asyncio
async def test_the_result_reports_the_size_it_ended_at(helpers, fake_ctx, folder):
    """A delta alone doesn't say whether the page still has anything in it."""
    folder.post(f"{EDITOR}/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/a.md",
                                               "bytes_before": 500, "bytes_after": 520}))
    out = await helpers.wiki_edit("AI/a.md", "append", content="more",
                                  ctx=fake_ctx(token_info=_RW))
    assert "now 520" in out


def test_the_tool_teaches_that_content_is_required(helpers):
    doc = helpers.wiki_edit.__doc__ or ""
    assert "delete" in doc
    assert "required for every mode except" in doc


def test_instructions_warn_against_empty_content(helpers):
    text = helpers.mcp.instructions
    assert "ALWAYS pass `content`" in text
    assert "mode='delete'" in text
