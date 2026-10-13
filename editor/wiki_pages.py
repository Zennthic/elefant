"""
What happens to a wiki page's content: revision capture, surgical edits,
heading anchors, write locks and the space ACL check.

Moved out of main.py by CQ-04 phase 2. The session-authed routes in
routers/wiki.py and the token-authed /api/internal/wiki/* endpoints in
main.py share them, so the rules each one enforces live in one place.
"""
import json as _json
import logging
import re
from typing import Dict, List, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from acl import auth_context_from_request, space_of_path, user_can_access_space
from config_db import Tenant, UserAccount, WikiPageLock, WikiRevision

log = logging.getLogger("editor")

# ── Wiki page version history (revisions) ──────────────────────────────────
# Every wiki save appends an immutable WikiRevision snapshot so the History
# view can show what changed, when, and by whom, and offer revert. Capture is
# best-effort: a bookkeeping failure is logged but never fails the save.

def _parse_frontmatter_meta(text_content: str):
    """Best-effort (tags: list[str], title: str|None) from a leading YAML
    frontmatter block. Dependency-free and tolerant of the common shapes (flow
    list, block list, comma string). Tags also live in the content snapshot, so
    imperfect parsing only affects History badges / changed-field detection —
    never the stored bytes."""
    tags: list = []
    title = None
    if not text_content:
        return tags, title
    lines = text_content.splitlines()
    if not lines or lines[0].strip() != "---":
        return tags, title
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return tags, title
    block = lines[1:end]
    i = 0
    while i < len(block):
        stripped = block[i].strip()
        if stripped.startswith("title:"):
            title = stripped[len("title:"):].strip().strip("'\"") or None
        elif stripped.startswith("tags:"):
            rest = stripped[len("tags:"):].strip()
            if rest.startswith("[") and rest.endswith("]"):
                tags = [t.strip().strip("'\"") for t in rest[1:-1].split(",") if t.strip()]
            elif rest:
                tags = [t.strip().strip("'\"") for t in rest.split(",") if t.strip()]
            else:
                j = i + 1
                while j < len(block):
                    bs = block[j].strip()
                    if bs.startswith("- "):
                        tags.append(bs[2:].strip().strip("'\""))
                        j += 1
                    elif bs == "":
                        j += 1
                    else:
                        break
                i = j - 1
        i += 1
    return tags, title


# ── Surgical page edits ────────────────────────────────────────────────────
# wiki_write replaces a page wholesale, which is the only write an agent had.
# Changing one line of a long page therefore meant regenerating the whole
# document from context — and an agent that regenerates 400 lines to fix one
# will silently drop some of the other 399. These apply a bounded change to the
# text instead, so the parts nobody touched are the same bytes afterwards.
#
# A side benefit falls out for free: revisions store whole snapshots, so the
# History diff between them is small precisely because the edit was small. A
# reviewer sees the actual change rather than a whole-page replacement.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _md_headings(lines: List[str]) -> List[tuple]:
    """(index, level, text) for every ATX heading, skipping fenced code.

    A '# comment' inside a ```bash block is not a heading, and treating it as
    one would splice an edit into the middle of someone's shell snippet.
    """
    out, fence = [], None
    # Nor is a '# comment' in the YAML frontmatter, which the reader strips
    # before rendering — counting it would shift every anchor after it.
    skip_to = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                skip_to = j + 1
                break
    for i, line in enumerate(lines):
        if i < skip_to:
            continue
        m = _FENCE_RE.match(line)
        if m:
            tok = m.group(1)
            if fence is None:
                fence = tok
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        h = _HEADING_RE.match(line)
        if h:
            out.append((i, len(h.group(1)), h.group(2).strip()))
    return out


# ── Heading anchors ─────────────────────────────────────────────
#
# Every heading gets a stable id so `page.md#heading-title` can point at one
# section. The rule is GitHub's, so a link written against a GitHub render of
# the same file keeps working here:
#
#   1. take the heading's TEXT as a reader sees it (markup stripped: `code`
#      keeps its content, [link](url) keeps its label, images and HTML tags go);
#   2. lowercase it;
#   3. drop every character that is not a letter, mark, digit, underscore,
#      space or hyphen;
#   4. turn each space into a hyphen;
#   5. a repeat of an earlier anchor on the same page gets -1, -2, ... in
#      document order. A heading with nothing left after step 3 is "section".
#
# The same rule runs in three places: here (sections for wiki_edit and the
# internal read), mcp/main.py (anchors on search hits) and memory-ui's reader
# (ids on the rendered page). tests/fixtures/heading-anchors.json is the one
# list of cases all three are checked against, so they cannot drift apart.

