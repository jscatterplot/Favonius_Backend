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


def _close_session_conn(meter_start_wh, returning):
    """Build a mock conn whose fetchrow returns SELECT then UPDATE rows in order.

    The refactored ``close_open_session`` (Issue 7A) splits the close into a
    locked SELECT (to read ``meter_start_wh``) followed by an UPDATE (to
    write the close + the helper-computed ``energy_delivered_kwh``).
    Tests need to mock both in sequence.
    """
    conn = AsyncMock()
    fetchrow_results = []
    if meter_start_wh is not None:
        fetchrow_results.append({"session_id": "sess-1", "meter_start_wh": meter_start_wh})
    else:
        fetchrow_results.append(None)  # SELECT found no open row
    fetchrow_results.append(returning)
    conn.fetchrow = AsyncMock(side_effect=fetchrow_results)
    return conn


@pytest.mark.asyncio
async def test_close_open_session_writes_energy_helper_value():
    """Close must compute energy_delivered_kwh via compute_energy_kwh helper.

    Verifies the refactored two-query close path:
      1. SELECT meter_start_wh FOR UPDATE
      2. UPDATE with helper-computed energy_kwh as a typed float parameter

    The Python helper is the single source of truth; SQL no longer carries
    a CASE expression for the kWh delta.
    """
    matched_row = {"meter_start_wh": 1000, "energy_delivered_kwh": 4.0}
    conn = _close_session_conn(meter_start_wh=1000, returning=matched_row)
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=300,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5000,
    )

    assert conn.fetchrow.await_count == 2
    update_call = conn.fetchrow.await_args_list[1]
    update_sql = update_call.args[0]
    assert "meter_stop_wh" in update_sql
    assert "energy_delivered_kwh = $5" in update_sql  # typed float param
    # Helper computed (5000 - 1000) / 1000 = 4.0 and passed as $5
    assert update_call.args[5] == pytest.approx(4.0)
    assert result == matched_row


@pytest.mark.asyncio
async def test_close_open_session_returns_none_when_no_row_matched():
    """Idempotent retry / already-closed row / imported source -> None.

    The caller distinguishes this from the matched-but-anomalous case
    to decide whether to log a WARN.
    """
    conn = _close_session_conn(meter_start_wh=None, returning=None)
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=999,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5000,
    )

    # SELECT returned None, UPDATE should not be issued.
    assert result is None
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_close_open_session_writes_null_kwh_when_helper_rejects_bracket():
    """Helper returns None for meter_stop < meter_start; close writes NULL.

    Simulates meter rollover: 5000 Wh start, 1000 Wh stop. The Python
    helper returns None (refusing the bogus bracket), and the UPDATE
    persists energy_delivered_kwh = NULL.
    """
    returning = {"meter_start_wh": 5000, "energy_delivered_kwh": None}
    conn = _close_session_conn(meter_start_wh=5000, returning=returning)
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=301,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=1000,  # < meter_start_wh: rollover anomaly
    )

    update_call = conn.fetchrow.await_args_list[1]
    # Helper returned None; SQL receives NULL for energy_delivered_kwh ($5)
    assert update_call.args[5] is None
    assert result is not None
    assert result["energy_delivered_kwh"] is None


@pytest.mark.asyncio
async def test_close_open_session_writes_null_kwh_on_zero_meter_start():
    """Issue 8A: meter_start_wh=0 must produce NULL energy, not a huge bogus number.

    Some chargers emit meterStart=0 when the register is unavailable.
    The helper rejects the bracket; the close path stores NULL.
    """
    returning = {"meter_start_wh": 0, "energy_delivered_kwh": None}
    conn = _close_session_conn(meter_start_wh=0, returning=returning)
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=304,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5_000_000,  # would be 5000 kWh if helper didn't guard
    )

    update_call = conn.fetchrow.await_args_list[1]
    assert update_call.args[5] is None


@pytest.mark.asyncio
async def test_close_open_session_filters_to_live_source():
    """Close must not touch imported rows even if their end_time is NULL."""
    conn = _close_session_conn(meter_start_wh=None, returning=None)
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=302,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=2000,
    )

    select_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "source = 'live'" in select_sql
    assert "end_time IS NULL" in select_sql


@pytest.mark.asyncio
async def test_close_open_session_none_meter_stop_does_not_raise():
    """meter_stop_wh=None must not raise 'could not determine data type of parameter $4'.

    The bug pattern from PR #186: chargers send StopTransaction without a
    meterStop value. After the helper refactor the helper returns None and
    the SQL stores NULL for both columns — the explicit ``$4::bigint`` cast
    in the UPDATE still survives untyped NULL parameters.
    """
    returning = {"meter_start_wh": 1000, "energy_delivered_kwh": None}
    conn = _close_session_conn(meter_start_wh=1000, returning=returning)
    client, _ = _client_with_conn(conn)

    # Must not raise — production bug: "could not determine data type of parameter $4"
    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=303,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=None,
    )

    assert result is not None
    update_call = conn.fetchrow.await_args_list[1]
    update_sql = update_call.args[0]
    # Explicit cast must still be present so PostgreSQL types untyped NULL.
    assert "$4::bigint" in update_sql
    # Helper returned None because meter_stop is None
    assert update_call.args[5] is None


@pytest.mark.asyncio
async def test_close_open_session_stamps_stop_reason():
    """stop_reason (Issue 3B/2A) is written via COALESCE so callers can pass it."""
    returning = {"meter_start_wh": 1000, "energy_delivered_kwh": 4.0}
    conn = _close_session_conn(meter_start_wh=1000, returning=returning)
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=305,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=5000,
        stop_reason="EVDisconnected",
    )

    update_call = conn.fetchrow.await_args_list[1]
    update_sql = update_call.args[0]
    assert "stop_reason" in update_sql
    assert update_call.args[6] == "EVDisconnected"
