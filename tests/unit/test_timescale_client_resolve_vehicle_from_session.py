"""Unit tests for ``TimescaleClient._resolve_vehicle_id_from_session``.

Upstream callers in ``ocpp16_adapter`` pass either a UUID
``charging_sessions.session_id`` PK or the OCPP 1.6 integer
``transaction_id`` (e.g. ``'1'``, ``'2'``) under the same dict key. The
resolver must dispatch to the matching column instead of letting an int
hit a UUID-typed column and raise ``invalid UUID '1'``, which collapsed
the whole telemetry batch in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


def _client():
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    return TimescaleClient(config)


@pytest.mark.asyncio
async def test_resolves_by_transaction_id_when_input_is_numeric():
    """Numeric input → query by ``transaction_id`` (BIGINT)."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"vehicle_id": "veh-1"})
    client = _client()

    result = await client._resolve_vehicle_id_from_session(conn, "1")

    assert result == "veh-1"
    sql = conn.fetchrow.await_args.args[0]
    assert "transaction_id = $1" in sql
    assert conn.fetchrow.await_args.args[1] == 1  # int, not str


@pytest.mark.asyncio
async def test_resolves_by_session_id_when_input_is_uuid():
    """UUID-shaped input → query by ``session_id`` PK."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"vehicle_id": "veh-2"})
    client = _client()

    result = await client._resolve_vehicle_id_from_session(
        conn, "11111111-2222-3333-4444-555555555555"
    )

    assert result == "veh-2"
    sql = conn.fetchrow.await_args.args[0]
    assert "session_id = $1::uuid" in sql


@pytest.mark.asyncio
async def test_returns_none_when_no_row_found():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client()

    assert await client._resolve_vehicle_id_from_session(conn, "999") is None


@pytest.mark.asyncio
async def test_returns_none_when_session_id_blank():
    conn = AsyncMock()
    client = _client()

    assert await client._resolve_vehicle_id_from_session(conn, None) is None
    assert await client._resolve_vehicle_id_from_session(conn, "") is None
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_for_unparseable_input():
    """Garbage that's neither UUID nor int → silent None, no DB call."""
    conn = AsyncMock()
    client = _client()

    assert await client._resolve_vehicle_id_from_session(conn, "not-a-uuid-or-int") is None
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_row_has_null_vehicle():
    """Override-mode session has NULL vehicle_id; resolver must return None."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"vehicle_id": None})
    client = _client()

    assert await client._resolve_vehicle_id_from_session(conn, "7") is None