_ANCHOR_CODE_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_ANCHOR_ESC_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_ANCHOR_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_ANCHOR_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ANCHOR_AUTOLINK_RE = re.compile(r"<((?:[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^<>\s]*)|(?:[^<>\s@]+@[^<>\s@]+))>")
_ANCHOR_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_ANCHOR_UNDERSCORE_RE = re.compile(r"(?<![^\W_])(_{1,3})(?=[^\s_])(.+?)(?<=[^\s_])\1(?![^\W_])")


def _heading_plain_text(raw: str) -> str:
    """The text a reader sees for a heading's markdown — what the anchor is
    built from. Covers the inline markup that changes the result; anything
    the slug step throws away anyway (`*`, `~`) needs no handling here."""
    import html as _html
    held: List[str] = []

    def _hold(s: str) -> str:
        held.append(s)
        return f"\x00{len(held) - 1}\x00"

    def _code(m: re.Match) -> str:
        body = m.group(2)
        if len(body) > 1 and body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]
        return _hold(body)

    s = _ANCHOR_CODE_RE.sub(_code, raw or "")
    s = _ANCHOR_ESC_RE.sub(lambda m: _hold(m.group(1)), s)
    s = _ANCHOR_IMG_RE.sub("", s)
    s = _ANCHOR_LINK_RE.sub(r"\1", s)
    s = _ANCHOR_AUTOLINK_RE.sub(lambda m: _hold(m.group(1)), s)
    s = _ANCHOR_TAG_RE.sub("", s)
    prev = None
    while prev != s:
        prev, s = s, _ANCHOR_UNDERSCORE_RE.sub(r"\2", s)
    s = _html.unescape(s)
    s = re.sub("\x00(\\d+)\x00", lambda m: held[int(m.group(1))], s)
    return s.strip()


def _heading_slug(text: str) -> str:
    """Steps 2-4 of the rule above, on text that is already plain."""
    import unicodedata
    kept = [ch for ch in (text or "").lower()
            if ch in " -" or unicodedata.category(ch)[0] in "LMN"
            or unicodedata.category(ch) == "Pc"]
    return "".join(kept).replace(" ", "-")


def _anchor_dedupe(seen: Dict[str, int], base: str) -> str:
    """Step 5: GitHub's counter, so `steps`, `steps-1`, `steps-2` in order."""
    base = base or "section"
    anchor = base
    while anchor in seen:
        seen[base] += 1
        anchor = f"{base}-{seen[base]}"
    seen[anchor] = 0
    return anchor


def _heading_outline(lines: List[str]) -> List[tuple]:
    """(index, level, text, anchor) for every heading `_md_headings` finds —
    the same headings wiki_edit addresses, so an anchor and the section an
    agent edits by it are one thing."""
    seen: Dict[str, int] = {}
    return [(i, lvl, t, _anchor_dedupe(seen, _heading_slug(_heading_plain_text(t))))
            for i, lvl, t in _md_headings(lines)]


def _match_anchor(outline: List[tuple], anchor: str) -> Optional[tuple]:
    """The outline entry `anchor` names, or None. Tried in order: the anchor
    exactly; the anchor re-slugged, so `#Rollback Steps` or a percent-encoded
    fragment still lands; then the heading text itself."""
    want = (anchor or "").strip().lstrip("#").strip()
    try:
        from urllib.parse import unquote
        want = unquote(want)
    except Exception:
        pass
    if not want:
        return None
    for key in (want, _heading_slug(want)):
        for h in outline:
            if h[3] == key:
                return h
    low = want.lower()
    hits = [h for h in outline if h[2].lower() == low]
    return hits[0] if len(hits) == 1 else None


def _section_end(heads: List[tuple], idx: int, level: int, total: int) -> int:
    """Where the section opened by the heading at `idx` stops: the next heading
    of the same or a shallower level, else the end of the page."""
    for h in heads:
        if h[0] > idx and h[1] <= level:
            return h[0]
    return total


