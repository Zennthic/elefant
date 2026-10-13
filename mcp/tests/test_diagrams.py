"""The diagram surface: Mermaid source in the wiki, patched, validated, rendered.

What these pin, in the card's own terms:
  - the source of truth is TEXT in a wiki page, so a patch to one node label is
    a one-line change to the page and everything else stays byte-identical;
  - every revision is pinned to the version it was computed from, and a stale
    one is refused rather than written over somebody else's change;
  - a source Mermaid rejects comes back with its line number and nothing is
    saved;
  - rendering is an optional sidecar, and its absence is said plainly rather
    than leaking where Elefant expected it to be;
  - validation does not depend on the renderer (card #412): the always-on
    diagram-validator checks every write, and with no validator at all a
    write is refused unless the operator opted out. Every write result
    carries `saved` and `validated` as structured fields.

The editor and both sidecars are mocked with respx; the editor's own half of
the version check is in editor/tests/test_internal_wiki_write_preconditions.py,
and Mermaid's real parser is exercised in diagram-validator/test/.
"""
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import respx

EDITOR = "http://test-editor:8080"
RENDERER = "http://diagram-renderer:8004"
VALIDATOR = "http://diagram-validator:8005"
_RW = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": True}
_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": False}
_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()

FLOW = "flowchart TD\n  A[Log in] --> B[Check password]\n  B --> C[Home]"


@pytest.fixture
def m():
    import main
    return main


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Wiki:
    """An in-memory wiki behind the editor's internal read/write endpoints,
    honouring if_match / if_none_match the way the editor does."""

    def __init__(self, mock):
        self.pages = {}
        self.writes = []
        mock.get(f"{EDITOR}/api/internal/tenant-settings").mock(
            return_value=httpx.Response(200, json={"settings": {"ai_wiki_folder": "AI"}}))
        mock.post(f"{EDITOR}/api/internal/wiki/read").mock(side_effect=self._read)
        mock.post(f"{EDITOR}/api/internal/wiki/write").mock(side_effect=self._write)
        mock.post(f"{EDITOR}/api/internal/wiki/tree").mock(side_effect=self._tree)

    def _read(self, request):
        body = json.loads(request.content)
        if body["path"] not in self.pages:
            return httpx.Response(404, json={"detail": "not found"})
        c = self.pages[body["path"]]
        return httpx.Response(200, json={"path": body["path"], "content": c, "sha256": _sha(c)})

    def _write(self, request):
        body = json.loads(request.content)
        path, exists = body["path"], body["path"] in self.pages
        if body.get("if_none_match") and exists:
            return httpx.Response(409, json={"detail": "already exists"})
        if "if_match" in body and (not exists or _sha(self.pages[path]) != body["if_match"]):
            return httpx.Response(409, json={"detail": "version conflict"})
        self.writes.append(body)
        self.pages[path] = body["content"]
        return httpx.Response(200, json={"path": path, "created": not exists,
                                         "sha256": _sha(body["content"])})

    def _tree(self, request):
        prefix = json.loads(request.content)["prefix"] + "/"
        paths = sorted(p for p in self.pages if p.startswith(prefix))
        return httpx.Response(200, json={"paths": paths, "count": len(paths)})


@pytest.fixture
def wiki():
    with respx.mock(assert_all_called=False) as mock:
        w = Wiki(mock)
        w.mock = mock
        validator_ok(w)
        yield w


def validator_ok(wiki):
    return wiki.mock.post(f"{VALIDATOR}/validate").mock(
        return_value=httpx.Response(200, json={"ok": True, "type": "flowchart-v2"}))


def validator_rejects(wiki, line, message="Parse error on line 2:\n...--> B[\n------^"):
    return wiki.mock.post(f"{VALIDATOR}/validate").mock(return_value=httpx.Response(
        422, json={"ok": False, "error": {"message": message, "line": line}}))


def validator_down(wiki):
    return wiki.mock.post(f"{VALIDATOR}/validate").mock(
        side_effect=httpx.ConnectError("Name or service not known"))


def renderer_ok(wiki):
    def _render(request):
        body = json.loads(request.content)
        if body["format"] == "none":
            return httpx.Response(200, json={"ok": True, "format": "none"})
        if body["format"] == "svg":
            return httpx.Response(200, json={"ok": True, "mime": "image/svg+xml", "data": "<svg/>"})
        return httpx.Response(200, json={"ok": True, "mime": "image/png", "data": _PNG})
    return wiki.mock.post(f"{RENDERER}/render").mock(side_effect=_render)


def renderer_rejects(wiki, line, message="Parse error on line 2:\n...--> B[\n------^"):
    return wiki.mock.post(f"{RENDERER}/render").mock(return_value=httpx.Response(
        422, json={"ok": False, "error": {"message": message, "line": line}}))


def renderer_down(wiki):
    return wiki.mock.post(f"{RENDERER}/render").mock(
        side_effect=httpx.ConnectError("Name or service not known"))


def _blocks(out):
    return getattr(out, "content", out)


def _text(out):
    if isinstance(out, str):
        return out
    return "\n".join(getattr(b, "text", "") for b in _blocks(out))


