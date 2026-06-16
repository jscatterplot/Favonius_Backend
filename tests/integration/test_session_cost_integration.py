"""Integration tests for src/core/billing/session_cost.py against real TimescaleDB.

Exercises:
  * The ``time_bucket('1 hour', …)`` SQL the unit tests can't fake.
  * The ``fetch_prices_with_fill`` helper (DRY refactor in §2.1).
  * The ``write_session_cost`` UPDATE predicate (excludes 'manual' and
    non-zero pre-existing costs).
  * Idempotency: re-running write doesn't double-charge.

Run with::

    # Local (single DB with sites + Timescale tables):
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test \\
        pytest tests/integration/test_session_cost_integration.py -v

    # Staging split (TigerCloud + Supabase static):
    TEST_DATABASE_URL=$TIMESCALE_SERVICE_URL \\
    SUPABASE_DB_HOST=... SUPABASE_DB_PASSWORD=... \\
    SUPABASE_DB_USER=postgres.<ref> SUPABASE_DB_PORT=6543 \\
        pytest tests/integration/test_session_cost_integration.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.core.billing.session_cost import (
    compute_session_cost,
    write_session_cost,
)
from src.db.queries import fetch_prices_by_zone, resolve_bidding_zone
from tests.integration.conftest import (
    LITHUANIA_BIDDING_ZONE,
    IntegrationPools,
    find_lithuania_site_id,
)


# A real ENTSO-E EIC code (Lithuania, the pilot zone). The bidding
# zone is opaque from the calculator's perspective — any string suffices
# for tests — but using a real one keeps the data plausible.
TEST_ZONE = "10YLT-1001A0008Q"


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool(db_pools: IntegrationPools):
    yield db_pools.ts_pool


@pytest_asyncio.fixture
async def cleanup_ids(pool):
    """Track IDs to delete in test cleanup. Avoids leaving rows around.

    ``zones`` is the list of bidding-zone strings whose
    ``electricity_prices`` rows the test inserted; ``depots`` is now
    only retained for symmetry — the new architecture writes to
    ``electricity_prices`` keyed by zone, not to ``prices`` keyed by
    depot.
    """
    sessions: list[UUID] = []
    depots: list[UUID] = []
    vehicles: list[UUID] = []
    zones: list[str] = []
    yield {
        "sessions": sessions,
        "depots": depots,
        "vehicles": vehicles,
        "zones": zones,
    }
    async with pool.acquire() as conn:
        if sessions:
            await conn.execute(
                "DELETE FROM charging_sessions WHERE session_id = ANY($1::uuid[])",
                sessions,
            )
        if zones:
            await conn.execute(
                "DELETE FROM electricity_prices WHERE node_id = ANY($1::text[])",
                zones,
            )
        if vehicles:
            await conn.execute(
                "DELETE FROM telemetry WHERE vehicle_id = ANY($1::uuid[])",
                vehicles,
            )


async def _seed_session(
    pool: asyncpg.Pool,
    *,
    session_id: UUID,
    site_id: UUID,
    vehicle_id: UUID,
    start_time: datetime,
    end_time: datetime,
    energy_delivered_kwh: float,
    cost_total: float | None = None,
    cost_total_source: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES (
                $1, 'cp-it', 1, 1,
                $2, $3, $4,
                $5, $6::text, 'live', $7, $8
            )
            """,
            session_id, start_time, end_time, energy_delivered_kwh,
            site_id, str(vehicle_id), cost_total, cost_total_source,
        )