def _find_section(lines: List[str], heading: str) -> tuple:
    """Locate a section by its heading text → (heading_index, body_start, body_end).

    The section body runs from just after the heading to the next heading of
    the same or shallower level — so editing "## Rollback" leaves its own
    "### Steps" subsection inside it, which is what a reader means by that
    section. Matching is case-insensitive on the heading text; an ambiguous or
    missing heading raises rather than guessing which one was meant.

    The heading's ANCHOR works too (`rollback`, `#rollback`, `steps-1`), and is
    how to reach one of two headings with the same text: the second "Steps" is
    `steps-1`, exactly as a `page.md#steps-1` link names it.
    """
    want = (heading or "").strip().lstrip("#").strip().lower()
    if not want:
        raise HTTPException(status_code=422, detail="A heading is required")
    heads = _md_headings(lines)
    hits = [h for h in heads if h[2].lower() == want]
    if not hits:
        by_anchor = _match_anchor(_heading_outline(lines), want)
        if by_anchor:
            hits = [by_anchor[:3]]
    if not hits:
        available = ", ".join(f"'{h[2]}'" for h in heads[:12]) or "(none)"
        raise HTTPException(
            status_code=404,
            detail=f"No heading '{heading}' in the page. Headings present: {available}")
    if len(hits) > 1:
        anchors = ", ".join(f"'{h[3]}'" for h in _heading_outline(lines)
                            if h[2].lower() == want)
        raise HTTPException(
            status_code=409,
            detail=f"Heading '{heading}' appears {len(hits)} times — it must be "
                   f"unique to edit by name. Name one by its anchor instead "
                   f"({anchors}), use mode='replace' with surrounding text, or "
                   f"make the headings distinct.")
    idx, level, _ = hits[0]
    return idx, idx + 1, _section_end(heads, idx, level, len(lines))


def _page_section(text: str, anchor: str) -> Optional[dict]:
    """The section `anchor` names, heading line included, or None when the
    page has no such heading (renamed or never there — the caller fails soft
    to the whole page)."""
    body_lines = text.splitlines()
    outline = _heading_outline(body_lines)
    hit = _match_anchor(outline, anchor)
    if not hit:
        return None
    idx, level, title, anc = hit
    end = _section_end(outline, idx, level, len(body_lines))
    return {"anchor": anc, "heading": title, "level": level,
            "content": "\n".join(body_lines[idx:end]).rstrip("\n") + "\n"}


def _locate_anchor(text: str, old: str, verb: str) -> None:
    """Check `old` occurs exactly once, or refuse and say how to fix it.

    Shared by replace and delete so both are equally fussy about aiming: an
    anchor matching twice is the difference between editing the line you meant
    and editing a different one that happens to read the same.
    """
    if not old:
        raise HTTPException(status_code=422,
                            detail=f"mode='{verb}' needs `old` — the exact text to {verb}")
    n = text.count(old)
    if n == 0:
        raise HTTPException(
            status_code=404,
            detail="`old` does not appear in the page. It must match the "
                   "current text exactly, whitespace included.")
    if n > 1:
        raise HTTPException(
            status_code=409,
            detail=f"`old` appears {n} times — include enough surrounding "
                   f"text to make it unique, so the right one is {verb}d.")


# Modes that write text INTO the page, and therefore have nothing to do without
# any. Kept as a set so the guard below can't drift from the list of modes.
_CONTENT_MODES = ("append", "replace", "replace_section", "append_section")


