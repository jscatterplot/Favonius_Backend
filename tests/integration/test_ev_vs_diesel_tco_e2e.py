"""End-to-end integration test for the EV-vs-diesel cost-per-km feature.

Exercises the real SQL of the new pieces against a live Postgres:
- the migration 047 DDL (vehicle_telemetry.odometer_km, diesel_prices,
  vehicle_type_fuel_baselines),
- distance.compute_distances_for_depot (odometer deltas),
- queries.fetch_or_pull_diesel_price / resolve_fuel_baselines / resolve_country_code,
- the diesel adapter's store_prices_to_db idempotency,
- cost_per_km.compute_cost_per_km end to end.

Skipped when TEST_DATABASE_URL is unreachable. Uses one Postgres for both the
TimescaleDB operational tables and the Supabase-static `sites`/`vehicles`
(matching the conftest's ``can_seed_sites`` single-DB mode); the test creates
minimal stand-ins for the static tables when they're absent so it can run
against a bare TimescaleDB image too.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.adapters.diesel_prices.adapter import DieselPriceAdapter
from src.adapters.diesel_prices.mapping import DieselPrice
from src.core.billing.cost_per_km import compute_cost_per_km
from src.core.billing.distance import compute_distances_for_depot
from src.db.pools import DatabasePools
from src.db.queries import (
    fetch_or_pull_diesel_price,
    resolve_country_code,
    resolve_fuel_baselines,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]

_SOURCE = "eu_oil_bulletin"


@pytest_asyncio.fixture
async def pool():
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )
    try:
        p = await asyncpg.create_pool(db_url, min_size=1, max_size=4, command_timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"test database unavailable: {exc}")
    yield p
    await p.close()


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    """Apply the feature's DDL idempotently (subset of migrations 044 + 047).

    Kept self-contained so the test runs against a bare Postgres without the
    full migration runner; all statements are IF NOT EXISTS.
    """
    # vehicle_telemetry (migration 044) + odometer column (migration 047).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_telemetry (
            time            TIMESTAMPTZ NOT NULL,
            vehicle_id      UUID NOT NULL,
            soc             DOUBLE PRECISION,
            location_lat    DOUBLE PRECISION,
            location_lon    DOUBLE PRECISION,
            source          TEXT NOT NULL DEFAULT 'navirec',
            raw_fields      JSONB,
            PRIMARY KEY (vehicle_id, time)
        );
        ALTER TABLE vehicle_telemetry
            ADD COLUMN IF NOT EXISTS odometer_km DOUBLE PRECISION;
        """)
    # diesel_prices (migration 047).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS diesel_prices (
            time                     TIMESTAMPTZ NOT NULL,
            region                   TEXT NOT NULL,
            source                   TEXT NOT NULL,
            price_eur_per_l          DOUBLE PRECISION,
            price_incl_tax_eur_per_l DOUBLE PRECISION,
            PRIMARY KEY (time, source, region)
        );
        """)
    # charging_sessions: only the columns the report reads.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS charging_sessions (
            session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            station_id           TEXT,
            site_id              UUID,
            vehicle_id           UUID,
            driver_id            UUID,
            card_id              UUID,
            start_time           TIMESTAMPTZ NOT NULL,
            end_time             TIMESTAMPTZ,
            energy_delivered_kwh DOUBLE PRECISION,
            cost_total           DOUBLE PRECISION,
            source               TEXT
        );
        """)
    # Static stand-ins (single-DB mode): sites + vehicles.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id UUID PRIMARY KEY,
            name TEXT,
            timezone TEXT,
            currency TEXT,
            organization_id UUID,
            billing_metadata JSONB,
            tariff_config JSONB
        );
        CREATE TABLE IF NOT EXISTS vehicles (
            id UUID PRIMARY KEY,
            site_id UUID,
            vehicle_type TEXT,
            license_plate TEXT
        );
        CREATE TABLE IF NOT EXISTS charging_stations (
            id UUID PRIMARY KEY,
            site_id UUID,
            station_id TEXT
        );
        CREATE TABLE IF NOT EXISTS vehicle_type_fuel_baselines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL,
            site_id UUID,
            vehicle_type VARCHAR(50),
            diesel_l_per_100km DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)


