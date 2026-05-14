"""Unit tests for ``TimescaleClient.insert_telemetry_batch`` after the V2G drop.

Covers the three behaviors that matter for charger-centric tracking now that
the legacy ``transaction_events_v2g`` fallback is gone:

1. ``telemetry_samples`` rows are written for every batch row that carries a
   ``raw_sample`` regardless of whether the vehicle can be attributed.
2. Open ``charging_sessions`` rows get their ``current_power_kw`` /
   ``current_soc`` / ``max_charge_power_kw`` refreshed on each MeterValues
   batch so per-charger charging rate is observable in real time without
   needing a vehicle.
3. The vehicle-keyed ``telemetry`` insert (optimizer view) is skipped silently
   when no vehicle attribution exists; the batch as a whole still succeeds and
   never queries ``transaction_events_v2g``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


def _client() -> TimescaleClient:
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    return TimescaleClient(config)


def _mock_pool(conn: AsyncMock) -> MagicMock:
    """Build a MagicMock pool whose ``acquire()`` yields ``conn``."""
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


# ---------------------------------------------------------------------------
# _coerce_transaction_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", 1),
        ("42", 42),
        (7, 7),
        ("  9  ", 9),
        (None, None),
        ("", None),
        ("not-an-int", None),
        ("11111111-2222-3333-4444-555555555555", None),
    ],
)
def test_coerce_transaction_id(value, expected):
    assert TimescaleClient._coerce_transaction_id(value) == expected


# ---------------------------------------------------------------------------
# _update_session_live_metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_session_live_metrics_writes_expected_sql():
    conn = AsyncMock()
    client = _client()

    await client._update_session_live_metrics(
        conn,
        station_id="cp-1",
        transaction_id=42,
        power_kw=11.0,
        soc_percent=75.0,
        max_charge_kw=22.0,
    )

    sql = conn.execute.await_args.args[0]
    args = conn.execute.await_args.args[1:]
    assert "UPDATE charging_sessions" in sql
    assert "current_power_kw" in sql
    assert "current_soc" in sql
    assert "max_charge_power_kw" in sql
    assert "GREATEST" in sql
    assert "end_time IS NULL" in sql
    assert "source = 'live'" in sql
    # Args: station_id, transaction_id, power_kw, soc_fraction, max_charge_kw,
    # meter_wh (None when MeterValues lacks an Energy.Active.Import.Register sample)
    assert args == ("cp-1", 42, 11.0, 0.75, 22.0, None)


@pytest.mark.asyncio
async def test_update_session_live_metrics_swallows_db_errors():
    """Failing live update must not propagate — telemetry_samples is canonical."""
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("boom"))
    client = _client()

    # Should not raise.
    await client._update_session_live_metrics(
        conn, "cp-1", 1, power_kw=1.0, soc_percent=None, max_charge_kw=None
    )


@pytest.mark.asyncio
async def test_update_session_live_metrics_backfills_deferred_meter_start():
    """The UPDATE backfills meter_start_wh from a positive register sample when NULL.

    Phase 1 of the Terra AC fix: meterStart=0 at StartTransaction is
    written as NULL ("deferred"). The first MeterValues sample that
    carries a positive Energy.Active.Import.Register backfills it on the
    same UPDATE that advances last_meter_wh. The SQL pattern must:

      * Backfill meter_start_wh only when it's currently NULL (never
        overwrite a legitimate non-zero start).
      * Use the backfilled value inline in the energy calculation
        (``COALESCE(meter_start_wh, $6 when positive)``) so on the first
        sample we get 0 kWh (no accumulated energy yet) rather than NULL.
    """
    conn = AsyncMock()
    client = _client()

    await client._update_session_live_metrics(
        conn,
        station_id="cp-1",
        transaction_id=42,
        power_kw=11.0,
        soc_percent=75.0,
        max_charge_kw=22.0,
        meter_wh=3000,
    )

    sql = conn.execute.await_args.args[0]
    args = conn.execute.await_args.args[1:]

    # Backfill CASE for meter_start_wh: only when currently NULL and the
    # incoming sample is positive.
    assert "meter_start_wh       = CASE" in sql
    assert "WHEN meter_start_wh IS NULL" in sql

    # Energy CASE references the COALESCE so the backfilled value is
    # picked up on the same UPDATE.
    assert "COALESCE(\n                                meter_start_wh," in sql

    # meter_wh is the 6th positional bind ($6::bigint).
    assert args[-1] == 3000


@pytest.mark.asyncio
async def test_update_session_live_metrics_no_backfill_on_null_sample():
    """When meter_wh is None the backfill CASE leaves meter_start_wh alone."""
    conn = AsyncMock()
    client = _client()

    await client._update_session_live_metrics(
        conn,
        station_id="cp-1",
        transaction_id=42,
        power_kw=11.0,
        soc_percent=75.0,
        max_charge_kw=22.0,
        meter_wh=None,
    )

    args = conn.execute.await_args.args[1:]
    assert args[-1] is None  # $6 is NULL → both CASE branches preserve column


@pytest.mark.asyncio
async def test_update_session_live_metrics_zero_sample_does_not_backfill():
    """A MeterValues sample of 0 Wh is just as poisonous as meterStart=0.

    Don't backfill meter_start_wh with 0 — the energy CASE would still
    refuse it via the ``> 0`` guard, but writing 0 into the column
    re-creates the original bug. Belt-and-suspenders: the backfill CASE
    also gates on ``$6 > 0``.
    """
    conn = AsyncMock()
    client = _client()

    await client._update_session_live_metrics(
        conn,
        station_id="cp-1",
        transaction_id=42,
        power_kw=11.0,
        soc_percent=75.0,
        max_charge_kw=22.0,
        meter_wh=0,
    )

    sql = conn.execute.await_args.args[0]
    args = conn.execute.await_args.args[1:]
    # The SQL contains the guard.
    assert "AND $6::bigint > 0" in sql
    # The bind is the literal 0.
    assert args[-1] == 0


# ---------------------------------------------------------------------------
# insert_telemetry_batch
# ---------------------------------------------------------------------------


def _row(**overrides):
    base = {
        "time": datetime.now(timezone.utc),
        "station_id": "cp-1",
        "connector_id": 1,
        "session_id": "42",
        "power_kw": 11.0,
        "energy_kwh": None,
        "soc_percent": 80.0,
        "max_charge_power_kw": 22.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_batch_writes_telemetry_samples_and_live_metrics_without_vehicle():
    """No vehicle attribution → telemetry_samples + live UPDATE still run.

    The vehicle-keyed ``telemetry`` insert is skipped silently. The whole
    batch must succeed and must NOT query ``transaction_events_v2g``.
    """
    conn = AsyncMock()
    # No vehicle resolvable from the open session.
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client = _client()
    client.pg_pool = _mock_pool(conn)

    raw_sample = {
        "measurand": "Power.Active.Import",
        "value": 11000.0,
        "unit": "W",
        "context": "Sample.Periodic",
        "phase": None,
        "location": "Outlet",
        "format": "Raw",
        "timestamp": datetime.now(timezone.utc),
    }
    await client.insert_telemetry_batch([_row(raw_sample=raw_sample)])

    executed_sql = [c.args[0] for c in conn.execute.await_args_list]
    # Per-measurand audit row was written.
    assert any("INSERT INTO telemetry_samples" in s for s in executed_sql)
    # Live-session metric refresh ran for the open session.
    assert any("UPDATE charging_sessions" in s for s in executed_sql)
    # Vehicle-keyed telemetry insert was NOT issued (no vehicle attributable).
    assert not any("INSERT INTO telemetry" in s and "telemetry_samples" not in s
                   for s in executed_sql)
    # V2G fallback is gone.
    assert not any("transaction_events_v2g" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_batch_writes_vehicle_telemetry_when_session_resolves_vehicle():
    """Session lookup returns a vehicle → optimizer-facing telemetry row inserted."""
    conn = AsyncMock()
    # First fetchrow: session → vehicle. Second: charger lookup.
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"vehicle_id": "11111111-1111-1111-1111-111111111111"},  # session resolve
            {"charger_id": "22222222-2222-2222-2222-222222222222"},  # charger resolve
        ]
    )
    conn.execute = AsyncMock()
    client = _client()
    client.pg_pool = _mock_pool(conn)

    await client.insert_telemetry_batch([_row()])

    executed_sql = [c.args[0] for c in conn.execute.await_args_list]
    # Live update fired.
    assert any("UPDATE charging_sessions" in s for s in executed_sql)
    # Vehicle-keyed telemetry insert fired.
    assert any(
        "INSERT INTO telemetry" in s and "telemetry_samples" not in s for s in executed_sql
    )
    # V2G fallback is gone.
    assert not any("transaction_events_v2g" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_batch_skips_live_update_when_session_id_is_uuid():
    """UUID session_id (no integer transaction_id) → skip the live UPDATE.

    transaction_id column is BIGINT; a UUID can never match. Coercion returns
    None and the live-update branch is short-circuited.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    client = _client()
    client.pg_pool = _mock_pool(conn)

    uuid_row = _row(session_id="11111111-2222-3333-4444-555555555555")
    await client.insert_telemetry_batch([uuid_row])

    executed_sql = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("UPDATE charging_sessions" in s for s in executed_sql)
    assert not any("transaction_events_v2g" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_batch_no_v2g_method_exists():
    """Regression guard: the V2G fallback method must not exist anymore."""
    client = _client()
    assert not hasattr(client, "_resolve_vehicle_id_from_id_token")


@pytest.mark.asyncio
async def test_batch_handles_mixed_rows_no_row_lost_to_v2g_error():
    """Multi-row batch: vehicle-known row + vehicle-unknown row.

    Both should be processed; the unknown row must not abort the batch.
    """
    conn = AsyncMock()
    # First row resolves; second does not.
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"vehicle_id": "11111111-1111-1111-1111-111111111111"},  # row1 session
            {"charger_id": "22222222-2222-2222-2222-222222222222"},  # row1 charger
            None,  # row2 session — no vehicle
        ]
    )
    conn.execute = AsyncMock()
    client = _client()
    client.pg_pool = _mock_pool(conn)

    await client.insert_telemetry_batch([_row(), _row(session_id="43")])

    executed_sql = [c.args[0] for c in conn.execute.await_args_list]
    # Live UPDATE happened for both rows (both have integer transaction_id).
    assert sum(1 for s in executed_sql if "UPDATE charging_sessions" in s) == 2
    # Exactly one vehicle-keyed telemetry insert (from the resolvable row).
    assert sum(
        1 for s in executed_sql if "INSERT INTO telemetry" in s and "telemetry_samples" not in s
    ) == 1
    assert not any("transaction_events_v2g" in s for s in executed_sql)
