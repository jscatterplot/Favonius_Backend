"""Unit tests for ``db.postgres_url`` helpers."""

import ssl

import pytest

from src.db.postgres_url import (
    build_postgres_dsn,
    describe_database_target,
    is_postgres_url,
    merge_timescale_params_from_url,
    parse_postgres_connection_url,
    prepare_asyncpg_url_and_ssl,
    ssl_context_for_postgres_sslmode,
)


def test_is_postgres_url() -> None:
    assert is_postgres_url("postgresql://h/db") is True
    assert is_postgres_url("postgres://h/db") is True
    assert is_postgres_url(None) is False
    assert is_postgres_url("mysql://h/db") is False


def test_parse_postgres_connection_url_basic() -> None:
    p = parse_postgres_connection_url("postgresql://user:pass@db.example.com:31413/tsdb?sslmode=require")
    assert p is not None
    assert p["host"] == "db.example.com"
    assert p["port"] == 31413
    assert p["database"] == "tsdb"
    assert p["user"] == "user"
    assert p["password"] == "pass"
    assert p["sslmode"] == "require"


def test_parse_postgres_connection_url_default_port_and_sslmode() -> None:
    p = parse_postgres_connection_url("postgresql://u:p@host/mydb")
    assert p is not None
    assert p["port"] == 5432
    assert p["sslmode"] == "require"


def test_merge_timescale_params_from_url_fills_host() -> None:
    url = "postgresql://tiger:secret@cloud.tsdb.example:11111/tsdb?sslmode=verify-full"
    h, port, db, user, pw, sm = merge_timescale_params_from_url(
        service_url=url,
        host=None,
        port=5432,
        database=None,
        user=None,
        password=None,
        sslmode="require",
        pgport_explicit=False,
    )
    assert h == "cloud.tsdb.example"
    assert port == 11111
    assert db == "tsdb"
    assert user == "tiger"
    assert pw == "secret"
    # Discrete sslmode wins over the URI when non-empty (matches WS handler PGSSLMODE default).
    assert sm == "require"


def test_merge_timescale_params_from_url_env_overrides() -> None:
    url = "postgresql://a:b@h:1/db?sslmode=require"
    h, port, db, user, pw, sm = merge_timescale_params_from_url(
        service_url=url,
        host="override-host",
        port=9999,
        database="other",
        user="u2",
        password="p2",
        sslmode="prefer",
        pgport_explicit=True,
    )
    assert h == "override-host"
    assert port == 9999
    assert db == "other"
    assert user == "u2"
    assert pw == "p2"
    assert sm == "prefer"


def test_build_postgres_dsn_encodes_password() -> None:
    dsn = build_postgres_dsn(
        host="h",
        port=5432,
        database="d",
        user="u@x",
        password="p/w",
        sslmode="require",
    )
    assert "u%40x" in dsn
    assert "p%2Fw" in dsn
    assert "sslmode=require" in dsn


@pytest.mark.parametrize("mode", ["require", "allow", "prefer"])
def test_ssl_context_for_postgres_sslmode_lenient_modes(mode: str) -> None:
    ctx = ssl_context_for_postgres_sslmode(mode)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE


def test_ssl_context_for_postgres_sslmode_disable() -> None:
    assert ssl_context_for_postgres_sslmode("disable") is False


def test_ssl_context_for_postgres_sslmode_verify_full() -> None:
    ctx = ssl_context_for_postgres_sslmode("verify-full")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_prepare_asyncpg_url_and_ssl_strip_require() -> None:
    raw = "postgresql://u:p@host:5432/db?sslmode=require"
    clean, ssl_arg = prepare_asyncpg_url_and_ssl(raw)
    assert "sslmode" not in clean
    assert isinstance(ssl_arg, ssl.SSLContext)
    assert ssl_arg.verify_mode == ssl.CERT_NONE


def test_prepare_asyncpg_url_and_ssl_disable() -> None:
    raw = "postgresql://u:p@host/db?sslmode=disable"
    clean, ssl_arg = prepare_asyncpg_url_and_ssl(raw)
    assert ssl_arg is False
    assert "sslmode" not in clean


def test_prepare_asyncpg_url_and_ssl_no_query() -> None:
    raw = "postgresql://u:p@host/db"
    clean, ssl_arg = prepare_asyncpg_url_and_ssl(raw)
    assert clean == raw
    assert ssl_arg is None


def test_prepare_asyncpg_url_and_ssl_second_param() -> None:
    raw = "postgresql://u:p@host/db?application_name=a&sslmode=require"
    clean, ssl_arg = prepare_asyncpg_url_and_ssl(raw)
    assert "sslmode" not in clean
    assert "application_name=a" in clean
    assert isinstance(ssl_arg, ssl.SSLContext)


def test_describe_database_target_masks_user_password_db() -> None:
    """Redact user/password/db; expose only port and a TLD-suffix host."""
    desc = describe_database_target(
        "postgresql://tsdbadmin:rotated_secret@abc123.tsdb.cloud.timescale.com:31413/tsdb?sslmode=require"
    )
    assert "tsdbadmin" not in desc
    assert "rotated_secret" not in desc
    assert "tsdb" not in desc.split("port=")[0]  # db name not before "port="
    assert "abc123" not in desc
    assert "port=31413" in desc
    assert "*****.cloud.timescale.com" in desc or "*****.timescale.com" in desc


def test_describe_database_target_short_host() -> None:
    """Hosts with no dotted suffix still mask cleanly without crashing."""
    desc = describe_database_target("postgresql://u:p@localhost:5432/db")
    assert "u" not in desc.split("port=")[0].replace("user=", "")
    assert "p" not in desc.split("port=")[0].replace("user=", "")
    assert "*****" in desc
    assert "port=5432" in desc
