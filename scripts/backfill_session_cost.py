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

Usage::

    python scripts/backfill_session_cost.py                # process all
    python scripts/backfill_session_cost.py --dry-run      # print decisions only
    python scripts/backfill_session_cost.py --depot-id <uuid>  # one depot
    python scripts/backfill_session_cost.py --max-rows 1000    # cap the run
    python scripts/backfill_session_cost.py --batch-size 200   # smaller chunks

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imports below intentionally after sys.path tweak (repo root → src.* packages).
from src.core.billing.session_cost import (  # noqa: E402
    compute_session_cost,
    write_session_cost,
)
from src.db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402
from src.db.queries import fetch_prices_with_fill  # noqa: E402

logger = logging.getLogger("backfill_session_cost")


_CANDIDATE_SQL = """
    SELECT session_id, site_id, vehicle_id, start_time, end_time,
           energy_delivered_kwh, cost_total, cost_total_source
      FROM charging_sessions
     WHERE end_time IS NOT NULL
       AND (cost_total IS NULL OR cost_total = 0)
       AND cost_total_source IS NULL
       AND ($1::uuid IS NULL OR site_id = $1)
     ORDER BY end_time
     LIMIT $2
       FOR UPDATE SKIP LOCKED
"""

_CANDIDATE_BY_SESSION_SQL = """
    SELECT session_id, site_id, vehicle_id, start_time, end_time,
           energy_delivered_kwh, cost_total, cost_total_source
      FROM charging_sessions
     WHERE end_time IS NOT NULL
       AND (cost_total IS NULL OR cost_total = 0)
       AND cost_total_source IS NULL
       AND ($1::uuid IS NULL OR site_id = $1)
       AND session_id = ANY($3::uuid[])
     ORDER BY end_time
     LIMIT $2
       FOR UPDATE SKIP LOCKED
"""


class LRUPriceLookup:
    """Bounded in-process price cache for the backfill run.

    Keys are ``(depot_id, hour_floor_utc)``. The cache is populated as
    needed by delegating uncovered ranges to ``fetch_prices_with_fill``
    and merging the result. Repeat depots within a chunk skip the DB
    round-trip — the win that motivates the cache.
    """

    def __init__(self, pool: asyncpg.Pool, *, max_entries: int = 10_000) -> None:
        self._pool = pool
        self._max = max_entries
        self._cache: OrderedDict[tuple[UUID, datetime], float] = OrderedDict()

    async def __call__(
        self,
        depot_id: UUID,
        start: datetime,
        end: datetime,
    ) -> dict[datetime, float]:
        async with self._pool.acquire() as conn:
            fresh = await fetch_prices_with_fill(conn, depot_id, start, end)
        for hour, price in fresh.items():
            self._put(depot_id, hour, price)
        return {
            hour: price for hour, price in fresh.items()
        }

    def _put(self, depot_id: UUID, hour: datetime, price: float) -> None:
        key = (depot_id, hour)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = price
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)


def _resolve_database_url() -> str:
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("TIMESCALE_SERVICE_URL")
    )
    if not url:
        raise RuntimeError(
            "Set DATABASE_URL or TIMESCALE_SERVICE_URL to point at the "
            "TimescaleDB instance to backfill."
        )
    return url


async def _run(args: argparse.Namespace) -> int:
    url = _resolve_database_url()
    logger.info(
        "Backfill starting — dry_run=%s depot=%s batch_size=%s max_rows=%s",
        args.dry_run, args.depot_id, args.batch_size, args.max_rows,
    )
    database_url, ssl_config = prepare_asyncpg_url_and_ssl(url)
    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, **connect_kw)
    try:
        price_cache = LRUPriceLookup(pool)
        counts: Counter[str] = Counter()
        processed = 0

        while True:
            if args.max_rows is not None and processed >= args.max_rows:
                break
            chunk_limit = args.batch_size
            if args.max_rows is not None:
                chunk_limit = min(chunk_limit, args.max_rows - processed)

            session_ids = getattr(args, "session_ids", None) or []

            async with pool.acquire() as conn, conn.transaction():
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
                    result = await compute_session_cost(
                        pool, dict(row), price_lookup=price_cache,
                    )
                    counts[result.source] += 1
                    if args.dry_run:
                        logger.info(
                            "DRY session=%s source=%s cost=%s",
                            row["session_id"], result.source, result.cost,
                        )
                    else:
                        wrote = await write_session_cost(
                            pool, row["session_id"], result, conn=conn,
                        )
                        if not wrote:
                            counts["__write_lost_race"] += 1

                processed += len(rows)
                logger.info(
                    "chunk done — total processed=%d (%s)",
                    processed,
                    ", ".join(f"{k}={v}" for k, v in counts.most_common()),
                )
                # Dry-run never mutates rows; the candidate predicate would match
                # the same chunk forever.
                if args.dry_run:
                    break

        logger.info(
            "Backfill complete — processed=%d sources=%s",
            processed,
            dict(counts),
        )
        return 0
    finally:
        await pool.close()


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
