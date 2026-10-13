"""Test-time fixtures for editor/.

Sets up the env vars and filesystem paths editor/main.py reads at import
time so `from main import ...` works inside a clean CI runner with no
deployed stack.  Each module-level side effect in main.py (init_db,
ConfigManager, etc.) gets a temp-directory home so tests are hermetic.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest


# ── sys.path setup ────────────────────────────────────────────────────────
# Tests live at editor/tests/, the code at editor/.  Insert the parent
# so `import main` and `import config_db` resolve to the editor package.
_EDITOR_DIR = Path(__file__).resolve().parent.parent
if str(_EDITOR_DIR) not in sys.path:
    sys.path.insert(0, str(_EDITOR_DIR))

# editor/main.py mounts StaticFiles(directory="static") and expects the
# process's CWD to be editor/.  In production the Dockerfile WORKDIRs
# into /app where static/ exists.  Mirror that for tests.
os.chdir(_EDITOR_DIR)


# ── Env vars editor/main.py reads at import time ──────────────────────────
# All set per-session in a tmp dir so tests don't write to /docs or /data
# and don't share state with a running deploy.
_TMPDIR = tempfile.mkdtemp(prefix="elefant-editor-test-")
os.environ.setdefault("DOCS_DIR", f"{_TMPDIR}/docs")
os.environ.setdefault("DB_PATH", f"{_TMPDIR}/config.db")
os.environ.setdefault("CRYPTO_KEY_PATH", f"{_TMPDIR}/crypto.key")
os.environ.setdefault("BACKEND_READINESS_FILE", f"{_TMPDIR}/ready")
# Previewing a restore stages the uploaded bundle; without this it would
# stage into the deployment's real /data volume.
os.environ.setdefault("RESTORE_STAGING_DIR", f"{_TMPDIR}/restore-staging")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-deadbeef" * 2)
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-service-token-cafef00d")
os.environ.setdefault("ELEFANT_MODE", "multi-org")
os.environ.setdefault("EDITOR_EXTERNAL_URL", "https://test.example.com/editor")
os.environ.setdefault("APP_NAME", "Elefant-Test")
os.environ.setdefault("ELEFANT_S3_ACCESS_KEY", "test-s3-access-key")
os.environ.setdefault("ELEFANT_S3_SECRET_KEY", "test-s3-secret-key-deadbeefcafe")
os.environ.setdefault("ELEFANT_S3_ENDPOINT_URL", "http://test-minio:9000")
Path(_TMPDIR, "docs").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def tmp_env_dir():
    """The per-session tmp dir editor's import-time side effects landed in.
    Tests can write files into it without leaking outside the session."""
    return Path(_TMPDIR)
