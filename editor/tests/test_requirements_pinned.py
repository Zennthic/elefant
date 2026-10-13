"""Every service pins its direct Python dependencies exactly (CQ-01).

A `>=` or a `==1.*` means a fresh build installs whatever is newest that day,
so a public user building from source gets different code from what CI tested
and stage runs. mcp learned this the hard way when an unpinned SDK resolved to
2.0.0 and broke the import. Nothing fails when a float creeps back in, so this
does.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
SERVICES = ("editor", "embedder", "mcp", "memory-api")

pytestmark = pytest.mark.skipif(
    not all((REPO / svc).is_dir() for svc in SERVICES),
    reason="the other services are not checked out beside the editor",
)

# name, optional [extras], then exactly `==` and a version with no wildcard.
EXACT = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!-]+$")


def _requirement_files():
    return sorted(p for svc in SERVICES for p in (REPO / svc).glob("requirements*.txt"))


def _requirements(path):
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def test_every_service_has_a_requirements_file():
    for svc in SERVICES:
        assert (REPO / svc / "requirements.txt").is_file(), svc


@pytest.mark.parametrize("path", _requirement_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_direct_dependencies_are_pinned_exactly(path):
    loose = [req for req in _requirements(path) if not EXACT.match(req)]
    assert not loose, f"{path.relative_to(REPO)} has unpinned requirements: {loose}"


def test_torch_is_pinned_in_the_embedder_dockerfile():
    """torch comes from PyTorch's CPU index, so it is installed by the
    Dockerfile rather than from requirements-local.txt. It is pinned there."""
    dockerfile = (REPO / "embedder" / "Dockerfile").read_text()
    assert re.search(r"download\.pytorch\.org/whl/cpu\s+torch==\d+\.\d+\.\d+\b", dockerfile)


def test_dependabot_bumps_each_service_separately():
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    pip = [u for u in config["updates"] if u["package-ecosystem"] == "pip"]
    dirs = {u["directory"] for u in pip}
    assert dirs == {f"/{svc}" for svc in SERVICES}
    for update in pip:
        assert update["schedule"]["interval"] == "weekly"
        assert update.get("groups"), f"{update['directory']} should group its bumps"
