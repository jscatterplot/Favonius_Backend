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


@pytest.mark.asyncio
async def test_insert_open_session_lock_binds_single_text_param():
    """Advisory-lock SQL must bind one text param so asyncpg doesn't choke.

    Regression: the prior SQL used ``$1 || ':' || $2::text`` with
    ``transaction_id`` as ``$2``. asyncpg's prepared-statement type
    inference resolved ``$2`` to text via the ``||`` chain and refused to
    coerce the int, raising ``invalid input for query argument $2: 1
    (expected str, got int)``. Building the lock key in Python keeps
    the lock semantics identical and binds a single, unambiguous text
    parameter.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.insert_open_session(
        station_id="hrx-uab_hrx-vilnius-001",
        transaction_id=42,
        evse_id=1,
        connector_id=1,
        id_token="OP-abc",
        start_time=datetime.now(timezone.utc),
    )

    lock_call = conn.execute.await_args_list[0]
    lock_args = lock_call.args[1:]
    assert len(lock_args) == 1, "lock query should bind exactly one parameter"
    lock_key = lock_args[0]
    assert isinstance(lock_key, str)
    assert lock_key == "hrx-uab_hrx-vilnius-001:42"
