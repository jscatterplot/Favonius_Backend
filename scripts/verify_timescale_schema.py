#!/usr/bin/env python3
"""Verify Timescale/TigerCloud schema for charger list and session enrichment.

Uses the same URL resolution as ``scripts/run_migrations.py --target ts``:
``TIMESCALE_SERVICE_URL`` preferred, else ``DATABASE_URL``. Read-only.

Exit codes:
  0 — required tables and columns exist.
  1 — connection failure, missing table, or missing column.

If verification fails on live TigerCloud, apply additive migrations only::

  TIMESCALE_SERVICE_URL=... python scripts/run_migrations.py --target ts

Migration ``027_charging_sessions_live_status_columns.sql`` adds session
columns used by ``open_sessions_by_stations``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db.postgres_url import prepare_asyncpg_url_and_ssl

# Tables/columns referenced by ``src/db/queries.py`` for depot chargers + sessions.
REQUIRED: dict[str, frozenset[str]] = {
    "charging_sessions": frozenset(
        {
            "station_id",
            "session_id",
            "vehicle_id",
            "start_time",
            "end_time",
            "current_power_kw",
            "current_soc",
            "target_soc",
            "estimated_end_time",
        }
    ),
    "connector_status": frozenset({"station_id", "connector_id", "status", "timestamp"}),
    "telemetry": frozenset(
        {"time", "vehicle_id", "soc", "charging_kw", "charger_id", "is_plugged"}
    ),
    # vehicle_telemetry.odometer_km (migration 047) backs the EV-vs-diesel report's
    # distance; diesel_prices (migration 047) is read by fetch_or_pull_diesel_price.
    "vehicle_telemetry": frozenset({"time", "vehicle_id", "soc", "odometer_km"}),
    "diesel_prices": frozenset({"time", "region", "source", "price_eur_per_l"}),
}


def _resolve_ts_url() -> tuple[str, str]:
    url = os.getenv("TIMESCALE_SERVICE_URL") or os.getenv("DATABASE_URL")
    if not url:
        print(
            "TIMESCALE_SERVICE_URL or DATABASE_URL must be set (same as run_migrations --target ts).",
            file=sys.stderr,
        )
        sys.exit(1)
    src = "TIMESCALE_SERVICE_URL" if os.getenv("TIMESCALE_SERVICE_URL") else "DATABASE_URL"
    return url.strip(), src


async def _verify() -> int:
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 1

    raw_url, url_source = _resolve_ts_url()
    database_url, ssl_config = prepare_asyncpg_url_and_ssl(raw_url)
    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config

    try:
        conn = await asyncpg.connect(database_url, **connect_kw)
    except Exception as e:
        print(f"Failed to connect ({url_source}): {e}", file=sys.stderr)
        return 1

    try:
        tables = list(REQUIRED.keys())
        rows = await conn.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            tables,
        )
        present: dict[str, set[str]] = {t: set() for t in tables}
        for r in rows:
            present[r["table_name"]].add(r["column_name"])

        failed = False
        for table, cols in REQUIRED.items():
            if table not in present or not present[table]:
                print(f"Missing table or no columns visible: {table}", file=sys.stderr)
                failed = True
                continue
            missing = sorted(cols - present[table])
            if missing:
                print(
                    f"Table {table} missing columns: {', '.join(missing)} "
                    f"(source: {url_source})",
                    file=sys.stderr,
                )
                failed = True

        ext = await conn.fetchval(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb' LIMIT 1"
        )
        if ext is None:
            print(
                "Warning: timescaledb extension not found; expected on TigerCloud.",
                file=sys.stderr,
            )

        if failed:
            print(
                "\nRemediation (additive): "
                "TIMESCALE_SERVICE_URL=... python scripts/run_migrations.py --target ts",
                file=sys.stderr,
            )
            return 1

        print(f"Timescale schema OK ({url_source}, {len(tables)} tables checked).")
        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(_verify())


if __name__ == "__main__":
    sys.exit(main())
