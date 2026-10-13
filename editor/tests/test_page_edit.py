"""Surgical page edits: the text transformation, in isolation.

wiki_write replaces a page wholesale, so changing one line of a long page meant
an agent regenerating the whole document from context — and an agent that
regenerates 400 lines to fix one will silently drop some of the other 399.
These cover the bounded alternative. Pure functions, so the genuinely tricky
parts (what counts as a section, what counts as a unique match) are pinned
down without a tenant, a bucket or a token in the way.
"""
import pytest
from fastapi import HTTPException

import wiki_pages

PAGE = """---
title: Runbook
---

# DB restore

Intro paragraph.

## Prepare

Take a snapshot first.

### Checklist

- disk space
- credentials

## Rollback

Undo it.

## Notes

Nothing yet.
"""


def edit(text, mode, **kw):
    return wiki_pages._apply_page_edit(text, mode, **kw)


# ── append ─────────────────────────────────────────────────────────────────

def test_append_adds_to_the_end():
    out = edit(PAGE, "append", content="## Extra\n\nAdded.")
    assert out.endswith("## Extra\n\nAdded.\n")
    assert "Nothing yet." in out


def test_append_to_an_empty_page():
    assert edit("", "append", content="first") == "first\n"


def test_append_does_not_pile_up_blank_lines():
    out = edit("a\n\n\n\n", "append", content="b")
    assert out == "a\n\nb\n"


# ── replace (exact string) ─────────────────────────────────────────────────

def test_replace_changes_only_the_match():
    out = edit(PAGE, "replace", old="Undo it.", content="Run `make rollback`.")
    assert "Run `make rollback`." in out
    assert "Undo it." not in out
    assert "Take a snapshot first." in out       # everything else untouched


def test_replace_refuses_a_missing_anchor():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "replace", old="not in the page", content="x")
    assert e.value.status_code == 404


def test_replace_refuses_an_ambiguous_anchor():
    """Silently picking the first match is how an edit lands in the wrong
    place; the agent is told to disambiguate instead."""
    with pytest.raises(HTTPException) as e:
        edit("a\nx\nb\nx\n", "replace", old="x", content="y")
    assert e.value.status_code == 409
    assert "2 times" in e.value.detail


def test_replace_needs_an_old_value():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "replace", content="x")
    assert e.value.status_code == 422


# ── replace_section ────────────────────────────────────────────────────────

def test_replace_section_swaps_the_body_and_keeps_the_heading():
    out = edit(PAGE, "replace_section", heading="Rollback", content="Now documented.")
    assert "## Rollback\nNow documented." in out
    assert "Undo it." not in out
    assert "## Notes" in out                      # the next section survives


def test_replace_section_swallows_its_own_subsections():
    """'## Prepare' owns '### Checklist' — that's what a reader means by the
    section, so replacing it takes the subsection with it."""
    out = edit(PAGE, "replace_section", heading="Prepare", content="New prep.")
    assert "### Checklist" not in out
    assert "disk space" not in out
    assert "## Rollback" in out                   # the sibling is untouched


def test_replace_section_stops_at_the_next_sibling():
    out = edit(PAGE, "replace_section", heading="Checklist", content="- one")
    assert "- disk space" not in out
    assert "## Rollback" in out and "Undo it." in out


def test_replace_section_matching_is_case_insensitive():
    out = edit(PAGE, "replace_section", heading="rOLLbaCK", content="ok")
    assert "## Rollback\nok" in out


def test_replace_section_reaching_the_end_of_the_page():
    out = edit(PAGE, "replace_section", heading="Notes", content="Some notes.")
    assert out.rstrip().endswith("Some notes.")


def test_replace_section_names_the_headings_that_do_exist():
    """A wrong guess should teach the agent the real structure, not just fail."""
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "replace_section", heading="Teardown", content="x")
    assert e.value.status_code == 404
    assert "Rollback" in e.value.detail


def test_duplicate_headings_are_refused():
    doc = "## Setup\na\n\n## Setup\nb\n"
    with pytest.raises(HTTPException) as e:
        edit(doc, "replace_section", heading="Setup", content="x")
    assert e.value.status_code == 409


# ── append_section ─────────────────────────────────────────────────────────

def test_append_section_adds_at_the_end_of_that_section():
    out = edit(PAGE, "append_section", heading="Rollback", content="Also drain the queue.")
    assert "Undo it." in out
    body = out[out.index("## Rollback"):out.index("## Notes")]
    assert "Also drain the queue." in body