def _images(out):
    return [] if isinstance(out, str) else [b for b in _blocks(out) if getattr(b, "type", "") == "image"]


def _fields(out):
    return getattr(out, "structuredContent", None) or {}


async def _create(m, ctx, wiki, name="login-flow", source=FLOW, **kw):
    return await m.diagram_create(name, source, ctx=ctx, **kw)


def _version(wiki, path="AI/diagrams/login-flow.md"):
    return _sha(wiki.pages[path])[:12]


# ── pure helpers ───────────────────────────────────────────────────────────

def test_blocks_are_found_and_other_fences_skipped(m):
    page = ("# T\n\n````markdown\n```mermaid\nnot this\n```\n````\n\n"
            "```mermaid\nflowchart TD\n  A-->B\n```\n\ntext\n~~~mermaid\npie\n~~~\n")
    blocks = m._mermaid_blocks(page)
    assert [b["source"] for b in blocks] == ["flowchart TD\n  A-->B", "pie"]


def test_an_unclosed_fence_is_not_a_diagram(m):
    assert m._mermaid_blocks("```mermaid\nflowchart TD\n") == []


def test_replacing_a_block_leaves_every_other_byte_alone(m):
    page = "# T\n\nintro\n\n```mermaid\nflowchart TD\n  A-->B\n```\n\noutro\n"
    b = m._mermaid_blocks(page)[0]
    out = m._replace_block(page, b, "flowchart TD\n  A-->C")
    assert out == page.replace("A-->B", "A-->C")


def test_replace_must_match_exactly_once(m):
    with pytest.raises(ValueError, match="not found"):
        m._apply_diagram_edits(FLOW, [{"op": "replace", "old": "Nope", "new": "x"}])
    with pytest.raises(ValueError, match="matches 2 places"):
        m._apply_diagram_edits(FLOW, [{"op": "replace", "old": "B", "new": "x"}])


def test_line_ops_address_the_source_as_read(m):
    src = "flowchart TD\n  A-->B\n  B-->C"
    out = m._apply_diagram_edits(src, [
        {"op": "insert_after", "line": 1, "text": "  Z-->A"},
        {"op": "replace_line", "line": 3, "text": "  B-->D"},
        {"op": "insert_after", "line": 0, "text": "%% top"},
    ])
    assert out == "%% top\nflowchart TD\n  Z-->A\n  A-->B\n  B-->D"


def test_deleting_the_last_line_takes_its_newline(m):
    assert m._apply_diagram_edits("a\nb\nc", [{"op": "delete_line", "line": 3}]) == "a\nb"
    assert m._apply_diagram_edits("a\nb\nc", [{"op": "delete_line", "line": 2}]) == "a\nc"


def test_overlapping_edits_are_refused(m):
    with pytest.raises(ValueError, match="same text"):
        m._apply_diagram_edits(FLOW, [
            {"op": "replace_line", "line": 2, "text": "x"},
            {"op": "replace", "old": "A[Log in]", "new": "A[Sign in]"},
        ])


def test_a_line_outside_the_source_is_refused(m):
    with pytest.raises(ValueError, match="outside the source"):
        m._apply_diagram_edits(FLOW, [{"op": "replace_line", "line": 9, "text": "x"}])


@pytest.mark.parametrize("src,line", [
    ("flowchat TD\n  A-->B", 1),
    ("%% a comment\n\nnotadiagram", 3),
    ("flowchart TD\n```\n", 2),
    ("", 1),
])
def test_the_static_check_names_the_line(m, src, line):
    assert m._mermaid_static_check(src)["line"] == line


def test_front_matter_and_comments_are_skipped(m):
    src = "---\ntitle: Login\n---\n%% note\nflowchart LR\n  A-->B"
    assert m._mermaid_static_check(src) is None


def test_names_map_to_the_diagrams_folder(m):
    assert m._diagram_path("login-flow", "AI") == "AI/diagrams/login-flow.md"
    assert m._diagram_path("AI/projects/x/design.md", "AI") == "AI/projects/x/design.md"
    with pytest.raises(ValueError):
        m._diagram_path("bad name!", "AI")
    with pytest.raises(ValueError):
        m._diagram_path("AI/../secrets", "AI")


# ── acceptance 1: create a flowchart and get a rendered image back ─────────

@pytest.mark.asyncio
async def test_create_stores_text_and_returns_an_image(m, fake_ctx, wiki):
    renderer_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, title="Login flow")
    assert "Created diagram `login-flow`" in _text(out)
    assert "Validated by Mermaid" in _text(out)
    imgs = _images(out)
    assert len(imgs) == 1 and imgs[0].mimeType == "image/png" and imgs[0].data == _PNG
    page = wiki.pages["AI/diagrams/login-flow.md"]
    assert page == f"# Login flow\n\n```mermaid\n{FLOW}\n```\n"
    assert wiki.writes[0]["if_none_match"] is True
    assert _version(wiki) in _text(out)
    assert _fields(out) == {"saved": True, "validated": True,
                            "path": "AI/diagrams/login-flow.md", "version": _version(wiki)}
    assert out.isError is False