async def _seed_prices(
    pool: asyncpg.Pool,
    bidding_zone: str,
    hours: dict[datetime, float],
) -> None:
    """Insert ENTSO-E DAM prices into electricity_prices.

    Test inputs are in €/kWh (caller-friendly); the table stores €/MWh,
    so we multiply by 1000 at the boundary.
    """
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh)
            VALUES ($1, $2, 'ENTSOE_DAM', $3)
            """,
            [(h, bidding_zone, p * 1000.0) for h, p in hours.items()],
        )


async def _cleanup_prices(pool: asyncpg.Pool, bidding_zone: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM electricity_prices WHERE node_id = $1", bidding_zone,
        )


async def _seed_telemetry(pool: asyncpg.Pool, vehicle_id: UUID, samples: list[tuple[datetime, float]]) -> None:
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry (time, vehicle_id, charging_kw, is_plugged)
            VALUES ($1, $2, $3, TRUE)
            """,
            [(t, vehicle_id, kw) for t, kw in samples],
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_granular_end_to_end(pool, cleanup_ids):
    session_id = uuid4()
    site_id = uuid4()
    vehicle_id = uuid4()
    cleanup_ids["sessions"].append(session_id)
    cleanup_ids["zones"].append(TEST_ZONE)
    cleanup_ids["vehicles"].append(vehicle_id)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)

    await _seed_prices(pool, TEST_ZONE, {start: 0.20})
    # Six samples 10 min apart, all 50 kW → ~50 kWh.
    await _seed_telemetry(
        pool, vehicle_id,
        [(start + timedelta(minutes=10 * i), 50.0) for i in range(6)],
    )
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        vehicle_id=vehicle_id,
        start_time=start,
        end_time=end,
        energy_delivered_kwh=42.0,  # trapezoidal over 50 min ≈ 41.67 kWh
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id
        )
    # Coerce vehicle_id text → UUID for the calculator.
    row_dict = dict(row)
    row_dict["vehicle_id"] = UUID(row_dict["vehicle_id"])
    row_dict["bidding_zone"] = TEST_ZONE

    result = await compute_session_cost(pool, row_dict)
    assert result.source == "granular"
    assert result.cost is not None and result.cost > Decimal("0")

    wrote = await write_session_cost(pool, session_id, result)
    assert wrote is True

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total_source"] == "granular"
    assert after["cost_total"] is not None and after["cost_total"] > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fallback_average_end_to_end(pool, cleanup_ids):
    """No telemetry rows for the vehicle → falls back to avg × energy."""
    session_id = uuid4()
    site_id = uuid4()
    vehicle_id = uuid4()
    cleanup_ids["sessions"].append(session_id)
    cleanup_ids["zones"].append(TEST_ZONE)

    start = _utc(2026, 5, 19, 10, 0)
    end = _utc(2026, 5, 19, 12, 0)
    await _seed_prices(pool, TEST_ZONE, {start: 0.10, start + timedelta(hours=1): 0.30})
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        vehicle_id=vehicle_id,
        start_time=start,
        end_time=end,
        energy_delivered_kwh=100.0,
    )

    async with pool.acquire() as conn:
        row = dict(await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id,
        ))
    row["vehicle_id"] = UUID(row["vehicle_id"])
    row["bidding_zone"] = TEST_ZONE

    result = await compute_session_cost(pool, row)
    assert result.source == "fallback_average"
    # avg = 0.20; cost = 100 * 0.20 = 20
    assert result.cost == Decimal("20.0000")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_write_skips_manual_rows(pool, cleanup_ids):
    """A row with cost_total_source='manual' must not be overwritten."""
    session_id = uuid4()
    site_id = uuid4()
    vehicle_id = uuid4()
    cleanup_ids["sessions"].append(session_id)
    cleanup_ids["zones"].append(TEST_ZONE)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)
    await _seed_prices(pool, TEST_ZONE, {start: 0.20})
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        vehicle_id=vehicle_id,
        start_time=start,
        end_time=end,
        energy_delivered_kwh=50.0,
        cost_total=99.99,
        cost_total_source="manual",
    )

    async with pool.acquire() as conn:
        row = dict(await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id,
        ))
    row["vehicle_id"] = UUID(row["vehicle_id"])
    row["bidding_zone"] = TEST_ZONE

    result = await compute_session_cost(pool, row)
    assert result.source == "manual"
    wrote = await write_session_cost(pool, session_id, result)
    assert wrote is False  # manual sentinel — write filtered by WHERE clause

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total"] == Decimal("99.99")
    assert after["cost_total_source"] == "manual"


@pytest_asyncio.fixture
async def cleanup_sites(db_pools: IntegrationPools):
    """Track sites.id rows the test seeded and remove on teardown (combined DB only)."""
    site_ids: list[UUID] = []
    yield site_ids
    if site_ids and db_pools.can_seed_sites:
        async with db_pools.static_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sites WHERE id = ANY($1::uuid[])", site_ids,
            )


