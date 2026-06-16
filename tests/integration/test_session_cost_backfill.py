"""Integration tests for scripts/backfill_session_cost.py.

Exercises the chunked SKIP-LOCKED loop end-to-end against a real
TimescaleDB. Idempotency, scope filtering, and the candidate predicate
are the safety-critical contracts here.

Run with::

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test \\
        pytest tests/integration/test_session_cost_backfill.py -v

    # Staging the pilot depot dry-run (split pools, no site INSERT):
    TEST_DATABASE_URL=$TIMESCALE_SERVICE_URL SUPABASE_DB_*=... \\
        pytest tests/integration/test_session_cost_backfill.py::test_backfill_pilot_pilot_dry_run_split -v
"""

from __future__ import annotations

import argparse
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
from tests.integration.conftest import (
    IntegrationPools,
    find_lithuania_site_id,
    integration_db_urls,
)


# Single fixed zone for backfill tests. We seed a sites row with this
# zone via tariff_config so resolve_bidding_zone returns it
# deterministically — independent of the row's timezone column.
TEST_ZONE = "10YLT-1001A0008Q"


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool(db_pools: IntegrationPools):
    yield db_pools.ts_pool


def _require_seedable_sites(db_pools: IntegrationPools) -> None:
    if not db_pools.can_seed_sites:
        pytest.skip(
            "backfill integration tests INSERT sites rows; use combined "
            "TEST_DATABASE_URL (local) or skip on TigerCloud+Supabase split"
        )


