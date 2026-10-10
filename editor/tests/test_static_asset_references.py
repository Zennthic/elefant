"""Every local asset a page references must actually exist.

Two dead references shipped for some time before this guard: mfa.html linked
`auth.css`, and admin.html declared an `og:image` of `/marketing/og-default.svg`
— neither file exists anywhere in the repo, so both 404'd on every load. They
are invisible in review and invisible at runtime unless you watch the network
tab, which is exactly the kind of rot a test should hold down.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
ROOTS = [REPO / "editor" / "static", REPO / "memory-ui" / "static"]

# Server routes and third-party URLs are not files on disk.
SKIP_PREFIX = ("http://", "https://", "//", "data:", "#", "mailto:",
               "/api/", "/editor/", "/platform/", "/auth/", "/oauth/",
               "/share/", "/logout", "/profile", "/login")

ATTR = re.compile(r'(?:src|href|content)\s*=\s*"([^"]+\.(?:svg|png|jpg|jpeg|gif|css|js|webp|ico))"')

PAGES = sorted(p for root in ROOTS for p in root.glob("*.html"))


def _resolve(ref: str, page: Path):
    """Where a reference should land, given the page that makes it."""
    if ref.startswith("/"):
        # Absolute: served from either static root depending on the service.
        return [root / ref.lstrip("/") for root in ROOTS]
    return [page.parent / ref]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: f"{p.parent.parent.parent.name}/{p.name}")
def test_referenced_assets_exist(page):
    missing = []
    for ref in ATTR.findall(page.read_text(errors="replace")):
        if ref.startswith(SKIP_PREFIX):
            continue
        if not any(c.is_file() for c in _resolve(ref, page)):
            missing.append(ref)
    assert not missing, f"{page.name} references files that do not exist: {sorted(set(missing))}"
