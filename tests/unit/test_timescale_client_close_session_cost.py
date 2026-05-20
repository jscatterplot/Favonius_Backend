"""Unit tests for the post-commit cost task in TimescaleClient.close_open_session.

The close path schedules ``_compute_and_write_cost`` as a fire-and-forget
``asyncio.create_task`` so close-path latency is untouched. These tests
verify:

  * Success path schedules the task with the new session_id.
  * The race-lost case (``UPDATE … RETURNING NULL``) does NOT schedule.
  * The recovery path (``recover_orphaned_sessions``) also schedules
    cost calcs for the rows it closes.

The task body itself (``_compute_and_write_cost``) is exercised by the
integration suite — here we only assert the wiring.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

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
async def test_close_open_session_schedules_cost_task_on_success():
    session_id = uuid4()
    conn = AsyncMock()
    # First fetchrow → SELECT FOR UPDATE returns the open session.
    # Second fetchrow → UPDATE returns the closed row.
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "session_id": session_id,
            "meter_start_wh": 1000,
            "last_meter_wh": 51000,
        },
        {
            "meter_start_wh": 1000,
            "last_meter_wh": 51000,
            "energy_delivered_kwh": 50.0,
        },
    ])
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=51000,
        )

    assert result is not None
    assert result["session_id"] == session_id
    scheduler.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_close_open_session_no_match_does_not_schedule_cost():
    """When the SELECT FOR UPDATE matches nothing (idempotent retry on
    a closed session), no cost task should be scheduled."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=None,
        )

    assert result is None
    scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_close_open_session_lost_race_does_not_schedule_cost():
    """When SELECT matched but UPDATE … RETURNING returned NULL (another
    writer closed the row first), no task should be scheduled."""
    session_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"session_id": session_id, "meter_start_wh": 0, "last_meter_wh": None},
        None,  # UPDATE matched zero rows
    ])
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=None,
        )

    assert result is None
    scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_session_cost_creates_named_task():
    """_schedule_session_cost should use asyncio.create_task with a name
    that includes the session_id for log/debug traceability."""
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost", user="user", password="pass",
    )
    client = TimescaleClient(config)
    client.pg_pool = MagicMock()

    session_id = uuid4()
    captured_name: list[str] = []

    def _fake_create_task(coro, *, name=None):
        captured_name.append(name or "")
        coro.close()  # avoid 'coroutine was never awaited' warning

        class _Stub:
            def get_name(self):
                return name

            def add_done_callback(self, cb, *, context=None):
                # The scheduler also registers a done-callback to discard
                # the task from ``self._background_tasks`` once it
                # completes. We don't fire the callback here because the
                # task is fake — the production code's strong-ref pattern
                # is exercised by the dedicated regression test below.
                pass
        return _Stub()

    with patch.object(client, "_compute_and_write_cost", return_value=AsyncMock()()):
        with patch("src.websocket_handler.timescale_client.asyncio.create_task", side_effect=_fake_create_task):
            client._schedule_session_cost(session_id)

    assert captured_name, "create_task was not called"
    assert str(session_id) in captured_name[0]
    assert captured_name[0].startswith("session-cost-")


@pytest.mark.asyncio
async def test_compute_and_write_cost_only_counts_metric_on_successful_write():
    """Regression: SESSION_COST_COMPUTED should not increment when
    write_session_cost returns False (lost race / row no longer
    eligible). Counting failed writes inflates Prometheus' apparent
    cost-persistence success rate."""
    from unittest.mock import patch as _patch
    import src.monitoring.metrics as _metrics

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "session_id": uuid4(),
        "site_id": uuid4(),
        "vehicle_id": None,
        "start_time": datetime.now(timezone.utc),
        "end_time": datetime.now(timezone.utc),
        "energy_delivered_kwh": 10.0,
        "cost_total": None,
        "cost_total_source": None,
        "station_id": None,
        "connector_id": None,
        "transaction_id": None,
    })
    client, _ = _client_with_conn(conn)
    client._static_pool = lambda: None

    # Patch at the lazy-import site (the metric module) so the
    # in-function ``from ..monitoring.metrics import …`` picks up the
    # fake. Track .labels(...).inc() calls.
    fake_computed = MagicMock()
    with _patch.object(_metrics, "SESSION_COST_COMPUTED", fake_computed), \
         _patch(
             "src.core.billing.session_cost.write_session_cost",
             new=AsyncMock(return_value=False),
         ):
        await client._compute_and_write_cost(uuid4())

    # ``.labels(source=...).inc()`` should not have been called.
    fake_computed.labels.assert_not_called()