def test_append_section_does_not_leak_into_the_next_section():
    out = edit(PAGE, "append_section", heading="Prepare", content="MARKER")
    assert out.index("MARKER") < out.index("## Rollback")


# ── fenced code is not structure ───────────────────────────────────────────

FENCED = """# Real

```bash
# not a heading
echo hi
```

## Also real

body
"""


def test_headings_inside_fences_are_ignored():
    heads = [h[2] for h in wiki_pages._md_headings(FENCED.splitlines())]
    assert heads == ["Real", "Also real"]


def test_a_fenced_hash_line_cannot_be_targeted_as_a_section():
    with pytest.raises(HTTPException) as e:
        edit(FENCED, "replace_section", heading="not a heading", content="x")
    assert e.value.status_code == 404


def test_editing_a_section_preserves_a_code_block_inside_it():
    out = edit(FENCED, "append_section", heading="Real", content="tail")
    assert "echo hi" in out and "# not a heading" in out


# ── misuse ─────────────────────────────────────────────────────────────────

def test_unknown_mode_is_refused_and_lists_the_real_ones():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "patch", content="x")
    assert e.value.status_code == 422
    for mode in ("append", "replace", "replace_section", "append_section"):
        assert mode in e.value.detail


def test_empty_heading_is_refused():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "replace_section", heading="  ", content="x")
    assert e.value.status_code == 422


def test_frontmatter_is_left_alone():
    out = edit(PAGE, "append", content="tail")
    assert out.startswith("---\ntitle: Runbook\n---")


# ── empty content must never become a deletion ─────────────────────────────
# Reported from the field: `replace` with the replacement missing removed the
# anchor and put nothing back, reported success, and quoted a byte delta
# exactly equal to what it had eaten. An agent trusting that message took out a
# status block and all four headings before a read-back caught it. `content` is
# an optional argument, so it arrives as "" whenever it is omitted — or passed
# under a name the schema drops — which made silent data loss the DEFAULT
# outcome of a mistyped call.

@pytest.mark.parametrize("mode,kw", [
    ("replace",         {"old": "Undo it."}),
    ("replace_section", {"heading": "Rollback"}),
    ("append_section",  {"heading": "Rollback"}),
    ("append",          {}),
])
def test_empty_content_is_refused_not_treated_as_delete(mode, kw):
    for empty in ("", "   ", "\n\n"):
        with pytest.raises(HTTPException) as e:
            edit(PAGE, mode, content=empty, **kw)
        assert e.value.status_code == 422
        assert "content" in e.value.detail


def test_the_refusal_names_the_deliberate_way_to_delete():
    """Refusing without offering the alternative would just teach the agent to
    reach for wiki_write and rewrite the whole page from memory — which is the
    very thing wiki_edit exists to stop."""
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "replace", old="Undo it.", content="")
    assert "mode='delete'" in e.value.detail


def test_the_exact_reported_case_no_longer_eats_the_heading():
    """The report: '-17 for a 17-character heading'."""
    heading = "## Rollback"
    with pytest.raises(HTTPException):
        edit(PAGE, "replace", old=heading, content="")
    # And nothing was mutated on the way to raising.
    assert heading in PAGE


# ── deletion, when it IS what you asked for ────────────────────────────────

def test_delete_removes_the_anchor():
    out = edit(PAGE, "delete", old="Undo it.\n")
    assert "Undo it." not in out
    assert "## Rollback" in out          # only the snippet went
    assert len(out) < len(PAGE)


def test_delete_needs_an_anchor():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "delete")
    assert e.value.status_code == 422


def test_delete_refuses_an_ambiguous_anchor():
    """Deletion is aimed exactly as fussily as replacement — more so matters,
    since there is no new text to notice landing in the wrong place."""
    with pytest.raises(HTTPException) as e:
        edit("a\nx\nb\nx\n", "delete", old="x")
    assert e.value.status_code == 409


def test_delete_refuses_a_missing_anchor():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "delete", old="not in the page")
    assert e.value.status_code == 404


def test_delete_ignores_content_rather_than_demanding_it():
    """`delete` is the one mode with nothing to put in, so the content guard
    must not fire on it."""
    assert "Undo it." not in edit(PAGE, "delete", old="Undo it.", content="")


def test_unknown_mode_lists_delete_too():
    with pytest.raises(HTTPException) as e:
        edit(PAGE, "patch", content="x")
    assert "delete" in e.value.detail
