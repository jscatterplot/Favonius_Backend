"""End-to-end test for the Sprint-4 readiness tools.

Seeds an isolated Postgres schema (``readiness_e2e``) with the static
and time-series tables the tools touch, drops a depot containing two
vehicles, two chargers, one schedule, and one driver, then dispatches
each tool through the Sprint-2 :class:`ToolRegistry` built by
:func:`build_readiness_tool_registry` and asserts the joined shape
end-to-end.

If the test database is unreachable the suite skips — the unit tests
still cover the pure-Python paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.readiness_tools import build_readiness_tool_registry

# ── Schema bootstrap ──────────────────────────────────────────────────────


_SCHEMA = "readiness_e2e"


_DDL = f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
SET search_path TO {_SCHEMA};

CREATE TABLE organizations (
    id   UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE sites (
    id              UUID PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    timezone        VARCHAR(50) DEFAULT 'UTC'
);

CREATE TABLE vehicles (
    id              UUID PRIMARY KEY,
    organization_id UUID,
    site_id         UUID REFERENCES sites(id) ON DELETE SET NULL,
    display_name    VARCHAR(255)
);

CREATE TABLE drivers (
    id                 UUID PRIMARY KEY,
    site_id            UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    display_name       VARCHAR(255) NOT NULL,
    status             VARCHAR(32) NOT NULL DEFAULT 'active'
);

CREATE TABLE charging_stations (
    id           UUID PRIMARY KEY,
    site_id      UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    station_id   VARCHAR(255) NOT NULL,
    display_name VARCHAR(255)
);

CREATE TABLE schedules (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id         UUID NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    route_id           VARCHAR(100),
    departure_time     TIMESTAMPTZ NOT NULL,
    return_time        TIMESTAMPTZ NOT NULL,
    actual_return_time TIMESTAMPTZ,
    energy_kwh         DOUBLE PRECISION,
    required_soc       DOUBLE PRECISION DEFAULT 1.0,
    driver_id          UUID REFERENCES drivers(id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE telemetry (
    time            TIMESTAMPTZ NOT NULL,
    station_id      TEXT NOT NULL,
    connector_id    INTEGER NOT NULL DEFAULT 1,
    vehicle_id      UUID,
    charger_id      UUID,
    soc             DOUBLE PRECISION,
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION,
    max_charge_kw   DOUBLE PRECISION,
    PRIMARY KEY (time, station_id, connector_id)
);

CREATE TABLE connector_status (
    station_id   VARCHAR(255) NOT NULL,
    connector_id INTEGER NOT NULL,
    status       VARCHAR(50) NOT NULL,
    error_code   VARCHAR(100),
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE optimization_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL,
    run_time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trigger_reason  VARCHAR(50) NOT NULL DEFAULT 'scheduled',
    horizon_start   TIMESTAMPTZ NOT NULL,
    horizon_end     TIMESTAMPTZ NOT NULL,
    status          VARCHAR(20) DEFAULT 'optimal',
    solver_used     VARCHAR(20) DEFAULT 'highs',
    schedule_json   JSONB NOT NULL
);
"""


# Stable UUIDs make assertions readable.
ORG_A = UUID("11111111-1111-4111-8111-111111111111")
DEPOT_A = UUID("22222222-2222-4222-8222-222222222222")
VEHICLE_1 = UUID("33333333-3333-4333-8333-333333333331")
VEHICLE_2 = UUID("33333333-3333-4333-8333-333333333332")
CHARGER_1 = UUID("44444444-4444-4444-8444-444444444441")
CHARGER_2 = UUID("44444444-4444-4444-8444-444444444442")
DRIVER_1 = UUID("55555555-5555-4555-8555-555555555555")
USER_A = UUID("66666666-6666-4666-8666-666666666666")
ROUTE_ID = "R-101"


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )


async def _bootstrap_schema() -> None:
    conn = await asyncpg.connect(_test_db_url())
    try:
        await conn.execute(_DDL)
    finally:
        await conn.close()


_SCHEMA_SERVER_SETTINGS = {"search_path": f"{_SCHEMA}, public"}


