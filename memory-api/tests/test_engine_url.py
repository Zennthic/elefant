"""A bare postgresql:// DSN connects through psycopg2, the driver the image ships.

SQLAlchemy 2.1 made psycopg 3 the default for that scheme, which memory-api
does not install, so every connection failed once the pin moved.
"""
import main
from conftest import pg_engine_url


def test_the_service_url_names_psycopg2():
    u = main.engine_url(main.DATABASE_URL)
    assert u.drivername == "postgresql+psycopg2"
    assert u.get_dialect().driver == "psycopg2"


def test_a_password_with_reserved_characters_survives():
    u = main.engine_url("postgresql://memory:p%40ss%2Fw0rd@db:5432/memory")
    assert u.password == "p@ss/w0rd"


def test_a_dsn_that_names_its_driver_is_left_alone():
    assert main.engine_url("postgresql+psycopg://h/db").drivername == "postgresql+psycopg"


def test_the_integration_dsn_gets_the_same_driver():
    assert pg_engine_url("postgresql://postgres@/postgres?host=/tmp&port=5599").drivername \
        == "postgresql+psycopg2"
