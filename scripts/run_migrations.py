#!/usr/bin/env python3
"""Run database migrations in order. For Railway pre-deploy or local.

Usage:
  python scripts/run_migrations.py                # TimescaleDB (default)
  python scripts/run_migrations.py --target ts    # TimescaleDB (explicit)
  python scripts/run_migrations.py --target supabase  # Supabase static schema

Target behaviour:
  ts        Reads TIMESCALE_SERVICE_URL (preferred) or DATABASE_URL.
            Runs migrations/*.sql (numbered files only).
  supabase  Reads DATABASE_URL.
            Runs migrations/supabase/*.sql (numbered files only).

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Repo root: script lives in scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db.postgres_url import prepare_asyncpg_url_and_ssl


def _read_migrations_dir(target: str) -> list[Path]:
    if target == "supabase":
        directory = MIGRATIONS_DIR / "supabase"
    else:
        directory = MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.sql"))
    return [f for f in files if f.name[0].isdigit()]


def _resolve_database_url(target: str) -> tuple[str, str]:
    """Return (url, source_env_var_name) for the given target."""
    if target == "supabase":
        url = os.getenv("DATABASE_URL")
        if not url:
            print("DATABASE_URL must be set for --target supabase", file=sys.stderr)
            sys.exit(1)
        return url, "DATABASE_URL"
    # ts (default): prefer TIMESCALE_SERVICE_URL, fall back to DATABASE_URL
    url = os.getenv("TIMESCALE_SERVICE_URL") or os.getenv("DATABASE_URL")
    if not url:
        print(
            "TIMESCALE_SERVICE_URL or DATABASE_URL must be set",
            file=sys.stderr,
        )
        sys.exit(1)
    source = "TIMESCALE_SERVICE_URL" if os.getenv("TIMESCALE_SERVICE_URL") else "DATABASE_URL"
    return url, source


async def run_migrations(target: str = "ts") -> int:
    database_url, url_source = _resolve_database_url(target)
    if not database_url:
        print(
            "TIMESCALE_SERVICE_URL or DATABASE_URL must be set",
            file=sys.stderr,
        )
        return 1

    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 1

    files = _read_migrations_dir(target)
    if not files:
        print("No migration files found under migrations/", file=sys.stderr)
        return 1

    database_url, ssl_config = prepare_asyncpg_url_and_ssl(database_url)

    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config

    try:
        conn = await asyncpg.connect(database_url, **connect_kw)
    except Exception as e:
        print(f"Failed to connect to database ({url_source}): {e}", file=sys.stderr)
        return 1

    try:
        for path in files:
            sql = path.read_text()
            try:
                await conn.execute(sql)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    print(f"Applied {path.name} (already applied)")
                else:
                    print(f"Migration {path.name} failed: {e}", file=sys.stderr)
                    return 1
            else:
                print(f"Applied {path.name}")
    finally:
        await conn.close()

    print(f"Migrations completed successfully (via {url_source}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run database migrations.")
    parser.add_argument(
        "--target",
        choices=["ts", "supabase"],
        default="ts",
        help=(
            "Database target: 'ts' (TimescaleDB, default) or 'supabase' (static schema). "
            "ts uses TIMESCALE_SERVICE_URL or DATABASE_URL; "
            "supabase uses DATABASE_URL."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(run_migrations(target=args.target))


if __name__ == "__main__":
    sys.exit(main())
