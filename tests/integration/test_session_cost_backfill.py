"""Integration tests for scripts/backfill_session_cost.py.

Exercises the chunked SKIP-LOCKED loop end-to-end against a real
TimescaleDB. Idempotency, scope filtering, and the candidate predicate
are the safety-critical contracts here.

Run with::

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test \\
        pytest tests/integration/test_session_cost_backfill.py -v
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

# Wire up the scripts/ dir so we can import the backfill module.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backfill_session_cost as bf  # noqa: E402


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


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
async def cleanup(pool):
    sessions: list[UUID] = []
    depots: list[UUID] = []
    yield {"sessions": sessions, "depots": depots}
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


async def _seed_session(
    pool: asyncpg.Pool,
    *,
    session_id: UUID,
    site_id: UUID,
    start: datetime,
    end: datetime,
    energy_kwh: float,
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
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, $4, $5, 'no-vehicle', 'import', $6, $7)
            """,
            session_id, start, end, energy_kwh, site_id, cost_total, cost_total_source,
        )


def _args(**overrides):
    base = {
        "dry_run": False,
        "depot_id": None,
        "batch_size": 500,
        "max_rows": None,
        "log_level": "INFO",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_processes_zero_cost_imports(pool, cleanup, monkeypatch):
    """Imported rows with cost_total=0 are the canonical population the
    backfill targets. Verify they're processed and updated."""
    site_id = uuid4()
    cleanup["depots"].append(site_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO prices (time, depot_id, energy_kwh, source) VALUES ($1, $2, $3, 'test')",
            _utc(2026, 5, 1, 10), site_id, 0.25,
        )
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        start=_utc(2026, 5, 1, 10),
        end=_utc(2026, 5, 1, 11),
        energy_kwh=40.0,
        cost_total=0,
        cost_total_source=None,
    )

    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    rc = await bf._run(_args(depot_id=site_id, max_rows=10))
    assert rc == 0

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total"] == Decimal("10.0000")  # 40 * 0.25
    assert after["cost_total_source"] == "fallback_average"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_idempotent_second_run_is_noop(pool, cleanup, monkeypatch):
    site_id = uuid4()
    cleanup["depots"].append(site_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO prices (time, depot_id, energy_kwh, source) VALUES ($1, $2, $3, 'test')",
            _utc(2026, 5, 2, 12), site_id, 0.30,
        )
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        start=_utc(2026, 5, 2, 12),
        end=_utc(2026, 5, 2, 13),
        energy_kwh=20.0,
        cost_total=None,
    )

    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id))

    async with pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT cost_total, updated_at FROM charging_sessions WHERE session_id = $1",
            session_id,
        )

    # Re-run. Predicate filters out the now-priced row.
    await bf._run(_args(depot_id=site_id))

    async with pool.acquire() as conn:
        second = await conn.fetchrow(
            "SELECT cost_total, updated_at FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert first["cost_total"] == second["cost_total"]
    # updated_at unchanged → second run did NOT touch the row.
    assert first["updated_at"] == second["updated_at"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_skips_manual_rows(pool, cleanup, monkeypatch):
    site_id = uuid4()
    cleanup["depots"].append(site_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO prices (time, depot_id, energy_kwh, source) VALUES ($1, $2, $3, 'test')",
            _utc(2026, 5, 3, 9), site_id, 0.40,
        )
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        start=_utc(2026, 5, 3, 9),
        end=_utc(2026, 5, 3, 10),
        energy_kwh=10.0,
        cost_total=99.0,
        cost_total_source="manual",
    )

    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id))

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total"] == Decimal("99.0")
    assert after["cost_total_source"] == "manual"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_dry_run_does_not_write(pool, cleanup, monkeypatch):
    site_id = uuid4()
    cleanup["depots"].append(site_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO prices (time, depot_id, energy_kwh, source) VALUES ($1, $2, $3, 'test')",
            _utc(2026, 5, 4, 14), site_id, 0.20,
        )
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        start=_utc(2026, 5, 4, 14),
        end=_utc(2026, 5, 4, 15),
        energy_kwh=30.0,
        cost_total=0,
    )

    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id, dry_run=True))

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    # Row unchanged.
    assert after["cost_total"] == Decimal("0")
    assert after["cost_total_source"] is None