@pytest.mark.asyncio
async def test_create_never_overwrites(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    out = await _create(m, ctx, wiki, source="flowchart LR\n  X-->Y")
    assert "already exists" in _text(out) and "diagram_patch" in _text(out)
    assert "X-->Y" not in wiki.pages["AI/diagrams/login-flow.md"]


@pytest.mark.asyncio
async def test_create_without_a_renderer_is_still_validated(m, fake_ctx, wiki):
    renderer_down(wiki)
    route = validator_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    text = _text(out)
    assert "Created diagram" in text and "Validated by Mermaid" in text
    assert "NOT validated" not in text
    assert _fields(out)["validated"] is True and _fields(out)["saved"] is True
    assert _images(out) == [] and "renderer is not running" in text
    assert json.loads(route.calls.last.request.content) == {"source": FLOW}
    # Where Elefant looked for the renderer is internal topology.
    assert "diagram-renderer:8004" not in text and "Name or service" not in text


# ── card #412: validation without the renderer ────────────────────────────

RICH = """---
title: Request path
config:
  theme: base
---
flowchart TD
  classDef hot fill:#f96
  client@{ shape: circle, label: "Client" }
  gw@{ shape: rounded, label: "Gateway" }
  client e1@--> gw
  e1@{ animate: true }
  subgraph cluster [Cluster]
    subgraph app [App]
      api[API]:::hot
    end
  end
  gw --> api"""


@pytest.mark.asyncio
async def test_renderer_off_a_broken_source_is_refused_with_its_line(m, fake_ctx, wiki):
    """Acceptance 1: the validator's line is in the stored source, front
    matter included, and nothing is written."""
    renderer_down(wiki)
    validator_rejects(wiki, line=5, message="Parse error on line 5:\n...")
    src = "---\ntitle: T\n---\nflowchart TD\n  A --> B[\n  B --> C"
    out = await _create(m, fake_ctx(token_info=_RW), wiki, source=src)
    text = _text(out)
    assert "at line 5 of the source" in text and "Nothing was saved" in text
    assert ">5 |   A --> B[" in text
    assert _fields(out) == {"saved": False, "validated": True,
                            "error": {"message": "Parse error on line 5:\n...", "line": 5}}
    assert out.isError is True
    assert wiki.writes == []


@pytest.mark.asyncio
async def test_renderer_off_a_rich_valid_source_is_saved_and_validated(m, fake_ctx, wiki):
    """Acceptance 2, the MCP half: the validator alone is enough, and it is
    sent the source exactly as it will be stored."""
    renderer_down(wiki)
    route = validator_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, name="rich", source=RICH)
    assert _fields(out)["validated"] is True and _fields(out)["saved"] is True
    assert json.loads(route.calls.last.request.content)["source"] == RICH
    assert RICH in wiki.pages["AI/diagrams/rich.md"]


@pytest.mark.asyncio
async def test_with_no_validator_a_create_is_refused_by_default(m, fake_ctx, wiki):
    """Acceptance 3: fail closed, and say how to fix it without leaking
    where Elefant looked."""
    renderer_down(wiki)
    validator_down(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    text = _text(out)
    assert "Nothing was saved" in text
    assert "diagram-validator" in text and "DIAGRAM_ALLOW_UNVALIDATED" in text
    assert "8005" not in text and "Name or service" not in text
    assert _fields(out)["saved"] is False and _fields(out)["validated"] is False
    assert out.isError is True
    assert wiki.writes == []


@pytest.mark.asyncio
async def test_with_no_validator_a_patch_is_refused_by_default(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    before = wiki.pages["AI/diagrams/login-flow.md"]
    renderer_down(wiki)
    validator_down(wiki)
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "C[Home]", "new": "C[Dashboard]"}], ctx=ctx)
    assert "Nothing was saved" in _text(out) and _fields(out)["saved"] is False
    assert wiki.pages["AI/diagrams/login-flow.md"] == before


@pytest.mark.asyncio
async def test_with_the_opt_out_an_unvalidated_create_is_saved_and_says_so(m, fake_ctx, wiki, monkeypatch):
    monkeypatch.setattr(m, "DIAGRAM_ALLOW_UNVALIDATED", True)
    renderer_down(wiki)
    validator_down(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert "Created diagram" in _text(out) and "NOT validated by Mermaid" in _text(out)
    assert _fields(out)["saved"] is True and _fields(out)["validated"] is False
    assert "AI/diagrams/login-flow.md" in wiki.pages


@pytest.mark.asyncio
async def test_with_the_opt_out_a_broken_source_is_still_refused(m, fake_ctx, wiki, monkeypatch):
    """The opt-out is for when nothing can validate, not a way past a
    validator that said no."""
    monkeypatch.setattr(m, "DIAGRAM_ALLOW_UNVALIDATED", True)
    renderer_down(wiki)
    validator_rejects(wiki, line=2)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, source="flowchart TD\n  A --> B[\n  B --> C")
    assert "at line 2" in _text(out) and wiki.writes == []


