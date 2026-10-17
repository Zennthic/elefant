"""A bare postgresql:// DSN connects through psycopg2, the driver the image ships.

SQLAlchemy 2.1 made psycopg 3 the default for that scheme. Without an explicit
driver, POSTGRES_EDITOR_DSN would stop connecting the day the pin moved, while
the suite (which runs on SQLite) stayed green.
"""
from config_db import engine_url


def test_a_bare_postgres_dsn_names_psycopg2():
    u = engine_url("postgresql://elefant:s3cret@db:5432/elefant")
    assert u.drivername == "postgresql+psycopg2"
    assert u.get_dialect().driver == "psycopg2"
    assert (u.username, u.password, u.host, u.port, u.database) == \
        ("elefant", "s3cret", "db", 5432, "elefant")


def test_a_dsn_that_names_its_driver_is_left_alone():
    assert engine_url("postgresql+psycopg://h/db").drivername == "postgresql+psycopg"


def test_sqlite_is_left_alone():
    assert engine_url("sqlite:////data/config.db").drivername == "sqlite"
