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
import importlib.util
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_backfill_module():
    """Load backfill script without putting scripts/ on sys.path (breaks src imports)."""
    path = REPO_ROOT / "scripts" / "backfill_session_cost.py"
    spec = importlib.util.spec_from_file_location("backfill_session_cost", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load backfill module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_session_cost"] = mod
    spec.loader.exec_module(mod)
    return mod


bf = _load_backfill_module()
from tests.integration.conftest import create_integration_pool


# Single fixed zone for backfill tests. We seed a sites row with this
# zone via tariff_config so resolve_bidding_zone returns it
# deterministically — independent of the row's timezone column.
TEST_ZONE = "10YLT-1001A0008Q"


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    db_pool = await create_integration_pool()
    yield db_pool
    await db_pool.close()


@pytest_asyncio.fixture
async def cleanup(pool):
    """Track IDs to delete on teardown.

    Backfill tests seed ``sites`` (so the resolver finds a zone),
    ``electricity_prices`` (by zone), and ``charging_sessions``.
    """
    sessions: list[UUID] = []
    sites: list[UUID] = []
    zones: list[str] = []
    yield {"sessions": sessions, "sites": sites, "zones": zones}
    async with pool.acquire() as conn:
        if sessions:
            await conn.execute(
                "DELETE FROM charging_sessions WHERE session_id = ANY($1::uuid[])",
                sessions,
            )
        if sites:
            await conn.execute(
                "DELETE FROM sites WHERE id = ANY($1::uuid[])", sites,
            )
        if zones:
            await conn.execute(
                "DELETE FROM electricity_prices WHERE node_id = ANY($1::text[])",
                zones,
            )


async def _seed_site_with_zone(
    pool: asyncpg.Pool,
    site_id: UUID,
    zone: str = TEST_ZONE,
) -> None:
    """Insert a sites row whose ``tariff_config.entsoe_zone`` is ``zone``.

    This is the canonical signal the resolver looks at first; the
    timezone fallback isn't exercised here — the resolver helper is
    tested directly in ``test_session_cost_integration.py``.
    """
    import json as _json
    payload = _json.dumps({"entsoe_zone": zone})
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sites (id, tariff_config)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (id) DO UPDATE SET tariff_config = EXCLUDED.tariff_config
            """,
            site_id, payload,
        )


async def _seed_price(
    pool: asyncpg.Pool,
    when: datetime,
    eur_per_kwh: float,
    zone: str = TEST_ZONE,
) -> None:
    """Insert one ENTSOE_DAM hour into electricity_prices (€/kWh → €/MWh)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh)
            VALUES ($1, $2, 'ENTSOE_DAM', $3)
            """,
            when, zone, eur_per_kwh * 1000.0,
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
        "session_ids": [],
        # Both pools point at the same test DB. The backfill script
        # opens two pools at startup so the static-schema reads (sites)
        # and TimescaleDB reads (charging_sessions, electricity_prices)
        # can target different deployments; in this single-pg test
        # environment they share a URL.
        "database_url": None,
        "static_database_url": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_processes_zero_cost_imports(pool, cleanup, monkeypatch):
    """Imported rows with cost_total=0 are the canonical population the
    backfill targets. Verify they're processed and updated."""
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(pool, site_id)
    await _seed_price(pool, _utc(2026, 5, 1, 10), 0.25)
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
        "STATIC_DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    rc = await bf._run(
        _args(depot_id=site_id, max_rows=10, session_ids=[session_id]),
    )
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
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(pool, site_id)
    await _seed_price(pool, _utc(2026, 5, 2, 12), 0.30)
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
        "STATIC_DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id, session_ids=[session_id]))

    async with pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT cost_total, updated_at FROM charging_sessions WHERE session_id = $1",
            session_id,
        )

    # Re-run. Predicate filters out the now-priced row.
    await bf._run(_args(depot_id=site_id, session_ids=[session_id]))

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
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(pool, site_id)
    await _seed_price(pool, _utc(2026, 5, 3, 9), 0.40)
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
        "STATIC_DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id, session_ids=[session_id]))

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
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(pool, site_id)
    await _seed_price(pool, _utc(2026, 5, 4, 14), 0.20)
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
        "STATIC_DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/favonius_test"),
    )
    await bf._run(_args(depot_id=site_id, dry_run=True, session_ids=[session_id]))

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    # Row unchanged.
    assert after["cost_total"] == Decimal("0")
    assert after["cost_total_source"] is None
