"""Unit tests for the readiness-workflow tools.

Each tool is exercised with a fake asyncpg pool whose ``fetch`` /
``fetchrow`` are :class:`AsyncMock`s. The tests verify:

- The SQL parameters carry the right tenant scope (``visible_depot_ids``).
- The happy path returns the expected shape.
- Cross-org access returns empty / ``None``-bearing rows (never the row).
- Stale telemetry (>15 min) flips ``telemetry_fresh`` to ``False`` and
  blanks the data fields.
- Empty data never raises.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows import get_registry  # type: ignore[attr-defined]
from src.api.agent_workflows.tools import readiness
from src.api.agent_workflows.tools.registry import ToolRegistry, register_tool
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


def _fresh_now() -> datetime:
    """Fixed UTC anchor used for staleness tests."""
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------- #
# get_registry side-effect: importing readiness must have populated it
# --------------------------------------------------------------------- #


class TestRegistry:
    def test_all_five_tools_registered(self):
        names = set(get_registry().names())
        assert {
            "get_scheduled_departures",
            "get_vehicle_state",
            "get_charger_state",
            "get_charging_plan",
            "get_driver_assignment",
        }.issubset(names)

    def test_registry_get_returns_callable(self):
        fn = get_registry().get("get_vehicle_state")
        assert fn is readiness.get_vehicle_state

    def test_registry_unknown_name_raises(self):
        with pytest.raises(KeyError):
            get_registry().get("does_not_exist")

    def test_registry_double_register_is_idempotent(self):
        reg = ToolRegistry()

        async def fn(_x):
            return _x

        reg.register("dup", fn)
        reg.register("dup", fn)
        assert reg.get("dup") is fn

    def test_registry_register_collision_raises(self):
        reg = ToolRegistry()

        async def a():
            return 1

        async def b():
            return 2

        reg.register("name", a)
        with pytest.raises(ValueError):
            reg.register("name", b)

    def test_decorator_inserts_into_global_registry(self):
        @register_tool("test_decorator_marker")
        async def _t():
            return None

        assert get_registry().get("test_decorator_marker") is _t

    def test_registry_snapshot_returns_copy(self):
        reg = ToolRegistry()

        async def fn():
            return None

        reg.register("snap_tool", fn)
        snap = reg.snapshot()
        assert snap == {"snap_tool": fn}
        # Mutating the snapshot must not affect the registry.
        snap.pop("snap_tool")
        assert reg.get("snap_tool") is fn


# --------------------------------------------------------------------- #
# get_scheduled_departures
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestScheduledDepartures:
    async def test_happy_path(self):
        v1 = uuid4()
        window_start = datetime(2026, 5, 12, 5, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
        pool = _pool(
            fetch=[
                {
                    "vehicle_id": v1,
                    "route_id": "R-101",
                    "departure_time": datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
                    "required_soc": 0.85,
                }
            ]
        )

        result = await readiness.get_scheduled_departures(
            _auth(), pool, DEPOT_A, window_start, window_end
        )

        assert result == [
            {
                "vehicle_id": v1,
                "route_id": "R-101",
                "departure_time": datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
                "required_soc": 0.85,
            }
        ]
        # SQL params include the depot UUID, window bounds.
        args = pool.fetch.await_args.args
        assert args[1] == DEPOT_A
        assert args[2] == window_start
        assert args[3] == window_end

    async def test_cross_org_returns_empty(self):
        pool = _pool(fetch=[{"vehicle_id": uuid4()}])  # never reached
        result = await readiness.get_scheduled_departures(
            _auth([DEPOT_A]),
            pool,
            DEPOT_OTHER,  # not in visible_depot_ids
            datetime(2026, 5, 12, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
        )
        assert result == []
        # SQL was short-circuited — fetch is never called for out-of-scope.
        pool.fetch.assert_not_awaited()

    async def test_no_rows_returns_empty_list(self):
        pool = _pool(fetch=[])
        result = await readiness.get_scheduled_departures(
            _auth(),
            pool,
            DEPOT_A,
            datetime(2026, 5, 12, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
        )
        assert result == []


# --------------------------------------------------------------------- #
# get_vehicle_state
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestVehicleState:
    async def test_happy_path_fresh(self):
        vehicle_id = uuid4()
        charger_id = uuid4()
        now = _fresh_now()
        last_at = now - timedelta(minutes=2)
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

        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=now
        )

        assert result == {
            "vehicle_id": vehicle_id,
            "current_soc": 0.74,
            "plugged_in_to": charger_id,
            "max_charge_kw": 120.0,
            "last_telemetry_at": last_at,
            "telemetry_fresh": True,
        }

    async def test_stale_telemetry_blanks_fields(self):
        vehicle_id = uuid4()
        now = _fresh_now()
        last_at = now - (MAX_TELEMETRY_AGE + timedelta(minutes=1))
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.9,  # would be misleading; must be None
                "plugged_in_to": uuid4(),
                "max_charge_kw": 80.0,
                "is_plugged": True,
            }
        )

        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=now
        )

        assert result["telemetry_fresh"] is False
        assert result["current_soc"] is None
        assert result["plugged_in_to"] is None
        assert result["max_charge_kw"] is None
        # Caller still gets the timestamp so it can surface "X minutes stale".
        assert result["last_telemetry_at"] == last_at

    async def test_threshold_is_exactly_max_telemetry_age(self):
        """Boundary: a row exactly at MAX_TELEMETRY_AGE is still fresh."""
        vehicle_id = uuid4()
        now = _fresh_now()
        last_at = now - MAX_TELEMETRY_AGE  # equal, not strictly greater
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

        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=now
        )

        assert result["telemetry_fresh"] is True
        assert result["current_soc"] == 0.5

    async def test_cross_org_returns_empty_dict(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow=None)  # vehicle not visible
        ts_pool = _pool(fetchrow={"current_soc": 0.99})  # would be misleading

        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=_fresh_now()
        )

        assert result["vehicle_id"] == vehicle_id
        assert result["current_soc"] is None
        assert result["telemetry_fresh"] is False
        assert result["last_telemetry_at"] is None
        # ts pool is never asked when the vehicle is out of scope.
        ts_pool.fetchrow.assert_not_awaited()

    async def test_no_telemetry_row(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(fetchrow=None)
        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=_fresh_now()
        )
        assert result["telemetry_fresh"] is False
        assert result["current_soc"] is None
        assert result["last_telemetry_at"] is None

    async def test_not_plugged_means_plugged_in_to_is_none(self):
        """is_plugged=False should null out the charger id even if telemetry has one."""
        vehicle_id = uuid4()
        now = _fresh_now()
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": now,
                "current_soc": 0.6,
                "plugged_in_to": uuid4(),  # vehicle was plugged in earlier
                "max_charge_kw": 50.0,
                "is_plugged": False,
            }
        )

        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=now
        )
        assert result["plugged_in_to"] is None
        assert result["current_soc"] == 0.6

    async def test_naive_now_is_coerced_to_utc(self):
        vehicle_id = uuid4()
        last_at = datetime(2026, 5, 12, 11, 59, tzinfo=timezone.utc)
        static_pool = _pool(fetchrow={"vehicle_id": vehicle_id})
        ts_pool = _pool(
            fetchrow={
                "last_telemetry_at": last_at,
                "current_soc": 0.5,
                "plugged_in_to": None,
                "max_charge_kw": 50.0,
                "is_plugged": False,
            }
        )
        # Naive datetime — must not raise; tool coerces to UTC.
        naive_now = datetime(2026, 5, 12, 12, 0, 0)
        result = await readiness.get_vehicle_state(
            _auth(), static_pool, ts_pool, vehicle_id, now=naive_now
        )
        assert result["telemetry_fresh"] is True

    async def test_now_defaults_to_real_clock(self):
        """No explicit ``now=`` argument should still produce a sane result."""
        vehicle_id = uuid4()
        # Telemetry is recent enough to be fresh under any real clock.
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
        result = await readiness.get_vehicle_state(_auth(), static_pool, ts_pool, vehicle_id)
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

        # Two fetchrow calls on ts pool — return a different value per call.
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(
            side_effect=[
                {"status": "Charging", "error_code": None, "timestamp": ts},
                {"charging_kw": 42.5, "time": ts_telem},
            ]
        )

        result = await readiness.get_charger_state(_auth(), static_pool, ts_pool, charger_id)

        assert result == {
            "charger_id": charger_id,
            "status": "Charging",
            "current_kw": 42.5,
            "fault_code": None,
            "last_update_at": ts,  # status is newer than telemetry sample
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
        result = await readiness.get_charger_state(_auth(), static_pool, ts_pool, charger_id)
        assert result["status"] == "Faulted"
        assert result["fault_code"] == "GroundFailure"
        assert result["current_kw"] is None
        assert result["last_update_at"] == ts

    async def test_cross_org_returns_empty(self):
        charger_id = uuid4()
        static_pool = _pool(fetchrow=None)
        ts_pool = MagicMock()
        ts_pool.fetchrow = AsyncMock(side_effect=[{"status": "Charging"}, {"charging_kw": 10}])
        result = await readiness.get_charger_state(_auth(), static_pool, ts_pool, charger_id)
        assert result == {
            "charger_id": charger_id,
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
        result = await readiness.get_charger_state(_auth(), static_pool, ts_pool, charger_id)
        assert result["status"] is None
        assert result["current_kw"] is None
        assert result["fault_code"] is None
        assert result["last_update_at"] is None

    async def test_telemetry_newer_than_status_wins_last_update_at(self):
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
        result = await readiness.get_charger_state(_auth(), static_pool, ts_pool, charger_id)
        assert result["last_update_at"] == telem_ts


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

        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)

        assert result == [
            {"timestep": 0, "target_kw": 10.0, "projected_soc": 0.5},
            {"timestep": 1, "target_kw": 20.0, "projected_soc": 0.6},
            {"timestep": 2, "target_kw": 30.0, "projected_soc": 0.7},
        ]

    async def test_cross_org_returns_empty(self):
        static_pool = _pool(fetchrow=None)
        ts_pool = _pool(fetchrow={"schedule_json": {"schedule": {"x": {}}}})
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, uuid4())
        assert result == []
        ts_pool.fetchrow.assert_not_awaited()

    async def test_no_optimization_run_returns_empty(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow=None)
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == []

    async def test_vehicle_not_in_latest_run(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": {"schedule": {"some-other-vehicle": {}}},
                "horizon_start": datetime(2026, 5, 12, tzinfo=timezone.utc),
            }
        )
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == []

    async def test_schedule_json_as_string(self):
        """Some legacy rows store JSON as text; the tool tolerates that."""
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
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == [{"timestep": 0, "target_kw": 5.0, "projected_soc": 0.4}]

    async def test_malformed_schedule_json_returns_empty(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": "not-valid-json", "horizon_start": None})
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == []

    async def test_schedule_json_non_dict(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": [1, 2, 3], "horizon_start": None})
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == []

    async def test_truncates_to_shorter_list(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(
            fetchrow={
                "schedule_json": {
                    "schedule": {
                        str(vehicle_id): {
                            "charging_power": [10.0, 20.0],
                            "soc": [0.5, 0.6, 0.7],  # longer than charging_power
                        }
                    }
                },
                "horizon_start": None,
            }
        )
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert len(result) == 2

    async def test_empty_schedule_json(self):
        vehicle_id = uuid4()
        static_pool = _pool(fetchrow={"id": vehicle_id, "site_id": DEPOT_A})
        ts_pool = _pool(fetchrow={"schedule_json": None, "horizon_start": None})
        result = await readiness.get_charging_plan(_auth(), static_pool, ts_pool, vehicle_id)
        assert result == []


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

        result = await readiness.get_driver_assignment(_auth(), pool, "R-101")

        assert result == {
            "route_id": "R-101",
            "driver_id": driver_id,
            "driver_name": "John Smith",
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": True,
        }
        # SQL params include the route id and visible-depot scope.
        args = pool.fetchrow.await_args.args
        assert args[1] == "R-101"
        assert args[2] == [DEPOT_A, DEPOT_B]

    async def test_unassigned_route_returns_invalid(self):
        pool = _pool(
            fetchrow={
                "driver_id": None,
                "driver_name": None,
                "departure_time": datetime(2026, 5, 12, 6, tzinfo=timezone.utc),
                "return_time": datetime(2026, 5, 12, 18, tzinfo=timezone.utc),
            }
        )
        result = await readiness.get_driver_assignment(_auth(), pool, "R-NO-DRV")
        assert result["driver_id"] is None
        assert result["shift_valid_for_route"] is False

    async def test_cross_org_returns_empty(self):
        pool = _pool(fetchrow=None)
        result = await readiness.get_driver_assignment(_auth(), pool, "R-OTHER-ORG")
        assert result == {
            "route_id": "R-OTHER-ORG",
            "driver_id": None,
            "driver_name": None,
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": False,
        }

    async def test_empty_visible_depots_returns_empty(self):
        """A caller with no visible depots can never resolve a route."""
        pool = _pool(fetchrow=None)
        auth = _auth(depots=[])
        result = await readiness.get_driver_assignment(auth, pool, "R-1")
        assert result["driver_id"] is None
        assert result["shift_valid_for_route"] is False