@pytest.mark.asyncio
async def test_with_no_validator_the_renderer_still_validates(m, fake_ctx, wiki):
    validator_down(wiki)
    renderer_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert _fields(out)["validated"] is True and len(_images(out)) == 1


@pytest.mark.asyncio
async def test_a_validator_error_counts_as_no_validator(m, fake_ctx, wiki):
    wiki.mock.post(f"{VALIDATOR}/validate").mock(return_value=httpx.Response(503, json={}))
    renderer_down(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert _fields(out)["saved"] is False and "diagram-validator" in _text(out)


@pytest.mark.asyncio
async def test_without_an_image_the_renderer_is_not_asked(m, fake_ctx, wiki):
    route = renderer_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, render=False)
    assert _fields(out)["validated"] is True and not route.called


@pytest.mark.asyncio
async def test_a_source_the_validator_rejects_never_reaches_the_renderer(m, fake_ctx, wiki):
    route = renderer_ok(wiki)
    validator_rejects(wiki, line=2)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, source="flowchart TD\n  A --> B[\n  B --> C")
    assert "at line 2" in _text(out) and not route.called and wiki.writes == []


@pytest.mark.asyncio
async def test_an_error_only_the_renderer_finds_is_still_refused(m, fake_ctx, wiki):
    renderer_rejects(wiki, line=3, message="Could not lay this out")
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert "at line 3" in _text(out) and wiki.writes == []


# ── acceptance 4: a broken source returns a line-numbered parse error ──────

@pytest.mark.asyncio
async def test_a_source_mermaid_rejects_is_not_saved(m, fake_ctx, wiki):
    renderer_rejects(wiki, line=2)
    out = await _create(m, fake_ctx(token_info=_RW), wiki,
                        source="flowchart TD\n  A --> B[\n  B --> C")
    text = _text(out)
    assert "at line 2 of the source" in text and "Nothing was saved" in text
    assert ">2 |   A --> B[" in text
    assert wiki.writes == []


@pytest.mark.asyncio
async def test_a_wrong_diagram_type_is_caught_before_the_renderer(m, fake_ctx, wiki):
    route = renderer_ok(wiki)
    checker = validator_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, source="flowchat TD\n  A-->B")
    assert "at line 1" in _text(out) and "not a Mermaid diagram type" in _text(out)
    assert not route.called and not checker.called and wiki.writes == []
    assert _fields(out)["validated"] is False and _fields(out)["error"]["line"] == 1


# ── acceptance 2: patch one label, and the page changes by one line ────────

@pytest.mark.asyncio
async def test_patching_a_label_is_a_one_line_change(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki, description="How a user signs in.")
    before = wiki.pages["AI/diagrams/login-flow.md"]
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "B[Check password]", "new": "B[Check password and MFA]"}],
        ctx=ctx)
    assert "Patched diagram `login-flow`" in _text(out), _text(out)
    assert len(_images(out)) == 1
    after = wiki.pages["AI/diagrams/login-flow.md"]
    changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
    assert changed == [("  A[Log in] --> B[Check password]",
                        "  A[Log in] --> B[Check password and MFA]")]
    assert len(before.split("\n")) == len(after.split("\n"))
    assert wiki.writes[-1]["if_match"] == _sha(before)


@pytest.mark.asyncio
async def test_a_diagram_inside_a_design_page_is_patched_in_place(m, fake_ctx, wiki):
    renderer_ok(wiki)
    path = "AI/projects/x/design.md"
    wiki.pages[path] = "# Design\n\nProse.\n\n```mermaid\nflowchart TD\n  A-->B\n```\n\nMore prose.\n"
    out = await m.diagram_patch(path, _sha(wiki.pages[path])[:12],
                                [{"op": "replace_line", "line": 2, "text": "  A-->C"}],
                                ctx=fake_ctx(token_info=_RW))
    assert "Patched" in _text(out)
    assert wiki.pages[path] == "# Design\n\nProse.\n\n```mermaid\nflowchart TD\n  A-->C\n```\n\nMore prose.\n"


# ── version conflict ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_stale_version_is_refused_and_nothing_written(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    stale = _version(wiki)
    wiki.pages["AI/diagrams/login-flow.md"] += "\nA human edited this.\n"
    n = len(wiki.writes)
    out = await m.diagram_patch("login-flow", stale, [
        {"op": "replace", "old": "C[Home]", "new": "C[Dashboard]"}], ctx=ctx)
    assert "Version conflict" in _text(out) and "Nothing was" in _text(out)
    assert len(wiki.writes) == n


@pytest.mark.asyncio
async def test_a_write_that_loses_the_race_is_reported_as_a_conflict(m, fake_ctx, wiki):
    """The page moves between our read and our write: the editor's if_match
    catches what the MCP-side check could not."""
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    path = "AI/diagrams/login-flow.md"
    real_read = wiki._read

    def read_then_someone_writes(request):
        resp = real_read(request)
        wiki.pages[path] += "\nraced\n"
        return resp
    wiki.mock.post(f"{EDITOR}/api/internal/wiki/read").mock(side_effect=read_then_someone_writes)
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "C[Home]", "new": "C[Dashboard]"}], ctx=ctx)
    assert "Version conflict" in _text(out)
    assert "C[Dashboard]" not in wiki.pages[path]


