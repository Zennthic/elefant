"""The public tree reads as a product, not as one deployment of it.

No host, person or tool from the environment Elefant is developed in may
appear in a tracked file. Examples use reserved names (example.com,
192.0.2.x) and neutral people (Jon, Jan, Ada).
"""
from pathlib import Path

from internal_names import INTERNAL_NAMES, LAB_HOST_MARKERS

REPO = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}

# Third-party bundles, which are not ours to edit.
VENDORED = {"memory-ui/static/tiptap.bundle.js", "memory-ui/static/marked.min.js",
            "memory-ui/static/mermaid.min.js"}


def _tracked_text_files():
    """Every text file in the checkout. A filesystem walk rather than
    `git ls-files`, so the scan cannot skip itself where git is unavailable."""
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO).as_posix()
        if not path.is_file() or set(path.relative_to(REPO).parts) & SKIP_DIRS:
            continue
        if rel in VENDORED:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        yield rel, data.decode("utf-8", errors="ignore")


def test_no_tracked_file_names_the_development_environment():
    hits = []
    for rel, text in _tracked_text_files():
        for n, line in enumerate(text.splitlines(), 1):
            m = INTERNAL_NAMES.search(line)
            if m:
                hits.append(f"{rel}:{n}: {m.group(0)!r}")
    assert not hits, "internal names in the public tree:\n" + "\n".join(hits[:50])


def test_no_tracked_file_names_a_lab_host():
    hits = [rel for rel, text in _tracked_text_files()
            if any(m in text.lower() for m in LAB_HOST_MARKERS)]
    assert not hits, f"lab hosts named in: {hits}"


def test_the_scan_would_catch_a_leak():
    """Guards the pattern itself: a regex that matches nothing passes forever."""
    for probe in ("T" + "SW-Docker01", "the smart " + "workshop", "ask J" + "oe", "j" + "oe@example.org",
                  "mkdir -p /home/" + "webservice/elefant",
                  "BASE_URL: https://elefant-" + "stage.zenn" + "thic.com",
                  "see docs." + "zenn" + "thic.com/install"):
        assert INTERNAL_NAMES.search(probe), probe
    assert not INTERNAL_NAMES.search("if name.startswith(prefix):")
    # Contact addresses and the licence URL are on the bare domain, not a host.
    for allowed in ("security@" + "zenn" + "thic.com", "conduct@" + "zenn" + "thic.com",
                    "trademarks@" + "zenn" + "thic.com", "* j" + "oe@" + "zenn" + "thic.com",
                    "Renew at https://" + "zenn" + "thic.com/license",
                    "ghcr.io/zennthic/elefant", "/home/user/elefant"):
        assert not INTERNAL_NAMES.search(allowed), allowed
