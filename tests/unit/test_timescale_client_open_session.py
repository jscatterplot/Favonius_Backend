"""Unit tests for TimescaleClient open-session insert behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


def _client_with_conn(conn):
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    client = TimescaleClient(config)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None
    client.pg_pool = pool
    return client, conn


@pytest.mark.asyncio
async def test_insert_open_session_skips_existing_open_row():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="existing-session")
    conn.execute = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.insert_open_session(
        station_id="cp-1",
        transaction_id=101,
        evse_id=1,
        connector_id=1,
        id_token="OP-abc",
        start_time=datetime.now(timezone.utc),
    )

    conn.fetchval.assert_awaited_once()
    conn.execute.assert_awaited_once()
    lock_query = conn.execute.await_args_list[0].args[0]
    assert "hashtextextended" in lock_query
    assert "pg_advisory_xact_lock" in lock_query


@pytest.mark.asyncio
async def test_insert_open_session_inserts_when_no_existing_row():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.insert_open_session(
        station_id="cp-1",
        transaction_id=102,
        evse_id=1,
        connector_id=1,
        id_token="RFID-1",
        start_time=datetime.now(timezone.utc),
    )

    conn.fetchval.assert_awaited_once()
    assert conn.execute.await_count == 2
    lock_query = conn.execute.await_args_list[0].args[0]
    assert "hashtextextended" in lock_query
    assert "pg_advisory_xact_lock" in lock_query
