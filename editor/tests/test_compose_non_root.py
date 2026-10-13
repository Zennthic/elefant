"""docker-compose.yml agrees with the non-root images (card #299).

The images moved to UID/GID 10001 (nginx to 101, listening on 8080) in CQ-03.
A compose file that still published nginx's port 80, pointed Traefik at 80, or
started the app services on root-owned volumes took down a production install
on its upgrade. Each of those is a one-line regression nobody notices until a
deploy, so these tests hold them.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "docker-compose.yml"
TRAEFIK = REPO / "docker-compose.traefik.yml"

# Volumes the third-party images own, with their own UIDs. volume-perms must
# never chown them.
THIRD_PARTY_VOLUMES = {"postgres_data", "minio_data", "qdrant_data"}

pytestmark = pytest.mark.skipif(
    not COMPOSE.exists(), reason="the repo root is not checked out beside the editor"
)


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def _named_volumes(svc):
    return {v.split(":", 1)[0] for v in svc.get("volumes", []) if isinstance(v, str)}


def test_no_obsolete_version_key():
    assert "version" not in _compose()


def test_nginx_publishes_its_unprivileged_port():
    ports = _compose()["services"]["nginx"]["ports"]
    assert ports == ["${PORT:-8321}:8080"]


def test_traefik_points_at_the_unprivileged_port():
    labels = yaml.safe_load(TRAEFIK.read_text())["services"]["nginx"]["labels"]
    port_labels = [l for l in labels if ".loadbalancer.server.port=" in l]
    assert port_labels == ["traefik.http.services.wiki.loadbalancer.server.port=8080"]


def test_volume_perms_covers_every_app_volume_and_no_third_party_one():
    services = _compose()["services"]
    perms = _named_volumes(services["volume-perms"])
    app = set()
    for name, svc in services.items():
        if name != "volume-perms" and svc.get("image", "").startswith("ghcr.io/zennthic/elefant/"):
            app |= _named_volumes(svc)
    assert app <= perms, f"not chowned on upgrade: {sorted(app - perms)}"
    assert not perms & THIRD_PARTY_VOLUMES


def test_volume_perms_runs_as_root_once_and_touches_nothing_else():
    perms = _compose()["services"]["volume-perms"]
    assert perms["user"] == "0:0"
    assert perms["network_mode"] == "none"
    assert perms["restart"] == "no"
    script = "".join(perms["command"])
    assert "10001:10001" in script
    # Ownership only. Regenerating or deleting crypto.key would orphan every
    # encrypted backup and secret.
    for forbidden in ("crypto.key", "rm ", "chmod"):
        assert forbidden not in script


def test_services_with_app_volumes_wait_for_volume_perms():
    services = _compose()["services"]
    for name, svc in services.items():
        if name == "volume-perms" or not svc.get("image", "").startswith("ghcr.io/zennthic/elefant/"):
            continue
        if not _named_volumes(svc):
            continue
        deps = svc.get("depends_on")
        assert isinstance(deps, dict), f"{name}: depends_on must be the long form"
        assert deps.get("volume-perms") == {"condition": "service_completed_successfully"}, name
