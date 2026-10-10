"""wiki_write and wiki_edit validate mermaid fences (card #420).

Swapping an ASCII sketch for a Mermaid diagram on an existing page goes
through wiki_edit, because diagram_create makes a new page and diagram_patch
needs a diagram already in place. That path used to save whatever it was
given, so a broken diagram was only found by a later diagram_render. These
pin the acceptance: a wiki_edit that introduces a syntax error inside a
mermaid fence is refused with a line number, and a valid one reports
`validated: true`. The fail-closed rule and DIAGRAM_ALLOW_UNVALIDATED escape
are the diagram tools' own.

The editor's preview and if_match are in
editor/tests/test_internal_wiki_edit_preview.py.
"""
import hashlib
import json

import httpx
import pytest
import respx

EDITOR = "http://test-editor:8080"
RENDERER = "http://diagram-renderer:8004"
VALIDATOR = "http://diagram-validator:8005"
_RW = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": True}

PAGE = "# Network\n\n## Topology\n\n```text\n[a]---[b]\n```\n\n## Notes\n\nkeep\n"
GOOD = "```mermaid\nflowchart LR\n  a[\"A\"] --> b[\"B\"]\n```"
BAD = "```mermaid\nflowchart LR\n  a[\"A\"] --> b[\n```"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _edit(page, body):
    """Just enough of the editor's edit modes for these tests."""
    mode, content = body["mode"], body["content"]
    if mode == "replace":
        return page.replace(body["old"], content, 1)
    if mode == "append":
        return page.rstrip("\n") + "\n\n" + content + "\n"
    if mode == "replace_section":
        head = f"## {body['heading']}\n"
        start = page.index(head) + len(head)
        end = page.find("\n## ", start)
        end = len(page) if end == -1 else end + 1
        return page[:start] + "\n" + content + "\n\n" + page[end:]
    raise AssertionError(mode)


class Editor:
    def __init__(self, mock):
        self.pages = {"AI/net.md": PAGE}
        self.edits = []
        self.writes = []
        mock.get(f"{EDITOR}/api/internal/tenant-settings").mock(
            return_value=httpx.Response(200, json={"settings": {"ai_wiki_folder": "AI"}}))
        mock.post(f"{EDITOR}/api/internal/wiki/edit/preview").mock(side_effect=self._preview)
        mock.post(f"{EDITOR}/api/internal/wiki/edit").mock(side_effect=self._commit)
        mock.post(f"{EDITOR}/api/internal/wiki/write").mock(side_effect=self._write)

    def _preview(self, request):
        body = json.loads(request.content)
        before = self.pages[body["path"]]
        after = _edit(before, body)
        return httpx.Response(200, json={"path": body["path"], "changed": after != before,
                                         "before": before, "content": after,
                                         "sha256": _sha(before)})

    def _commit(self, request):
        body = json.loads(request.content)
        before = self.pages[body["path"]]
        if "if_match" in body and _sha(before) != body["if_match"]:
            return httpx.Response(409, json={"detail": "changed since the edit was checked"})
        after = _edit(before, body)
        self.edits.append(body)
        self.pages[body["path"]] = after
        return httpx.Response(200, json={"changed": True, "path": body["path"],
                                         "bytes_before": len(before), "bytes_after": len(after)})

    def _write(self, request):
        body = json.loads(request.content)
        self.writes.append(body)
        self.pages[body["path"]] = body["content"]
        return httpx.Response(200, json={"path": body["path"], "created": True})


@pytest.fixture
def m():
    import main
    return main


@pytest.fixture
def editor():
    with respx.mock(assert_all_called=False) as mock:
        e = Editor(mock)
        e.mock = mock
        e.validate = mock.post(f"{VALIDATOR}/validate").mock(side_effect=_mermaid)
        mock.post(f"{RENDERER}/render").mock(side_effect=httpx.ConnectError("down"))
        yield e


def _mermaid(request):
    """A stand-in for Mermaid's parser: an unclosed `[` is a parse error on
    the line it is on."""
    src = json.loads(request.content)["source"]
    for n, line in enumerate(src.split("\n"), 1):
        if line.count("[") != line.count("]"):
            return httpx.Response(422, json={"ok": False, "error": {
                "message": f"Parse error on line {n}", "line": n}})
    return httpx.Response(200, json={"ok": True, "type": "flowchart-v2"})


def _fields(out):
    return out.structuredContent


def _text(out):
    return out if isinstance(out, str) else out.content[0].text


@pytest.mark.asyncio
async def test_an_edit_that_breaks_a_diagram_is_refused_with_the_page_line(m, fake_ctx, editor):
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content=BAD, ctx=fake_ctx(token_info=_RW))
    text = _text(out)
    # The fence opens on page line 5; the bad line is line 2 inside it.
    assert "opens at line 5" in text and "line 7 of the page as edited" in text
    assert "line 2 inside the fence" in text and "Nothing was saved" in text
    f = _fields(out)
    assert f["saved"] is False and f["error"]["line"] == 7 and f["error"]["block_line"] == 2
    assert out.isError is True
    assert editor.edits == [] and editor.pages["AI/net.md"] == PAGE


@pytest.mark.asyncio
async def test_a_valid_diagram_edit_reports_validated(m, fake_ctx, editor):
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content=GOOD, ctx=fake_ctx(token_info=_RW))
    assert "Edited `AI/net.md`" in _text(out) and "Validated by Mermaid" in _text(out)
    f = _fields(out)
    assert f["saved"] is True and f["validated"] is True
    assert f["diagrams"] == [{"line": 5, "validated": True}]
    assert editor.edits[-1]["if_match"] == _sha(PAGE)
    assert "flowchart LR" in editor.pages["AI/net.md"]