@pytest.mark.asyncio
async def test_if_version_is_required(m, fake_ctx, wiki):
    out = await m.diagram_patch("login-flow", "", [{"old": "a", "new": "b"}],
                                ctx=fake_ctx(token_info=_RW))
    assert "if_version" in _text(out) and wiki.writes == []


# ── invalid patches ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_patch_that_breaks_the_diagram_is_not_saved(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    before = wiki.pages["AI/diagrams/login-flow.md"]
    renderer_rejects(wiki, line=3, message="Parse error on line 3")
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "B --> C[Home]", "new": "B --> C[Home"}], ctx=ctx)
    assert "at line 3 of the source" in _text(out)
    assert wiki.pages["AI/diagrams/login-flow.md"] == before


@pytest.mark.asyncio
async def test_an_edit_that_cannot_apply_says_which(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "C[Home]", "new": "C[Dash]"},
        {"op": "replace", "old": "nowhere", "new": "x"}], ctx=ctx)
    assert "Edit 2" in _text(out) and "Nothing was written" in _text(out)


# ── acceptance 3 (the MCP half): render what the page holds now ────────────

@pytest.mark.asyncio
async def test_render_draws_the_current_source(m, fake_ctx, wiki):
    route = renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RO)
    wiki.pages["AI/diagrams/login-flow.md"] = f"# L\n\n```mermaid\n{FLOW}\n```\n"
    out = await m.diagram_render("login-flow", ctx=ctx)
    assert len(_images(out)) == 1
    assert json.loads(route.calls.last.request.content)["source"] == FLOW
    # A reverted page is just a page with the old text in it; render reads it.
    wiki.pages["AI/diagrams/login-flow.md"] = "# L\n\n```mermaid\nflowchart TD\n  A-->B\n```\n"
    await m.diagram_render("login-flow", ctx=ctx)
    assert json.loads(route.calls.last.request.content)["source"] == "flowchart TD\n  A-->B"


@pytest.mark.asyncio
async def test_render_a_preview_writes_nothing(m, fake_ctx, wiki):
    renderer_ok(wiki)
    out = await m.diagram_render(source="flowchart TD\n  A-->B", format="svg",
                                 ctx=fake_ctx(token_info=_RO))
    assert "<svg/>" in _text(out) and wiki.writes == []


@pytest.mark.asyncio
async def test_render_without_a_renderer_explains_itself(m, fake_ctx, wiki):
    renderer_down(wiki)
    out = await m.diagram_render(source="flowchart TD\n  A-->B", ctx=fake_ctx(token_info=_RO))
    assert "not running" in _text(out)
    assert "8004" not in _text(out)


# ── read side ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_shows_numbered_source_and_version(m, fake_ctx, wiki):
    wiki.pages["AI/diagrams/login-flow.md"] = f"# L\n\n```mermaid\n{FLOW}\n```\n"
    out = await m.diagram_get("login-flow", ctx=fake_ctx(token_info=_RO))
    assert f"`{_version(wiki)}`" in out
    assert " 2 |   A[Log in] --> B[Check password]" in out


@pytest.mark.asyncio
async def test_list_names_the_diagrams(m, fake_ctx, wiki):
    wiki.pages["AI/diagrams/a.md"] = "x"
    wiki.pages["AI/diagrams/b.md"] = "x"
    out = await m.diagram_list(ctx=fake_ctx(token_info=_RO))
    assert "`a` — `AI/diagrams/a.md`" in out and "`b`" in out


# ── scopes ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_writes_need_wiki_write_and_reads_need_wiki_read(m, fake_ctx, wiki):
    ro = fake_ctx(token_info=_RO)
    assert "wiki_write" in _text(await m.diagram_create("x", FLOW, ctx=ro))
    assert "wiki_write" in _text(await m.diagram_patch("x", "abc", [{"old": "a"}], ctx=ro))
    none = fake_ctx(token_info={"tenant_slug": "acme"})
    for out in (await m.diagram_get("x", ctx=none), await m.diagram_list(ctx=none),
                await m.diagram_render("x", ctx=none)):
        assert "wiki_read" in _text(out)
    assert wiki.writes == []


def test_tools_carry_read_and_write_annotations(m):
    tools = {t.name: t for t in m.mcp._tool_manager.list_tools()}
    for name in ("diagram_get", "diagram_list", "diagram_render"):
        assert tools[name].annotations.readOnlyHint is True, name
    for name in ("diagram_create", "diagram_patch"):
        assert tools[name].annotations.readOnlyHint is False, name
        assert tools[name].annotations.destructiveHint is False, name
    edit = tools["diagram_patch"].parameters["$defs"]["DiagramEdit"]["properties"]
    assert set(edit["op"]["enum"]) == {"replace", "replace_line", "insert_after", "delete_line"}


