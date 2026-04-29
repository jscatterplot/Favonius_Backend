"""Controller-level integration of snapshot persistence + readiness gating.

These tests live separately from ``test_controller.py`` so the existing
retry-logic suite remains untouched. They focus on the new behaviour
introduced by migration 019:

    1. The controller persists exactly one snapshot per optimization
       attempt and links it to the resulting ``run_id``.
    2. Forecast-fallback building load downgrades the optimization run
       status to ``degraded``.
    3. A ``not_ready`` readiness verdict aborts the run with an
       informative error after a snapshot has been captured.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.db.pools import DatabasePools


@pytest.fixture
def pool_pair():
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return DatabasePools(static=pool, ts=pool), conn


@pytest.fixture
def full_depot_config():
    return DepotConfig(
        vehicle_capacities={"bus_1": 324.0, "bus_2": 324.0},
        vehicle_max_charge_kw={"bus_1": 80.0, "bus_2": 80.0},
        charger_groups={80.0: 4},
        charger_efficiency=0.95,
        charger_vehicle_access={
            "charger_a": {"bus_1", "bus_2"},
            "charger_b": {"bus_1"},
        },
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@pytest.fixture
def controller_config():
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=1,
        max_optimization_failures=3,
        dispatch_retry_attempts=1,
        dispatch_retry_delay_seconds=0.01,
        shutdown_timeout_seconds=5.0,
    )


def _real_state() -> DepotState:
    n = 96
    return DepotState(
        vehicle_socs={"bus_1": 0.45, "bus_2": 0.82},
        battery_soc=0.55,
        prices=[0.10] * n,
        demand_charge_rate=20.0,
        current_month_peak=380.0,
        vehicle_availability={"bus_1": [True] * n, "bus_2": [True] * n},
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * n,
    )


def _opt_result() -> OptimizationResult:
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            "bus_1": {"charging_power": [80.0] * 96, "soc": [0.5] * 96},
            "bus_2": {"charging_power": [60.0] * 96, "soc": [0.85] * 96},
        },
        battery_dispatch=[0.0] * 96,
        grid_power=[140.0] * 96,
        peak_demand=200.0,
        objective_value=1000.0,
        solve_time=5.0,
        status="optimal",
    )


def _prime_assembler(controller: DepotController, *, building_source: str) -> None:
    """Seed the metadata that the controller reads after assembly."""
    now = datetime.utcnow()
    controller.assembler._last_horizon = (now, now + timedelta(hours=24))
    controller.assembler._last_building_load_source = building_source
    controller.assembler._last_schedules_present = True
    controller.assembler._last_schedules = [
        {
            "vehicle_id": "bus_1",
            "departure_time": now + timedelta(hours=6),
            "return_time": now + timedelta(hours=18),
            "estimated_energy_kwh": 200.0,
        }
    ]
    controller.assembler._last_weather_features = []
    controller.assembler._last_organization_id = str(uuid4())


@pytest.mark.asyncio
async def test_run_persists_snapshot_and_links_to_run(
    pool_pair, full_depot_config, controller_config
):
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )

    state = _real_state()
    controller.assembler.get_current_state = AsyncMock(return_value=state)
    _prime_assembler(controller, building_source="meter")

    persisted: list = []
    linked: list = []

    async def fake_persist(_pools, snapshot):
        persisted.append(snapshot)
        return snapshot.snapshot_id

    async def fake_link(_pools, snapshot_id, run_id):
        linked.append((snapshot_id, run_id))

    controller.assembler.fetch_snapshot_extras = AsyncMock()
    with (
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", side_effect=fake_link),
        patch("src.core.controller.optimize", return_value=_opt_result()),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        result = await controller.run_optimization("test")

    assert len(persisted) == 1
    snap = persisted[0]
    assert snap.readiness.status == "ready"
    # Linked to the run row that _store_result wrote.
    assert linked == [(snap.snapshot_id, result.run_id)]
    # Result status untouched in the ready path.
    assert result.status == "optimal"


@pytest.mark.asyncio
async def test_forecast_fallback_marks_run_degraded(
    pool_pair, full_depot_config, controller_config
):
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )

    state = _real_state()
    controller.assembler.get_current_state = AsyncMock(return_value=state)
    _prime_assembler(controller, building_source="forecast_fallback")

    captured: list = []

    async def fake_persist(_pools, snapshot):
        captured.append(snapshot)
        return snapshot.snapshot_id

    controller.assembler.fetch_snapshot_extras = AsyncMock()
    with (
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", new=AsyncMock()),
        patch("src.core.controller.optimize", return_value=_opt_result()),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        result = await controller.run_optimization("test")

    assert len(captured) == 1
    assert captured[0].readiness.status == "degraded"
    assert "building_load_meter_unavailable" in captured[0].readiness.degraded_reasons
    # Solver returned 'optimal' but readiness downgrades to 'degraded'.
    assert result.status == "degraded"


@pytest.mark.asyncio
async def test_snapshot_persisted_once_across_solver_retries(
    pool_pair, full_depot_config, controller_config
):
    """Snapshot capture must NOT live inside the solver retry loop.

    Two solver failures followed by a success → exactly one snapshot row,
    one ``link_snapshot_to_run`` call.
    """
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )

    state = _real_state()
    controller.assembler.get_current_state = AsyncMock(return_value=state)
    _prime_assembler(controller, building_source="meter")

    persisted: list = []
    linked: list = []

    async def fake_persist(_pools, snapshot):
        persisted.append(snapshot)
        return snapshot.snapshot_id

    async def fake_link(_pools, snapshot_id, run_id):
        linked.append((snapshot_id, run_id))

    success = _opt_result()
    optimize_mock = MagicMock(
        side_effect=[
            RuntimeError("solver timeout"),
            RuntimeError("solver crashed"),
            success,
        ]
    )

    controller.assembler.fetch_snapshot_extras = AsyncMock()
    with (
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", side_effect=fake_link),
        patch("src.core.controller.optimize", optimize_mock),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
        patch("src.core.controller.asyncio.sleep", new=AsyncMock()),
    ):
        result = await controller.run_optimization("test")

    assert len(persisted) == 1
    snap = persisted[0]
    assert linked == [(snap.snapshot_id, result.run_id)]
    assert optimize_mock.call_count == 3


@pytest.mark.asyncio
async def test_stub_snapshot_persisted_on_construction_failure(
    pool_pair, full_depot_config, controller_config
):
    """When ``build_snapshot`` raises, the stub row MUST hit the DB.

    The stub is marked ``degraded`` (run proceeds) so a transient
    snapshot-construction bug never blocks optimization, but the audit
    trail row is preserved.
    """
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )
    controller.assembler.get_current_state = AsyncMock(return_value=_real_state())
    _prime_assembler(controller, building_source="meter")
    controller.assembler.fetch_snapshot_extras = AsyncMock()

    persisted: list = []

    async def fake_persist(_pools, snapshot):
        persisted.append(snapshot)
        return snapshot.snapshot_id

    with (
        patch("src.core.controller.build_snapshot", side_effect=RuntimeError("boom")),
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", new=AsyncMock()),
        patch("src.core.controller.optimize", return_value=_opt_result()),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        result = await controller.run_optimization("test")

    assert len(persisted) == 1
    stub = persisted[0]
    assert stub.readiness.status == "degraded"
    assert "snapshot_construction_failed" in stub.readiness.degraded_reasons
    assert stub.charger_vehicle_access == {"mode": "all_to_all", "matrix": {}}
    # The run still proceeds even though build_snapshot failed.
    assert result.status == "degraded"


@pytest.mark.asyncio
async def test_build_snapshot_failure_preserves_not_ready_block(
    pool_pair, full_depot_config, controller_config
):
    """A fallback snapshot must not turn a hard readiness miss into degraded."""
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )
    controller.assembler.get_current_state = AsyncMock(return_value=_real_state())
    _prime_assembler(controller, building_source="meter")
    controller.assembler._last_schedules_present = False
    controller.assembler._last_schedules = []
    controller.assembler.fetch_snapshot_extras = AsyncMock()

    captured: list = []

    async def fake_persist(_pools, snapshot):
        captured.append(snapshot)
        return snapshot.snapshot_id

    optimize_mock = MagicMock(return_value=_opt_result())
    with (
        patch("src.core.controller.build_snapshot", side_effect=RuntimeError("boom")),
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", new=AsyncMock()),
        patch("src.core.controller.optimize", optimize_mock),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        with pytest.raises(Exception, match="not ready"):
            await controller.run_optimization("test")

    assert len(captured) == 1
    stub = captured[0]
    assert stub.readiness.status == "not_ready"
    assert "schedules" in stub.readiness.missing_inputs
    assert "snapshot_construction_failed" in stub.readiness.degraded_reasons
    optimize_mock.assert_not_called()


@pytest.mark.asyncio
async def test_static_assumption_marks_run_degraded(
    pool_pair, full_depot_config, controller_config
):
    """Building load source 'static_assumption' → degraded status."""
    pool, _ = pool_pair
    full_depot_config.building_load_assumption_kw = 30.0
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )
    controller.assembler.get_current_state = AsyncMock(return_value=_real_state())
    _prime_assembler(controller, building_source="static_assumption")
    controller.assembler.fetch_snapshot_extras = AsyncMock()

    captured: list = []

    async def fake_persist(_pools, snapshot):
        captured.append(snapshot)
        return snapshot.snapshot_id

    with (
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", new=AsyncMock()),
        patch("src.core.controller.optimize", return_value=_opt_result()),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        result = await controller.run_optimization("test")

    assert len(captured) == 1
    snap = captured[0]
    assert snap.readiness.status == "degraded"
    assert snap.readiness.building_load_source == "static_assumption"
    assert snap.readiness.assumptions["building_load"]["value_kw"] == 30.0
    assert result.status == "degraded"


@pytest.mark.asyncio
async def test_not_ready_aborts_with_persisted_snapshot(
    pool_pair, full_depot_config, controller_config
):
    pool, _ = pool_pair
    controller = DepotController(
        pools=pool,
        depot_id=str(uuid4()),
        config=full_depot_config,
        controller_config=controller_config,
    )

    state = _real_state()
    controller.assembler.get_current_state = AsyncMock(return_value=state)
    _prime_assembler(controller, building_source="meter")
    # Force a hard miss.
    controller.assembler._last_schedules_present = False
    controller.assembler._last_schedules = []

    captured: list = []

    async def fake_persist(_pools, snapshot):
        captured.append(snapshot)
        return snapshot.snapshot_id

    optimize_mock = MagicMock(return_value=_opt_result())
    controller.assembler.fetch_snapshot_extras = AsyncMock()
    with (
        patch("src.core.controller.persist_snapshot", side_effect=fake_persist),
        patch("src.core.controller.link_snapshot_to_run", new=AsyncMock()),
        patch("src.core.controller.optimize", optimize_mock),
        patch.object(controller, "_store_result", new=AsyncMock()),
        patch.object(controller, "_dispatch_commands", new=AsyncMock()),
    ):
        with pytest.raises(Exception, match="not ready"):
            await controller.run_optimization("test")

    # Exactly one snapshot per logical run, even on hard veto: the
    # capture happens once before the retry loop and the readiness gate
    # raises before any solver attempt.
    assert len(captured) == 1
    snap = captured[0]
    assert snap.readiness.status == "not_ready"
    assert "schedules" in snap.readiness.missing_inputs
    optimize_mock.assert_not_called()
