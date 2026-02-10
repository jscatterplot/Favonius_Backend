#!/usr/bin/env python3
"""Run TimescaleDB migrations in order. For Railway pre-deploy or local.

Reads TIMESCALE_SERVICE_URL (preferred) or DATABASE_URL from env.
Runs migrations/001_*.sql, 003_*.sql, 004_*.sql, etc.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import ssl
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
    database_url = os.getenv("TIMESCALE_SERVICE_URL") or os.getenv("DATABASE_URL")
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

    files = _read_migrations_dir()
    if not files:
        print("No migration files found under migrations/", file=sys.stderr)
        return 1

    # Build SSL context when sslmode is present in the URL.
    # asyncpg needs an explicit ssl.SSLContext for cloud-hosted databases.
    ssl_context: ssl.SSLContext | bool | None = None
    if "sslmode=" in database_url:
        ssl_context = ssl.create_default_context()
        # Timescale Cloud / most managed DBs use valid certs, but if the
        # provider uses self-signed certs, fall back to unverified context.
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        # Strip sslmode param so asyncpg doesn't choke on it
        import re
        database_url = re.sub(r"[?&]sslmode=[^&]*", "", database_url)
        # Fix URL if stripping left a trailing '?' or '&'
        database_url = database_url.rstrip("?&")

    conn = await asyncpg.connect(database_url, ssl=ssl_context)
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

    url_source = "TIMESCALE_SERVICE_URL" if os.getenv("TIMESCALE_SERVICE_URL") else "DATABASE_URL"
    print(f"Migrations completed successfully (via {url_source}).")
    return 0


def main() -> int:
    return asyncio.run(run_migrations())


if __name__ == "__main__":
    sys.exit(main())
