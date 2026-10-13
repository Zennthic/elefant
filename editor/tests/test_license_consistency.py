"""Every place that names the project's license agrees with LICENSE.

The license is stated in six places — the LICENSE text, NOTICE, README,
CONTRIBUTING, two package.json files and two doc footers. They drifted apart
once already (the repo shipped an AGPL LICENSE while the docs and package
metadata said different things at different times), and a mismatched license
declaration is the kind of thing a downstream legal review stops on.
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

SPDX = "Apache-2.0"


def test_license_file_is_the_canonical_apache_text():
    text = (REPO / "LICENSE").read_text()
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    # The clauses that are the reason for choosing it over MIT.
    assert "3. Grant of Patent License." in text
    assert "patent litigation" in text


def test_notice_file_exists_and_names_the_holder():
    """Apache-2.0 §4(d): a NOTICE file, if present, must travel with copies.
    Shipping one is how the copyright holder is stated without a per-file
    header on ten thousand lines."""
    notice = (REPO / "NOTICE").read_text()
    assert "Zennthic LLC" in notice
    assert "Apache License" in notice


@pytest.mark.parametrize("pkg", ["memory-ui/package.json", "diagram-renderer/package.json",
                                 "diagram-validator/package.json"])
def test_package_metadata_declares_the_spdx_id(pkg):
    assert json.loads((REPO / pkg).read_text())["license"] == SPDX


@pytest.mark.parametrize("doc", ["README.md", "CONTRIBUTING.md"])
def test_governance_docs_name_the_license(doc):
    assert SPDX in (REPO / doc).read_text()


@pytest.mark.parametrize("guide", ["docs/onboarding/user-guide.html",
                                   "docs/onboarding/admin-guide.html"])
def test_onboarding_footers_name_the_license(guide):
    assert f"Open source ({SPDX})" in (REPO / guide).read_text()


def test_no_stale_agpl_references_anywhere():
    """The previous license. A leftover mention is worse than none — it tells a
    reader they may not do something this license explicitly allows."""
    stale = []
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"}
    for p in REPO.rglob("*"):
        if not p.is_file() or p.suffix.lower() in {".png", ".jpg", ".ico", ".woff2"}:
            continue
        if set(p.relative_to(REPO).parts) & skip_dirs:
            continue
        if p.name == Path(__file__).name:
            continue                      # this file names it on purpose
        try:
            body = p.read_text(errors="ignore")
        except Exception:
            continue
        if re.search(r"AGPL|Affero", body):
            stale.append(str(p.relative_to(REPO)))
    assert not stale, f"stale AGPL references: {stale}"