def _apply_page_edit(text: str, mode: str, *, heading: str = "",
                     content: str = "", old: str = "") -> str:
    """Return `text` with one bounded change applied. Pure — no I/O, so the
    tricky part (what counts as a section, what counts as a unique match) is
    testable without a tenant, a bucket or a token."""
    lines = text.splitlines()

    # A destructive result must be ASKED for, never defaulted into.
    #
    # `content` is an optional argument, so it arrives as "" whenever an agent
    # omits it — or passes the replacement under some other name and the schema
    # drops it. That turned mode='replace' into text.replace(old, "", 1): a
    # silent delete, reported as a success, with a byte delta exactly equal to
    # what it removed. An agent trusting that message gutted a page one anchor
    # at a time before a read-back caught it. Deletion is still available, but
    # only by naming it.
    if mode in _CONTENT_MODES and not content.strip():
        target = {"replace": "the snippet in `old`",
                  "replace_section": f"the '{heading}' section"}.get(mode, "content")
        raise HTTPException(
            status_code=422,
            detail=f"mode='{mode}' needs non-empty `content` — the text to put "
                   f"there. Empty content would have deleted {target} instead, "
                   f"which is too destructive to infer from a missing argument. "
                   f"If removal IS what you want, say so: mode='delete' with "
                   f"`old` set to the exact text to remove.")

    if mode == "append":
        body = text.rstrip("\n")
        return (body + "\n\n" + content.strip("\n") + "\n") if body else content.strip("\n") + "\n"

    if mode == "replace":
        _locate_anchor(text, old, "replace")
        return text.replace(old, content, 1)

    if mode == "delete":
        _locate_anchor(text, old, "delete")
        return text.replace(old, "", 1)

    if mode in ("replace_section", "append_section"):
        _h, start, end = _find_section(lines, heading)
        new_body = content.strip("\n").splitlines()
        if mode == "replace_section":
            kept = lines[:start] + new_body + lines[end:]
        else:
            body = lines[start:end]
            while body and not body[-1].strip():      # drop the blank run, re-add one
                body.pop()
            kept = lines[:start] + body + [""] + new_body + lines[end:]
        out = "\n".join(kept)
        return out if out.endswith("\n") else out + "\n"

    raise HTTPException(
        status_code=422,
        detail=f"Unknown mode '{mode}'. Use append, replace, replace_section, "
               f"append_section or delete.")


def _user_label(session: Session, user_id: Optional[str]) -> Optional[str]:
    """Friendly display name for a user id (the 'by who' column). Best
    effort — falls back to the id, then None."""
    if not user_id:
        return None
    try:
        u = session.query(UserAccount).filter_by(id=user_id).first()
        if u:
            return getattr(u, "full_username", None) or getattr(u, "username", None) or user_id
    except Exception:
        pass
    return user_id


def _record_wiki_revision(session: Session, org: Tenant, rel: str, new_content: bytes, *,
                          edited_by_id: Optional[str] = None,
                          edited_by_name: Optional[str] = None,
                          source: Optional[str] = None,
                          message: Optional[str] = None,
                          created: bool = False) -> None:
    """Append a WikiRevision capturing the just-saved version of `rel`.

    Diffs against the latest tracked revision to compute changed_fields and to
    skip true no-ops (identical content + tags + title), mirroring
    memory_revisions. History begins at the feature's first save of a page:
    pre-existing pages get their first row on their next edit. Best-effort —
    never raises into the caller; a failure is logged and rolled back so the
    underlying save still succeeds."""
    try:
        if not rel.lower().endswith((".md", ".markdown")):
            return
        new_text = (new_content.decode("utf-8", "replace")
                    if isinstance(new_content, (bytes, bytearray)) else str(new_content))
        tenant_id = org.id

        latest = (session.query(WikiRevision)
                  .filter_by(tenant_id=tenant_id, path=rel)
                  .order_by(WikiRevision.edited_at.desc(), WikiRevision.id.desc())
                  .first())

        new_tags, new_title = _parse_frontmatter_meta(new_text)

        if latest is not None:
            prior_text = latest.content or ""
            try:
                prior_tags = _json.loads(latest.tags) if latest.tags else []
            except Exception:
                prior_tags = []
            prior_title = latest.title
            if prior_text == new_text and prior_tags == new_tags and prior_title == new_title:
                return  # no-op: nothing actually changed
            changed = []
            if prior_text != new_text:
                changed.append("content")
            if prior_tags != new_tags:
                changed.append("tags")
            if prior_title != new_title:
                changed.append("title")
        else:
            changed = ["created"] if created else ["initial"]

        session.add(WikiRevision(
            tenant_id=tenant_id,
            path=rel,
            content=new_text,
            tags=_json.dumps(new_tags),
            title=new_title,
            edited_by_id=edited_by_id,
            edited_by_name=edited_by_name,
            source=source,
            message=message,
            changed_fields=_json.dumps(changed),
            created=bool(created),
        ))
        session.commit()
    except Exception as exc:
        log.warning("wiki revision capture failed: org=%s path=%s err=%s",
                    getattr(org, "slug", "?"), rel, exc)
        try:
            session.rollback()
        except Exception:
            pass


