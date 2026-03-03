#!/usr/bin/env python3
"""Run database migrations in order. For Railway pre-deploy or local.

Runs against two targets when both are configured:
  - TIMESCALE_SERVICE_URL (Timescale): full schema including hypertables.
  - DATABASE_URL (Supabase): static/reference schema; TimescaleDB-specific
    calls (create_hypertable, add_compression_policy, etc.) are stripped.

If only one variable is set, migrations run against that target only.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import ssl
import sys
from pathlib import Path

# Repo root: script lives in scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Matches TimescaleDB-specific function calls to strip when targeting plain
# PostgreSQL (Supabase).  DOTALL so multi-line calls are captured.
_TIMESCALE_CALL_RE = re.compile(
    r"SELECT\s+(create_hypertable|add_compression_policy|add_retention_policy)"
    r"\s*\([^;]*\)\s*;?",
    re.IGNORECASE | re.DOTALL,
)


def _read_migrations_dir() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f.name[0].isdigit()]


def _parse_url(url: str) -> tuple[str, ssl.SSLContext | bool | None]:
    """Return (cleaned_url, ssl_config) for asyncpg, stripping the sslmode param."""
    ssl_config: ssl.SSLContext | bool | None = None

    sslmode_match = re.search(r"[?&]sslmode=([^&#]*)", url)
    sslmode: str | None = sslmode_match.group(1) if sslmode_match else None

    if sslmode:
        mode = sslmode.lower()
        if mode == "disable":
            ssl_config = False
        else:
            ctx = ssl.create_default_context()
            if mode in {"require", "allow", "prefer"}:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ssl_config = ctx

        url = re.sub(r"\?sslmode=[^&#]*&?", "?", url)
        url = re.sub(r"&sslmode=[^&#]*", "", url)
        url = url.rstrip("?")

    return url, ssl_config


async def _run_against(
    url: str,
    label: str,
    files: list[Path],
    strip_timescale: bool = False,
) -> int:
    """Connect to *url* and apply *files*. Returns 0 on success, 1 on failure."""
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 1

    cleaned_url, ssl_config = _parse_url(url)

    try:
        conn = await asyncpg.connect(cleaned_url, ssl=ssl_config)
    except Exception as e:
        print(f"Failed to connect to database ({label}): {e}", file=sys.stderr)
        return 1

    try:
        for path in files:
            sql = path.read_text()
            if strip_timescale:
                sql = _TIMESCALE_CALL_RE.sub("", sql)
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

    print(f"Migrations completed successfully (via {label}).")
    return 0


async def run_migrations() -> int:
    timescale_url = os.getenv("TIMESCALE_SERVICE_URL")
    database_url = os.getenv("DATABASE_URL")

    if not timescale_url and not database_url:
        print(
            "TIMESCALE_SERVICE_URL or DATABASE_URL must be set",
            file=sys.stderr,
        )
        return 1

    files = _read_migrations_dir()
    if not files:
        print("No migration files found under migrations/", file=sys.stderr)
        return 1

    # Run against Timescale first (full schema with hypertables).
    if timescale_url:
        rc = await _run_against(timescale_url, "TIMESCALE_SERVICE_URL", files, strip_timescale=False)
        if rc != 0:
            return rc

    # Run against Supabase / plain Postgres (strip TimescaleDB-specific calls).
    if database_url:
        rc = await _run_against(database_url, "DATABASE_URL", files, strip_timescale=True)
        if rc != 0:
            return rc

    return 0


def main() -> int:
    return asyncio.run(run_migrations())


if __name__ == "__main__":
    sys.exit(main())
