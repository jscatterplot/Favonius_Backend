"""Integration tests for GET /depots/{id}/power-timeline against real TimescaleDB.

Exercises the history-aggregation SQL the unit tests can't fake (they mock the
query's output rows). The regression of record: a real depot (HRX Vilnius) runs
ABB Terra chargers whose firmware emits only the cumulative
``Energy.Active.Import.Register`` measurand and no instantaneous
``Power.Active.Import`` — so ``telemetry.charging_kw`` is NULL and the
"tonight's plan" chart rendered blank. ``_build_power_timeline`` must derive the
per-bucket kW from the energy-register delta and return a populated ``history``
series even when no optimization run exists.

Run with::

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test \\
        pytest tests/integration/test_power_timeline_integration.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

import src.api.main as main
from src.api.main import _build_power_timeline
from tests.integration.conftest import IntegrationPools


@pytest_asyncio.fixture
async def seeded(db_pools: IntegrationPools):
    """Seed a throwaway depot + ABB-style charger and clean up afterwards.

    Skips unless both static (sites/charging_stations) and Timescale
    (telemetry) tables live in the same DB, which is the local combined-DB
    setup integration CI uses.
    """
    if not db_pools.can_seed_sites:
        pytest.skip("needs combined TEST_DATABASE_URL (sites + telemetry in one DB)")

    depot_id = uuid4()
    vehicle_id = uuid4()
    station_id = f"it-pt-{uuid4().hex[:8]}"

    static = db_pools.static_pool
    ts = db_pools.ts_pool
    async with static.acquire() as conn:
        # name + address are NOT NULL with no default on the Supabase sites table.
        await conn.execute(
            "INSERT INTO sites (id, name, address, timezone) VALUES ($1, $2, $3, $4)",
            depot_id,
            "IT Power-Timeline Depot",
            "1 Integration Way",
            "Europe/Vilnius",
        )
        await conn.execute(
            "INSERT INTO charging_stations (site_id, station_id) VALUES ($1, $2)",
            depot_id,
            station_id,
        )

    yield SimpleNamespace(
        depot_id=depot_id,
        vehicle_id=vehicle_id,
        station_id=station_id,
        ts=ts,
    )

    async with ts.acquire() as conn:
        await conn.execute("DELETE FROM telemetry WHERE station_id = $1", station_id)
    async with static.acquire() as conn:
        await conn.execute("DELETE FROM charging_stations WHERE station_id = $1", station_id)
        await conn.execute("DELETE FROM sites WHERE id = $1", depot_id)


async def _seed_energy_only_telemetry(
    ts: asyncpg.Pool,
    *,
    station_id: str,
    vehicle_id: UUID,
    samples: list[tuple[datetime, float]],
) -> None:
    """Insert telemetry rows with charging_kw NULL but a cumulative energy register.

    This is the ABB Terra shape: only ``Energy.Active.Import.Register`` arrives,
    so ``charging_kw`` is never populated.
    """
    async with ts.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry (
                time, station_id, connector_id, vehicle_id, charging_kw, energy_kwh
            )
            VALUES ($1, $2, 1, $3::uuid, NULL, $4)
            """,
            [(t, station_id, str(vehicle_id), energy) for t, energy in samples],
        )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.database
async def test_history_derives_kw_from_energy_register_with_no_plan(db_pools, seeded):
    """Energy-only telemetry → non-empty history with derived kW, even with no plan."""
    now = datetime.now(tz=timezone.utc)
    # Six samples 5 min apart, cumulative register rising 5 kWh each step.
    # 5 kWh over 5 min (1/12 h) → a steady 60 kW derived rate.
    samples = [(now - timedelta(minutes=5 * (5 - i)), 1000.0 + 5.0 * i) for i in range(6)]
    await _seed_energy_only_telemetry(
        seeded.ts,
        station_id=seeded.station_id,
        vehicle_id=seeded.vehicle_id,
        samples=samples,
    )

    pools = main.DatabasePools(static=db_pools.static_pool, ts=db_pools.ts_pool)
    with patch.object(main, "db_pools", pools):
        resp = await _build_power_timeline(str(seeded.depot_id), 24)

    # History is populated despite charging_kw being NULL for every row …
    assert resp.history, "history must not be blank when energy-register telemetry exists"
    derived = [h.charging_kw for h in resp.history if h.charging_kw is not None]
    assert derived, "expected at least one bucket with a derived kW value"
    # … and the derived rate reflects the 60 kW the energy delta implies.
    assert max(derived) == pytest.approx(60.0, rel=0.15)
    # The energy-derived path also feeds the distinct-vehicle count.
    assert max(h.vehicle_count for h in resp.history) >= 1

    # No optimization run for this depot → empty plan, but history still present.
    assert resp.plan == []
    assert resp.plan_meta is None
    # Required envelope fields the frontend rejects the response without.
    assert resp.timezone == "Europe/Vilnius"
    assert resp.timestep_minutes == 15


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.database
async def test_history_prefers_reported_charging_kw_over_derived(db_pools, seeded):
    """When charging_kw is reported it is used verbatim (no energy derivation)."""
    now = datetime.now(tz=timezone.utc)
    async with seeded.ts.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry (
                time, station_id, connector_id, vehicle_id, charging_kw, energy_kwh
            )
            VALUES ($1, $2, 1, $3::uuid, $4, NULL)
            """,
            [
                (
                    now - timedelta(minutes=5 * (4 - i)),
                    seeded.station_id,
                    str(seeded.vehicle_id),
                    42.0,
                )
                for i in range(5)
            ],
        )

    pools = main.DatabasePools(static=db_pools.static_pool, ts=db_pools.ts_pool)
    with patch.object(main, "db_pools", pools):
        resp = await _build_power_timeline(str(seeded.depot_id), 24)

    derived = [h.charging_kw for h in resp.history if h.charging_kw is not None]
    assert derived
    assert max(derived) == pytest.approx(42.0, rel=0.01)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.database
async def test_history_derivation_skips_interleaved_null_energy_rows(db_pools, seeded):
    """Register deltas must skip the NULL-energy rows a MeterValues frame also writes.

    The legacy WS handler stores each sampled value as its own telemetry row, so a
    single frame yields a register row (``energy_kwh`` set) plus SoC/current/voltage
    rows (``energy_kwh`` NULL) a few microseconds apart. An unfiltered ``LAG`` would
    read one of those NULL rows as the "previous" register and derive nothing — the
    real production shape behind the blank chart.
    """
    now = datetime.now(tz=timezone.utc)
    rows: list[tuple] = []
    for i in range(5):
        t = now - timedelta(minutes=5 * (5 - i))
        # Register row (energy_kwh set) + a sibling SoC row 1 µs later (energy_kwh NULL).
        rows.append((t, seeded.station_id, str(seeded.vehicle_id), None, 1000.0 + 5.0 * i))
        rows.append(
            (t + timedelta(microseconds=1), seeded.station_id, str(seeded.vehicle_id), None, None)
        )
    async with seeded.ts.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry (
                time, station_id, connector_id, vehicle_id, charging_kw, energy_kwh
            )
            VALUES ($1, $2, 1, $3::uuid, $4, $5)
            """,
            rows,
        )

    pools = main.DatabasePools(static=db_pools.static_pool, ts=db_pools.ts_pool)
    with patch.object(main, "db_pools", pools):
        resp = await _build_power_timeline(str(seeded.depot_id), 24)

    derived = [h.charging_kw for h in resp.history if h.charging_kw is not None]
    assert derived, "interleaved NULL-energy rows must not blank the derived series"
    assert max(derived) == pytest.approx(60.0, rel=0.15)
