"""SEC-05: /api/internal/config must require the internal-service token.

The endpoint returns decrypted git-source credentials (include_token=True), so
an unauthenticated caller landing on the internal docker network — or any SSRF
that can reach http://editor:8080 directly — would otherwise exfiltrate every
stored PAT and the storage config. The gate mirrors memory-api's constant-time
X-Internal-Service check and fails closed.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_config_without_token_is_forbidden(client):
    r = client.get("/api/internal/config")
    assert r.status_code == 403


def test_config_with_wrong_token_is_forbidden(client):
    r = client.get("/api/internal/config",
                   headers={"X-Internal-Service": "definitely-not-the-token"})
    assert r.status_code == 403


def test_config_with_valid_token_ok(client):
    """The legitimate peer path (embedder) still works."""
    import internal_service
    r = client.get("/api/internal/config",
                   headers={"X-Internal-Service": internal_service.INTERNAL_SERVICE_TOKEN})
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