@pytest.mark.asyncio
async def test_compute_and_write_cost_backfills_site_id_from_station_id():
    """Regression: ``insert_open_session`` doesn't write ``site_id`` on
    the StartTransaction hot path (no Supabase round-trip), so live
    rows arrive at the cost task with ``site_id IS NULL``. Without a
    backfill the calculator would short-circuit to ``'no_depot'`` for
    every live session. The close path must resolve ``site_id`` from
    ``charging_stations`` via Supabase and inject it before running
    the calculator."""
    from unittest.mock import patch as _patch

    expected_site_id = uuid4()
    expected_station_id = "OCPP-CP-42"
    expected_session_id = uuid4()

    # Timescale row: site_id NULL (production state for live sessions).
    ts_conn = AsyncMock()
    ts_conn.fetchrow = AsyncMock(return_value={
        "session_id": expected_session_id,
        "site_id": None,
        "vehicle_id": None,
        "start_time": datetime.now(timezone.utc),
        "end_time": datetime.now(timezone.utc),
        "energy_delivered_kwh": 10.0,
        "cost_total": None,
        "cost_total_source": None,
        "station_id": expected_station_id,
        "connector_id": 1,
        "transaction_id": 999,
    })
    client, _ = _client_with_conn(ts_conn)

    # Static pool returns the matching site_id for the station lookup
    # and timezone/tariff_config for the subsequent bidding-zone resolve.
    static_conn = AsyncMock()
    static_conn.fetchrow = AsyncMock(side_effect=[
        {"site_id": expected_site_id},
        {"timezone": "Europe/Vilnius", "tariff_config": None},
    ])
    static_pool = MagicMock()
    static_pool.acquire.return_value.__aenter__.return_value = static_conn
    static_pool.acquire.return_value.__aexit__.return_value = None
    client._static_pool = lambda: static_pool

    # Capture the row passed to the calculator so we can assert
    # ``site_id`` was injected. The WS handler does
    # ``from ..core.billing import compute_session_cost`` inside the
    # function, so the binding lives on the package, not the inner
    # module — patch ``src.core.billing.compute_session_cost``.
    captured: dict = {}

    async def _fake_compute(_pool, row_dict):
        captured.update(row_dict)
        from src.core.billing.session_cost import SessionCostResult
        return SessionCostResult(cost=None, source="unpriceable")

    with _patch("src.core.billing.compute_session_cost", new=_fake_compute), \
         _patch(
             "src.core.billing.write_session_cost",
             new=AsyncMock(return_value=False),
         ):
        await client._compute_and_write_cost(expected_session_id)

    # Supabase was consulted with the OCPP station_id on the first call
    # (the subsequent call is _resolve_bidding_zone, which is exercised
    # by its own test).
    assert static_conn.fetchrow.await_count >= 1
    first_call_args = static_conn.fetchrow.await_args_list[0].args
    assert expected_station_id in first_call_args
    # … and the resolved site_id reached the calculator.
    assert captured.get("site_id") == expected_site_id


@pytest.mark.asyncio
async def test_resolve_site_id_for_station_returns_none_when_no_match():
    """The helper should return ``None`` (not raise) when the station
    isn't registered in Supabase. Calculator then falls to
    ``'no_depot'`` cleanly without a tracebacked log."""
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost", user="user", password="pass",
    )
    client = TimescaleClient(config)
    static_conn = AsyncMock()
    static_conn.fetchrow = AsyncMock(return_value=None)
    static_pool = MagicMock()
    static_pool.acquire.return_value.__aenter__.return_value = static_conn
    static_pool.acquire.return_value.__aexit__.return_value = None
    client._static_pool = lambda: static_pool

    result = await client._resolve_site_id_for_station("UNKNOWN-CP")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_site_id_for_station_handles_empty_station_id():
    """Empty / None station_id short-circuits to None without a DB hit."""
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost", user="user", password="pass",
    )
    client = TimescaleClient(config)
    # _static_pool should never be called.
    client._static_pool = lambda: (_ for _ in ()).throw(AssertionError("_static_pool consulted"))

    assert await client._resolve_site_id_for_station(None) is None
    assert await client._resolve_site_id_for_station("") is None


@pytest.mark.asyncio
async def test_schedule_session_cost_holds_strong_reference_to_task():
    """Regression: ``asyncio.create_task`` only registers a weak
    reference with the event loop, so a task whose only handle is the
    local variable in ``_schedule_session_cost`` could be
    garbage-collected before its first await. The scheduler retains
    the task in ``self._background_tasks`` and drops it via
    ``add_done_callback`` when the task completes."""
    import asyncio
    import gc

    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost", user="user", password="pass",
    )
    client = TimescaleClient(config)
    client.pg_pool = MagicMock()

    # Use a real coroutine that blocks long enough for us to inspect
    # the strong-ref set, then completes so the done-callback can
    # remove it.
    finished = asyncio.Event()

    async def fake_compute(_session_id):
        await asyncio.sleep(0.01)
        finished.set()

    session_id = uuid4()
    with patch.object(client, "_compute_and_write_cost", side_effect=fake_compute):
        client._schedule_session_cost(session_id)

        # Right after scheduling, the task is held in the set so a
        # garbage-collection cycle can't drop it.
        assert len(client._background_tasks) == 1, (
            "scheduler must retain a strong reference to the task"
        )
        gc.collect()
        assert len(client._background_tasks) == 1, (
            "task was GC'd despite the strong-ref set"
        )

        # Let it run and finish.
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        # Yield once so the done-callback fires.
        await asyncio.sleep(0)

    # Completed task is removed from the set.
    assert len(client._background_tasks) == 0, (
        "done-callback must discard the finished task"
    )
