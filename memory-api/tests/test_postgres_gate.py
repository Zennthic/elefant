"""The PostgreSQL integration modules may skip on a laptop, never under CI.

For a long time nothing in CI set ELEFANT_TEST_PG_DSN, so all eight
`*_integration.py` modules skipped and the memory-api job stayed green while
the board's real-database behaviour went untested. `require_postgres` is the
gate that makes that impossible, and these pin both halves of it: the gate
itself, and that every integration module goes through it.
"""
from pathlib import Path

import pytest

import conftest
from conftest import require_postgres

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def unreachable(monkeypatch):
    monkeypatch.setattr(conftest, "_pg_reachable", lambda dsn: False)


@pytest.mark.parametrize("dsn", ["", "postgresql://nobody@127.0.0.1:1/x"])
def test_no_database_under_ci_is_a_failure(monkeypatch, unreachable, dsn):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception) as exc:
        require_postgres(dsn)
    assert "ELEFANT_TEST_PG_DSN" in str(exc.value)
    assert "nobody" not in str(exc.value), "the DSN may carry a password"


@pytest.mark.parametrize("dsn", ["", "postgresql://nobody@127.0.0.1:1/x"])
def test_no_database_on_a_laptop_is_a_skip(monkeypatch, unreachable, dsn):
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_postgres(dsn)


def test_a_reachable_database_passes_the_gate(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(conftest, "_pg_reachable", lambda dsn: True)
    require_postgres("postgresql://postgres@db/postgres")


def test_every_integration_module_goes_through_the_gate():
    modules = sorted(TESTS.glob("*_integration.py"))
    assert len(modules) >= 8, f"expected the eight integration modules, found {len(modules)}"
    for path in modules:
        src = path.read_text()
        if "ELEFANT_TEST_PG_DSN" not in src:
            continue
        assert "require_postgres(DSN)" in src, f"{path.name} does not call require_postgres"
        assert "pytest.skip(" not in src, \
            f"{path.name} skips on its own, which CI would read as green"