# ── review follow-ups ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_path_create_placed_under_the_ai_folder_reads_back_as_given(m, fake_ctx, wiki):
    """create puts `notes/flow` at `AI/notes/flow.md`; get and render must
    find it by the same name the agent created it with."""
    route = renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    out = await m.diagram_create("notes/flow", FLOW, ctx=ctx)
    assert "AI/notes/flow.md" in _text(out)
    assert "Log in" in await m.diagram_get("notes/flow", ctx=ctx)
    assert len(_images(await m.diagram_render("notes/flow", ctx=ctx))) == 1
    assert route.called


@pytest.mark.asyncio
async def test_a_title_cannot_break_out_of_the_heading(m, fake_ctx, wiki):
    renderer_ok(wiki)
    await _create(m, fake_ctx(token_info=_RW), wiki, title="Login\n\n```\nflow")
    assert wiki.pages["AI/diagrams/login-flow.md"].startswith("# Login ``` flow\n\n```mermaid\n")


@pytest.mark.asyncio
async def test_a_too_short_version_is_refused_as_such(m, fake_ctx, wiki):
    out = await m.diagram_patch("login-flow", "ab", [{"old": "a", "new": "b"}],
                                ctx=fake_ctx(token_info=_RW))
    assert "too short" in _text(out) and wiki.writes == []


# ── card #413: the shared rich fixture, as the second block of a page ──────
# The fixture is the one the validator, the renderer and the wiki reader are
# tested with (diagram-validator/scripts/fixtures/rich-network.mmd). The
# corpus page holds it as the second of three blocks; memory-ui's
# wiki-mermaid test pins that it does, byte for byte.

_REPO = Path(__file__).resolve().parents[2]
RICH_PAGE = (_REPO / "memory-ui/test/corpus/mermaid-rich-network.md").read_text(encoding="utf-8")
NETWORK = (_REPO / "diagram-validator/scripts/fixtures/rich-network.mmd").read_text(encoding="utf-8")
RICH_PATH = "AI/projects/example/deployment.md"


def test_the_rich_page_holds_the_fixture_as_its_second_block(m):
    blocks = m._mermaid_blocks(RICH_PAGE)
    assert len(blocks) == 3
    assert blocks[1]["source"] == NETWORK.rstrip("\n")
    # What makes it rich, so a trimmed fixture cannot pass for the real one.
    for feature in ("themeVariables:", "classDef ", "\n  class ", "\n  style front ",
                    "direction TB", "{{Edge cache}}", "[(Orders DB)]", "([Visitor])",
                    "[[Job runner]]", "e1@==>", "e1@{ animate: true, animation: fast }",
                    "-.->|", "==>|"):
        assert feature in blocks[1]["source"], feature


@pytest.mark.asyncio
async def test_get_reads_the_rich_fixture_as_block_1(m, fake_ctx, wiki):
    wiki.pages[RICH_PATH] = RICH_PAGE
    out = await m.diagram_get(RICH_PATH, block=1, ctx=fake_ctx(token_info=_RO))
    assert "block 1 of 3" in out
    assert f"`{_sha(RICH_PAGE)[:12]}`" in out
    assert f"```mermaid\n{NETWORK.rstrip(chr(10))}\n```" in out
    line = NETWORK.split("\n").index("      db[(Orders DB)]") + 1
    assert f"{line:>2} |       db[(Orders DB)]" in out


@pytest.mark.asyncio
async def test_patching_one_label_in_the_rich_fixture_changes_one_line(m, fake_ctx, wiki):
    renderer_ok(wiki)
    route = validator_ok(wiki)
    wiki.pages[RICH_PATH] = RICH_PAGE
    out = await m.diagram_patch(RICH_PATH, _sha(RICH_PAGE)[:12], [
        {"op": "replace", "old": "db[(Orders DB)]", "new": "db[(Orders database)]"}],
        block=1, ctx=fake_ctx(token_info=_RW))
    assert "Patched" in _text(out), _text(out)
    after = wiki.pages[RICH_PATH]
    assert after == RICH_PAGE.replace("db[(Orders DB)]", "db[(Orders database)]")
    before_lines, after_lines = RICH_PAGE.split("\n"), after.split("\n")
    assert len(before_lines) == len(after_lines)
    assert [(a, b) for a, b in zip(before_lines, after_lines) if a != b] == [
        ("      db[(Orders DB)]", "      db[(Orders database)]")]
    diff = _text(out).split("```diff\n", 1)[1].split("\n```", 1)[0].split("\n")
    changes = [d for d in diff if d[:1] in "+-" and not d.startswith(("---", "+++"))]
    assert changes == ["-      db[(Orders DB)]", "+      db[(Orders database)]"]
    # The validator checked the patched fixture, not the page or another block.
    sent = json.loads(route.calls.last.request.content)["source"]
    assert sent == NETWORK.rstrip("\n").replace("Orders DB", "Orders database")
    assert _fields(out)["saved"] is True and _fields(out)["validated"] is True


