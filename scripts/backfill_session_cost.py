#!/usr/bin/env python3
"""One-shot backfill for charging_sessions.cost_total.

Computes electricity cost for every closed charging_sessions row whose
``cost_total`` is NULL or 0 (and whose ``cost_total_source`` is not
``'manual'``). Reuses ``src/core/billing/session_cost.py`` so the math
is identical to the live close path.

The script is **idempotent and re-runnable**:

  * Candidate predicate ``WHERE cost_total IS NULL OR cost_total = 0`` is
    re-checked on every chunk. Already-priced rows are skipped.
  * ``FOR UPDATE SKIP LOCKED`` lets a concurrent live close take its
    own row without us blocking on it; we'll process it next sweep if
    the live path's cost task failed.
  * Rows where ``cost_total_source = 'manual'`` are never touched.

The backfill needs **two** pools: one to the TimescaleDB instance that
holds ``charging_sessions`` + ``electricity_prices``, and a separate
one to the Supabase static schema that holds ``sites`` (the source of
each depot's ENTSO-E bidding zone). When the two databases share a
single URL (local dev, single-pg deployments), point both flags at the
same URL.

Usage::

    # Two-pool deployment (production / TigerCloud + Supabase):
    python scripts/backfill_session_cost.py \\
        --database-url postgresql://.../timescale \\
        --static-database-url postgresql://.../supabase

    # Single-pool deployment (defaults from env):
    DATABASE_URL=postgresql://... \\
        STATIC_DATABASE_URL=postgresql://... \\
        python scripts/backfill_session_cost.py

    # Other flags:
    --dry-run                    # print decisions, no writes
    --depot-id <uuid>            # one depot
    --max-rows N                 # stop after N rows total
    --batch-size N               # rows per transaction (default 500)

Exit codes:
    0 — completed successfully (dry-run or apply)
    1 — connection / fatal error
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imports below intentionally after sys.path tweak (repo root → src.* packages).
from src.core.billing.session_cost import (  # noqa: E402
    _expected_hour_buckets,
    compute_session_cost,
    write_session_cost,
)
from src.db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402
from src.db.queries import fetch_prices_by_zone, resolve_bidding_zone  # noqa: E402

logger = logging.getLogger("backfill_session_cost")


_CANDIDATE_SQL = """
    SELECT session_id, site_id, vehicle_id, station_id, connector_id,
           transaction_id, start_time, end_time,
           energy_delivered_kwh, cost_total, cost_total_source
      FROM charging_sessions
     WHERE end_time IS NOT NULL
       AND (cost_total IS NULL OR cost_total = 0)
       AND cost_total_source IS DISTINCT FROM 'manual'
       AND ($1::uuid IS NULL OR site_id = $1)
     ORDER BY end_time
     LIMIT $2
       FOR UPDATE SKIP LOCKED
"""

_CANDIDATE_BY_SESSION_SQL = """
    SELECT session_id, site_id, vehicle_id, station_id, connector_id,
           transaction_id, start_time, end_time,
           energy_delivered_kwh, cost_total, cost_total_source
      FROM charging_sessions
     WHERE end_time IS NOT NULL
       AND (cost_total IS NULL OR cost_total = 0)
       AND cost_total_source IS DISTINCT FROM 'manual'
       AND ($1::uuid IS NULL OR site_id = $1)
       AND session_id = ANY($3::uuid[])
     ORDER BY end_time
     LIMIT $2
       FOR UPDATE SKIP LOCKED
