"""Integration tests for src/core/billing/session_cost.py against real TimescaleDB.

Exercises:
  * The ``time_bucket('1 hour', …)`` SQL the unit tests can't fake.
  * The ``fetch_prices_with_fill`` helper (DRY refactor in §2.1).
  * The ``write_session_cost`` UPDATE predicate (excludes 'manual' and
    non-zero pre-existing costs).
  * Idempotency: re-running write doesn't double-charge.

Run with::

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test \\
        pytest tests/integration/test_session_cost_integration.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg
import pytest

from src.core.billing import (
    compute_session_cost,
    write_session_cost,
)
from src.db.queries import fetch_prices_with_fill


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/favonius_test",
    )
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest.fixture
async def cleanup_ids(pool):
    """Track UUIDs to delete in test cleanup. Avoids leaving rows around."""
    sessions: list[UUID] = []
    depots: list[UUID] = []
    vehicles: list[UUID] = []
    yield {"sessions": sessions, "depots": depots, "vehicles": vehicles}
    async with pool.acquire() as conn:
        if sessions:
            await conn.execute(
                "DELETE FROM charging_sessions WHERE session_id = ANY($1::uuid[])",
                sessions,
            )
        if depots:
            await conn.execute(
                "DELETE FROM prices WHERE depot_id = ANY($1::uuid[])",
                depots,
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


async def _seed_prices(pool: asyncpg.Pool, depot_id: UUID, hours: dict[datetime, float]) -> None:
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO prices (time, depot_id, energy_kwh, source)
            VALUES ($1, $2, $3, 'test')
            ON CONFLICT (time, depot_id) DO UPDATE SET energy_kwh = EXCLUDED.energy_kwh
            """,
            [(h, depot_id, p) for h, p in hours.items()],
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
    cleanup_ids["depots"].append(site_id)
    cleanup_ids["vehicles"].append(vehicle_id)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)

    await _seed_prices(pool, site_id, {start: 0.20})
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
    cleanup_ids["depots"].append(site_id)

    start = _utc(2026, 5, 19, 10, 0)
    end = _utc(2026, 5, 19, 12, 0)
    await _seed_prices(pool, site_id, {start: 0.10, start + timedelta(hours=1): 0.30})
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
    cleanup_ids["depots"].append(site_id)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)
    await _seed_prices(pool, site_id, {start: 0.20})
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fetch_prices_with_fill_forward_fills(pool, cleanup_ids):
    """Prices at 13:00 only; the helper should forward-fill 14:00 within the 1h window."""
    site_id = uuid4()
    cleanup_ids["depots"].append(site_id)
    await _seed_prices(pool, site_id, {_utc(2026, 5, 19, 13, 0): 0.20})

    async with pool.acquire() as conn:
        filled = await fetch_prices_with_fill(
            conn, site_id, _utc(2026, 5, 19, 13, 0), _utc(2026, 5, 19, 15, 0),
        )

    # 13:00 known. 14:00 within 1h of 13:00 → filled. 15:00 is excluded by < end.
    assert _utc(2026, 5, 19, 13, 0) in filled
    assert _utc(2026, 5, 19, 14, 0) in filled
    assert filled[_utc(2026, 5, 19, 14, 0)] == 0.20


@pytest.mark.asyncio
@pytest.mark.integration
async def test_idempotent_write(pool, cleanup_ids):
    """Running the calculator twice doesn't double-charge."""
    session_id = uuid4()
    site_id = uuid4()
    vehicle_id = uuid4()
    cleanup_ids["sessions"].append(session_id)
    cleanup_ids["depots"].append(site_id)

    start = _utc(2026, 5, 19, 13, 0)
    end = _utc(2026, 5, 19, 14, 0)
    await _seed_prices(pool, site_id, {start: 0.20})
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

    result_one = await compute_session_cost(pool, row)
    wrote_one = await write_session_cost(pool, session_id, result_one)
    assert wrote_one is True

    # Refetch the row — cost_total now populated → predicate excludes it.
    async with pool.acquire() as conn:
        row2 = dict(await conn.fetchrow(
            "SELECT * FROM charging_sessions WHERE session_id = $1", session_id,
        ))
    row2["vehicle_id"] = UUID(row2["vehicle_id"])

    result_two = await compute_session_cost(pool, row2)
    # The pre-existing non-zero cost is now respected as 'manual' sentinel
    # (calculator's contract — see comment in compute_session_cost).
    # We assert: second write is a no-op.
    wrote_two = await write_session_cost(pool, session_id, result_two)
    assert wrote_two is False
