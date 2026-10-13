"""Every relative link in the repository's Markdown points at something real.

The README and `docs/` are the way in for anybody who arrives from the release
video, and a link that 404s on GitHub is the cheapest way to lose them. Links
break silently: a file is renamed or a heading is reworded, and nothing fails.
This walks every tracked `.md` file, resolves each relative link against the
file that holds it, and checks that the target exists. When the target is a
Markdown file and the link names a `#section`, the heading must exist too,
spelled the way GitHub turns a heading into an anchor.

External links (`https:`, `mailto:` and so on) are not fetched. The test corpus
under `memory-ui/test/corpus/` is excluded: it is input data for the editor's
round-trip tests, and its links are deliberately odd.
"""
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
EXCLUDE = ("memory-ui/test/corpus/",)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
# [text](target "title") and ![alt](target). The text may hold one level of
# brackets, as in a badge: [![alt](img)](target).
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HTML_ANCHOR = re.compile(r"""<a\s[^>]*(?:name|id)=["']([^"']+)["']""", re.I)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def _markdown_files():
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z", "--", "*.md"],
            check=True, capture_output=True,
        ).stdout.decode()
        files = [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        # No git in the test container: walk the tree instead.
        files = [
            p.relative_to(REPO).as_posix()
            for p in REPO.rglob("*.md")
            if not SKIP_DIRS.intersection(p.relative_to(REPO).parts)
        ]
    return sorted(f for f in files if not f.startswith(EXCLUDE))


def _prose_lines(text):
    """The file's lines with fenced code blocks dropped and inline code
    blanked, so a link shown as an example is not checked as a link."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                continue
        if fence is None:
            yield number, INLINE_CODE.sub("", line)


def _links(text):
    for number, line in _prose_lines(text):
        for m in INLINE_LINK.finditer(line):
            yield number, m.group(1)
        m = REF_DEF.match(line)
        if m:
            yield number, m.group(1)


def _slug(heading):
    """GitHub's heading anchor: lower case, punctuation dropped except `-` and
    `_`, each space a hyphen."""
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = heading.strip().lower()
    heading = re.sub(r"[^\w\- ]", "", heading)
    return heading.replace(" ", "-")


_ANCHORS = {}


def _anchors(path):
    if path not in _ANCHORS:
        seen, found = {}, set()
        for _, line in _prose_lines(path.read_text(encoding="utf-8")):
            m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
            if m:
                base = _slug(m.group(1))
                count = seen.get(base, 0)
                seen[base] = count + 1
                found.add(base if count == 0 else f"{base}-{count}")
            found.update(HTML_ANCHOR.findall(line))
        _ANCHORS[path] = found
    return _ANCHORS[path]


def _problems(rel, repo=REPO):
    source = repo / rel
    text = source.read_text(encoding="utf-8")
    for number, target in _links(text):
        if SCHEME.match(target) or target.startswith("//"):
            continue
        path_part, _, anchor = target.partition("#")
        path_part = unquote(path_part.split("?", 1)[0])
        if path_part.startswith("/"):
            # A site-absolute path is a route in the running app, not a file.
            continue
        resolved = (source.parent / path_part).resolve() if path_part else source
        if not resolved.exists():
            yield f"{rel}:{number}: {target} -> no such file"
            continue
        if anchor and resolved.is_file() and resolved.suffix == ".md":
            if unquote(anchor).lower() not in _anchors(resolved):
                yield f"{rel}:{number}: {target} -> no heading #{anchor}"


def test_the_markdown_files_are_found():
    files = _markdown_files()
    assert "README.md" in files and "docs/index.md" in files
    assert not any(f.startswith(EXCLUDE) for f in files)


@pytest.mark.parametrize("rel", _markdown_files())
def test_relative_links_resolve(rel):
    problems = list(_problems(rel))
    assert not problems, "broken relative links:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("text, expected", [
    ("Upgrading", "upgrading"),
    ("Install: pick one", "install-pick-one"),
    ("4a. [DECISION] Proof block", "4a-decision-proof-block"),
    ("Promoting a release: preflight → pull → up → verify",
     "promoting-a-release-preflight--pull--up--verify"),
    ("Check the host: `scripts/prod-preflight.sh`",
     "check-the-host-scriptsprod-preflightsh"),
])
def test_slugs_match_github(text, expected):
    assert _slug(text) == expected


def test_a_broken_link_is_reported(tmp_path):
    (tmp_path / "a.md").write_text(
        "# Top\n\n[ok](b.md#there) [gone](c.md) [bad](b.md#nowhere)\n"
        "```\n[example](not-checked.md)\n```\n`[code](x.md)` [web](https://example.com)\n"
    )
    (tmp_path / "b.md").write_text("## There\n")
    problems = list(_problems("a.md", repo=tmp_path))
    assert problems == [
        "a.md:3: c.md -> no such file",
        "a.md:3: b.md#nowhere -> no heading #nowhere",
    ]
