"""Unit tests for the Sprint-4 readiness tools.

Each tool is exercised by building the production
:class:`~src.api.agent_workflows.tools.ToolRegistry` via
:func:`~src.api.agent_workflows.readiness_tools.build_readiness_tool_registry`
and dispatching against fake asyncpg pools whose ``fetch`` /
``fetchrow`` are :class:`AsyncMock`s.

The tests verify:

- The factory registers exactly the five tools §6.1 lists.
- Each tool returns the expected shape on the happy path.
- Cross-org reads (``depot_id`` / entity outside ``visible_depot_ids``)
  return empty / ``None``-bearing data without touching the time-series
  pool.
- Stale telemetry (>15 min) flips ``telemetry_fresh`` to ``False`` and
  blanks the live data fields.
- Tools never raise on empty data — they return ``{"plan": []}`` /
  ``{"departures": []}`` / ``None``-bearing dicts.
- The registry's Anthropic schema export carries the right JSON
  schemas for the LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.readiness_tools import build_readiness_tool_registry
from src.api.agent_workflows.tools import ToolRegistry
from src.security.data_freshness import MAX_TELEMETRY_AGE

# Stable IDs make assertions readable.
DEPOT_A = UUID("11111111-1111-4111-8111-111111111111")
DEPOT_B = UUID("22222222-2222-4222-8222-222222222222")
DEPOT_OTHER = UUID("99999999-9999-4999-8999-999999999999")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
ORG_ID = UUID("44444444-4444-4444-8444-444444444444")


def _auth(depots: list[UUID] | None = None) -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        role="customer_admin",
        visible_depot_ids=depots if depots is not None else [DEPOT_A, DEPOT_B],
    )


def _pool(*, fetch: list | None = None, fetchrow: dict | None = None) -> MagicMock:
    """Build a fake asyncpg pool. Per-call return values can be overridden."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=fetch or [])
    pool.fetchrow = AsyncMock(return_value=fetchrow)
    return pool


_FRESH_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _build(static_pool, ts_pool, *, auth=None, now: datetime | None = _FRESH_NOW) -> ToolRegistry:
    return build_readiness_tool_registry(
        static_pool=static_pool,
        ts_pool=ts_pool,
        auth=auth or _auth(),
        now=now,
    )


# --------------------------------------------------------------------- #
# Registry shape / schema export
# --------------------------------------------------------------------- #


class TestRegistryShape:
    def test_factory_registers_all_five_tools(self):
        registry = _build(_pool(), _pool())
        assert set(registry.names()) == {
            "get_scheduled_departures",
            "get_vehicle_state",
            "get_charger_state",
            "get_charging_plan",
            "get_driver_assignment",
        }

    def test_anthropic_schemas_in_allow_list_order(self):
        registry = _build(_pool(), _pool())
        schemas = registry.anthropic_schemas(
            [
                "get_vehicle_state",
                "get_scheduled_departures",
                "get_driver_assignment",
            ]
        )
        assert [s["name"] for s in schemas] == [
            "get_vehicle_state",
            "get_scheduled_departures",
            "get_driver_assignment",
        ]
        # Spot-check schema shape — required keys are propagated.
        v_state = next(s for s in schemas if s["name"] == "get_vehicle_state")
        assert v_state["input_schema"]["required"] == ["vehicle_id"]

    def test_each_tool_definition_carries_description_and_schema(self):
        registry = _build(_pool(), _pool())
        for name in registry.names():
            defn = registry.get(name)
            assert defn.description.strip(), f"{name} missing description"
            assert defn.input_schema["type"] == "object"