@pytest.mark.asyncio
async def test_a_one_line_replace_inside_a_fence_is_checked(m, fake_ctx, editor):
    """The case the edit arguments alone cannot see: no fence in `old` or
    `content`, yet the change lands inside a diagram."""
    editor.pages["AI/net.md"] = PAGE.replace("```text\n[a]---[b]\n```", GOOD)
    out = await m.wiki_edit("AI/net.md", "replace", old='b["B"]', content="b[",
                            ctx=fake_ctx(token_info=_RW))
    assert _fields(out)["saved"] is False and _fields(out)["error"]["line"] == 7
    assert editor.edits == []


@pytest.mark.asyncio
async def test_an_unchanged_diagram_elsewhere_does_not_block_an_edit(m, fake_ctx, editor):
    """A page already holding a broken diagram can still have its prose
    fixed: only the blocks the edit adds or changes are checked."""
    editor.pages["AI/net.md"] = PAGE.replace("```text\n[a]---[b]\n```", BAD)
    out = await m.wiki_edit("AI/net.md", "replace", old="keep", content="kept",
                            ctx=fake_ctx(token_info=_RW))
    assert isinstance(out, str) and "Edited" in out
    assert not editor.validate.called


@pytest.mark.asyncio
async def test_a_prose_edit_is_unchanged_plain_text(m, fake_ctx, editor):
    out = await m.wiki_edit("AI/net.md", "append", content="more",
                            ctx=fake_ctx(token_info=_RW))
    assert isinstance(out, str) and "Edited" in out


@pytest.mark.asyncio
async def test_a_page_that_moved_on_after_the_check_is_not_saved(m, fake_ctx, editor):
    orig = editor._preview

    def racing(request):
        resp = orig(request)
        editor.pages["AI/net.md"] = PAGE + "someone else\n"
        return resp
    editor.mock.post(f"{EDITOR}/api/internal/wiki/edit/preview").mock(side_effect=racing)
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content=GOOD, ctx=fake_ctx(token_info=_RW))
    assert "409" in _text(out) and editor.edits == []


@pytest.mark.asyncio
async def test_with_no_validator_a_diagram_edit_is_refused(m, fake_ctx, editor):
    editor.mock.post(f"{VALIDATOR}/validate").mock(side_effect=httpx.ConnectError("x"))
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content=GOOD, ctx=fake_ctx(token_info=_RW))
    assert "DIAGRAM_ALLOW_UNVALIDATED" in _text(out) and _fields(out)["saved"] is False
    assert editor.edits == []


@pytest.mark.asyncio
async def test_the_opt_out_saves_and_says_not_validated(m, fake_ctx, editor, monkeypatch):
    monkeypatch.setattr(m, "DIAGRAM_ALLOW_UNVALIDATED", True)
    editor.mock.post(f"{VALIDATOR}/validate").mock(side_effect=httpx.ConnectError("x"))
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content=GOOD, ctx=fake_ctx(token_info=_RW))
    assert _fields(out)["saved"] is True and _fields(out)["validated"] is False
    assert "NOT validated" in _text(out)


@pytest.mark.asyncio
async def test_an_editor_without_the_preview_checks_the_content_sent(m, fake_ctx, editor):
    editor.mock.post(f"{EDITOR}/api/internal/wiki/edit/preview").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"}))
    out = await m.wiki_edit("AI/net.md", "replace_section", heading="Topology",
                            content="intro\n" + BAD, ctx=fake_ctx(token_info=_RW))
    assert "opens at line 2 of your content" in _text(out)
    assert _fields(out)["error"]["line"] == 4 and editor.edits == []


@pytest.mark.asyncio
async def test_a_missing_page_is_still_reported_by_the_preview(m, fake_ctx, editor):
    editor.mock.post(f"{EDITOR}/api/internal/wiki/edit/preview").mock(
        return_value=httpx.Response(404, json={"detail": "No page at 'AI/x.md'."}))
    out = await m.wiki_edit("AI/x.md", "append", content="x", ctx=fake_ctx(token_info=_RW))
    assert "No page at" in out and editor.edits == []


@pytest.mark.asyncio
async def test_wiki_write_refuses_a_broken_diagram(m, fake_ctx, editor):
    out = await m.wiki_write("AI/new.md", "# New\n\n" + BAD + "\n",
                             ctx=fake_ctx(token_info=_RW))
    assert "opens at line 3 of the page" in _text(out)
    assert _fields(out)["error"]["line"] == 5 and editor.writes == []


@pytest.mark.asyncio
async def test_wiki_write_reports_validated(m, fake_ctx, editor):
    out = await m.wiki_write("AI/new.md", "# New\n\n" + GOOD + "\n\n" + GOOD + "\n",
                             ctx=fake_ctx(token_info=_RW))
    f = _fields(out)
    assert f["saved"] is True and f["validated"] is True
    assert [d["line"] for d in f["diagrams"]] == [3, 8]
    assert "2 diagrams checked" in _text(out)


@pytest.mark.asyncio
async def test_wiki_write_without_a_diagram_is_plain_text(m, fake_ctx, editor):
    out = await m.wiki_write("AI/new.md", "# New\n\nprose\n", ctx=fake_ctx(token_info=_RW))
    assert isinstance(out, str) and "Created" in out
    assert not editor.validate.called


@pytest.mark.asyncio
async def test_a_mermaid_fence_quoted_in_a_markdown_example_is_not_a_diagram(m, fake_ctx, editor):
    page = "# Howto\n\n````markdown\n" + BAD + "\n````\n"
    out = await m.wiki_write("AI/new.md", page, ctx=fake_ctx(token_info=_RW))
    assert isinstance(out, str) and "Created" in out
