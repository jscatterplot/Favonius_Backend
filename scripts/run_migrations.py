#!/usr/bin/env python3
"""Run TimescaleDB migrations in order. For Railway pre-deploy or local.

Reads DATABASE_URL from env. Runs migrations/001_*.sql, 003_*.sql, 004_*.sql, etc.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Repo root: script lives in scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _read_migrations_dir() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f.name[0].isdigit()]


async def run_migrations() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 1

    files = _read_migrations_dir()
    if not files:
        print("No migration files found under migrations/", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
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

    print("Migrations completed successfully.")
    return 0


def main() -> int:
    return asyncio.run(run_migrations())


if __name__ == "__main__":
    sys.exit(main())