"""


class LRUPriceLookup:
    """Bounded in-process price cache for the backfill run.

    Keys are ``(bidding_zone, hour_floor_utc)``. The cache is populated
    as needed by delegating uncovered ranges to
    :func:`fetch_prices_by_zone` and merging the result. Repeat zones
    within a chunk skip the DB round-trip — the win that motivates the
    cache.
    """

    def __init__(self, ts_pool: asyncpg.Pool, *, max_entries: int = 10_000) -> None:
        self._pool = ts_pool
        self._max = max_entries
        self._cache: OrderedDict[tuple[str, datetime], float] = OrderedDict()

    async def __call__(
        self,
        bidding_zone: str,
        start: datetime,
        end: datetime,
    ) -> dict[datetime, float]:
        needed = _expected_hour_buckets(start, end)
        if not needed:
            async with self._pool.acquire() as conn:
                return await fetch_prices_by_zone(conn, bidding_zone, start, end)

        result: dict[datetime, float] = {}
        for hour in needed:
            key = (bidding_zone, hour)
            if key in self._cache:
                self._cache.move_to_end(key)
                result[hour] = self._cache[key]

        if len(result) == len(needed):
            return result

        async with self._pool.acquire() as conn:
            fresh = await fetch_prices_by_zone(conn, bidding_zone, start, end)
        for hour, price in fresh.items():
            self._put(bidding_zone, hour, price)
        for hour in needed:
            key = (bidding_zone, hour)
            if key in self._cache:
                result[hour] = self._cache[key]
        return result

    def _put(self, bidding_zone: str, hour: datetime, price: float) -> None:
        key = (bidding_zone, hour)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = price
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)


class ZoneResolver:
    """Caches ``site_id → bidding_zone`` for the duration of the backfill.

    sites.tariff_config / sites.timezone don't change during a backfill
    run, so one round-trip per depot is enough — even with thousands
    of sessions per depot.
    """

    def __init__(self, static_pool: asyncpg.Pool) -> None:
        self._pool = static_pool
        self._cache: dict[UUID, Optional[str]] = {}

    async def __call__(self, site_id: Optional[UUID]) -> Optional[str]:
        if site_id is None:
            return None
        if site_id in self._cache:
            return self._cache[site_id]
        async with self._pool.acquire() as conn:
            zone = await resolve_bidding_zone(conn, site_id)
        self._cache[site_id] = zone
        return zone


def _resolve_ts_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("TIMESCALE_SERVICE_URL")
    if not url:
        raise RuntimeError(
            "Set DATABASE_URL or TIMESCALE_SERVICE_URL to point at the "
            "TimescaleDB instance to backfill."
        )
    return url


def _resolve_static_url() -> str:
    """Static-schema URL: SUPABASE_DB_URL / STATIC_DATABASE_URL / DATABASE_URL."""
    url = (
        os.getenv("STATIC_DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "Set STATIC_DATABASE_URL (or SUPABASE_DB_URL, or DATABASE_URL) "
            "to point at the Supabase static schema. Bidding-zone "
            "resolution requires reading sites.tariff_config / "
            "sites.timezone."
        )
    return url


async def _open_pool(url: str, *, label: str) -> asyncpg.Pool:
    clean_url, ssl_config = prepare_asyncpg_url_and_ssl(url)
    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config
    logger.info("Opening %s pool", label)
    return await asyncpg.create_pool(
        clean_url, min_size=1, max_size=10, **connect_kw
    )


async def _run(args: argparse.Namespace) -> int:
    ts_url = args.database_url or _resolve_ts_url()
    static_url = args.static_database_url or _resolve_static_url()
    logger.info(
        "Backfill starting — dry_run=%s depot=%s batch_size=%s max_rows=%s",
        args.dry_run, args.depot_id, args.batch_size, args.max_rows,
    )

    ts_pool = await _open_pool(ts_url, label="timescaledb")
    # If both URLs resolve to the same DSN we still open a second pool —
    # cleaner than sharing connections, and the cost is negligible for a
    # one-shot script.
    static_pool = await _open_pool(static_url, label="static")
    try:
        price_cache = LRUPriceLookup(ts_pool)
        zone_resolver = ZoneResolver(static_pool)
        counts: Counter[str] = Counter()
        processed = 0

        while True:
            if args.max_rows is not None and processed >= args.max_rows:
                break
            chunk_limit = args.batch_size
            if args.max_rows is not None:
                chunk_limit = min(chunk_limit, args.max_rows - processed)

            session_ids = getattr(args, "session_ids", None) or []

            async with ts_pool.acquire() as conn, conn.transaction():
                if session_ids:
                    rows = await conn.fetch(
                        _CANDIDATE_BY_SESSION_SQL,
                        args.depot_id,
                        chunk_limit,
                        session_ids,
                    )
                else:
                    rows = await conn.fetch(
                        _CANDIDATE_SQL,
                        args.depot_id,
                        chunk_limit,
                    )
                if not rows:
                    break

                for row in rows:
                    row_dict = dict(row)
                    row_dict["bidding_zone"] = await zone_resolver(
                        row_dict.get("site_id")
                    )
                    result = await compute_session_cost(
                        ts_pool, row_dict, price_lookup=price_cache,
                    )
                    counts[result.source] += 1
                    if args.dry_run:
                        logger.info(
                            "DRY session=%s source=%s cost=%s",
                            row["session_id"], result.source, result.cost,
                        )
                    else:
                        wrote = await write_session_cost(
                            ts_pool, row["session_id"], result, conn=conn,
                        )
                        if not wrote:
                            counts["__write_lost_race"] += 1

                processed += len(rows)
                logger.info(
                    "chunk done — total processed=%d (%s)",
                    processed,
                    ", ".join(f"{k}={v}" for k, v in counts.most_common()),
                )
                # Dry-run never mutates rows; the candidate predicate
                # would match the same chunk forever.
                if args.dry_run:
                    break

        logger.info(
            "Backfill complete — processed=%d sources=%s",
            processed,
            dict(counts),
        )
        return 0
    finally:
        await ts_pool.close()
        await static_pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Compute but do not write. Logs every decision.")
    p.add_argument("--depot-id", type=str, default=None,
                   help="Restrict backfill to one depot UUID.")
    p.add_argument("--batch-size", type=int, default=500,
                   help="Rows per transaction (default: 500).")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Stop after processing N rows total (default: no limit).")
    p.add_argument("--log-level", type=str, default="INFO",
                   help="Python logging level (default: INFO).")
    p.add_argument("--database-url", type=str, default=None,
                   help="TimescaleDB URL. Overrides DATABASE_URL / TIMESCALE_SERVICE_URL.")
    p.add_argument("--static-database-url", type=str, default=None,
                   help="Supabase static-schema URL. Overrides "
                        "STATIC_DATABASE_URL / SUPABASE_DB_URL / DATABASE_URL.")
    args = p.parse_args()
    if args.depot_id is not None:
        try:
            args.depot_id = UUID(args.depot_id)
        except ValueError:
            p.error(f"--depot-id must be a UUID, got {args.depot_id!r}")
    return args


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception:
        logger.exception("Fatal error during backfill")
        return 1


if __name__ == "__main__":
    sys.exit(main())
