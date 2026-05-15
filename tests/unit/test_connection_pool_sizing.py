"""Regression tests for ``EnhancedConnectionPool`` connection sizing.

Background: ``_create_sqlalchemy_engine`` used to size its QueuePool to
``pool_size=min_connections, max_overflow=max_connections - min_connections``
— mirroring the asyncpg pool's sizing. That doubled the per-replica
TimescaleDB connection budget without benefit (the SQLAlchemy engine is
only used by a handful of cold pandas reporting calls in
``timescale_client.py``). Under load, Postgres rejected acquires with
SQLSTATE 53300 ("remaining connection slots are reserved for ...
pg_use_reserved_connections"), surfacing as the periodic data_sync ERROR
this branch fixes.

The fix: cap the SQLAlchemy engine small and independent
(``pool_size=1, max_overflow=4`` by default, overridable via
``SQLALCHEMY_POOL_SIZE`` / ``SQLALCHEMY_MAX_OVERFLOW``).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.connection_pool import EnhancedConnectionPool, open_dedicated_connection


@pytest.fixture
def config():
    """A config that intentionally requests a fat asyncpg pool.

    The old behaviour would have used the same numbers for the SQLAlchemy
    QueuePool. The fix decouples them.
    """
    return TimescaleConfig(
        service_url="postgresql://test:test@localhost:5432/testdb",
        host="localhost",
        port=5432,
        database="testdb",
        user="test",
        password="test",
        max_connections=100,
        pool_size=20,
    )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_sqlalchemy_pool_does_not_inherit_asyncpg_sizing(config, monkeypatch):
    """The SQLAlchemy engine must use small fixed defaults regardless of asyncpg sizing."""
    monkeypatch.delenv("SQLALCHEMY_POOL_SIZE", raising=False)
    monkeypatch.delenv("SQLALCHEMY_MAX_OVERFLOW", raising=False)

    captured: dict = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return MagicMock()

    pool = EnhancedConnectionPool(config)
    # Skip the asyncpg pool — we only care about the SQLAlchemy sizing here.
    with patch(
        "src.websocket_handler.connection_pool.create_engine", side_effect=fake_create_engine
    ):
        await pool._create_sqlalchemy_engine()

    assert captured["kwargs"]["pool_size"] == 1
    assert captured["kwargs"]["max_overflow"] == 4
    # The fat asyncpg numbers must not leak in.
    assert captured["kwargs"]["pool_size"] != config.pool_size
    assert captured["kwargs"]["max_overflow"] != config.max_connections - config.pool_size


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_sqlalchemy_pool_honours_env_overrides(config, monkeypatch):
    """Operators can raise the SQLAlchemy pool size via env vars without touching code."""
    monkeypatch.setenv("SQLALCHEMY_POOL_SIZE", "3")
    monkeypatch.setenv("SQLALCHEMY_MAX_OVERFLOW", "7")

    captured: dict = {}

    def fake_create_engine(url, **kwargs):
        captured["kwargs"] = kwargs
        return MagicMock()

    pool = EnhancedConnectionPool(config)
    with patch(
        "src.websocket_handler.connection_pool.create_engine", side_effect=fake_create_engine
    ):
        await pool._create_sqlalchemy_engine()

    assert captured["kwargs"]["pool_size"] == 3
    assert captured["kwargs"]["max_overflow"] == 7


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_open_dedicated_connection_uses_config_credentials(config):
    """``open_dedicated_connection`` must pass through the TimescaleConfig fields.

    The LISTEN consumers (alerts dispatcher, charging_command_queue) use
    this helper to open a single asyncpg connection that sits outside the
    pool. The contract is: connection credentials and SSL mode come from
    the same TimescaleConfig as the pool itself, so an operator only has
    to rotate one set of secrets.
    """
    captured: dict = {}
    fake_conn = MagicMock()

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return fake_conn

    with patch(
        "src.websocket_handler.connection_pool.asyncpg.connect", side_effect=fake_connect
    ):
        conn = await open_dedicated_connection(config)

    assert conn is fake_conn
    assert captured["host"] == config.host
    assert captured["port"] == config.port
    assert captured["database"] == config.database
    assert captured["user"] == config.user
    assert captured["password"] == config.password
    # SSL context is built from sslmode and must be present (not just True/False);
    # exact type is provided by ssl_context_for_postgres_sslmode.
    assert "ssl" in captured


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_sqlalchemy_engine_failure_does_not_raise(config, monkeypatch):
    """Existing behaviour: a SQLAlchemy engine creation failure is logged, not raised.

    The asyncpg pool is the hot path; the engine is reporting-only. A bad
    URL or driver issue must not block startup.
    """
    monkeypatch.delenv("SQLALCHEMY_POOL_SIZE", raising=False)
    monkeypatch.delenv("SQLALCHEMY_MAX_OVERFLOW", raising=False)

    def boom(url, **kwargs):
        raise RuntimeError("boom")

    pool = EnhancedConnectionPool(config)
    with patch("src.websocket_handler.connection_pool.create_engine", side_effect=boom):
        await pool._create_sqlalchemy_engine()

    assert pool.sqlalchemy_engine is None
