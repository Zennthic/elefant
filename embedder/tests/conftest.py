"""Test-time fixtures for embedder/.

Wires sys.path and env vars so `from main import ...` works in CI.  The
embedder also imports `qdrant-client` and `sentence-transformers` at
module load, both of which are heavy; tests that don't need them mock
them via the patches in this conftest.  Tests that DO need a real Qdrant
should be marked `@pytest.mark.integration` and skipped by default.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── sys.path setup ────────────────────────────────────────────────────────
_EMBEDDER_DIR = Path(__file__).resolve().parent.parent
if str(_EMBEDDER_DIR) not in sys.path:
    sys.path.insert(0, str(_EMBEDDER_DIR))


# ── Env vars main.py reads at import time ────────────────────────────────
_TMPDIR = tempfile.mkdtemp(prefix="elefant-embedder-test-")
os.environ.setdefault("DOCS_DIR", f"{_TMPDIR}/docs")
os.environ.setdefault("QDRANT_HOST", "test-qdrant")
os.environ.setdefault("QDRANT_PORT", "6333")
os.environ.setdefault("BACKEND_READINESS_FILE", f"{_TMPDIR}/ready")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-service-token-cafef00d")
os.environ.setdefault("EMBEDDING_PROVIDER", "local")
os.environ.setdefault("EMBEDDER_URL", "http://test-editor:8080")
os.environ.setdefault("MEMORY_API_URL", "http://test-memory-api:8002")
os.environ.setdefault("EDITOR_URL", "http://test-editor:8080")
os.environ.setdefault("MULTI_TENANT_DOCS", "true")
Path(_TMPDIR, "docs").mkdir(parents=True, exist_ok=True)
# Touch the readiness file so the embedder doesn't block waiting for it.
Path(_TMPDIR, "ready").write_text("ready")


# ── Pre-import patches for heavy deps ────────────────────────────────────
# These keep `import main` cheap so unit tests of pure helpers don't pay
# the 2GB sentence-transformers download cost in CI.

# Stub qdrant_client.QdrantClient — main.py instantiates it lazily, but
# also imports types eagerly.  Real Qdrant integration tests would
# replace this with a real client against a docker'd Qdrant.
_qdrant_stub = MagicMock()
_qdrant_stub.QdrantClient = MagicMock
_qdrant_stub.models = MagicMock()


@pytest.fixture
def mock_qdrant(monkeypatch):
    """Replace the embedder's qdrant() factory with a magic mock so a
    test can assert on the calls made (upsert, search, retrieve) without
    a running Qdrant. Returns the mock so the test can configure side
    effects."""
    import main
    mock = MagicMock()
    monkeypatch.setattr(main, "qdrant", lambda: mock)
    return mock
