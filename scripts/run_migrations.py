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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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

    # Build SSL configuration when sslmode is present in the URL.
    # asyncpg needs an explicit ssl.SSLContext or bool for cloud-hosted databases.
    ssl_config: ssl.SSLContext | bool | None = None
    parsed = urlparse(database_url)
    query_params = parse_qsl(parsed.query, keep_blank_values=True)

    sslmode: str | None = None
    filtered_query: list[tuple[str, str]] = []
    for key, value in query_params:
        if key.lower() == "sslmode":
            sslmode = value
        else:
            filtered_query.append((key, value))

    if sslmode:
        mode = sslmode.lower()
        if mode == "disable":
            # Explicitly requested no SSL.
            ssl_config = False
        else:
            # For all non-disable modes we establish an SSL context.
            # libpq-style negotiation modes like "allow" and "prefer" cannot be
            # expressed directly in asyncpg, so we treat them as "require" from
            # the client's perspective.
            ctx = ssl.create_default_context()
            if mode in {"require", "allow", "prefer"}:
                # Timescale Cloud / most managed DBs use valid certs, but if the
                # provider uses self-signed certs, fall back to unverified context.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            # For verify-ca / verify-full we keep default verification behaviour.
            ssl_config = ctx

        # Strip sslmode param so asyncpg doesn't choke on it while keeping the URL valid.
        new_query = urlencode(filtered_query, doseq=True)
        parsed = parsed._replace(query=new_query)
        database_url = urlunparse(parsed)

    conn = await asyncpg.connect(database_url, ssl=ssl_config)
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
