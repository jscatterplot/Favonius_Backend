"""Unit tests for TimescaleClient.recover_orphaned_sessions (Issue 2A).

Covers the four cases from the test review (Issue 9A):
 1. No stale rows  -> no UPDATE issued, returns []
 2. Stale row with last_meter_wh  -> closed with computed energy_kwh
 3. Stale row without last_meter_wh  -> closed with NULL energy (no
    bogus value invented from thin air)
 4. Idempotency: re-running on the same stale rows should be a no-op
    because end_time is now non-NULL
"""

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
    client.pg_pool = pool
    return client


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_no_stale_rows_returns_empty():
    """No stale rows in DB -> sweep returns []; no UPDATE issued."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client_with_conn(conn)

    closed = await client.recover_orphaned_sessions(stale_after_seconds=1800)

    assert closed == []
    conn.fetch.assert_awaited_once()
    # No rows -> no UPDATE should be issued
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_closes_stale_row_with_running_meter():
    """Happy path: stale row with last_meter_wh becomes closed with computed energy."""
    last_seen = datetime(2026, 5, 13, 4, 0, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "station_id": "pilot-station-1",
                "transaction_id": 42,
                "meter_start_wh": 1000,
                "last_meter_wh": 5000,
                "last_seen_at": last_seen,
                "start_time": last_seen,
            }
        ]
    )
    conn.fetchrow = AsyncMock(
        return_value={
            "session_id": "00000000-0000-0000-0000-000000000001",
            "station_id": "pilot-station-1",
            "transaction_id": 42,
            "meter_start_wh": 1000,
            "meter_stop_wh": 5000,
            "energy_delivered_kwh": 4.0,
        }
    )
    client = _client_with_conn(conn)

    closed = await client.recover_orphaned_sessions(stale_after_seconds=1800)

    assert len(closed) == 1
    assert closed[0]["energy_delivered_kwh"] == 4.0
    update_call = conn.fetchrow.await_args
    update_sql = update_call.args[0]
    assert "stop_reason          = 'orphaned_recovered'" in update_sql
    # Helper computed (5000 - 1000) / 1000 = 4.0; passed as the typed $5
    assert update_call.args[3] == last_seen  # close_time = last_seen_at
    assert update_call.args[4] == 5000  # meter_stop_wh = last_meter_wh
    assert update_call.args[5] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_closes_without_meter_writes_null_energy():
    """No last_meter_wh recorded -> close with NULL meter_stop and NULL energy.

    Better to leave the row closed-with-NULL than perpetually open; the
    audit trail (stop_reason='orphaned_recovered') tells operators
    exactly which sessions need manual reconciliation.
    """
    last_seen = datetime(2026, 5, 13, 4, 0, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "session_id": "00000000-0000-0000-0000-000000000002",
                "station_id": "pilot-station-2",
                "transaction_id": 43,
                "meter_start_wh": 1000,
                "last_meter_wh": None,  # MeterValues never carried register
                "last_seen_at": last_seen,
                "start_time": last_seen,
            }
        ]
    )
    conn.fetchrow = AsyncMock(
        return_value={
            "session_id": "00000000-0000-0000-0000-000000000002",
            "station_id": "pilot-station-2",
            "transaction_id": 43,
            "meter_start_wh": 1000,
            "meter_stop_wh": None,
            "energy_delivered_kwh": None,
        }
    )
    client = _client_with_conn(conn)

    closed = await client.recover_orphaned_sessions()

    assert len(closed) == 1
    assert closed[0]["energy_delivered_kwh"] is None
    update_call = conn.fetchrow.await_args
    # Helper rejects (None stop, valid start) -> energy_kwh is None ($5)
    assert update_call.args[4] is None  # meter_stop_wh
    assert update_call.args[5] is None  # energy_delivered_kwh


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_filters_to_live_source():
    """Recovery must not touch imported rows or rows without transaction_id."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client_with_conn(conn)

    await client.recover_orphaned_sessions(stale_after_seconds=1800)

    select_sql = conn.fetch.await_args.args[0]
    assert "source = 'live'" in select_sql
    assert "transaction_id IS NOT NULL" in select_sql
    assert "end_time IS NULL" in select_sql
    assert "last_seen_at IS NOT NULL" in select_sql
    assert "last_seen_at < NOW() - make_interval(secs => $1)" in select_sql
    assert "COALESCE(last_meter_seen_at, updated_at, start_time)" in select_sql


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_respects_batch_limit():
    """batch_limit must be passed to LIMIT $2 to bound sweep duration."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client_with_conn(conn)

    await client.recover_orphaned_sessions(stale_after_seconds=300, batch_limit=25)

    select_call = conn.fetch.await_args
    # $1 = stale_after_seconds, $2 = batch_limit
    assert select_call.args[1] == 300
    assert select_call.args[2] == 25


@pytest.mark.asyncio
async def test_recover_orphaned_sessions_idempotent_when_update_loses_race():
    """If another writer closes the row between SELECT and UPDATE, skip it.

    The orphan recovery loop runs every N minutes; a real StopTransaction
    arriving mid-sweep would also UPDATE the same row. We must not produce
    a second close.
    """
    last_seen = datetime(2026, 5, 13, 4, 0, 0, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "session_id": "00000000-0000-0000-0000-000000000004",
                "station_id": "pilot-station-4",
                "transaction_id": 45,
                "meter_start_wh": 1000,
                "last_meter_wh": 5000,
                "last_seen_at": last_seen,
                "start_time": last_seen,
            }
        ]
    )
    # UPDATE returns None -> row was already closed by another writer
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client_with_conn(conn)

    closed = await client.recover_orphaned_sessions()

    # The row was claimed by another writer; recovery silently skips it.
    assert closed == []


@pytest.mark.asyncio
async def test_recover_skips_freshly_reconnected_session():
    """A row whose ``updated_at`` was just bumped by ``clear_sessions_seen``
    must NOT be closed by the case-2 predicate.

    Scenario: a charger reconnects after a long absence. Boot calls
    ``clear_sessions_seen`` which nulls ``last_seen_at`` and bumps
    ``updated_at = NOW()``. If the orphan-recovery sweep runs before the
    first MeterValues arrives, the case-2 COALESCE in the SELECT picks
    up the fresh ``updated_at`` and the row is not returned for closure.

    This test verifies the SELECT predicate semantics by confirming that
    when the DB returns no stale rows (the realistic outcome with a
    freshly-bumped ``updated_at``), recovery is a no-op.
    """
    conn = AsyncMock()
    # No stale rows surface — the DB-side filter already excluded the
    # freshly reconnected session via the COALESCE check.
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    client = _client_with_conn(conn)

    closed = await client.recover_orphaned_sessions(stale_after_seconds=1800)

    assert closed == []
    select_sql = conn.fetch.await_args.args[0]
    # The predicate that protects fresh reconnects must still be present.
    assert "COALESCE(last_meter_seen_at, updated_at, start_time)" in select_sql