@pytest_asyncio.fixture
async def db_pools():
    try:
        await _bootstrap_schema()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"readiness e2e: test database unavailable: {exc}")

    static_pool = await asyncpg.create_pool(
        _test_db_url(),
        min_size=1,
        max_size=4,
        command_timeout=15,
        server_settings=_SCHEMA_SERVER_SETTINGS,
    )
    ts_pool = await asyncpg.create_pool(
        _test_db_url(),
        min_size=1,
        max_size=4,
        command_timeout=15,
        server_settings=_SCHEMA_SERVER_SETTINGS,
    )
    try:
        yield static_pool, ts_pool
    finally:
        await static_pool.close()
        await ts_pool.close()


@pytest_asyncio.fixture
async def seeded(db_pools):
    static_pool, ts_pool = db_pools
    now = datetime.now(timezone.utc).replace(microsecond=0)

    async with static_pool.acquire() as conn:
        await conn.execute("INSERT INTO organizations (id, name) VALUES ($1, 'Org A')", ORG_A)
        await conn.execute(
            "INSERT INTO sites (id, organization_id, name, timezone) "
            "VALUES ($1, $2, 'Vilnius depot', 'Europe/Vilnius')",
            DEPOT_A,
            ORG_A,
        )
        await conn.executemany(
            "INSERT INTO vehicles (id, organization_id, site_id, display_name) "
            "VALUES ($1, $2, $3, $4)",
            [
                (VEHICLE_1, ORG_A, DEPOT_A, "BUS-001"),
                (VEHICLE_2, ORG_A, DEPOT_A, "BUS-002"),
            ],
        )
        await conn.executemany(
            "INSERT INTO charging_stations (id, site_id, station_id, display_name) "
            "VALUES ($1, $2, $3, $4)",
            [
                (CHARGER_1, DEPOT_A, "CP-001", "Charger 1"),
                (CHARGER_2, DEPOT_A, "CP-002", "Charger 2"),
            ],
        )
        await conn.execute(
            "INSERT INTO drivers (id, site_id, display_name) VALUES ($1, $2, $3)",
            DRIVER_1,
            DEPOT_A,
            "John Smith",
        )
        await conn.execute(
            """
            INSERT INTO schedules
                (vehicle_id, route_id, departure_time, return_time,
                 energy_kwh, required_soc, driver_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            VEHICLE_1,
            ROUTE_ID,
            now + timedelta(hours=1),
            now + timedelta(hours=9),
            120.0,
            0.85,
            DRIVER_1,
        )

    async with ts_pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry (
                time, station_id, connector_id, vehicle_id, charger_id,
                soc, is_plugged, charging_kw, max_charge_kw
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            [
                (now, "CP-001", 1, VEHICLE_1, CHARGER_1, 0.72, True, 40.0, 120.0),
                (now, "CP-002", 1, VEHICLE_2, CHARGER_2, 0.40, False, 0.0, 100.0),
            ],
        )

        await conn.executemany(
            "INSERT INTO connector_status (station_id, connector_id, status, "
            "error_code, timestamp) VALUES ($1, $2, $3, $4, $5)",
            [
                ("CP-001", 1, "Charging", None, now),
                ("CP-002", 1, "Available", None, now),
            ],
        )

        import json

        await conn.execute(
            """
            INSERT INTO optimization_runs
                (depot_id, run_time, horizon_start, horizon_end,
                 status, solver_used, schedule_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            DEPOT_A,
            now,
            now,
            now + timedelta(hours=4),
            "optimal",
            "highs",
            json.dumps(
                {
                    "schedule": {
                        str(VEHICLE_1): {
                            "charging_power": [40.0, 40.0, 30.0, 20.0],
                            "soc": [0.72, 0.82, 0.90, 0.95],
                        }
                    }
                }
            ),
        )

    return {"static_pool": static_pool, "ts_pool": ts_pool, "now": now}


def _auth_for(depots: list[UUID]) -> AuthContext:
    return AuthContext(
        user_id=USER_A,
        organization_id=ORG_A,
        role="customer_admin",
        visible_depot_ids=depots,
    )


# ── The tests ─────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_tools_end_to_end(seeded):
    static_pool: Any = seeded["static_pool"]
    ts_pool: Any = seeded["ts_pool"]
    now: datetime = seeded["now"]

    registry = build_readiness_tool_registry(
        static_pool=static_pool,
        ts_pool=ts_pool,
        auth=_auth_for([DEPOT_A]),
        now=now,
    )

    # 1) get_scheduled_departures
    scheduled = await registry.dispatch(
        "get_scheduled_departures",
        {
            "depot_id": str(DEPOT_A),
            "window_start": now.isoformat(),
            "window_end": (now + timedelta(hours=24)).isoformat(),
        },
    )
    assert len(scheduled["departures"]) == 1
    row = scheduled["departures"][0]
    assert row["vehicle_id"] == str(VEHICLE_1)
    assert row["route_id"] == ROUTE_ID
    assert row["required_soc"] == 0.85

    # 2) get_vehicle_state — fresh, plugged in
    state_v1 = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(VEHICLE_1)})
    assert state_v1["vehicle_id"] == str(VEHICLE_1)
    assert state_v1["current_soc"] == pytest.approx(0.72)
    assert state_v1["plugged_in_to"] == str(CHARGER_1)
    assert state_v1["max_charge_kw"] == pytest.approx(120.0)
    assert state_v1["telemetry_fresh"] is True

    # Vehicle 2 — not plugged in, charger field nulled.
    state_v2 = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(VEHICLE_2)})
    assert state_v2["plugged_in_to"] is None
    assert state_v2["telemetry_fresh"] is True

    # 3) get_charger_state
    chg = await registry.dispatch("get_charger_state", {"charger_id": str(CHARGER_1)})
    assert chg["status"] == "Charging"
    assert chg["fault_code"] is None
    assert chg["current_kw"] == pytest.approx(40.0)
    assert chg["last_update_at"] is not None

    # 4) get_charging_plan
    plan = await registry.dispatch("get_charging_plan", {"vehicle_id": str(VEHICLE_1)})
    assert len(plan["plan"]) == 4
    assert plan["plan"][0] == {"timestep": 0, "target_kw": 40.0, "projected_soc": 0.72}
    assert plan["plan"][-1]["projected_soc"] == pytest.approx(0.95)

    plan_v2 = await registry.dispatch("get_charging_plan", {"vehicle_id": str(VEHICLE_2)})
    assert plan_v2 == {"plan": []}

    # 5) get_driver_assignment
    assignment = await registry.dispatch("get_driver_assignment", {"route_id": ROUTE_ID})
    assert assignment["driver_id"] == str(DRIVER_1)
    assert assignment["driver_name"] == "John Smith"
    assert assignment["shift_valid_for_route"] is True

    missing = await registry.dispatch("get_driver_assignment", {"route_id": "R-DOES-NOT-EXIST"})
    assert missing["driver_id"] is None
    assert missing["shift_valid_for_route"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_org_isolation_e2e(seeded):
    """A caller whose visible_depot_ids excludes DEPOT_A sees nothing."""
    static_pool = seeded["static_pool"]
    ts_pool = seeded["ts_pool"]
    now: datetime = seeded["now"]
    other_depot = UUID("88888888-8888-4888-8888-888888888888")

    registry = build_readiness_tool_registry(
        static_pool=static_pool,
        ts_pool=ts_pool,
        auth=_auth_for([other_depot]),
        now=now,
    )

    scheduled = await registry.dispatch(
        "get_scheduled_departures",
        {
            "depot_id": str(DEPOT_A),
            "window_start": now.isoformat(),
            "window_end": (now + timedelta(hours=24)).isoformat(),
        },
    )
    assert scheduled == {"departures": []}

    state = await registry.dispatch("get_vehicle_state", {"vehicle_id": str(VEHICLE_1)})
    assert state["current_soc"] is None
    assert state["telemetry_fresh"] is False

    chg = await registry.dispatch("get_charger_state", {"charger_id": str(CHARGER_1)})
    assert chg["status"] is None

    plan = await registry.dispatch("get_charging_plan", {"vehicle_id": str(VEHICLE_1)})
    assert plan == {"plan": []}

    assignment = await registry.dispatch("get_driver_assignment", {"route_id": ROUTE_ID})
    assert assignment["driver_id"] is None