@pytest.mark.asyncio
async def test_create_at_a_full_path_that_exists_refuses_to_overwrite(m, fake_ctx, wiki):
    renderer_ok(wiki)
    wiki.pages[RICH_PATH] = RICH_PAGE
    out = await m.diagram_create(RICH_PATH, FLOW, ctx=fake_ctx(token_info=_RW))
    assert "already exists" in _text(out) and "diagram_patch" in _text(out)
    assert wiki.pages[RICH_PATH] == RICH_PAGE
    assert wiki.writes == []


# ── card #414: the descriptions teach, dashes warn, a missing renderer says how ──

_REPO = Path(__file__).resolve().parent.parent.parent
EM, EN = "\u2014", "\u2013"   # escapes, so the house dash guard does not trip here


def _descriptions(m):
    return {t.name: t.description or "" for t in m.mcp._tool_manager.list_tools()
            if t.name in ("diagram_create", "diagram_patch", "diagram_get", "diagram_render")}


@pytest.mark.parametrize("phrase", [
    "Front matter config is allowed before the diagram type",
    "stable short node IDs",
    "quoted labels",
    "classDef", "`class api,db hot`", "`style front",
    "Edge IDs and `animate` need Mermaid 11.16.0",
    "`block` selects a diagram when a page holds several",
    "`if_version` comes from `diagram_get` or the last create or patch",
    "em and en dashes",
])
def test_every_diagram_tool_description_carries_the_guidance(m, phrase):
    descs = _descriptions(m)
    assert len(descs) == 4
    for name, desc in descs.items():
        assert phrase in desc, f"{name} lost: {phrase}"


def test_the_named_mermaid_version_is_the_one_shipped(m):
    for pkg in ("diagram-validator", "diagram-renderer"):
        deps = json.loads((_REPO / pkg / "package.json").read_text())["dependencies"]
        assert deps["mermaid"] == m.MERMAID_VERSION, pkg
    bundle = (_REPO / "memory-ui" / "static" / "mermaid.min.js").read_text(errors="ignore")
    assert f'version:"{m.MERMAID_VERSION}"' in bundle


def test_render_describes_the_renderer_off_result(m):
    desc = _descriptions(m)["diagram_render"]
    assert "COMPOSE_PROFILES=diagrams" in desc and "rendered: false" in desc


def test_the_guidance_itself_has_no_dashes(m):
    for desc in _descriptions(m).values():
        assert EM not in desc and EN not in desc


@pytest.mark.asyncio
async def test_an_em_dash_in_a_label_saves_with_a_warning_naming_the_line(m, fake_ctx, wiki):
    renderer_ok(wiki)
    src = f"flowchart TD\n  A[Log in] --> B[Check {EM} password]\n  B --> C[Home]"
    out = await _create(m, fake_ctx(token_info=_RW), wiki, source=src)
    assert _fields(out)["saved"] is True and out.isError is False
    assert EM in wiki.pages["AI/diagrams/login-flow.md"]
    (w,) = _fields(out)["warnings"]
    assert w["code"] == "dash" and w["lines"] == [2]
    assert "line 2" in w["message"] and "em dash (U+2014)" in w["message"]
    assert "line 2" in _text(out)


@pytest.mark.asyncio
async def test_a_patch_that_adds_an_en_dash_saves_with_a_warning(m, fake_ctx, wiki):
    renderer_ok(wiki)
    ctx = fake_ctx(token_info=_RW)
    await _create(m, ctx, wiki)
    out = await m.diagram_patch("login-flow", _version(wiki), [
        {"op": "replace", "old": "C[Home]", "new": f"C[Home {EN} start]"}], ctx=ctx)
    assert _fields(out)["saved"] is True
    (w,) = _fields(out)["warnings"]
    assert w["lines"] == [3] and "en dash (U+2013)" in w["message"]


@pytest.mark.asyncio
async def test_a_clean_source_carries_no_warning(m, fake_ctx, wiki):
    renderer_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert "warnings" not in _fields(out)


def test_the_dash_warning_names_every_line(m):
    w = m._dash_warning(f"flowchart TD\n  A[a {EM} b]\n  B[c]\n  C[d {EN} e]")
    assert w["lines"] == [2, 4] and "lines 2, 4 contain" in w["message"]
    assert m._dash_warning("flowchart TD\n  A[a - b]") is None


@pytest.mark.asyncio
async def test_render_with_the_renderer_off_names_the_enabling_setting(m, fake_ctx, wiki):
    renderer_down(wiki)
    out = await m.diagram_render(source="flowchart TD\n  A-->B", ctx=fake_ctx(token_info=_RO))
    f = _fields(out)
    assert f["rendered"] is False and f["validated"] is True
    assert f["renderer"]["enable"]["compose"].startswith("COMPOSE_PROFILES=diagrams")
    assert "diagram-renderer" in f["renderer"]["enable"]["kubernetes"]
    assert any("validation" in s for s in f["renderer"]["still_works"])
    assert any("images" in s for s in f["renderer"]["does_not_work"])
    assert "COMPOSE_PROFILES=diagrams" in _text(out) and "Mermaid parses the source" in _text(out)
    assert "8004" not in _text(out) and "8005" not in _text(out)