def _wiki_revisions_for(session: Session, tenant_id: str, rel: str, limit: int = 50):
    """Latest-first WikiRevision rows for (tenant, path)."""
    return (session.query(WikiRevision)
            .filter_by(tenant_id=tenant_id, path=rel)
            .order_by(WikiRevision.edited_at.desc(), WikiRevision.id.desc())
            .limit(max(1, min(int(limit or 50), 500)))
            .all())


def _last_wiki_edit(session: Session, tenant_id: str, rel: str) -> Optional[dict]:
    """Who last saved this page and when — the header line for the page view.

    Reads the newest wiki_revisions row, so a page whose history predates the
    revision log (or was written straight into the bucket) simply has no
    last-edit line rather than a wrong one.
    """
    row = (session.query(WikiRevision)
           .filter_by(tenant_id=tenant_id, path=rel)
           .order_by(WikiRevision.edited_at.desc(), WikiRevision.id.desc())
           .first())
    if row is None:
        return None
    return {
        "at":     row.edited_at.isoformat() if row.edited_at else None,
        "by":     row.edited_by_name,
        "by_id":  row.edited_by_id,
        "source": row.source,
    }


# ── Wiki page write locks ──────────────────────────────────────────────────
# A lock row's existence is the lock. Every write path — web and MCP alike —
# calls _assert_wiki_unlocked() before touching the bucket, so there is one
# place the rule lives and no way to write around it by picking another
# endpoint. Only session-authed humans reach the lock/unlock endpoints.

def _wiki_lock_for(session: Session, tenant_id: str, rel: str) -> Optional[WikiPageLock]:
    return (session.query(WikiPageLock)
            .filter_by(tenant_id=tenant_id, path=rel)
            .first())


def _assert_wiki_unlocked(session: Session, tenant_id: str, rel: str, action: str = "edited"):
    """Raise 423 Locked if this page carries a write lock.

    423 rather than 403: the caller's credentials are fine, the resource is
    held. Both the web UI and the MCP tools key off the status code.
    """
    lock = _wiki_lock_for(session, tenant_id, rel)
    if lock is None:
        return
    who = lock.locked_by_name or "a user"
    raise HTTPException(
        status_code=423,
        detail=f"'{rel}' is locked by {who} and cannot be {action}. "
               f"A human must unlock the page first.",
    )


def _assert_wiki_prefix_unlocked(session: Session, tenant_id: str, rel: str,
                                 action: str = "moved"):
    """Same check for a whole folder: a locked page anywhere beneath it stops
    the operation. Without this, a recursive move or delete would carry a
    locked page along with it and the lock would have protected nothing."""
    prefix = rel.rstrip("/") + "/"
    lock = (session.query(WikiPageLock)
            .filter(WikiPageLock.tenant_id == tenant_id,
                    WikiPageLock.path.like(prefix.replace("%", r"\%") + "%"))
            .first())
    if lock is None:
        return
    who = lock.locked_by_name or "a user"
    raise HTTPException(
        status_code=423,
        detail=f"'{rel}' holds a locked page ('{lock.path}', locked by {who}) "
               f"and cannot be {action}. A human must unlock it first.",
    )


def _filter_tree_by_acl(node: dict, allowed_spaces, is_root=True):
    """
    Walk a tree_node() result and prune top-level subtrees the caller
    cannot read.  Files at the root (not under any space dir) are kept as-is.
    """
    if not is_root or not node.get("children"):
        return node
    pruned = []
    for child in node["children"]:
        if child.get("type") == "directory" and child.get("name") in allowed_spaces:
            pruned.append(child)
        elif child.get("type") == "file":
            pruned.append(child)
    node["children"] = pruned
    return node


def _check_space_access(request: Request, session: Session, path: str, *, write: bool = False):
    """Raise 403 if the caller can't access the wiki space containing `path`."""
    ctx = auth_context_from_request(request)
    if not ctx:
        return  # let downstream auth handle it
    if ctx["role"] in ("platform_admin", "org_admin"):
        return  # admins bypass space ACLs
    space = space_of_path(path)
    if not space:
        return  # root-level files are unrestricted
    if not user_can_access_space(session, ctx["user_id"], ctx["org_id"], space, write=write):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have {'write' if write else 'read'} access to space '{space}'",
        )


def _share_page_title(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name