@pytest_asyncio.fixture
async def seeded(pool):
    """Seed a depot with two buses (odometer + sessions) and a diesel price.

    Yields a dict of the ids + the period so assertions can reference them, and
    cleans every row up afterward.
    """
    org_id = uuid4()
    depot_id = uuid4()
    v1 = uuid4()  # bus_large, 300 km driven, charged
    v2 = uuid4()  # bus_large, no odometer (unknown distance)
    station = f"ST-{uuid4().hex[:8]}"

    period_start = date(2026, 5, 18)
    period_end = date(2026, 5, 24)
    # A couple of odometer readings inside the window for v1 (300 km delta).
    t0 = datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 23, 18, 0, tzinfo=timezone.utc)

    async with pool.acquire() as conn:
        await _ensure_schema(conn)
        await conn.execute(
            "INSERT INTO sites (id, name, timezone, currency, organization_id) "
            "VALUES ($1,$2,$3,$4,$5)",
            depot_id,
            "Vilnius Depot",
            "Europe/Vilnius",
            "EUR",
            org_id,
        )
        await conn.executemany(
            "INSERT INTO vehicles (id, site_id, vehicle_type, license_plate) "
            "VALUES ($1,$2,$3,$4)",
            [
                (v1, depot_id, "bus_large", "ABC111"),
                (v2, depot_id, "bus_large", "ABC222"),
            ],
        )
        await conn.execute(
            "INSERT INTO charging_stations (id, site_id, station_id) VALUES ($1,$2,$3)",
            uuid4(),
            depot_id,
            station,
        )
        # Odometer readings for v1 (10000 → 10300 km). v2 gets none → unknown.
        await conn.executemany(
            "INSERT INTO vehicle_telemetry (time, vehicle_id, soc, source, odometer_km) "
            "VALUES ($1,$2,$3,'navirec',$4)",
            [
                (t0, v1, 0.9, 10000.0),
                (t1, v1, 0.4, 10300.0),
            ],
        )
        # One charging session for v1: 200 kWh, €40 → EV €40 for 300 km.
        await conn.execute(
            "INSERT INTO charging_sessions "
            "(station_id, site_id, vehicle_id, start_time, end_time, "
            " energy_delivered_kwh, cost_total, source) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,'ocpp')",
            station,
            depot_id,
            v1,
            datetime(2026, 5, 19, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 20, 4, 0, tzinfo=timezone.utc),
            200.0,
            40.0,
        )
        # Diesel price for LT: €1.50/L ex-tax.
        await conn.execute(
            "INSERT INTO diesel_prices (time, region, source, price_eur_per_l) "
            "VALUES ($1,'LT',$2,$3) ON CONFLICT DO NOTHING",
            datetime(2026, 5, 20, tzinfo=timezone.utc),
            _SOURCE,
            1.50,
        )

    yield {
        "org_id": str(org_id),
        "depot_id": str(depot_id),
        "v1": str(v1),
        "v2": str(v2),
        "station": station,
        "period_start": period_start,
        "period_end": period_end,
    }

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM vehicle_telemetry WHERE vehicle_id = ANY($1::uuid[])", [v1, v2]
        )
        await conn.execute("DELETE FROM charging_sessions WHERE site_id = $1", depot_id)
        await conn.execute("DELETE FROM diesel_prices WHERE region = 'LT' AND source = $1", _SOURCE)
        await conn.execute("DELETE FROM vehicles WHERE site_id = $1", depot_id)
        await conn.execute("DELETE FROM charging_stations WHERE site_id = $1", depot_id)
        await conn.execute(
            "DELETE FROM vehicle_type_fuel_baselines WHERE organization_id = $1", org_id
        )
        await conn.execute("DELETE FROM sites WHERE id = $1", depot_id)