@pytest.mark.asyncio
async def test_render_with_the_renderer_off_still_reports_a_parse_error(m, fake_ctx, wiki):
    renderer_down(wiki)
    validator_rejects(wiki, 2)
    out = await m.diagram_render(source="flowchart TD\n  A-->B[", ctx=fake_ctx(token_info=_RO))
    assert _fields(out)["error"]["line"] == 2 and "line 2" in _text(out)
    assert "COMPOSE_PROFILES=diagrams" in _text(out)


@pytest.mark.asyncio
async def test_create_with_the_renderer_off_says_how_to_turn_it_on(m, fake_ctx, wiki):
    renderer_down(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki)
    assert _fields(out)["renderer"]["available"] is False
    assert "COMPOSE_PROFILES=diagrams" in _text(out)


# ── card #419: layout warnings for what dagre gets wrong ──────────────────────

# An invented two-site network: nested subgraphs that each set a `direction`,
# with edges crossing their borders. That is the shape that renders as one
# tall column of stacked clusters, because every subgraph `direction` is
# ignored once an edge crosses it. The line numbers below are asserted.
NESTED = (Path(__file__).parent / "fixtures" / "nested-network.mmd").read_text()


def _codes(ws):
    return {w["code"]: w for w in ws}


@pytest.mark.asyncio
async def test_recreating_the_first_nested_network_warns_direction_is_ignored(m, fake_ctx, wiki):
    renderer_ok(wiki)
    out = await _create(m, fake_ctx(token_info=_RW), wiki, name="nested-network", source=NESTED)
    f = _fields(out)
    assert f["saved"] is True and out.isError is False
    ws = _codes(f["warnings"])
    d = ws["layout-direction-ignored"]
    assert d["subgraphs"] == ["SITEB", "SRV", "LAB", "K3S"]
    assert d["lines"] == [21, 25, 43, 47]
    assert ws["layout-subgraph-title-br"]["lines"] == [24]
    assert 71 in ws["layout-deep-nesting"]["lines"]      # OVL ==> KR, three deep
    assert "will be ignored" in _text(out)


def test_direction_is_kept_when_no_edge_crosses_the_border(m):
    src = ("flowchart TB\n  X --> G\n  subgraph G[\"Group\"]\n    direction LR\n"
           "    A --> B\n  end")
    assert m._layout_warnings(src) == []


def test_an_edge_out_of_a_subgraph_with_a_direction_warns(m):
    src = ("flowchart TB\n  X\n  subgraph G[\"Group\"]\n    direction LR\n"
           "    A --> B\n  end\n  B -.->|\"out\"| X")
    (w,) = m._layout_warnings(src)
    assert w["code"] == "layout-direction-ignored"
    assert w["lines"] == [4] and w["subgraphs"] == ["G"] and "line 7" in w["message"]


def test_edges_chained_with_ampersand_and_inline_labels_are_read(m):
    src = ("flowchart LR\n  subgraph G\n    direction TB\n    A\n  end\n"
           "  X & Y -- goes to --> A")
    (w,) = m._layout_warnings(src)
    assert w["subgraphs"] == ["G"] and "line 6" in w["message"]


def test_a_br_in_a_subgraph_title_warns_but_one_in_a_node_does_not(m):
    src = ('flowchart TB\n  subgraph S["Servers<br/>VLAN 10"]\n    A["a<br/>b"]\n  end\n'
           '  subgraph T["Lab"]\n    B["c<br>d"]\n  end')
    (w,) = m._layout_warnings(src)
    assert w["code"] == "layout-subgraph-title-br" and w["lines"] == [2]


def test_two_levels_of_nesting_are_fine_three_with_an_outside_edge_warn(m):
    two = "flowchart TB\n  subgraph A\n    subgraph B\n      N\n    end\n  end\n  X --> N"
    assert m._layout_warnings(two) == []
    three = ("flowchart TB\n  subgraph A\n    subgraph B\n      subgraph C\n        N\n"
             "        M\n      end\n    end\n  end\n  N --> M\n  X --> N")
    (w,) = m._layout_warnings(three)
    assert w["code"] == "layout-deep-nesting" and w["lines"] == [11]


def test_the_fixed_nested_layout_has_no_direction_warning(m):
    fixed = "\n".join(line for line in NESTED.split("\n")
                      if line.strip() not in ("direction TB", "direction LR"))
    assert "layout-direction-ignored" not in _codes(m._layout_warnings(fixed))


def test_edge_settings_lines_are_not_nodes(m):
    model = m._flow_model(NESTED)
    assert not {"e1", "e2", "e5"} & set(model["home"])


def test_other_diagram_types_are_not_linted(m):
    assert m._layout_warnings("sequenceDiagram\n  A->>B: hi") == []


@pytest.mark.parametrize("phrase", [
    "A `direction` line inside a subgraph is ignored when any edge crosses",
    "nested more than 2 subgraphs deep",
    "Keep subgraph titles to one line",
    "about 30 nodes",
])
def test_every_diagram_tool_description_carries_the_layout_rules(m, phrase):
    for name, desc in _descriptions(m).items():
        assert phrase in desc, f"{name} lost: {phrase}"
