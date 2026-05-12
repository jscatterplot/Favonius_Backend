"""Unit tests for :class:`DatabaseToolBundle` against a fake asyncpg pool.

Validates that each tool produces the dict shape the workflow expects
when the DB returns realistic rows, and that empty-input short-circuits
correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from src.core.workflows.tools import DatabaseToolBundle


DEPOT_ID = UUID("33333333-3333-4333-8333-333333333333")
VEH_A = "11111111-1111-4111-8111-111111111111"
CH_1 = "55555555-5555-4555-8555-555555555555"


class _Conn:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        for key, rows in self.plan.items():
            if key in query:
                return [dict(r) for r in rows]
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        for key, row in self.plan.get("__rows__", {}).items():
            if key in query:
                return row
        return None


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, plan: dict[str, Any]) -> None:
        self._conn = _Conn(plan)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _pools(static_plan: dict[str, Any], ts_plan: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(static=_Pool(static_plan), ts=_Pool(ts_plan))


@pytest.mark.asyncio
async def test_get_scheduled_departures_returns_workflow_shape():
    now = datetime(2026, 5, 13, 4, 0, tzinfo=timezone.utc)
    static_plan = {
        "FROM schedules s": [
            {
                "vehicle_id": VEH_A,
                "route_id": "R-101",
                "departure_time": now,
                "required_soc": 0.99,
            }
        ]
    }
    pools = _pools(static_plan, {})
    bundle = DatabaseToolBundle(pools=pools)
    rows = await bundle.get_scheduled_departures(DEPOT_ID, now, now)
    assert rows == [
        {
            "vehicle_id": VEH_A,
            "route_id": "R-101",
            "departure_time": now,
            "required_soc": 0.99,
            "charger_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_scheduled_departures_with_no_pools_short_circuits():
    bundle = DatabaseToolBundle(pools=None)
    rows = await bundle.get_scheduled_departures(DEPOT_ID, datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert rows == []


@pytest.mark.asyncio
async def test_get_vehicle_state_merges_static_and_telemetry():
    static_plan = {
        "battery_capacity_kwh": [
            {"vehicle_id": VEH_A, "battery_kwh": 200.0, "max_charge_kw": 60.0}
        ]
    }
    ts_plan = {
        "FROM telemetry": [
            {
                "vehicle_id": VEH_A,
                "soc": 0.81,
                "is_plugged": True,
                "charging_kw": 22.0,
                "max_charge_kw": 60.0,
                "charger_id": CH_1,
            }
        ]
    }
    pools = _pools(static_plan, ts_plan)
    bundle = DatabaseToolBundle(pools=pools)
    state = await bundle.get_vehicle_state([VEH_A])
    assert state == {
        VEH_A: {
            "soc": 0.81,
            "plugged_in": True,
            "charger_id": CH_1,
            "battery_kwh": 200.0,
            "max_charge_kw": 60.0,
        }
    }


@pytest.mark.asyncio
async def test_get_vehicle_state_empty_input_short_circuits():
    bundle = DatabaseToolBundle(pools=_pools({}, {}))
    state = await bundle.get_vehicle_state([])
    assert state == {}


@pytest.mark.asyncio
async def test_get_charger_state_falls_back_to_available_for_unknown_status():
    """A charger with no connector_status rows is treated as Available."""
    static_plan = {
        "FROM charging_stations": [
            {"charger_id": CH_1, "station_id": "OCPP-1", "max_power_kw": 22.0}
        ]
    }
    ts_plan = {
        "FROM connector_status": []  # no status rows
    }
    pools = _pools(static_plan, ts_plan)
    bundle = DatabaseToolBundle(pools=pools)
    state = await bundle.get_charger_state([CH_1])
    assert state[CH_1]["status"] == "Available"
    assert state[CH_1]["fault_code"] is None
    assert state[CH_1]["rated_kw"] == 22.0


@pytest.mark.asyncio
async def test_get_charger_state_surfaces_latest_fault():
    static_plan = {
        "FROM charging_stations": [
            {"charger_id": CH_1, "station_id": "OCPP-1", "max_power_kw": 22.0}
        ]
    }
    ts_plan = {
        "FROM connector_status": [
            {
                "station_id": "OCPP-1",
                "status": "Faulted",
                "error_code": "GroundFailure",
                "timestamp": datetime.now(timezone.utc),
            }
        ]
    }
    pools = _pools(static_plan, ts_plan)
    bundle = DatabaseToolBundle(pools=pools)
    state = await bundle.get_charger_state([CH_1])
    assert state[CH_1]["status"] == "Faulted"
    assert state[CH_1]["fault_code"] == "GroundFailure"


@pytest.mark.asyncio
async def test_get_charging_plan_pulls_first_power_kw_from_run():
    static_plan = {}
    ts_plan = {
        "__rows__": {
            "FROM optimization_runs": {
                "schedule_json": json.dumps(
                    {VEH_A: [{"timestep": 0, "power_kw": 22.0}, {"timestep": 1, "power_kw": 30.0}]}
                )
            }
        }
    }
    pools = _pools(static_plan, ts_plan)
    bundle = DatabaseToolBundle(pools=pools)
    plans = await bundle.get_charging_plan([VEH_A])
    assert plans == {VEH_A: {"planned_power_kw": 22.0}}


@pytest.mark.asyncio
async def test_get_charging_plan_tuple_entry_format():
    static_plan = {}
    ts_plan = {
        "__rows__": {
            "FROM optimization_runs": {
                "schedule_json": {VEH_A: [(0, 30.0), (1, 20.0)]}
            }
        }
    }
    pools = _pools(static_plan, ts_plan)
    bundle = DatabaseToolBundle(pools=pools)
    plans = await bundle.get_charging_plan([VEH_A])
    assert plans == {VEH_A: {"planned_power_kw": 30.0}}


@pytest.mark.asyncio
async def test_get_charging_plan_missing_run_defaults_to_zero():
    pools = _pools({}, {"__rows__": {}})
    bundle = DatabaseToolBundle(pools=pools)
    plans = await bundle.get_charging_plan([VEH_A])
    assert plans == {VEH_A: {"planned_power_kw": 0.0}}


@pytest.mark.asyncio
async def test_get_driver_assignment_v1_default_valid():
    """V1: every route is treated as having a valid driver (PRD §6.4 deferred)."""
    bundle = DatabaseToolBundle(pools=_pools({}, {}))
    out = await bundle.get_driver_assignment(["R-101", "R-102"])
    assert out["R-101"]["valid"] is True
    assert out["R-102"]["valid"] is True


@pytest.mark.asyncio
async def test_find_alternate_chargers_returns_capable_only():
    static_plan = {
        "FROM charging_stations\n        WHERE site_id": [
            {"charger_id": CH_1, "rated_kw": 150.0, "station_id": "CH-FAST"}
        ]
    }
    pools = _pools(static_plan, {})
    bundle = DatabaseToolBundle(pools=pools)
    rows = await bundle.find_alternate_chargers(DEPOT_ID, 50.0)
    assert rows == [{"charger_id": CH_1, "rated_kw": 150.0, "station_id": "CH-FAST"}]


@pytest.mark.asyncio
async def test_find_alternate_chargers_no_pools_short_circuits():
    bundle = DatabaseToolBundle(pools=None)
    rows = await bundle.find_alternate_chargers(DEPOT_ID, 50.0)
    assert rows == []