# --------------------------------------------------------------------- #
# get_scheduled_departures
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestScheduledDepartures:
    async def test_happy_path(self):
        v1 = uuid4()
        window_start = "2026-05-12T05:00:00+00:00"
        window_end = "2026-05-12T09:00:00+00:00"
        pool = _pool(
            fetch=[
                {
                    "vehicle_id": str(v1),
                    "route_id": "R-101",
                    "departure_time": datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
                    "required_soc": 0.85,
                }
            ]
        )
        registry = _build(pool, _pool())
        result = await registry.dispatch(
            "get_scheduled_departures",
            {
                "depot_id": str(DEPOT_A),
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        assert result == {
            "departures": [
                {
                    "vehicle_id": str(v1),
                    "route_id": "R-101",
                    "departure_time": "2026-05-12T06:30:00+00:00",
                    "required_soc": 0.85,
                }
            ]
        }
        # SQL params include depot UUID + parsed datetimes.
        args = pool.fetch.await_args.args
        assert args[1] == DEPOT_A
        assert args[2] == datetime(2026, 5, 12, 5, 0, tzinfo=timezone.utc)
        assert args[3] == datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)

    async def test_cross_org_returns_empty(self):
        pool = _pool(fetch=[{"vehicle_id": str(uuid4())}])
        registry = _build(pool, _pool(), auth=_auth([DEPOT_A]))
        result = await registry.dispatch(
            "get_scheduled_departures",
            {
                "depot_id": str(DEPOT_OTHER),
                "window_start": "2026-05-12T05:00:00+00:00",
                "window_end": "2026-05-12T09:00:00+00:00",
            },
        )
        assert result == {"departures": []}
        pool.fetch.assert_not_awaited()

    async def test_zulu_time_format_accepted(self):
        pool = _pool(fetch=[])
        registry = _build(pool, _pool())
        await registry.dispatch(
            "get_scheduled_departures",
            {
                "depot_id": str(DEPOT_A),
                "window_start": "2026-05-12T05:00:00Z",
                "window_end": "2026-05-12T09:00:00Z",
            },
        )
        args = pool.fetch.await_args.args
        assert args[2] == datetime(2026, 5, 12, 5, 0, tzinfo=timezone.utc)

    async def test_naive_datetime_coerced_to_utc(self):
        pool = _pool(fetch=[])
        registry = _build(pool, _pool())
        await registry.dispatch(
            "get_scheduled_departures",
            {
                "depot_id": str(DEPOT_A),
                "window_start": "2026-05-12T05:00:00",
                "window_end": "2026-05-12T09:00:00",
            },
        )
        args = pool.fetch.await_args.args
        assert args[2].tzinfo is not None

    async def test_no_rows_returns_empty(self):
        registry = _build(_pool(fetch=[]), _pool())
        result = await registry.dispatch(
            "get_scheduled_departures",
            {
                "depot_id": str(DEPOT_A),
                "window_start": "2026-05-12T05:00:00+00:00",
                "window_end": "2026-05-12T09:00:00+00:00",
            },
        )
        assert result == {"departures": []}


# --------------------------------------------------------------------- #
# get_vehicle_state
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestVehicleState:
    async def test_happy_path_fresh(self):
        vehicle_id = uuid4()
        charger_id = uuid4()
        last_at = _FRESH_NOW - timedelta(minutes=2)
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.74,
                "plugged_in_to": charger_id,
                "max_charge_kw": 120.0,
                "is_plugged": True,
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result == {
            "vehicle_id": str(vehicle_id),
            "current_soc": 0.74,
            "plugged_in_to": str(charger_id),
            "max_charge_kw": 120.0,
            "last_telemetry_at": last_at.isoformat(),
            "telemetry_fresh": True,
        }

    async def test_stale_telemetry_blanks_fields(self):
        vehicle_id = uuid4()
        last_at = _FRESH_NOW - (MAX_TELEMETRY_AGE + timedelta(minutes=1))
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.9,
                "plugged_in_to": uuid4(),
                "max_charge_kw": 80.0,
                "is_plugged": True,
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["telemetry_fresh"] is False
        assert result["current_soc"] is None
        assert result["plugged_in_to"] is None
        assert result["max_charge_kw"] is None
        assert result["last_telemetry_at"] == last_at.isoformat()

    async def test_threshold_is_exactly_max_telemetry_age(self):
        vehicle_id = uuid4()
        last_at = _FRESH_NOW - MAX_TELEMETRY_AGE  # equal → still fresh
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.5,
                "plugged_in_to": None,
                "max_charge_kw": 100.0,
                "is_plugged": False,
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["telemetry_fresh"] is True
        assert result["current_soc"] == 0.5

    async def test_cross_org_returns_none_bearing(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow=None)  # not visible
        ts_pool = _pool(fetchrow={"current_soc": 0.99})
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["vehicle_id"] == str(vehicle_id)
        assert result["current_soc"] is None
        assert result["telemetry_fresh"] is False
        ts_pool.fetchrow.assert_not_awaited()

    async def test_no_telemetry_row(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(fetchrow=None)
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["telemetry_fresh"] is False
        assert result["current_soc"] is None
        assert result["last_telemetry_at"] is None

    async def test_not_plugged_nullifies_charger(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": _FRESH_NOW,
                "current_soc": 0.6,
                "plugged_in_to": uuid4(),
                "max_charge_kw": 50.0,
                "is_plugged": False,
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["plugged_in_to"] is None
        assert result["current_soc"] == 0.6

    async def test_default_clock_when_no_now_passed(self):
        """The factory's ``now`` is optional and defaults to wall-clock UTC."""
        vehicle_id = uuid4()
        last_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.8,
                "plugged_in_to": None,
                "max_charge_kw": 50.0,
                "is_plugged": False,
            }
        )
        registry = build_readiness_tool_registry(
            static_pool=static_pool,
            ts_pool=ts_pool,
            auth=_auth(),
        )
        result = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(vehicle_id)})
        assert result["telemetry_fresh"] is True


