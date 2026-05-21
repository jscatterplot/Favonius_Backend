#!/usr/bin/env python3
"""One-shot repair for imported charging_sessions with bad end_time.

Background
==========
The historical-charging-session XLSX importer (``POST
/admin/depots/{id}/charging-sessions/import``) previously round-tripped
the spreadsheet's ``end_time`` column verbatim. Some customer files
populated that column with absurd values (e.g. December 2026 timestamps)
while the trustworthy session length lived in a separate ``duration``
column. Rows persisted with these bogus ends then poisoned downstream
billing: ``scripts/backfill_session_cost.py`` pulls prices across the
full ``[start_time, end_time]`` window and gives up
(``cost_total_source = 'unpriceable'``) when the span exceeds the
ENTSO-E retention horizon.

This script clears ``end_time`` (and resets derived cost columns) on
the bad rows so that:

  1. ``scripts/backfill_session_cost.py`` skips them until the operator
     re-imports the original XLSX through the duration-aware API.
  2. The customer's re-import is allowed to write a corrected
     ``end_time`` via the new overwrite-on-non-null UPSERT clause.

Safety
======
* **Dry-run is the default.** Pass ``--apply`` to commit.
* Only rows with ``source = 'import'`` are touched — never live OCPP
  sessions.
* Selection predicate is the same one the API now rejects up front:
  ``end_time > now + 1 day`` OR ``end_time - start_time > 7 days``.
* ``cost_total_source = 'manual'`` rows are excluded — operator-supplied
  costs are sacrosanct.
* Wraps the UPDATE in a transaction; failure leaves no partial state.

Usage
=====
::

    # HRX pilot — dry-run first
    python scripts/repair_import_session_end_times.py \\
        --depot-id f6a8acca-d9c2-4db1-9174-f43641f291cf

    # Commit the same scope
    python scripts/repair_import_session_end_times.py \\
        --depot-id f6a8acca-d9c2-4db1-9174-f43641f291cf \\
        --apply

    # Audit all imports (no depot filter, dry-run)
    python scripts/repair_import_session_end_times.py

Exit codes:
    0 — completed successfully (dry-run or apply).
    1 — connection / fatal error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402

logger = logging.getLogger("repair_import_session_end_times")


_AUDIT_SQL = """
    SELECT
      COUNT(*) FILTER (WHERE end_time IS NOT NULL)                                                AS with_end,
      COUNT(*)                                                                                    AS total_import,
      COUNT(*) FILTER (WHERE end_time > NOW() + INTERVAL '1 day')                                 AS ends_in_future,
      COUNT(*) FILTER (WHERE end_time - start_time > INTERVAL '7 days')                           AS span_over_7d,
      COUNT(*) FILTER (
          WHERE end_time IS NOT NULL
            AND (end_time > NOW() + INTERVAL '1 day'
                 OR end_time - start_time > INTERVAL '7 days')
            AND COALESCE(cost_total_source, '') <> 'manual'
            AND cost_total IS NOT NULL
            AND cost_total > 0
      )                                                                                           AS bad_with_cost
    FROM charging_sessions
    WHERE source = 'import'
      AND ($1::uuid IS NULL OR site_id = $1::uuid)
"""

_SAMPLE_SQL = """
    SELECT session_id, start_time, end_time,
           end_time - start_time AS span,
           energy_delivered_kwh, cost_total, cost_total_source
      FROM charging_sessions
     WHERE source = 'import'
       AND end_time IS NOT NULL
       AND ($1::uuid IS NULL OR site_id = $1::uuid)
       AND (end_time > NOW() + INTERVAL '1 day'
            OR end_time - start_time > INTERVAL '7 days')
     ORDER BY end_time DESC
     LIMIT 20
"""

# IMPORTANT: this query is the unit of repair. Pre-sanitisation matches the
# resolver bounds in src/api/main.py (_resolve_import_end_time):
#   end_time > now + 1 day  OR  end_time - start_time > 7 days
# 'manual' cost rows are excluded — those are operator-supplied numbers.
_REPAIR_SQL = """
    UPDATE charging_sessions
       SET end_time          = NULL,
           cost_total        = NULL,
           cost_total_source = NULL
     WHERE source = 'import'
       AND end_time IS NOT NULL
       AND ($1::uuid IS NULL OR site_id = $1::uuid)
       AND COALESCE(cost_total_source, '') <> 'manual'
       AND (end_time > NOW() + INTERVAL '1 day'
            OR end_time - start_time > INTERVAL '7 days')
"""


async def _audit(conn: asyncpg.Connection, depot_id: Optional[UUID]) -> dict:
    row = await conn.fetchrow(_AUDIT_SQL, depot_id)
    return dict(row) if row else {}


async def _sample(conn: asyncpg.Connection, depot_id: Optional[UUID]) -> list[dict]:
    rows = await conn.fetch(_SAMPLE_SQL, depot_id)
    return [dict(r) for r in rows]


async def _run(database_url: str, depot_id: Optional[UUID], apply: bool) -> int:
    url, ssl = prepare_asyncpg_url_and_ssl(database_url)
    conn = await asyncpg.connect(url, ssl=ssl)
    try:
        audit = await _audit(conn, depot_id)
        scope = f"depot={depot_id}" if depot_id else "all-import"
        logger.info(
            "audit scope=%s total_import=%s with_end=%s ends_in_future=%s "
            "span_over_7d=%s bad_with_cost=%s",
            scope,
            audit.get("total_import", 0),
            audit.get("with_end", 0),
            audit.get("ends_in_future", 0),
            audit.get("span_over_7d", 0),
            audit.get("bad_with_cost", 0),
        )

        samples = await _sample(conn, depot_id)
        for row in samples:
            logger.info(
                "  worst-offender session_id=%s start=%s end=%s span=%s "
                "energy_kwh=%s cost=%s cost_source=%s",
                row["session_id"],
                row["start_time"],
                row["end_time"],
                row["span"],
                row["energy_delivered_kwh"],
                row["cost_total"],
                row["cost_total_source"],
            )

        if not apply:
            logger.info("dry-run: no changes applied. Re-run with --apply to commit.")
            return 0

        async with conn.transaction():
            result = await conn.execute(_REPAIR_SQL, depot_id)
        # asyncpg's execute() returns the tag string e.g. "UPDATE 1234".
        logger.info("applied: %s", result)
        return 0
    finally:
        await conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="TimescaleDB connection URL (env: DATABASE_URL)",
    )
    parser.add_argument(
        "--depot-id",
        type=UUID,
        default=None,
        help="Scope the repair to a single depot (UUID). Omit to scan all.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the UPDATE. Without this flag the script is dry-run.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.database_url:
        logger.error("DATABASE_URL is required (env or --database-url)")
        return 1
    return asyncio.run(_run(args.database_url, args.depot_id, args.apply))


if __name__ == "__main__":
    sys.exit(main())
