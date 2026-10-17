"""Test-time fixtures for memory-api/.

memory-api connects to PostgreSQL and Qdrant at startup.  Pure unit tests
of helpers + the auth middleware don't need either — we mock both at the
function level via fixtures here.  Real RLS integration tests would
override the postgres host to a CI-side container.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── sys.path setup ────────────────────────────────────────────────────────
_MEMORY_API_DIR = Path(__file__).resolve().parent.parent
if str(_MEMORY_API_DIR) not in sys.path:
    sys.path.insert(0, str(_MEMORY_API_DIR))


# ── Env vars main.py reads at import time ────────────────────────────────
os.environ.setdefault("POSTGRES_HOST", "test-postgres")
os.environ.setdefault("POSTGRES_DB", "test_memory")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password-deadbeef")
os.environ.setdefault("QDRANT_HOST", "test-qdrant")
os.environ.setdefault("EMBEDDER_URL", "http://test-embedder:8001")
os.environ.setdefault("EDITOR_URL", "http://test-editor:8080")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-service-token-cafef00d")


# ── Shared assertion helper ───────────────────────────────────────────────

def _balanced(src: str, open_at: int) -> tuple:
    """The text inside the parenthesis starting at `open_at`, and the index
    after its close. Parenthesis-aware because a VALUES list contains casts —
    `CAST(:aliases AS text[])` — and a naive scan to the first ')' would cut
    the list in half at the first one."""
    assert src[open_at] == "(", "expected a parenthesis"
    depth, i = 0, open_at
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_at + 1:i], i + 1
        i += 1
    raise AssertionError("unbalanced parentheses in the INSERT")


def _split_top_level(text: str) -> list:
    """Split on commas that are not inside a parenthesis, so a cast stays one
    item rather than becoming two."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [" ".join(p.split()) for p in out]


def card_insert_lists(src: str) -> tuple:
    """The (columns, values) of a literal `INSERT INTO cards (...) VALUES (...)`.

    Every card field has to appear in BOTH lists or it is silently dropped on
    write, and both lists are written by hand — so several test modules assert
    against them. They do it through this rather than against a literal run of
    adjacent column names, because the next field added to a card lands between
    two existing ones and would fail every such assertion at once, reporting
    the column order rather than a bug.
    """
    at = src.find("INSERT INTO cards")
    assert at != -1, "no literal INSERT INTO cards found"
    cols, after = _balanced(src, src.index("(", at))
    values_at = src.index("VALUES", after)
    values, _ = _balanced(src, src.index("(", values_at))
    return _split_top_level(cols), _split_top_level(values)


# ── The real-PostgreSQL integration modules ───────────────────────────────

def pg_engine_url(dsn: str):
    """The test DSN with the driver the service uses (see main.engine_url).
    Kept here rather than imported from main, which needs its env to import."""
    from sqlalchemy.engine import make_url
    u = make_url(dsn)
    return u.set(drivername="postgresql+psycopg2") if u.drivername == "postgresql" else u


def _pg_reachable(dsn: str) -> bool:
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(pg_engine_url(dsn))
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in ("true", "1", "yes")


def require_postgres(dsn: str) -> None:
    """Gate a `*_integration.py` module on a reachable PostgreSQL.

    Call it at module level. On a laptop with no database it skips the module,
    which is what a contributor running the unit suite wants. Under CI it FAILS
    instead: for a long time nothing in CI set ELEFANT_TEST_PG_DSN, so every one
    of these modules skipped, the job was green, and the board's real-database
    behaviour was never tested. A green run that tested nothing must not be
    possible again, so in CI a missing or unreachable database is an error.

    The DSN itself is never printed: it may carry a password.
    """
    if dsn and _pg_reachable(dsn):
        return
    why = ("ELEFANT_TEST_PG_DSN is not set" if not dsn
           else "no PostgreSQL answers at ELEFANT_TEST_PG_DSN")
    if _in_ci():
        pytest.fail(f"{why}, and under CI the PostgreSQL integration tests must "
                    "run rather than skip (see CONTRIBUTING.md)", pytrace=False)
    pytest.skip(f"no PostgreSQL for the integration tests ({why})",
                allow_module_level=True)


@pytest.fixture
def mock_engine(monkeypatch):
    """Replace get_engine() with a mock so handlers that try to open a
    PostgreSQL connection during a unit test don't reach out.  The
    returned mock's `.connect()` returns a contextmanager-shaped mock
    so `with engine.connect() as conn:` patterns work."""
    import main
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    return engine, conn


@pytest.fixture
def mock_qdrant(monkeypatch):
    """Same idea for Qdrant — handlers that touch the vector store get
    a mock they can be asserted against."""
    import main
    mock = MagicMock()
    monkeypatch.setattr(main, "get_qdrant", lambda: mock)
    return mock
