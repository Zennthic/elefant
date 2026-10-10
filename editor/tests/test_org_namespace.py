"""The GitHub org slug is `Zennthic`, and nothing may reintroduce the old one.

The org was renamed Zennthic-LLC → Zennthic on 2026-09-02. GitHub redirects git
and the web, so a stale reference does not look broken — it fails later and
quietly: a GHCR push refused as cross-namespace on the next build, or an image
that pulls fine from cached layers until a pod moves to a new node overnight.
That delay is the whole reason this is worth a test rather than a careful eye.

`*.zennthic.com` is the DNS domain and deliberately stays: a regex on
"zennthic" rather than the full old slug would break it.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"}

# Files allowed to name the old slug.
ALLOWED: set[str] = set()


def _repo_files():
    for p in REPO.rglob("*"):
        if not p.is_file() or p.suffix.lower() in {".png", ".jpg", ".ico", ".woff2"}:
            continue
        if set(p.relative_to(REPO).parts) & SKIP_DIRS:
            continue
        yield p


def test_no_stale_org_slug_anywhere():
    stale = []
    for p in _repo_files():
        rel = str(p.relative_to(REPO))
        if rel in ALLOWED or rel == str(Path(__file__).relative_to(REPO)):
            continue
        try:
            body = p.read_text(errors="ignore")
        except Exception:
            continue
        if re.search(r"zennthic-llc", body, re.I):
            stale.append(rel)
    assert not stale, (
        f"these still name the old GitHub org: {stale}. "
        f"Rewrite ghcr.io/zennthic-llc/ → ghcr.io/zennthic/ and "
        f"github.com/Zennthic-LLC/ → github.com/Zennthic/")


def test_the_dns_domain_was_not_swept_up():
    """`*.zennthic.com` is where the product actually answers — public URLs,
    allowed-host lists, OAuth resource identifiers. It has nothing to do with
    the org slug, and a too-broad rewrite here breaks logins."""
    hits = sum(1 for p in _repo_files()
               if "zennthic.com" in p.read_text(errors="ignore"))
    assert hits > 0, "the DNS domain has gone missing — was it rewritten by mistake?"


def test_published_image_paths_use_the_new_namespace():
    """The GHCR push is the step that fails first after a rename: the build
    token is minted for the repo's current org, so pushing an old-namespace
    path is cross-namespace and refused."""
    wf = (REPO / ".github" / "workflows" / "build-deploy.yml").read_text()
    assert "IMAGE_BASE: ghcr.io/zennthic/elefant" in wf

    compose = (REPO / "docker-compose.yml").read_text()
    images = re.findall(r"image:\s*\$?\{?[^}\n]*?(ghcr\.io/[^\s:$}]+)", compose)
    assert images, "no GHCR images found in docker-compose.yml — did the format change?"
    for img in images:
        assert img.startswith("ghcr.io/zennthic/elefant/"), img


def test_the_legal_entity_name_is_untouched():
    """Renaming a GitHub org does not dissolve the company. `Zennthic LLC`
    remains the copyright holder and the named data processor."""
    assert "Zennthic LLC" in (REPO / "NOTICE").read_text()
    assert "Zennthic LLC" in (REPO / "README.md").read_text()
