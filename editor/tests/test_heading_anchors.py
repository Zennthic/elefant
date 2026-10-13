"""Heading anchors: every heading gets a stable id, so
`page.md#heading` can point at one section of a page.

The slug rule lives in three places — here, the MCP and memory-ui's reader —
and all three are checked against tests/fixtures/heading-anchors.json. These
cover the editor's copy, plus the two things only the editor does with it:
cutting a section out for a `path#anchor` read, and letting wiki_edit address
a section by its anchor.
"""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import main
import wiki_pages

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "heading-anchors.json")
    .read_text(encoding="utf-8"))


@pytest.mark.parametrize("raw,anchor", FIXTURE["headings"])
def test_single_heading_anchor(raw, anchor):
    outline = wiki_pages._heading_outline([f"## {raw}"])
    assert [h[3] for h in outline] == [anchor]


@pytest.mark.parametrize("page", FIXTURE["pages"])
def test_page_anchors_in_order(page):
    outline = wiki_pages._heading_outline(page["markdown"].splitlines())
    assert [h[3] for h in outline] == page["anchors"]


PAGE = """---
title: Runbook
---

# DB restore

Intro.

## Prepare

Snapshot first.

### Checklist

- disk

## Steps

First steps.

## Steps

Second steps.

## Rollback Plan

Undo it.
"""


def test_section_by_anchor_includes_heading_and_subsections():
    sec = wiki_pages._page_section(PAGE, "prepare")
    assert sec["heading"] == "Prepare" and sec["level"] == 2
    assert sec["content"].startswith("## Prepare\n")
    assert "### Checklist" in sec["content"] and "- disk" in sec["content"]
    assert "## Steps" not in sec["content"]


def test_section_by_deduplicated_anchor_reaches_the_second_heading():
    sec = wiki_pages._page_section(PAGE, "steps-1")
    assert "Second steps." in sec["content"]
    assert "First steps." not in sec["content"]


@pytest.mark.parametrize("ref", ["rollback-plan", "#rollback-plan", "Rollback Plan",
                                 "rollback%20plan", "ROLLBACK-PLAN"])
def test_section_lookup_is_forgiving_about_how_the_anchor_is_written(ref):
    sec = wiki_pages._page_section(PAGE, ref)
    assert sec and sec["anchor"] == "rollback-plan"


def test_missing_anchor_is_none_so_callers_fail_soft():
    assert wiki_pages._page_section(PAGE, "renamed-heading") is None


def test_wiki_edit_can_name_a_section_by_anchor():
    out = wiki_pages._apply_page_edit(PAGE, "replace_section", heading="steps-1",
                                      content="Rewritten.")
    assert "First steps." in out
    assert "Second steps." not in out and "Rewritten." in out


def test_duplicate_heading_refusal_names_the_anchors():
    with pytest.raises(HTTPException) as exc:
        wiki_pages._apply_page_edit(PAGE, "replace_section", heading="Steps", content="x")
    assert exc.value.status_code == 409
    assert "'steps'" in exc.value.detail and "'steps-1'" in exc.value.detail


def test_frontmatter_comment_is_not_a_heading():
    text = "---\n# yaml comment\ntitle: x\n---\n\n## Real\n"
    assert [h[2] for h in wiki_pages._md_headings(text.splitlines())] == ["Real"]


# ── the internal read an agent's get_wiki_page goes through ────────────────

@pytest.fixture
def reader(monkeypatch):
    """A real tenant and read token, with storage stubbed to serve PAGE."""
    import hashlib
    import secrets
    from fastapi.testclient import TestClient
    from config_db import get_db_session, Tenant, ApiToken, TenantSettings

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"ha-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.add(TenantSettings(tenant_slug=slug, ai_wiki_folder="AI"))
    h = hashlib.sha256(secrets.token_urlsafe(24).encode()).hexdigest()
    db.add(ApiToken(name="reader", token_hash=h, tenant_id=oid, wiki_read=True))
    db.commit(); db.close()

    read = []
    monkeypatch.setattr(main, "_s3_read",
                        lambda tenant, rel: read.append(rel) or PAGE.encode())
    client = TestClient(main.app)

    def post(**body):
        return client.post("/api/internal/wiki/read", json={"token_hash": h, **body})
    post.read = read
    return post


def test_read_returns_the_outline_with_anchors(reader):
    data = reader(path="runbooks/db.md").json()
    assert "section" not in data
    assert [h["anchor"] for h in data["headings"]] == [
        "db-restore", "prepare", "checklist", "steps", "steps-1", "rollback-plan"]


def test_read_with_a_fragment_on_the_path_returns_that_section(reader):
    data = reader(path="runbooks/db.md#steps-1").json()
    assert reader.read == ["runbooks/db.md"]          # the fragment is not a filename
    assert data["path"] == "runbooks/db.md"
    assert data["section"]["heading"] == "Steps"
    assert "Second steps." in data["section"]["content"]
    assert data["content"] == PAGE                     # whole page still there


def test_read_with_an_unknown_anchor_fails_soft(reader):
    data = reader(path="runbooks/db.md", anchor="was-renamed").json()
    assert data["anchor"] == "was-renamed" and data["section"] is None
    assert data["content"] == PAGE
