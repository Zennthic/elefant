"""Tests for embedder/main.py::_assert_payload_tenant — the defence-in-
depth check that runs on retrieve-by-id (GET/PUT/DELETE /api/memories/
{id}).  Catches the routing bug where _resolve_tenant_slug picks the
right collection but Qdrant returns a memory stamped for a different
tenant.  Returns 404 (not 403) to avoid leaking existence."""
import pytest
from fastapi import HTTPException


@pytest.fixture
def assert_tenant():
    import main
    return main._assert_payload_tenant


def test_matching_tenant_passes(assert_tenant):
    assert_tenant({"tenant_id": "acme", "content": "x"}, "acme", "mem-1")


def test_mismatched_tenant_raises_404(assert_tenant):
    with pytest.raises(HTTPException) as exc:
        assert_tenant({"tenant_id": "victim", "content": "x"}, "acme", "mem-1")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


def test_legacy_payload_without_tenant_id_passes(assert_tenant):
    """Memories written before the tenant_id stamp are tolerated —
    they live in the right collection by virtue of collection naming."""
    assert_tenant({"content": "legacy"}, "acme", "legacy-1")


def test_none_payload_is_treated_as_legacy(assert_tenant):
    """Defensive: a None payload shouldn't blow up — handler will
    deal with it as 'memory not found' elsewhere."""
    assert_tenant(None, "acme", "lost-1")


def test_explicit_none_tenant_id_is_legacy(assert_tenant):
    """tenant_id=None on a payload is the same as missing — legacy."""
    assert_tenant({"tenant_id": None}, "acme", "mem-1")
