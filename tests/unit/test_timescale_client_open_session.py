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
        station_id="pilot-depot-001",
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
    assert lock_key == "pilot-depot-001:42"


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


def _close_session_conn(
    meter_start_wh,
    returning,
    *,
    last_meter_wh=None,
    select_finds_row=True,
):
    """Build a mock conn whose fetchrow returns SELECT then UPDATE rows in order.

    The refactored ``close_open_session`` (Issue 7A) splits the close into a
    locked SELECT (to read ``meter_start_wh`` + ``last_meter_wh``) followed
    by an UPDATE (to write the close + the helper-computed
    ``energy_delivered_kwh``). Tests need to mock both in sequence.

    ``select_finds_row=False`` simulates the idempotent-retry case where
    the SELECT finds no open row; in that case the UPDATE is never issued.
    ``meter_start_wh`` / ``last_meter_wh`` are the column values on the
    returned SELECT row — both default to None so synthesis-fallback tests
    can express "open row with both columns NULL" naturally.
    """
    conn = AsyncMock()
    fetchrow_results = []
    if select_finds_row:
        fetchrow_results.append(
            {
                "session_id": "sess-1",
                "meter_start_wh": meter_start_wh,
                "last_meter_wh": last_meter_wh,
            }
        )
    else:
        fetchrow_results.append(None)
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
    # The returned dict carries the UPDATE RETURNING values plus a
    # ``synthesized`` flag (Phase 2 audit trail). Compare on the keys we
    # care about rather than exact equality.
    assert result is not None
    assert result["meter_start_wh"] == 1000
    assert result["energy_delivered_kwh"] == 4.0
    assert result["synthesized"] is False


@pytest.mark.asyncio
async def test_close_open_session_returns_none_when_no_row_matched():
    """Idempotent retry / already-closed row / imported source -> None.

    The caller distinguishes this from the matched-but-anomalous case
    to decide whether to log a WARN.
    """
    conn = _close_session_conn(
        meter_start_wh=None, returning=None, select_finds_row=False
    )
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
    conn = _close_session_conn(
        meter_start_wh=None, returning=None, select_finds_row=False
    )
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


# ─── Phase 2 synthesis fallback (close path) ──────────────────────────


@pytest.mark.asyncio
async def test_close_open_session_synthesizes_when_both_meter_columns_null():
    """meter_start_wh IS NULL AND last_meter_wh IS NULL → synthesize from meter_stop_wh.

    The tx_id=18 case: Terra AC sent meterStart=0 (coerced to NULL by Phase
    1), never produced an Energy.Active.Import.Register sample (so no
    backfill, no last_meter_wh), then sent StopTransaction with
    meterStop=2982 Wh. Phase 2 treats the small meter_stop as a per-session
    delta under the 50 kWh cap.
    """
    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": 2.982,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=18,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=2982,
        stop_reason="EVDisconnected",
    )

    update_call = conn.fetchrow.await_args_list[1]
    assert update_call.args[5] == pytest.approx(2.982)  # $5 = synthesised kWh
    # stop_reason ($6) is suffixed for audit
    assert update_call.args[6] == "EVDisconnected|synthesized_delta"
    assert result is not None
    assert result["synthesized"] is True


@pytest.mark.asyncio
async def test_close_open_session_synthesis_respects_50_kwh_cap():
    """meter_stop > 50 kWh is rejected by the synthesizer; energy stays NULL."""
    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": None,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=19,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=51_000,  # 51 kWh — above the 50 kWh default cap
        stop_reason="EVDisconnected",
    )

    update_call = conn.fetchrow.await_args_list[1]
    # Synthesizer refused (too high to be a single-session delta on the
    # pilot fleet), so $5 stays NULL.
    assert update_call.args[5] is None
    # stop_reason is NOT suffixed.
    assert update_call.args[6] == "EVDisconnected"
    assert result["synthesized"] is False


