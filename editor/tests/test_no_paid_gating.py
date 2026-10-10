"""No tier, licence key or paywall comes back into the public tree (card #319).

The open-source build has every feature on, with no licence and no limits.
This scan fails if a tracked file outside editor/tests/ reintroduces the words
of the old paid gating: a licence token variable, a "Premium" tier, an
"upgrade your licence" message, an HTTP 402 or the honour-system mode.

Each pattern is assembled from pieces, as internal_names.py does, so this file
does not match itself. "Hot Tier" (the memory_capture queue) is a different
thing and is not matched. The Apache-2.0 LICENSE file and references to it are
not matched either: none of the patterns names it.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}

# Third-party bundles, which are not ours to edit.
VENDORED = {"memory-ui/static/tiptap.bundle.js", "memory-ui/static/marked.min.js",
            "memory-ui/static/mermaid.min.js"}

_LIC = r"licen[cs]e"
PAID_GATING = re.compile(
    r"ELEFANT_" + "LICENSE"
    + r"|" + "Prem" + "ium"
    + r"|" + "premium" + r" tier"
    + r"|" + _LIC + r" (?:key|token)"
    + r"|" + "upgrade" + r" your .*" + "licen"
    + r"|" + "status_code" + r"\s*=\s*" + "402"
    + r"|" + "honou?r" + r".system",
    re.IGNORECASE,
)


def _tracked_text_files(root=REPO):
    """Every text file in the checkout outside editor/tests/. A filesystem walk
    rather than `git ls-files`, so it runs where git is unavailable."""
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or set(parts) & SKIP_DIRS:
            continue
        if rel in VENDORED or rel.startswith("editor/tests/"):
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        yield rel, data.decode("utf-8", errors="ignore")


def test_no_tracked_file_brings_back_paid_gating():
    hits = []
    for rel, text in _tracked_text_files():
        for n, line in enumerate(text.splitlines(), 1):
            m = PAID_GATING.search(line)
            if m:
                hits.append(f"{rel}:{n}: {m.group(0)!r}")
    assert not hits, "paid-gating language in the public tree:\n" + "\n".join(hits[:50])


def test_the_scan_would_catch_it_coming_back():
    """Guards the pattern itself: a regex that matches nothing passes forever."""
    for probe in ("Upgrade to " + "Prem" + "ium", "ELEFANT_" + "LICENSE_TOKEN=abc",
                  "requires the " + "premium" + " tier", "paste your licen" + "se key",
                  "a licen" + "ce token", "Upgrade your Elefant licen" + "se to add more users",
                  "raise HTTPException(status_code" + "=402)", "honour" + "-system mode",
                  "honor" + " system"):
        assert PAID_GATING.search(probe), probe
    for allowed in ("Hot Tier", "the hot memory tier", "Licensed under the Apache License 2.0",
                    "see [LICENSE](LICENSE)", "Keep the license and NOTICE with any copies",
                    "status_code=403", "# noqa: E402", "Front matter `config` is honoured"):
        assert not PAID_GATING.search(allowed), allowed


def test_a_planted_upgrade_message_fails_the_scan(tmp_path):
    """Plant a paywall in a file and the walk plus the pattern find it."""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "planted.html").write_text(
        "<p>Upgrade to " + "Prem" + "ium to invite more people.</p>\n")
    (tmp_path / "clean.md").write_text("Every feature is included.\n")
    hits = [rel for rel, text in _tracked_text_files(tmp_path) if PAID_GATING.search(text)]
    assert hits == ["static/planted.html"]
