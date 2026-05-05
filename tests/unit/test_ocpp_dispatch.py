"""Unit tests for the queue-mediated dispatch path.

Session 3 replaces in-process ``set_charging_profile`` calls with
``charging_command_queue`` INSERTs (migration 014). These tests focus on
the dispatch contract:

  * The function enqueues one row per scheduled vehicle.
  * Vehicles without a charger mapping are skipped.
  * Empty / all-None schedules do not produce queue rows.
  * Profile-conversion failures are isolated per-vehicle.
  * The audit-table write is best-effort (DB error never blocks dispatch).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.adapters.ocpp.dispatch import _store_charging_command, dispatch_charging_profiles
from src.core.models import OptimizationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _result(**schedule_overrides) -> OptimizationResult:
    """Build a minimally-valid OptimizationResult.

    Override schedule via ``schedule={...}``.
    """
    base = {
        "bus_1": {"charging_power": [22.0, 22.0, 11.0, 0.0]},
        "bus_2": {"charging_power": [11.0, 11.0, 0.0, 0.0]},
    }
    base.update(schedule_overrides.pop("schedule", {}))
    return OptimizationResult(
        run_id=uuid4(),
        schedule=base if "schedule" not in schedule_overrides else schedule_overrides["schedule"],
        battery_dispatch=[0.0, 0.0, 0.0, 0.0],
        grid_power=[33.0, 33.0, 11.0, 0.0],
        peak_demand_kw=33.0,
        objective_value=100.0,
        solve_time_s=1.0,
        status="optimal",
        solver_used="highs",
    )


@pytest.fixture
def fake_pools():
    """DatabasePools-shaped object whose ``ts.acquire()`` returns an async-CM
    yielding a connection that records every fetchval/execute call.
    """
    conn = AsyncMock()
    # fetchval is the dispatch._enqueue INSERT — return a fresh queue id each time.
    counter = {"n": 0}

    async def _fetchval(_sql: str, *_args):
        counter["n"] += 1
        return counter["n"]

    conn.fetchval = AsyncMock(side_effect=_fetchval)

    async def _execute(_sql: str, *_args):
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)

    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    return SimpleNamespace(static=pool, ts=pool, _conn=conn)


@pytest.fixture
def vehicle_to_charger_map():
    return {"bus_1": ("CHARGER_001", 1), "bus_2": ("CHARGER_002", 1)}


# ---------------------------------------------------------------------------
# Happy path: every vehicle becomes a queue row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_enqueues_row_per_vehicle(fake_pools, vehicle_to_charger_map):
    result = _result()

    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )

    assert results == {"bus_1": True, "bus_2": True}
    # Two INSERTs into charging_command_queue.
    assert fake_pools._conn.fetchval.await_count == 2
    # Audit-table writes happen after each enqueue.
    assert fake_pools._conn.execute.await_count >= 2


# ---------------------------------------------------------------------------
# Skip cases: missing mapping, empty / None schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skips_unmapped_vehicle(fake_pools):
    result = _result()
    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map={"bus_1": ("CHARGER_001", 1)},
    )

    assert results["bus_1"] is True
    assert results["bus_2"] is False
    assert fake_pools._conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_skips_empty_schedule(fake_pools, vehicle_to_charger_map):
    result = OptimizationResult(
        run_id=uuid4(),
        schedule={"bus_1": {"charging_power": []}, "bus_2": {"charging_power": []}},
        battery_dispatch=[],
        grid_power=[],
        peak_demand_kw=0.0,
        objective_value=0.0,
        solve_time_s=0.5,
        status="optimal",
        solver_used="highs",
    )

    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )

    assert results == {"bus_1": False, "bus_2": False}
    assert fake_pools._conn.fetchval.await_count == 0


@pytest.mark.asyncio
async def test_dispatch_skips_all_none_schedule(fake_pools, vehicle_to_charger_map):
    result = OptimizationResult(
        run_id=uuid4(),
        schedule={"bus_1": {"charging_power": [None, None, None]}},
        battery_dispatch=[0.0, 0.0, 0.0],
        grid_power=[0.0, 0.0, 0.0],
        peak_demand_kw=0.0,
        objective_value=0.0,
        solve_time_s=0.5,
        status="optimal",
        solver_used="highs",
    )

    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )

    assert results["bus_1"] is False
    assert fake_pools._conn.fetchval.await_count == 0


# ---------------------------------------------------------------------------
# Profile conversion errors are isolated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_handles_conversion_error(fake_pools, vehicle_to_charger_map):
    result = _result()
    # Make conversion fail for *all* vehicles in this run.
    with patch(
        "src.adapters.ocpp.dispatch.convert_schedule_to_ocpp_profile",
        side_effect=ValueError("Invalid schedule"),
    ):
        results = await dispatch_charging_profiles(
            result,
            pools=fake_pools,
            depot_id="depot_a",
            vehicle_to_charger_map=vehicle_to_charger_map,
        )

    assert results == {"bus_1": False, "bus_2": False}
    assert fake_pools._conn.fetchval.await_count == 0


# ---------------------------------------------------------------------------
# Mapping resolution: when vehicle_to_charger_map is None, dispatch loads it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_loads_mapping_from_static_pool(fake_pools):
    result = _result()
    depot_id = uuid4()

    with patch(
        "src.adapters.ocpp.dispatch.get_vehicle_to_charger_map"
    ) as mock_map:
        mock_map.return_value = {
            "bus_1": ("CHARGER_001", 1),
            "bus_2": ("CHARGER_002", 1),
        }

        await dispatch_charging_profiles(
            result, pools=fake_pools, depot_id=depot_id
        )

        mock_map.assert_called_once_with(fake_pools.static, depot_id, use_cache=True)


# ---------------------------------------------------------------------------
# Empty schedule overall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_empty_dict_for_empty_schedule(
    fake_pools, vehicle_to_charger_map
):
    result = OptimizationResult(
        run_id=uuid4(),
        schedule={},
        battery_dispatch=[],
        grid_power=[],
        peak_demand_kw=0.0,
        objective_value=0.0,
        solve_time_s=0.5,
        status="optimal",
        solver_used="highs",
    )

    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )

    assert results == {}
    assert fake_pools._conn.fetchval.await_count == 0


# ---------------------------------------------------------------------------
# Audit insert: best-effort, must not block on DB error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_insert_failure_does_not_break_dispatch(fake_pools, vehicle_to_charger_map):
    """A PostgresError in the audit insert is logged, not raised."""
    fake_pools._conn.execute = AsyncMock(side_effect=asyncpg.PostgresError("DB error"))

    results = await dispatch_charging_profiles(
        _result(),
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )
    assert results == {"bus_1": True, "bus_2": True}


@pytest.mark.asyncio
async def test_store_charging_command_swallows_db_error():
    """`_store_charging_command` propagates DB errors to callers."""
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=asyncpg.PostgresError("DB error"))
    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Should raise; caller-level path handles best-effort behavior.
    with pytest.raises(asyncpg.PostgresError):
        await _store_charging_command(
            pool,
            vehicle_id="bus_1",
            charge_point_id="CHARGER_001",
            connector_id=1,
            charging_profile={"chargingSchedule": {}},
            optimization_result=_result(),
        )
    # Note: dispatch.py wraps the call in a try/except. The helper itself
    # may surface the error — see dispatch.dispatch_charging_profiles.