@pytest.mark.asyncio
async def test_distance_from_real_odometer(pool, seeded):
    start = datetime(2026, 5, 18, tzinfo=timezone.utc)
    end = datetime(2026, 5, 25, tzinfo=timezone.utc)
    out = await compute_distances_for_depot(
        pool, vehicle_ids=[seeded["v1"], seeded["v2"]], start=start, end=end
    )
    assert out[seeded["v1"]].distance_km == pytest.approx(300.0)
    assert out[seeded["v2"]].distance_km is None  # no odometer → unknown


@pytest.mark.asyncio
async def test_diesel_price_cache_read(pool, seeded, monkeypatch):
    monkeypatch.delenv("DIESEL_PRICE_API_BASE_URL", raising=False)
    async with pool.acquire() as conn:
        price = await fetch_or_pull_diesel_price(
            conn, "LT", datetime(2026, 5, 24, tzinfo=timezone.utc), source=_SOURCE
        )
    assert price == pytest.approx(1.50)


@pytest.mark.asyncio
async def test_diesel_store_idempotent(pool, seeded):
    adapter = DieselPriceAdapter(pool=pool)
    rows = [
        DieselPrice(
            time=datetime(2026, 5, 20, tzinfo=timezone.utc),
            region="LT",
            price_eur_per_l=1.50,
            source=_SOURCE,
        )
    ]
    # The seed already inserted this exact (time, source, region) → 0 new rows.
    assert await adapter.store_prices_to_db(rows) == 0


@pytest.mark.asyncio
async def test_resolve_helpers(pool, seeded):
    async with pool.acquire() as conn:
        country = await resolve_country_code(conn, seeded["depot_id"])
        baselines = await resolve_fuel_baselines(
            conn, depot_id=seeded["depot_id"], organization_id=seeded["org_id"]
        )
    assert country == "LT"  # Europe/Vilnius → LT
    assert baselines["bus_large"] == pytest.approx(35.0)  # code default


@pytest.mark.asyncio
async def test_compute_cost_per_km_end_to_end(pool, seeded):
    from src.api.reports import SessionRow

    # The orchestrator takes session rows grouped by vehicle (the report handler
    # builds these); construct them directly here for v1.
    sessions_by_vehicle = {
        seeded["v1"]: [
            SessionRow(
                start_time=datetime(2026, 5, 19, 22, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 20, 4, 0, tzinfo=timezone.utc),
                energy_kwh=200.0,
                cost_total=40.0,
                vehicle_id=seeded["v1"],
                charger_id=None,
                driver_id=None,
                card_id=None,
            )
        ]
    }
    pools = DatabasePools(static=pool, ts=pool)
    result = await compute_cost_per_km(
        pools,
        depot_id=seeded["depot_id"],
        organization_id=seeded["org_id"],
        period_start=seeded["period_start"],
        period_end=seeded["period_end"],
        timezone_name="Europe/Vilnius",
        currency="EUR",
        under_cap_rate=None,
        session_rows_by_vehicle=sessions_by_vehicle,
    )

    # v1: 300 km, EV €40 → €0.1333/km. Diesel: 35 L/100km × 300 = 105 L × €1.50
    # = €157.50 → €0.525/km. Savings = (0.525-0.1333)/0.525 ≈ 74.6%.
    assert result.diesel_price_eur_per_l == pytest.approx(1.50)
    assert result.diesel_region == "LT"
    assert result.priceable_vehicle_count == 1  # only v1 has distance
    assert result.unpriceable_vehicle_count == 1  # v2 (no odometer)
    assert result.fleet_ev_eur_per_km == pytest.approx(40.0 / 300.0, abs=1e-4)
    assert result.fleet_diesel_eur_per_km == pytest.approx(157.5 / 300.0, abs=1e-4)
    assert result.fleet_pct_difference == pytest.approx(74.6, abs=0.5)
    assert result.currency_mismatch is False
