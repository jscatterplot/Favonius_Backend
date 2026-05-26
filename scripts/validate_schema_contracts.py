#!/usr/bin/env python3
"""Validate cross-service schema contracts used by runtime queries.

This is a static validator (no DB connection needed). It cross-checks:
- SQL migrations under migrations/
- schema builder modules under src/websocket_handler/
- query contracts in key runtime modules
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_create_table_columns(text: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    pattern = re.compile(
        r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?([a-zA-Z_][\w]*)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(text):
        table = m.group(1).lower()
        body = m.group(2)
        cols: set[str] = set()
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            token = line.split()[0].strip('"').lower()
            if token in {"constraint", "primary", "foreign", "unique", "check"}:
                continue
            if token == ")":
                continue
            cols.add(token)
        tables[table] = cols
    return tables


def merge_table_maps(*maps: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for mp in maps:
        for table, cols in mp.items():
            merged.setdefault(table, set()).update(cols)
    return merged


def load_tables(path: Path) -> dict[str, set[str]]:
    return parse_create_table_columns(path.read_text())


def load_tables_from_dir(path: Path) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for sql in sorted(path.glob('*.sql')):
        mp = load_tables(sql)
        for table, cols in mp.items():
            merged.setdefault(table, set()).update(cols)
    return merged


def main() -> int:
    # Static schema definitions
    ts_migrations = load_tables_from_dir(ROOT / "migrations")
    supabase_migrations = load_tables_from_dir(ROOT / "migrations/supabase")
    ws_timescale_schema = load_tables(ROOT / "src/websocket_handler/timescale_schema.py")

    static_contract = merge_table_maps(ts_migrations, supabase_migrations)
    timescale_contract = merge_table_maps(ts_migrations, ws_timescale_schema)

    required_static = {
        "depots": {"depot_id", "created_at"},
        "schedules": {"vehicle_id", "departure_time"},
    }
    required_timescale = {
        "service_heartbeat": {"service", "last_seen"},
        "charging_sessions": {
            "session_id",
            "station_id",
            "vehicle_id",
            "start_time",
            "end_time",
            "energy_delivered_kwh",
            "energy_received_kwh",
            "cost_total",
            "revenue_v2g",
            "sync_status",
        },
    }

    errors: list[str] = []

    def check(source: str, tables: dict[str, set[str]], req: dict[str, set[str]]):
        for table, req_cols in req.items():
            if table not in tables:
                errors.append(f"[{source}] missing table: {table}")
                continue
            missing = sorted(req_cols - tables[table])
            if missing:
                errors.append(f"[{source}] {table} missing columns: {', '.join(missing)}")

    check("static", static_contract, required_static)
    check("timescale", timescale_contract, required_timescale)

    print("Schema contract validation report")
    print("- static tables checked:", ", ".join(sorted(required_static)))
    print("- timescale tables checked:", ", ".join(sorted(required_timescale)))

    if errors:
        print("\nFAIL:")
        for err in errors:
            print(" -", err)
        return 1

    print("\nPASS: all required tables/columns are present in schema sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
