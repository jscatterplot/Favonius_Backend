"""Unit tests for the health-check fallback path.

Regression: the fallback used to instantiate a full ``TimescaleClient`` —
whose ``connect()`` spins up the 20-100 connection ``EnhancedConnectionPool``
just to run a ``SELECT 1``. On a Postgres instance near its
``max_connections`` ceiling, that disposable pool can be the difference
between a healthy boot and a connection-slot-exhaustion crash loop.

The new fallback opens a single ``asyncpg.connect`` and returns the slot
as soon as the probe is done.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.websocket_handler.config import (
    Config,
    MonitoringConfig,
    SupabaseConfig,
    TimescaleConfig,
    WebSocketConfig,
)
from src.websocket_handler.health_checks import _make_timescale_check


def _minimal_config() -> Config:
    """Build a Pydantic-valid Config that's just enough to exercise health checks."""
    return Config(
        environment="test",
        timescale=TimescaleConfig(
            service_url="postgresql://u:p@localhost:5432/tsdb",
            host="localhost",
            port=5432,
            database="tsdb",
            user="u",
            password="p",
            sslmode="disable",
        ),
        supabase=SupabaseConfig(
            url="https://example.supabase.co",
            anon_key="anon",
            service_key="service",
            db_host="localhost",
            db_user="u",
            db_password="p",
        ),
        websocket=WebSocketConfig(host="0.0.0.0", port=8080),
        monitoring=MonitoringConfig(),
    )


@pytest.mark.asyncio
async def test_timescale_check_uses_live_client_when_provided():
    """When a live client is supplied, the check delegates to its ``health_check``.

    No new connection is opened — the live pool serves the probe.
    """
    config = _minimal_config()
    live_client = MagicMock()
    live_client.health_check = AsyncMock(return_value=True)

    check_fn = _make_timescale_check(config, live_client)

    assert await check_fn() is True
    live_client.health_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_timescale_fallback_opens_single_connection_not_pool():
    """Fallback must call ``asyncpg.connect`` (one connection), not a pool."""
    config = _minimal_config()
    check_fn = _make_timescale_check(config, client=None)

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.close = AsyncMock()

    with (
        patch(
            "src.websocket_handler.health_checks.asyncpg.connect",
            new=AsyncMock(return_value=mock_conn),
        ) as mock_connect,
        patch(
            "src.websocket_handler.health_checks.asyncpg.create_pool",
            new=AsyncMock(),
        ) as mock_create_pool,
    ):
        result = await check_fn()

    assert result is True
    mock_connect.assert_awaited_once()
    mock_create_pool.assert_not_called()
    mock_conn.fetchval.assert_awaited_once_with("SELECT 1")
    mock_conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_timescale_fallback_returns_false_on_connect_failure():
    """Connection failure (e.g. exhausted slots) is caught and reported as unhealthy."""
    config = _minimal_config()
    check_fn = _make_timescale_check(config, client=None)

    with patch(
        "src.websocket_handler.health_checks.asyncpg.connect",
        new=AsyncMock(side_effect=ConnectionError("slots exhausted")),
    ):
        result = await check_fn()

    assert result is False


@pytest.mark.asyncio
async def test_timescale_fallback_closes_conn_even_when_query_fails():
    """Half-open connections must not leak when the SELECT raises after connect."""
    config = _minimal_config()
    check_fn = _make_timescale_check(config, client=None)

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=RuntimeError("network reset"))
    mock_conn.close = AsyncMock()

    with patch(
        "src.websocket_handler.health_checks.asyncpg.connect",
        new=AsyncMock(return_value=mock_conn),
    ):
        result = await check_fn()

    assert result is False
    mock_conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_timescale_fallback_returns_false_when_select_returns_unexpected():
    """A ``SELECT 1`` that returns anything but ``1`` is treated as unhealthy."""
    config = _minimal_config()
    check_fn = _make_timescale_check(config, client=None)

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.close = AsyncMock()

    with patch(
        "src.websocket_handler.health_checks.asyncpg.connect",
        new=AsyncMock(return_value=mock_conn),
    ):
        result = await check_fn()

    assert result is False
    mock_conn.close.assert_awaited_once()