# --------------------------------------------------------------------- #
# get_charger_state
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestChargerState:
    async def test_happy_path(self):
        charger_id = uuid4()
        ts = datetime(2026, 5, 12, 11, 59, tzinfo=timezone.utc)
        ts_telem = datetime(2026, 5, 12, 11, 58, tzinfo=timezone.utc)
        static_pool = _pool(
            fetchrow={
                "charger_id": charger_id,
                "ocpp_id": "CP-001",
                "site_id": DEPOT_A,
            }
        )
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(
            side_effect=[
                {"status": "Charging", "error_code": None, "timestamp": ts},
                {"charging_kw": 42.5, "time": ts_telem},
            ]
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charger_state", {"charger_id": str(charger_id)})
        assert result == {
            "charger_id": str(charger_id),
            "status": "Charging",
            "current_kw": 42.5,
            "fault_code": None,
            "last_update_at": ts.isoformat(),  # status newer than telemetry
        }

    async def test_faulted_with_error_code(self):
        charger_id = uuid4()
        ts = datetime(2026, 5, 12, 11, 30, tzinfo=timezone.utc)
        static_pool = _pool(
            fetchrow={"charger_id": charger_id, "ocpp_id": "CP-002", "site_id": DEPOT_A}
        )
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(
            side_effect=[
                {"status": "Faulted", "error_code": "GroundFailure", "timestamp": ts},
                None,
            ]
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charger_state", {"charger_id": str(charger_id)})
        assert result["status"] == "Faulted"
        assert result["fault_code"] == "GroundFailure"
        assert result["current_kw"] is None
        assert result["last_update_at"] == ts.isoformat()

    async def test_cross_org_returns_empty(self):
        charger_id = uuid4()
        static_pool = _pool(fetchrow=None)
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(side_effect=[{"status": "Charging"}, {"charging_kw": 10}])
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charger_state", {"charger_id": str(charger_id)})
        assert result == {
            "charger_id": str(charger_id),
            "status": None,
            "current_kw": None,
            "fault_code": None,
            "last_update_at": None,
        }
        ts_pool.fetchrow.assert_not_awaited()

    async def test_no_status_no_telemetry(self):
        charger_id = uuid4()
        static_pool = _pool(
            fetchrow={"charger_id": charger_id, "ocpp_id": "CP-3", "site_id": DEPOT_A}
        )
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(side_effect=[None, None])
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charger_state", {"charger_id": str(charger_id)})
        assert result["status"] is None
        assert result["current_kw"] is None
        assert result["fault_code"] is None
        assert result["last_update_at"] is None

    async def test_telemetry_newer_than_status_wins(self):
        charger_id = uuid4()
        status_ts = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
        telem_ts = datetime(2026, 5, 12, 11, 30, tzinfo=timezone.utc)
        static_pool = _pool(
            fetchrow={"charger_id": charger_id, "ocpp_id": "CP-4", "site_id": DEPOT_A}
        )
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(
            side_effect=[
                {"status": "Available", "error_code": None, "timestamp": status_ts},
                {"charging_kw": 0.0, "time": telem_ts},
            ]
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charger_state", {"charger_id": str(charger_id)})
        assert result["last_update_at"] == telem_ts.isoformat()


# --------------------------------------------------------------------- #
# get_charging_plan
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestChargingPlan:
    async def test_happy_path(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": {
                    "schedule": {
                        str(vehicle_id): {
                            "charging_power": [10.0, 20.0, 30.0],
                            "soc": [0.5, 0.6, 0.7],
                        }
                    }
                },
                "horizon_start": datetime(2026, 5, 12, tzinfo=timezone.utc),
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {
            "plan": [
                {"timestep": 0, "target_kw": 10.0, "projected_soc": 0.5},
                {"timestep": 1, "target_kw": 20.0, "projected_soc": 0.6},
                {"timestep": 2, "target_kw": 30.0, "projected_soc": 0.7},
            ]
        }

    async def test_cross_org_returns_empty(self):
        static_pool = _pool(fetchrow=None)
        ts_pool = _pool(fetchrow={"schedule_json": {"schedule": {"x": {}}}})
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(uuid4())})
        assert result == {"plan": []}
        ts_pool.fetchrow.assert_not_awaited()

    async def test_no_optimization_run(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow=None)
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": []}

    async def test_vehicle_not_in_latest_run(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": {"schedule": {"some-other-vehicle": {}}},
                "horizon_start": datetime(2026, 5, 12, tzinfo=timezone.utc),
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": []}

    async def test_schedule_json_as_text(self):
        import json

        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": json.dumps(
                    {
                        "schedule": {
                            str(vehicle_id): {
                                "charging_power": [5.0],
                                "soc": [0.4],
                            }
                        }
                    }
                ),
                "horizon_start": datetime(2026, 5, 12, tzinfo=timezone.utc),
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": [{"timestep": 0, "target_kw": 5.0, "projected_soc": 0.4}]}

    async def test_malformed_schedule_json(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": "not-valid-json", "horizon_start": None})
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": []}

    async def test_schedule_json_non_dict(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": [1, 2, 3], "horizon_start": None})
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": []}

    async def test_mismatched_list_lengths_truncate(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": {
                    "schedule": {
                        str(vehicle_id): {
                            "charging_power": [10.0, 20.0],
                            "soc": [0.5, 0.6, 0.7],
                        }
                    }
                },
                "horizon_start": None,
            }
        )
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert len(result["plan"]) == 2

    async def test_empty_schedule_json(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": None, "horizon_start": None})
        registry = _build(static_pool, ts_pool)
        result = await registry.dispatch("get_charging_plan", {"vehicle_id": str(vehicle_id)})
        assert result == {"plan": []}


# --------------------------------------------------------------------- #
# get_driver_assignment
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestDriverAssignment:
    async def test_happy_path(self):
        driver_id = uuid4()
        pool = _pool(
            fetchrow={
                "driver_id": driver_id,
                "driver_name": "John Smith",
                "departure_time": datetime(2026, 5, 12, 6, tzinfo=timezone.utc),
                "return_time": datetime(2026, 5, 12, 18, tzinfo=timezone.utc),
            }
        )
        registry = _build(pool, _pool())
        result = await registry.dispatch("get_driver_assignment", {"route_id": "R-101"})
        assert result == {
            "route_id": "R-101",
            "driver_id": str(driver_id),
            "driver_name": "John Smith",
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": True,
        }
        args = pool.fetchrow.await_args.args
        assert args[1] == "R-101"
        assert args[2] == [DEPOT_A, DEPOT_B]

    async def test_unassigned_route(self):
        pool = _pool(
            fetchrow={
                "driver_id": None,
                "driver_name": None,
                "departure_time": datetime(2026, 5, 12, 6, tzinfo=timezone.utc),
                "return_time": datetime(2026, 5, 12, 18, tzinfo=timezone.utc),
            }
        )
        registry = _build(pool, _pool())
        result = await registry.dispatch("get_driver_assignment", {"route_id": "R-NO-DRV"})
        assert result["driver_id"] is None
        assert result["shift_valid_for_route"] is False

    async def test_cross_org_returns_empty(self):
        pool = _pool(fetchrow=None)
        registry = _build(pool, _pool())
        result = await registry.dispatch("get_driver_assignment", {"route_id": "R-OTHER-ORG"})
        assert result == {
            "route_id": "R-OTHER-ORG",
            "driver_id": None,
            "driver_name": None,
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": False,
        }

    async def test_empty_visible_depots_returns_empty(self):
        pool = _pool(fetchrow=None)
        registry = _build(pool, _pool(), auth=_auth(depots=[]))
        result = await registry.dispatch("get_driver_assignment", {"route_id": "R-1"})
        assert result["driver_id"] is None
        assert result["shift_valid_for_route"] is False