@pytest_asyncio.fixture
async def cleanup(db_pools: IntegrationPools):
    """Track IDs to delete on teardown.

    Backfill tests seed ``sites`` (so the resolver finds a zone),
    ``electricity_prices`` (by zone), and ``charging_sessions``.
    """
    sessions: list[UUID] = []
    sites: list[UUID] = []
    zones: list[str] = []
    yield {"sessions": sessions, "sites": sites, "zones": zones}
    async with db_pools.ts_pool.acquire() as conn:
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
    if sites and db_pools.can_seed_sites:
        async with db_pools.static_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sites WHERE id = ANY($1::uuid[])", sites,
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
async def test_backfill_processes_zero_cost_imports(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    """Imported rows with cost_total=0 are the canonical population the
    backfill targets. Verify they're processed and updated."""
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
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

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    rc = await bf._run(
        _args(
            depot_id=site_id,
            max_rows=10,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
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
async def test_backfill_idempotent_second_run_is_noop(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
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

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    await bf._run(
        _args(
            depot_id=site_id,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

    async with pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT cost_total, updated_at FROM charging_sessions WHERE session_id = $1",
            session_id,
        )

    await bf._run(
        _args(
            depot_id=site_id,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

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
async def test_backfill_skips_manual_rows(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
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

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    await bf._run(
        _args(
            depot_id=site_id,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total"] == Decimal("99.0")
    assert after["cost_total_source"] == "manual"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_dry_run_does_not_write(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
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

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    await bf._run(
        _args(
            depot_id=site_id,
            dry_run=True,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    # Row unchanged.
    assert after["cost_total"] == Decimal("0")
    assert after["cost_total_source"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_candidate_sql_skips_stranded_terminal_rows(
    db_pools: IntegrationPools, pool, cleanup,
):
    """Regression: terminal rows whose underlying data is in the exact
    state that originally produced the terminal label have no chance
    of healing — they would land the same source on every backfill
    run forever. The candidate predicate filters them out:

      * ``'no_energy'`` rows whose ``energy_delivered_kwh`` is still
        NULL/≤0 → skip.
      * ``'no_depot'`` rows whose ``site_id`` AND ``station_id`` are
        both NULL → skip (no recovery path).

    Self-healing is preserved: when an admin populates either field,
    the predicate stops matching and the row re-enters the candidate
    set. ``'unpriceable'`` always resweeps because prices can arrive
    later via the ENTSO-E feeder."""
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)

    # Six fixtures spanning the predicate's truth table.
    stranded_no_energy = uuid4()
    healing_no_energy = uuid4()
    stranded_no_depot = uuid4()
    healing_no_depot_via_station = uuid4()
    healing_no_depot_via_site = uuid4()
    always_resweep_unpriceable = uuid4()
    for sid in [
        stranded_no_energy, healing_no_energy,
        stranded_no_depot, healing_no_depot_via_station,
        healing_no_depot_via_site,
        always_resweep_unpriceable,
    ]:
        cleanup["sessions"].append(sid)

    # Hand-roll inserts so we can set station_id and site_id to NULL.
    async with pool.acquire() as conn:
        # 1) Stranded no_energy: energy still NULL.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, NULL, $4, 'no-vehicle', 'import', NULL, 'no_energy')
            """,
            stranded_no_energy, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15), site_id,
        )
        # 2) Healing no_energy: data was repaired — energy now positive.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, 10.0, $4, 'no-vehicle', 'import', NULL, 'no_energy')
            """,
            healing_no_energy, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15), site_id,
        )
        # 3) Stranded no_depot: site_id NULL, station_id NULL — unrecoverable.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, NULL, 1, 1, $2, $3, 10.0, NULL, 'no-vehicle', 'import', NULL, 'no_depot')
            """,
            stranded_no_depot, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15),
        )
        # 4) Healing no_depot via station_id: SiteResolver may pick it up.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, 10.0, NULL, 'no-vehicle', 'import', NULL, 'no_depot')
            """,
            healing_no_depot_via_station, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15),
        )
        # 5) Healing no_depot via site_id: admin patched site_id directly.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, NULL, 1, 1, $2, $3, 10.0, $4, 'no-vehicle', 'import', NULL, 'no_depot')
            """,
            healing_no_depot_via_site, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15), site_id,
        )
        # 6) Unpriceable: prices may arrive later, always resweep.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, 10.0, $4, 'no-vehicle', 'import', NULL, 'unpriceable')
            """,
            always_resweep_unpriceable, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15), site_id,
        )

        rows = await conn.fetch(
            bf._CANDIDATE_BY_SESSION_SQL,
            None,
            100,
            [
                stranded_no_energy, healing_no_energy,
                stranded_no_depot, healing_no_depot_via_station,
                healing_no_depot_via_site,
                always_resweep_unpriceable,
            ],
            bf._UUID_FLOOR,
        )

    picked = {r["session_id"] for r in rows}
    assert stranded_no_energy not in picked, (
        "no_energy row with NULL/≤0 energy must be filtered — nothing changed"
    )
    assert stranded_no_depot not in picked, (
        "no_depot row with NULL site_id AND NULL station_id has no recovery path"
    )
    assert healing_no_energy in picked, "energy_delivered_kwh > 0 must re-enter candidate set"
    assert healing_no_depot_via_station in picked, (
        "no_depot row with a station_id must resweep — SiteResolver may now match"
    )
    assert healing_no_depot_via_site in picked, (
        "no_depot row with a populated site_id must resweep — admin repair path"
    )
    assert always_resweep_unpriceable in picked, (
        "unpriceable always resweeps — ENTSO-E publication may arrive between runs"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_candidate_sql_under_depot_scope_admits_null_site_rows(
    db_pools: IntegrationPools, pool, cleanup,
):
    """Regression: ``--depot-id <X>`` applied ``site_id = $1`` strictly,
    dropping every NULL-site row even when the row's ``station_id``
    could resolve to depot X via ``SiteResolver``. The candidate SQL
    now also admits rows where ``site_id IS NULL AND station_id IS
    NOT NULL`` so the Python loop can run resolution + an exact-match
    post-filter. Without this, depot-scoped remediation misses
    exactly the rows operators most need to backfill (post-close
    cost task crashed; pre-site_id-backfill imports)."""
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)

    # Three fixtures to verify the depot-scoped clause:
    explicit_match = uuid4()       # site_id = depot_id → admitted
    null_with_station = uuid4()    # site_id NULL + station_id set → admitted (SiteResolver path)
    null_without_station = uuid4() # site_id NULL + station_id NULL → excluded (no recovery)
    for sid in (explicit_match, null_with_station, null_without_station):
        cleanup["sessions"].append(sid)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, 10.0, $4, 'no-vehicle', 'import', 0, NULL)
            """,
            explicit_match, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15), site_id,
        )
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, 10.0, NULL, 'no-vehicle', 'import', 0, NULL)
            """,
            null_with_station, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15),
        )
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, NULL, 1, 1, $2, $3, 10.0, NULL, 'no-vehicle', 'import', 0, NULL)
            """,
            null_without_station, _utc(2026, 5, 4, 14), _utc(2026, 5, 4, 15),
        )

        rows = await conn.fetch(
            bf._CANDIDATE_BY_SESSION_SQL,
            site_id,
            100,
            [explicit_match, null_with_station, null_without_station],
            bf._UUID_FLOOR,
        )

    picked = {r["session_id"] for r in rows}
    assert explicit_match in picked
    assert null_with_station in picked, (
        "depot-scoped run must admit NULL-site rows with a station_id "
        "so SiteResolver can recover them"
    )
    assert null_without_station not in picked, (
        "rows with no recovery path (site_id NULL AND station_id NULL) "
        "stay excluded — they would land 'no_depot' again"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_dry_run_iterates_all_chunks(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls, caplog,
):
    """Regression: ``--dry-run`` used to break out of the chunk loop after
    the first batch, an artifact of a pre-cursor design where dry-run
    couldn't terminate (the candidate predicate only flipped on writes).
    With the ``session_id > $cursor`` advance now in place, dry-run
    must cover the full population so operators can preview
    ``--max-rows 5000 --batch-size 500`` correctly across all ten
    chunks instead of seeing only 500."""
    import logging

    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
    await _seed_price(pool, _utc(2026, 5, 4, 14), 0.20)

    # Seed 5 candidate sessions; batch_size=2 forces ≥ 3 chunks.
    seeded_ids: list[UUID] = []
    for i in range(5):
        sid = uuid4()
        seeded_ids.append(sid)
        cleanup["sessions"].append(sid)
        await _seed_session(
            pool,
            session_id=sid,
            site_id=site_id,
            start=_utc(2026, 5, 4, 14) + timedelta(minutes=i),
            end=_utc(2026, 5, 4, 15) + timedelta(minutes=i),
            energy_kwh=30.0,
            cost_total=0,
        )

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)

    caplog.set_level(logging.INFO, logger="backfill_session_cost")
    await bf._run(
        _args(
            depot_id=site_id,
            dry_run=True,
            session_ids=seeded_ids,
            batch_size=2,
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

    # Each candidate must show up in the dry-run log line. Earlier
    # behavior would have logged only the first chunk's 2 sessions.
    dry_lines = [r.getMessage() for r in caplog.records if "DRY session=" in r.getMessage()]
    seen = {line.split("session=")[1].split(" ")[0] for line in dry_lines}
    assert {str(sid) for sid in seeded_ids} <= seen, (
        f"dry-run only previewed {len(seen)}/{len(seeded_ids)} sessions — "
        f"loop broke after first chunk. Got: {seen}"
    )

    # And the rows are still unchanged.
    async with pool.acquire() as conn:
        after = await conn.fetch(
            "SELECT cost_total, cost_total_source FROM charging_sessions "
            "WHERE session_id = ANY($1)",
            seeded_ids,
        )
    for row in after:
        assert row["cost_total"] == Decimal("0")
        assert row["cost_total_source"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_resweeps_unpriceable_when_prices_arrive(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    """Regression: rows the live close path tagged ``unpriceable``
    (ENTSO-E hadn't published yet) must be picked up again once prices
    land. The candidate predicate ``cost_total_source IS DISTINCT FROM
    'manual'`` includes 'unpriceable' / 'no_energy' / 'no_depot' rows
    so the backfill can heal them."""
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
    # Prices arrive AFTER the live close already wrote 'unpriceable'.
    await _seed_price(pool, _utc(2026, 5, 5, 8), 0.50)
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    await _seed_session(
        pool,
        session_id=session_id,
        site_id=site_id,
        start=_utc(2026, 5, 5, 8),
        end=_utc(2026, 5, 5, 9),
        energy_kwh=20.0,
        cost_total=None,
        cost_total_source="unpriceable",  # live close path's verdict
    )

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    await bf._run(
        _args(
            depot_id=site_id,
            session_ids=[session_id],
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions WHERE session_id = $1",
            session_id,
        )
    assert after["cost_total"] == Decimal("10.0000")  # 20 kWh × €0.50
    assert after["cost_total_source"] == "fallback_average"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_terminates_when_all_candidates_are_terminal(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    """Regression: rows whose calculator outcome is terminal
    (``'unpriceable'`` / ``'no_energy'`` / ``'no_depot'``) keep
    ``cost_total = NULL`` and so keep matching the candidate
    predicate. Without a within-run cursor the loop would re-pick
    them forever. The session_id-ordered cursor advances past them
    so the loop terminates after one pass."""
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
    # No prices seeded → every session is 'unpriceable' / terminal.
    session_id_a = uuid4()
    session_id_b = uuid4()
    cleanup["sessions"].extend([session_id_a, session_id_b])
    await _seed_session(
        pool, session_id=session_id_a, site_id=site_id,
        start=_utc(2026, 5, 5, 8), end=_utc(2026, 5, 5, 9),
        energy_kwh=10.0, cost_total=0, cost_total_source=None,
    )
    await _seed_session(
        pool, session_id=session_id_b, site_id=site_id,
        start=_utc(2026, 5, 5, 9), end=_utc(2026, 5, 5, 10),
        energy_kwh=10.0, cost_total=0, cost_total_source=None,
    )

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    # batch_size=1 forces multiple chunks; without the cursor fix the
    # first session keeps re-matching and the loop never terminates.
    # ``asyncio.wait_for`` is the watchdog — if the bug regresses we
    # fail fast with a clear timeout rather than hanging the suite.
    import asyncio as _asyncio
    await _asyncio.wait_for(
        bf._run(
            _args(
                depot_id=site_id,
                session_ids=[session_id_a, session_id_b],
                batch_size=1,
                database_url=ts_url,
                static_database_url=static_url,
            ),
        ),
        timeout=15.0,
    )

    # Both rows reached terminal state (no prices → 'unpriceable').
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT cost_total_source FROM charging_sessions "
            "WHERE session_id = ANY($1::uuid[])",
            [session_id_a, session_id_b],
        )
    assert {r["cost_total_source"] for r in rows} == {"unpriceable"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_continues_past_unpriceable_chunk_to_priceable_rows(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    """Regression: an earlier draft broke the chunk loop when
    ``chunk_priced == 0``, which meant a batch full of ``unpriceable``
    rows ended the run before the cursor reached higher-UUID
    candidates. With ``batch_size=1`` and a low-UUID unpriceable row
    seeded before a high-UUID priceable one, the loop must keep
    going — the cursor has advanced past the unpriceable row, so the
    next chunk fetches the priceable one and prices it.

    Forces deterministic UUID ordering with ``UUID(int=...)`` so the
    unpriceable row is guaranteed to sort first.
    """
    _require_seedable_sites(db_pools)
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)

    # Low session_id, no price in DB → 'unpriceable'
    unpriceable_id = UUID(int=1)
    # High session_id, price seeded → 'fallback_average'
    priceable_id = UUID(int=2**64)
    cleanup["sessions"].extend([unpriceable_id, priceable_id])

    await _seed_session(
        pool, session_id=unpriceable_id, site_id=site_id,
        start=_utc(2026, 6, 1, 8), end=_utc(2026, 6, 1, 9),
        energy_kwh=10.0, cost_total=0, cost_total_source=None,
    )
    # Seed price for the priceable row's window so it lands non-terminal.
    await _seed_price(pool, _utc(2026, 6, 1, 12), 0.50)
    await _seed_session(
        pool, session_id=priceable_id, site_id=site_id,
        start=_utc(2026, 6, 1, 12), end=_utc(2026, 6, 1, 13),
        energy_kwh=20.0, cost_total=0, cost_total_source=None,
    )

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    # batch_size=1 puts each session in its own chunk; if the loop
    # stops after the unpriceable chunk, the priceable row stays at
    # cost_total=0 / source=NULL and the assertion below fails.
    import asyncio as _asyncio
    await _asyncio.wait_for(
        bf._run(
            _args(
                depot_id=site_id,
                session_ids=[unpriceable_id, priceable_id],
                batch_size=1,
                database_url=ts_url,
                static_database_url=static_url,
            ),
        ),
        timeout=15.0,
    )

    async with pool.acquire() as conn:
        priced = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions "
            "WHERE session_id = $1",
            priceable_id,
        )
    assert priced["cost_total_source"] == "fallback_average"
    assert priced["cost_total"] == Decimal("10.0000")  # 20 kWh × €0.50


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_reprices_no_depot_row_after_site_id_repair(
    db_pools: IntegrationPools, pool, cleanup, monkeypatch, integration_db_urls,
):
    """Regression: a row tagged ``'no_depot'`` on a previous run
    (e.g. import missing ``site_id``) must become eligible again on
    the next backfill once the underlying data is repaired. An earlier
    draft excluded ``cost_total_source IN ('no_energy', 'no_depot')``
    from the candidate set, which permanently stranded those rows."""
    _require_seedable_sites(db_pools)
    session_id = uuid4()
    cleanup["sessions"].append(session_id)
    # First, the row exists with no site_id and cost_total_source=no_depot
    # (simulates the prior run's verdict).
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id,
                start_time, end_time, energy_delivered_kwh,
                site_id, vehicle_id, source, cost_total, cost_total_source
            ) VALUES ($1, 'cp-bf', 1, 1, $2, $3, $4, NULL, 'no-vehicle',
                      'import', NULL, 'no_depot')
            """,
            session_id,
            _utc(2026, 7, 1, 10), _utc(2026, 7, 1, 11), 30.0,
        )
    # Operator fixes the import: backfill site_id, seed price + sites row.
    site_id = uuid4()
    cleanup["sites"].append(site_id)
    cleanup["zones"].append(TEST_ZONE)
    await _seed_site_with_zone(db_pools.static_pool, site_id)
    await _seed_price(pool, _utc(2026, 7, 1, 10), 0.40)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE charging_sessions SET site_id = $1 WHERE session_id = $2",
            site_id, session_id,
        )

    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    import asyncio as _asyncio
    await _asyncio.wait_for(
        bf._run(
            _args(
                depot_id=site_id,
                session_ids=[session_id],
                database_url=ts_url,
                static_database_url=static_url,
            ),
        ),
        timeout=15.0,
    )

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT cost_total, cost_total_source FROM charging_sessions "
            "WHERE session_id = $1",
            session_id,
        )
    # Re-priced via fallback (no telemetry seeded).
    assert after["cost_total_source"] == "fallback_average"
    assert after["cost_total"] == Decimal("12.0000")  # 30 kWh × €0.40


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backfill_pilot_pilot_dry_run_split(
    db_pools: IntegrationPools, monkeypatch, integration_db_urls,
):
    """Dry-run backfill against the pilot depot on TigerCloud + Supabase (no site INSERT)."""
    if not db_pools.is_split or not db_pools.static_sites_readable:
        pytest.skip("needs TIMESCALE_SERVICE_URL + STATIC_DATABASE_URL / SUPABASE_DB_*")
    site_id = await find_lithuania_site_id(db_pools.static_pool)
    if site_id is None:
        pytest.skip("no Lithuania-resolving site in Supabase")
    ts_url, static_url = integration_db_urls
    monkeypatch.setenv("DATABASE_URL", ts_url)
    monkeypatch.setenv("STATIC_DATABASE_URL", static_url)
    rc = await bf._run(
        _args(
            depot_id=site_id,
            dry_run=True,
            max_rows=25,
            database_url=ts_url,
            static_database_url=static_url,
        ),
    )
    assert rc == 0
