"""Integration tests for the Navirec telematics feed (real DB).

Three concerns the unit suite (fakes) can't cover:

1. The dual-source SoC merge SQL in ``StateAssembler._get_vehicle_socs`` —
   UNION of ``telemetry`` + ``vehicle_telemetry``, freshest-wins, 24h bound.
2. The Postgres advisory lock that makes per-depot writes single-flight.
3. End-to-end backfill idempotency (re-run adds zero rows).

Skips cleanly when the static schema isn't seedable (split deployment) or when
migration 044 (``vehicle_telemetry``) hasn't been applied.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.backfill_vehicle_telemetry_from_navirec import run_backfill
from src.core.models import DepotConfig
from src.core.state.assembler import StateAssembler
from src.db.pools import DatabasePools

pytestmark = [pytest.mark.integration, pytest.mark.database]


async def _table_exists(conn, name: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=$1)",
            name,
        )
    )


def _minimal_config() -> DepotConfig:
    return DepotConfig(
        vehicle_capacities={},
        vehicle_max_charge_kw={},
        charger_groups={},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=0.0,
        battery_power=0.0,
    )


async def _seed_org_site(conn, org_id, site_id):
    await conn.execute(
        "INSERT INTO organizations (id, name) VALUES ($1, 'Navirec Test Org') "
        "ON CONFLICT (id) DO NOTHING",
        org_id,
    )
    await conn.execute(
        "INSERT INTO sites (id, organization_id, name, timezone) "
        "VALUES ($1, $2, 'Navirec depot', 'Europe/Vilnius') ON CONFLICT (id) DO NOTHING",
        site_id,
        org_id,
    )


@pytest.mark.asyncio
async def test_soc_merge_freshest_wins(db_pools):
    if not db_pools.can_seed_sites:
        pytest.skip("static schema not seedable on this deployment")
    ts = db_pools.ts_pool
    async with ts.acquire() as conn:
        if not await _table_exists(conn, "vehicle_telemetry"):
            pytest.skip("migration 044 (vehicle_telemetry) not applied")

    org_id = uuid.uuid4()
    site_id = uuid.uuid4()
    v_charger_wins = uuid.uuid4()  # charger fresher than telematics
    v_telematics_only = uuid.uuid4()  # only telematics
    v_too_stale = uuid.uuid4()  # only a >24h telematics reading → excluded
    now = datetime.now(timezone.utc)

    try:
        async with ts.acquire() as conn:
            await _seed_org_site(conn, org_id, site_id)
            await conn.executemany(
                "INSERT INTO vehicles (id, organization_id, site_id, display_name) "
                "VALUES ($1, $2, $3, $4)",
                [
                    (v_charger_wins, org_id, site_id, "BUS-CW"),
                    (v_telematics_only, org_id, site_id, "BUS-TO"),
                    (v_too_stale, org_id, site_id, "BUS-ST"),
                ],
            )
            # Charger telemetry: only for v_charger_wins, 2 min old.
            await conn.execute(
                "INSERT INTO telemetry (time, vehicle_id, soc) VALUES ($1, $2, $3)",
                now - timedelta(minutes=2),
                v_charger_wins,
                0.40,
            )
            # Telematics: older for v_charger_wins, sole source for the others.
            await conn.executemany(
                "INSERT INTO vehicle_telemetry (time, vehicle_id, soc, source) "
                "VALUES ($1, $2, $3, 'navirec')",
                [
                    (now - timedelta(minutes=10), v_charger_wins, 0.55),
                    (now - timedelta(minutes=5), v_telematics_only, 0.70),
                    (now - timedelta(hours=48), v_too_stale, 0.90),
                ],
            )

        assembler = StateAssembler(DatabasePools(static=ts, ts=ts), site_id, _minimal_config())
        socs = await assembler._get_vehicle_socs()

        assert socs[str(v_charger_wins)] == pytest.approx(0.40)  # fresher charger wins
        assert socs[str(v_telematics_only)] == pytest.approx(0.70)  # telematics fills the gap
        assert str(v_too_stale) not in socs  # >24h excluded by the scan bound
    finally:
        async with ts.acquire() as conn:
            ids = [v_charger_wins, v_telematics_only, v_too_stale]
            await conn.execute("DELETE FROM telemetry WHERE vehicle_id = ANY($1::uuid[])", ids)
            await conn.execute(
                "DELETE FROM vehicle_telemetry WHERE vehicle_id = ANY($1::uuid[])", ids
            )
            await conn.execute("DELETE FROM vehicles WHERE id = ANY($1::uuid[])", ids)
            await conn.execute("DELETE FROM sites WHERE id = $1", site_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)


@pytest.mark.asyncio
async def test_advisory_lock_single_flight(db_pools):
    ts = db_pools.ts_pool
    key = f"navirec_poll:test:{uuid.uuid4()}"
    lock_sql = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))"
    unlock_sql = "SELECT pg_advisory_unlock(hashtextextended($1, 0))"

    async with ts.acquire() as conn_a, ts.acquire() as conn_b:
        assert await conn_a.fetchval(lock_sql, key) is True
        # Second connection cannot take the same lock while A holds it.
        assert await conn_b.fetchval(lock_sql, key) is False
        # Release on A; now B can take it.
        assert await conn_a.fetchval(unlock_sql, key) is True
        assert await conn_b.fetchval(lock_sql, key) is True
        await conn_b.fetchval(unlock_sql, key)


class _FakeNavirec:
    def __init__(self, vehicles, history):
        self._vehicles = vehicles
        self._history = history

    async def iter_vehicles(self):
        for v in self._vehicles:
            yield v

    async def iter_vehicle_history(self, *, vehicle_id, start_iso, end_iso):
        for h in self._history.get(vehicle_id, []):
            yield h


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_pools):
    if not db_pools.can_seed_sites:
        pytest.skip("static schema not seedable on this deployment")
    ts = db_pools.ts_pool
    async with ts.acquire() as conn:
        if not await _table_exists(conn, "vehicle_telemetry"):
            pytest.skip("migration 044 (vehicle_telemetry) not applied")

    org_id = uuid.uuid4()
    site_id = uuid.uuid4()
    vehicle_id = uuid.uuid4()
    plate = f"NAV{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now(timezone.utc)
    history = {
        "nav-1": [
            {"soc": 50, "timestamp": (now - timedelta(hours=3)).isoformat()},
            {"soc": 55, "timestamp": (now - timedelta(hours=2)).isoformat()},
            {"soc": 60, "timestamp": (now - timedelta(hours=1)).isoformat()},
        ]
    }
    fake = _FakeNavirec(
        vehicles=[{"id": "nav-1", "licensePlate": plate}],
        history=history,
    )

    async def _count() -> int:
        async with ts.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM vehicle_telemetry WHERE vehicle_id = $1", vehicle_id
            )

    try:
        async with ts.acquire() as conn:
            await _seed_org_site(conn, org_id, site_id)
            await conn.execute(
                "INSERT INTO vehicles (id, organization_id, site_id, display_name, license_plate) "
                "VALUES ($1, $2, $3, 'BUS-NAV', $4)",
                vehicle_id,
                org_id,
                site_id,
                plate,
            )

        # Dry-run writes nothing.
        dry = await run_backfill(ts, ts, fake, since=now - timedelta(days=1), execute=False)
        assert dry["written"] == {}
        assert await _count() == 0

        # First execute writes all 3.
        first = await run_backfill(ts, ts, fake, since=now - timedelta(days=1), execute=True)
        assert first["written"].get(str(site_id)) == 3
        assert await _count() == 3

        # Second execute is a no-op (ON CONFLICT DO NOTHING) — row count unchanged.
        await run_backfill(ts, ts, fake, since=now - timedelta(days=1), execute=True)
        assert await _count() == 3
    finally:
        async with ts.acquire() as conn:
            await conn.execute("DELETE FROM vehicle_telemetry WHERE vehicle_id = $1", vehicle_id)
            await conn.execute("DELETE FROM vehicles WHERE id = $1", vehicle_id)
            await conn.execute("DELETE FROM sites WHERE id = $1", site_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
