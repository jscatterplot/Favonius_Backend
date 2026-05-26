#!/usr/bin/env python3
"""One-shot historical backfill for ``vehicle_telemetry`` from Navirec.

Pulls historical telematics readings (SoC + position) from Navirec and upserts
them into the ``vehicle_telemetry`` hypertable so analytics and surrogate-model
training have data from a depot's first day, not just from when the live poller
started. Reuses ``src/adapters/navirec`` so plate normalization, reading
parsing, and the DB upsert are identical to the live feed.

The backfill is **idempotent and re-runnable**: rows upsert with
``ON CONFLICT (vehicle_id, time) DO NOTHING`` (the same writer the poller uses),
so re-running over the same window adds zero rows.

Two pools, like ``scripts/backfill_session_cost.py``: ``--database-url`` for the
TimescaleDB instance holding ``vehicle_telemetry``, and ``--static-database-url``
for the Supabase static schema holding ``vehicles`` (the plate → vehicle_id
source). When both share one URL (local dev), point both flags at it.

Usage::

    python scripts/backfill_vehicle_telemetry_from_navirec.py \\
        --since 2026-01-01 --depot-id <uuid> --execute

    # dry-run (default) prints the plan and writes nothing
    python scripts/backfill_vehicle_telemetry_from_navirec.py --since 2026-01-01

Env fallbacks: DATABASE_URL, STATIC_DATABASE_URL / SUPABASE_DB_URL, plus the
NAVIREC_* client credentials.

Exit codes: 0 — completed (dry-run or apply); 1 — connection / fatal error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.adapters.navirec import (  # noqa: E402
    NavirecClient,
    build_plate_map,
    navirec_vehicle_id,
    navirec_vehicle_plate,
    navirec_vehicle_to_reading,
    write_depot_readings,
)
from src.adapters.navirec.poller import _reading_to_row  # noqa: E402

logger = logging.getLogger("backfill_vehicle_telemetry")


async def run_backfill(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    client: Any,
    *,
    since: datetime,
    until: Optional[datetime] = None,
    depot_id: Optional[str] = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Backfill historical readings. Returns a summary dict.

    Injectable ``client`` (anything exposing ``iter_vehicles`` +
    ``iter_vehicle_history``) so this is unit/integration testable with a fake.
    """
    until = until or datetime.now(timezone.utc)
    start_iso = since.astimezone(timezone.utc).isoformat()
    end_iso = until.astimezone(timezone.utc).isoformat()

    plate_map = await build_plate_map(static_pool)

    rows_by_depot: dict[str, list[tuple[Any, ...]]] = {}
    matched_vehicles = 0
    skipped_vehicles = 0

    async for nv in client.iter_vehicles():
        plate = navirec_vehicle_plate(nv)
        hit = plate_map.get(plate)
        if hit is None:
            skipped_vehicles += 1
            continue
        vehicle_id, vehicle_depot = hit
        if depot_id is not None and vehicle_depot != depot_id:
            continue
        nav_id = navirec_vehicle_id(nv)
        if nav_id is None:
            skipped_vehicles += 1
            continue
        matched_vehicles += 1
        async for raw in client.iter_vehicle_history(
            vehicle_id=nav_id, start_iso=start_iso, end_iso=end_iso
        ):
            if "licensePlate" in raw or "plate" in raw:
                reading_input = raw
            else:
                reading_input = dict(raw)
                reading_input["licensePlate"] = plate
            reading = navirec_vehicle_to_reading(reading_input)
            if reading is None:
                continue
            rows_by_depot.setdefault(vehicle_depot, []).append(_reading_to_row(vehicle_id, reading))

    planned = {d: len(r) for d, r in rows_by_depot.items()}
    total_planned = sum(planned.values())

    written: dict[str, int] = {}
    if execute:
        for depot, rows in rows_by_depot.items():
            count = await write_depot_readings(ts_pool, depot, rows)
            if count is not None:
                written[depot] = count

    summary = {
        "matched_vehicles": matched_vehicles,
        "skipped_vehicles": skipped_vehicles,
        "planned_rows": planned,
        "total_planned": total_planned,
        "written": written,
        "executed": execute,
    }
    logger.info("Backfill summary: %s", summary)
    return summary


def _parse_dt(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _amain(args: argparse.Namespace) -> int:
    ts_url = args.database_url or os.getenv("DATABASE_URL")
    static_url = (
        args.static_database_url
        or os.getenv("STATIC_DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or ts_url
    )
    if not ts_url:
        logger.error("No TimescaleDB URL (--database-url or DATABASE_URL).")
        return 1

    ts_pool = await asyncpg.create_pool(ts_url, min_size=1, max_size=4)
    static_pool = (
        ts_pool
        if static_url == ts_url
        else await asyncpg.create_pool(static_url, min_size=1, max_size=4)
    )
    client = NavirecClient()
    try:
        await run_backfill(
            static_pool,
            ts_pool,
            client,
            since=_parse_dt(args.since),
            until=_parse_dt(args.until) if args.until else None,
            depot_id=args.depot_id,
            execute=args.execute,
        )
    finally:
        await client.aclose()
        await ts_pool.close()
        if static_pool is not ts_pool:
            await static_pool.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="ISO date/datetime lower bound (UTC).")
    parser.add_argument("--until", help="ISO upper bound (default: now).")
    parser.add_argument("--depot-id", help="Restrict to one depot (Favonius site id).")
    parser.add_argument("--database-url", help="TimescaleDB URL (else DATABASE_URL).")
    parser.add_argument("--static-database-url", help="Static schema URL (else env).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Commit writes.")
    mode.add_argument("--dry-run", action="store_true", help="Plan only (default).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return asyncio.run(_amain(args))
    except Exception:  # noqa: BLE001
        logger.exception("Backfill failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
