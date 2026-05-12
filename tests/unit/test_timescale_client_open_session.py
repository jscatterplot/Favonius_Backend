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


# =====================================================================
# Migration 036: meter_start_wh / meter_stop_wh persistence
# =====================================================================


@pytest.mark.asyncio
async def test_insert_open_session_persists_meter_start_wh():
    """meter_start_wh must be in the INSERT column list and bound value list.

    Without this column being persisted at StartTransaction, a handler
    restart between Start and Stop loses the meter_start and
    close_open_session cannot compute energy_delivered_kwh on the way
    out. See migration 036.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.insert_open_session(
        station_id="cp-1",
        transaction_id=200,
        evse_id=1,
        connector_id=1,
        id_token="RFID-2",
        start_time=datetime.now(timezone.utc),
        meter_start_wh=15000,
    )

    insert_call = conn.execute.await_args_list[-1]
    insert_sql = insert_call.args[0]
    assert "meter_start_wh" in insert_sql
    assert 15000 in insert_call.args[1:]


@pytest.mark.asyncio
async def test_insert_open_session_accepts_null_meter_start_wh():
    """meter_start_wh is optional — vendors that omit meterStart still insert."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.insert_open_session(
        station_id="cp-1",
        transaction_id=201,
        evse_id=1,
        connector_id=1,
        id_token=None,
        start_time=datetime.now(timezone.utc),
    )

    insert_call = conn.execute.await_args_list[-1]
    insert_sql = insert_call.args[0]
    assert "meter_start_wh" in insert_sql
    # The 10th bind value (after station, tx, evse, connector, id_token,
    # start_time, vehicle_id, driver_id, card_id) is meter_start_wh.
    assert insert_call.args[-1] is None


@pytest.mark.asyncio
async def test_close_open_session_writes_meter_stop_and_kwh_atomically():
    """Close must UPDATE meter_stop_wh + energy_delivered_kwh in one statement.

    Two writes would race against analytics readers — the row would
    briefly have end_time set but the kWh column NULL. One UPDATE with
    a CASE expression keeps the close atomic.
    """
    conn = AsyncMock()
    matched_row = {"meter_start_wh": 1000, "energy_delivered_kwh": 4.0}
    conn.fetchrow = AsyncMock(return_value=matched_row)
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=300,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5000,
    )

    conn.fetchrow.assert_awaited_once()
    update_sql = conn.fetchrow.await_args.args[0]
    assert "meter_stop_wh" in update_sql
    assert "energy_delivered_kwh" in update_sql
    # The CASE gate: NULL meter_start_wh or meter_stop < meter_start must
    # NOT write a bogus kWh. The SQL keeps the existing value via ELSE.
    assert "CASE" in update_sql
    assert "meter_start_wh IS NOT NULL" in update_sql
    assert ">= meter_start_wh" in update_sql
    assert result == matched_row


@pytest.mark.asyncio
async def test_close_open_session_returns_none_when_no_row_matched():
    """Idempotent retry / already-closed row / imported source -> None.

    The caller distinguishes this from the matched-but-anomalous case
    to decide whether to log a WARN.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=999,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5000,
    )

    assert result is None


@pytest.mark.asyncio
async def test_close_open_session_returns_null_kwh_on_anomaly():
    """RETURNING surfaces the NULL kWh so the caller can WARN.

    Simulates a charger sending meter_stop < meter_start (meter rollover
    or replacement) — the SQL leaves energy_delivered_kwh untouched
    (NULL on a fresh row) and the dict reflects that.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"meter_start_wh": 5000, "energy_delivered_kwh": None})
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=301,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=1000,  # < meter_start_wh
    )

    assert result is not None
    assert result["meter_start_wh"] == 5000
    assert result["energy_delivered_kwh"] is None


@pytest.mark.asyncio
async def test_close_open_session_filters_to_live_source():
    """Close must not touch imported rows even if their end_time is NULL."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=302,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=2000,
    )

    update_sql = conn.fetchrow.await_args.args[0]
    assert "source = 'live'" in update_sql
    assert "end_time IS NULL" in update_sql