@pytest.mark.asyncio
async def test_close_open_session_does_not_synthesize_when_last_meter_present():
    """Phase 1 won: we have register samples. Don't synthesize from meter_stop.

    If last_meter_wh is non-NULL it means MeterValues DID carry an
    Energy.Active.Import.Register sample during the session — so meter_start_wh
    was either non-NULL all along (real meterStart) or got backfilled by the
    live UPDATE. compute_energy_kwh handled the proper bracket already; if it
    refused, the bracket itself is broken (rollover/replacement) and the
    operator should investigate, not have us synthesize a guess.
    """
    returning = {
        "meter_start_wh": None,
        "last_meter_wh": 5000,  # register samples arrived
        "energy_delivered_kwh": None,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=5000,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=20,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=2000,
        stop_reason="Local",
    )

    update_call = conn.fetchrow.await_args_list[1]
    # Synthesis would have computed 2.0 — but last_meter_wh is non-NULL,
    # so the synthesis branch is suppressed.
    assert update_call.args[5] is None
    assert update_call.args[6] == "Local"  # no suffix
    assert result["synthesized"] is False


@pytest.mark.asyncio
async def test_close_open_session_synthesis_handles_missing_stop_reason():
    """When no OCPP reason was supplied, synthesis still tags the row.

    Defensive: a charger that omits ``reason`` from StopTransaction must
    not blow up the f-string suffix. The synthesizer falls back to
    'unknown' as the base.
    """
    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": 2.982,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=21,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=2982,
        stop_reason=None,  # charger omitted
    )

    update_call = conn.fetchrow.await_args_list[1]
    assert update_call.args[6] == "unknown|synthesized_delta"


@pytest.mark.asyncio
async def test_close_open_session_synthesis_cap_overridable_by_env(monkeypatch):
    """OCPP_SYNTHESIZED_DELTA_CAP_KWH widens (or narrows) the cap at runtime."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "10")  # narrow to 10 kWh

    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": None,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    result = await client.close_open_session(
        station_id="cp-1",
        transaction_id=22,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=20_000,  # 20 kWh, above the narrowed 10 kWh cap
        stop_reason="EVDisconnected",
    )

    update_call = conn.fetchrow.await_args_list[1]
    assert update_call.args[5] is None  # cap rejected
    assert result["synthesized"] is False


@pytest.mark.asyncio
async def test_close_open_session_synthesis_invalid_env_falls_back_to_default(monkeypatch):
    """Bad env value must not silently widen the cap — fall back to 50 kWh default."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "not-a-number")

    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": 30.0,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=23,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=30_000,  # 30 kWh — under the default 50 kWh cap
        stop_reason="Local",
    )

    update_call = conn.fetchrow.await_args_list[1]
    assert update_call.args[5] == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_close_open_session_synthesis_negative_env_falls_back_to_default(monkeypatch):
    """Negative env value (operator typo) falls back to the safe default."""
    monkeypatch.setenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH", "-1")

    returning = {
        "meter_start_wh": None,
        "last_meter_wh": None,
        "energy_delivered_kwh": 30.0,
    }
    conn = _close_session_conn(
        meter_start_wh=None,
        last_meter_wh=None,
        returning=returning,
    )
    client, _ = _client_with_conn(conn)

    await client.close_open_session(
        station_id="cp-1",
        transaction_id=24,
        end_time=datetime.now(timezone.utc),
        meter_stop_wh=30_000,
        stop_reason="Local",
    )

    update_call = conn.fetchrow.await_args_list[1]
    # Default cap kicked in, synthesis succeeded.
    assert update_call.args[5] == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_clear_sessions_seen_clears_last_seen_only():
    """clear_sessions_seen must only null ``last_seen_at`` on open live sessions."""
    conn = AsyncMock()
    client, _ = _client_with_conn(conn)

    await client.clear_sessions_seen("cp-1")

    update_sql = conn.execute.await_args.args[0]
    assert "last_seen_at = NULL" in update_sql
    assert "updated_at" not in update_sql
    # The WHERE clause must still scope to live, still-open sessions.
    assert "end_time IS NULL" in update_sql
    assert "source = 'live'" in update_sql


@pytest.mark.asyncio
async def test_is_transaction_open_returns_true_when_row_open():
    """``is_transaction_open`` returns True when a matching live row exists with end_time IS NULL."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    client, _ = _client_with_conn(conn)

    assert await client.is_transaction_open("cp-1", 42) is True

    sql = conn.fetchval.await_args.args[0]
    assert "end_time IS NULL" in sql
    assert "source = 'live'" in sql
    assert conn.fetchval.await_args.args[1] == "cp-1"
    assert conn.fetchval.await_args.args[2] == 42


@pytest.mark.asyncio
async def test_is_transaction_open_returns_false_when_no_row():
    """Missing or closed row -> False; caller drops in-memory cache entry."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    client, _ = _client_with_conn(conn)

    assert await client.is_transaction_open("cp-1", 42) is False