async def _seed_site(
    pool: asyncpg.Pool,
    site_id: UUID,
    *,
    timezone_name: Optional[str] = None,
    tariff_config: Optional[dict] = None,
) -> None:
    """Insert a sites row with the bits resolve_bidding_zone reads."""
    import json as _json
    payload = _json.dumps(tariff_config) if tariff_config is not None else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sites (id, timezone, tariff_config)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (id) DO UPDATE
              SET timezone      = EXCLUDED.timezone,
                  tariff_config = EXCLUDED.tariff_config
            """,
            site_id, timezone_name, payload,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resolve_bidding_zone_uses_tariff_config_first(
    db_pools: IntegrationPools, cleanup_sites,
):
    if not db_pools.can_seed_sites:
        pytest.skip("sites INSERT requires combined TEST_DATABASE_URL (local test DB)")
    site_id = uuid4()
    cleanup_sites.append(site_id)
    await _seed_site(
        db_pools.static_pool, site_id,
        timezone_name="Europe/Vilnius",
        tariff_config={"entsoe_zone": "10YDE-RWENET---I"},
    )
    async with db_pools.static_pool.acquire() as conn:
        zone = await resolve_bidding_zone(conn, site_id)
    assert zone == "10YDE-RWENET---I"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resolve_bidding_zone_falls_back_to_timezone(
    db_pools: IntegrationPools, cleanup_sites,
):
    if not db_pools.static_sites_readable:
        pytest.skip("needs Supabase static pool (STATIC_DATABASE_URL or SUPABASE_DB_*)")
    if db_pools.can_seed_sites:
        site_id = uuid4()
        cleanup_sites.append(site_id)
        await _seed_site(
            db_pools.static_pool, site_id,
            timezone_name="Europe/Vilnius", tariff_config=None,
        )
    else:
        site_id = await find_lithuania_site_id(
            db_pools.static_pool, timezone_fallback_only=True,
        )
        if site_id is None:
            pytest.skip(
                "no Europe/Vilnius site without entsoe_zone override in Supabase"
            )
    async with db_pools.static_pool.acquire() as conn:
        zone = await resolve_bidding_zone(conn, site_id)
    assert zone == LITHUANIA_BIDDING_ZONE


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resolve_bidding_zone_unknown_returns_none(
    db_pools: IntegrationPools, cleanup_sites,
):
    if not db_pools.static_sites_readable:
        pytest.skip("needs Supabase static pool (STATIC_DATABASE_URL or SUPABASE_DB_*)")
    site_id = uuid4()
    if db_pools.can_seed_sites:
        cleanup_sites.append(site_id)
        await _seed_site(
            db_pools.static_pool, site_id,
            timezone_name="America/Los_Angeles", tariff_config=None,
        )
    async with db_pools.static_pool.acquire() as conn:
        zone = await resolve_bidding_zone(conn, site_id)
    assert zone is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resolve_pilot_pilot_site_readonly(db_pools: IntegrationPools):
    """Read-only check against Supabase ``sites`` (TigerCloud + Supabase split)."""
    if not db_pools.is_split or not db_pools.static_sites_readable:
        pytest.skip("needs split pools + Supabase STATIC_DATABASE_URL / SUPABASE_DB_*")
    site_id = await find_lithuania_site_id(db_pools.static_pool)
    if site_id is None:
        pytest.skip("no Lithuania-resolving site in Supabase")
    async with db_pools.static_pool.acquire() as conn:
        zone = await resolve_bidding_zone(conn, site_id)
    assert zone == LITHUANIA_BIDDING_ZONE


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fetch_prices_by_zone_forward_fills(pool, cleanup_ids):
    """Prices at 13:00 only; the helper should forward-fill 14:00 within
    the 1h window. Also asserts the EUR/MWh → EUR/kWh conversion."""
    cleanup_ids["zones"].append(TEST_ZONE)
    # _seed_prices already does the kWh → MWh conversion at the boundary.
    await _seed_prices(pool, TEST_ZONE, {_utc(2026, 5, 19, 13, 0): 0.20})

    async with pool.acquire() as conn:
        filled = await fetch_prices_by_zone(
            conn, TEST_ZONE, _utc(2026, 5, 19, 13, 0), _utc(2026, 5, 19, 15, 0),
        )

    # 13:00 known. 14:00 within 1h of 13:00 → filled. 15:00 is excluded by < end.
    assert _utc(2026, 5, 19, 13, 0) in filled
    assert _utc(2026, 5, 19, 14, 0) in filled
    assert filled[_utc(2026, 5, 19, 14, 0)] == pytest.approx(0.20)
    assert filled[_utc(2026, 5, 19, 13, 0)] == pytest.approx(0.20)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fetch_prices_by_zone_includes_floored_start_hour(pool, cleanup_ids):
    """Regression: a 13:30 → 14:30 session needs the 13:00 hour bucket,
    not just 14:00. The helper must floor ``start_time`` to the hour
    rather than rounding up — _expected_hour_buckets in the calculator
    and time_bucket() in the granular SQL both floor."""
    cleanup_ids["zones"].append(TEST_ZONE)
    await _seed_prices(
        pool, TEST_ZONE,
        {
            _utc(2026, 5, 19, 13, 0): 0.10,
            _utc(2026, 5, 19, 14, 0): 0.20,
        },
    )

    async with pool.acquire() as conn:
        filled = await fetch_prices_by_zone(
            conn, TEST_ZONE,
            _utc(2026, 5, 19, 13, 30),  # mid-hour start
            _utc(2026, 5, 19, 14, 30),
        )

    assert _utc(2026, 5, 19, 13, 0) in filled, "13:00 hour bucket must be returned"
    assert _utc(2026, 5, 19, 14, 0) in filled
    assert filled[_utc(2026, 5, 19, 13, 0)] == pytest.approx(0.10)
    assert filled[_utc(2026, 5, 19, 14, 0)] == pytest.approx(0.20)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fetch_prices_by_zone_ignores_legacy_market_rows(pool, cleanup_ids):
    """Only ENTSOE_DAM rows are returned — legacy CAISO LMP rows in the
    same table must be skipped by the helper's market_type filter."""
    cleanup_ids["zones"].append(TEST_ZONE)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh) "
            "VALUES ($1, $2, 'ENTSOE_DAM', $3)",
            _utc(2026, 5, 19, 13, 0), TEST_ZONE, 100.0,  # €0.10/kWh
        )
        await conn.execute(
            "INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh) "
            "VALUES ($1, $2, 'RTM', $3)",
            _utc(2026, 5, 19, 13, 0), TEST_ZONE, 999.0,  # would leak as €0.999/kWh
        )

    async with pool.acquire() as conn:
        filled = await fetch_prices_by_zone(
            conn, TEST_ZONE, _utc(2026, 5, 19, 13, 0), _utc(2026, 5, 19, 14, 0),
        )
    assert filled[_utc(2026, 5, 19, 13, 0)] == pytest.approx(0.10)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_idempotent_write(pool, cleanup_ids):
    """Running the calculator twice doesn't double-charge."""
    session_id = uuid4()
    site_id = uuid4()
    vehicle_id = uuid4()
    cleanup_ids["sessions"].append(session_id)
    cleanup_ids["zones"].append(TEST_ZONE)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)
    await _seed_prices(pool, TEST_ZONE, {start: 0.20})
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        vehicle_id=vehicle_id,
        start_time=start,
        end_time=end,
        energy_delivered_kwh=50.0,
    )

    async with pool.acquire() as conn:
        row = dict(await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id,
        ))
    row["vehicle_id"] = UUID(row["vehicle_id"])
    row["bidding_zone"] = TEST_ZONE

    result_one = await compute_session_cost(pool, row)
    wrote_one = await write_session_cost(pool, session_id, result_one)
    assert wrote_one is True
    assert result_one.source == "fallback_average"
    assert result_one.cost == Decimal("10.0000")

    async with pool.acquire() as conn:
        row2 = dict(await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id,
        ))
    row2["vehicle_id"] = UUID(row2["vehicle_id"])
    row2["bidding_zone"] = TEST_ZONE

    result_two = await compute_session_cost(pool, row2)
    wrote_two = await write_session_cost(pool, session_id, result_two)
    # cost_total is already non-zero → UPDATE predicate excludes the row.
    assert wrote_two is False
