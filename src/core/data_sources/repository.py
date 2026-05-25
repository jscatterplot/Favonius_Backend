"""asyncpg data-access layer for data-source connections and ingestion jobs.

All rows live on the Supabase static pool (migrations 042/043). Functions return
``asyncpg.Record`` objects; callers convert to the wire shape. SQL here never
selects ``encrypted_credentials`` except in :func:`get_connection_secret`, which
is only called by the ingestion runtime.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

import asyncpg

_ORPHAN_THRESHOLD_S = int(os.getenv("DATA_SOURCES_ORPHAN_THRESHOLD_S", "1800"))

# Columns safe to expose to the API (never the encrypted blob).
_CONNECTION_PUBLIC_COLS = """
    id::text AS id,
    organization_id::text AS organization_id,
    site_id::text AS site_id,
    provider_key,
    display_name,
    status,
    config,
    sync_interval_minutes,
    scheduled_sync_enabled,
    next_sync_at,
    last_run_at,
    last_status,
    created_by::text AS created_by,
    created_at,
    updated_at
"""

_JOB_COLS = """
    id::text AS id,
    connection_id::text AS connection_id,
    organization_id::text AS organization_id,
    site_id::text AS site_id,
    provider_key,
    trigger,
    status,
    progress,
    error_detail,
    import_batch_id::text AS import_batch_id,
    triggered_by::text AS triggered_by,
    created_at,
    started_at,
    finished_at,
    heartbeat_at
"""


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


async def insert_connection(
    pool: asyncpg.Pool,
    *,
    organization_id: str,
    site_id: str,
    provider_key: str,
    display_name: Optional[str],
    config: dict[str, Any],
    encrypted_credentials: bytes,
    encryption_version: int,
    sync_interval_minutes: int,
    scheduled_sync_enabled: bool,
    next_sync_at: Optional[datetime],
    created_by: Optional[str],
) -> asyncpg.Record:
    """Insert a connection row, returning the public columns.

    Raises:
        asyncpg.UniqueViolationError: If a non-disabled connection already
            exists for ``(site_id, provider_key)``.
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"""
            INSERT INTO data_source_connections (
                organization_id, site_id, provider_key, display_name,
                config, encrypted_credentials, encryption_version,
                sync_interval_minutes, scheduled_sync_enabled, next_sync_at,
                created_by
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::jsonb, $6, $7,
                $8, $9, $10,
                $11::uuid
            )
            RETURNING {_CONNECTION_PUBLIC_COLS}
            """,
            organization_id,
            site_id,
            provider_key,
            display_name,
            json.dumps(config),
            encrypted_credentials,
            encryption_version,
            sync_interval_minutes,
            scheduled_sync_enabled,
            next_sync_at,
            created_by,
        )


async def get_connection(
    pool: asyncpg.Pool,
    connection_id: str,
    *,
    organization_id: Optional[str] = None,
) -> Optional[asyncpg.Record]:
    """Fetch a connection's public columns, optionally scoped to an org."""
    async with pool.acquire() as conn:
        if organization_id is not None:
            return await conn.fetchrow(
                f"SELECT {_CONNECTION_PUBLIC_COLS} FROM data_source_connections "
                "WHERE id = $1::uuid AND organization_id = $2::uuid",
                connection_id,
                organization_id,
            )
        return await conn.fetchrow(
            f"SELECT {_CONNECTION_PUBLIC_COLS} FROM data_source_connections " "WHERE id = $1::uuid",
            connection_id,
        )


async def get_connection_secret(pool: asyncpg.Pool, connection_id: str) -> Optional[asyncpg.Record]:
    """Fetch the encrypted blob + config for the ingestion runtime only."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id::text AS id,
                   site_id::text AS site_id,
                   organization_id::text AS organization_id,
                   provider_key,
                   config,
                   encrypted_credentials,
                   encryption_version
            FROM data_source_connections
            WHERE id = $1::uuid
            """,
            connection_id,
        )


async def list_connections(
    pool: asyncpg.Pool,
    *,
    organization_id: str,
    site_id: Optional[str] = None,
) -> list[asyncpg.Record]:
    """List an org's connections (optionally filtered to one depot)."""
    async with pool.acquire() as conn:
        if site_id is not None:
            return await conn.fetch(
                f"SELECT {_CONNECTION_PUBLIC_COLS} FROM data_source_connections "
                "WHERE organization_id = $1::uuid AND site_id = $2::uuid "
                "AND status <> 'disabled' ORDER BY created_at DESC",
                organization_id,
                site_id,
            )
        return await conn.fetch(
            f"SELECT {_CONNECTION_PUBLIC_COLS} FROM data_source_connections "
            "WHERE organization_id = $1::uuid AND status <> 'disabled' "
            "ORDER BY created_at DESC",
            organization_id,
        )


async def update_connection(
    pool: asyncpg.Pool,
    connection_id: str,
    *,
    updates: dict[str, Any],
    encrypted_credentials: Optional[bytes] = None,
    encryption_version: Optional[int] = None,
) -> Optional[asyncpg.Record]:
    """Patch mutable fields; optionally rotate the encrypted credentials.

    ``updates`` keys are restricted to a known set by the caller.
    """
    sets: list[str] = ["updated_at = NOW()"]
    args: list[Any] = []
    idx = 1
    column_casts = {
        "display_name": "",
        "sync_interval_minutes": "",
        "scheduled_sync_enabled": "",
        "status": "",
    }
    for key, value in updates.items():
        if key not in column_casts:
            continue
        sets.append(f"{key} = ${idx}")
        args.append(value)
        idx += 1
    if encrypted_credentials is not None:
        sets.append(f"encrypted_credentials = ${idx}")
        args.append(encrypted_credentials)
        idx += 1
        sets.append(f"encryption_version = ${idx}")
        args.append(encryption_version)
        idx += 1
    args.append(connection_id)
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"UPDATE data_source_connections SET {', '.join(sets)} "
            f"WHERE id = ${idx}::uuid RETURNING {_CONNECTION_PUBLIC_COLS}",
            *args,
        )


async def disable_connection(pool: asyncpg.Pool, connection_id: str) -> bool:
    """Soft-delete a connection (status='disabled'); returns True if a row changed."""
    async with pool.acquire() as conn:
        result: str = await conn.execute(
            "UPDATE data_source_connections SET status = 'disabled', updated_at = NOW() "
            "WHERE id = $1::uuid AND status <> 'disabled'",
            connection_id,
        )
    return result.endswith(" 1")


async def set_next_sync_now_plus_interval(pool: asyncpg.Pool, connection_id: str) -> None:
    """Advance ``next_sync_at`` by the connection's interval from now."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE data_source_connections "
            "SET next_sync_at = NOW() + make_interval(mins => sync_interval_minutes) "
            "WHERE id = $1::uuid",
            connection_id,
        )


async def touch_connection_after_run(
    pool: asyncpg.Pool, connection_id: str, *, last_status: str
) -> None:
    """Record the terminal status of the most recent run on the connection."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE data_source_connections "
            "SET last_run_at = NOW(), last_status = $2, "
            "status = CASE WHEN status IN ('disabled', 'paused') THEN status "
            "             WHEN $2 = 'failed' THEN 'error' ELSE 'active' END, "
            "updated_at = NOW() "
            "WHERE id = $1::uuid",
            connection_id,
            last_status,
        )


async def find_due_connections(pool: asyncpg.Pool, *, limit: int) -> list[asyncpg.Record]:
    """Active, schedule-enabled connections due for a sync with no active job."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT {_CONNECTION_PUBLIC_COLS}
            FROM data_source_connections c
            WHERE c.scheduled_sync_enabled
              AND c.status = 'active'
              AND (c.next_sync_at IS NULL OR c.next_sync_at <= NOW())
              AND NOT EXISTS (
                  SELECT 1 FROM data_source_ingestion_jobs j
                  WHERE j.connection_id = c.id
                    AND j.status IN ('pending', 'running')
              )
            ORDER BY c.next_sync_at NULLS FIRST
            LIMIT $1
            """,
            limit,
        )


# --------------------------------------------------------------------------- #
# Ingestion jobs
# --------------------------------------------------------------------------- #


async def enqueue_job(
    pool: asyncpg.Pool,
    *,
    connection_id: str,
    organization_id: str,
    site_id: str,
    provider_key: str,
    trigger: str,
    triggered_by: Optional[str],
) -> asyncpg.Record:
    """Insert a pending job row.

    Raises:
        asyncpg.UniqueViolationError: If a pending/running job already exists
            for the connection (the overlap guard).
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"""
            INSERT INTO data_source_ingestion_jobs (
                connection_id, organization_id, site_id, provider_key,
                trigger, triggered_by
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid)
            RETURNING {_JOB_COLS}
            """,
            connection_id,
            organization_id,
            site_id,
            provider_key,
            trigger,
            triggered_by,
        )


async def claim_job(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    stale_threshold_seconds: int = _ORPHAN_THRESHOLD_S,
) -> Optional[asyncpg.Record]:
    """Single-winner start: pending jobs, or stale running jobs for recovery."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"""
            UPDATE data_source_ingestion_jobs
            SET status = 'running',
                started_at = COALESCE(started_at, NOW()),
                heartbeat_at = NOW()
            WHERE id = $1::uuid
              AND (
                status = 'pending'
                OR (
                  status = 'running'
                  AND COALESCE(heartbeat_at, started_at, created_at)
                      < NOW() - make_interval(secs => $2)
                )
              )
            RETURNING {_JOB_COLS}
            """,
            job_id,
            stale_threshold_seconds,
        )


async def merge_job_progress(pool: asyncpg.Pool, job_id: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into the job's progress JSONB and bump the heartbeat."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE data_source_ingestion_jobs "
            "SET progress = progress || $2::jsonb, heartbeat_at = NOW() "
            "WHERE id = $1::uuid",
            job_id,
            json.dumps(fields),
        )


async def finalize_job(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    status: str,
    progress: dict[str, Any],
    error_detail: Optional[str],
    import_batch_id: Optional[str],
) -> None:
    """Write the terminal state of a job."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE data_source_ingestion_jobs "
            "SET status = $2, progress = progress || $3::jsonb, "
            "error_detail = $4, import_batch_id = $5::uuid, "
            "finished_at = NOW(), heartbeat_at = NOW() "
            "WHERE id = $1::uuid",
            job_id,
            status,
            json.dumps(progress),
            error_detail,
            import_batch_id,
        )


async def get_job(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    organization_id: Optional[str] = None,
) -> Optional[asyncpg.Record]:
    """Fetch a job row, optionally scoped to an org."""
    async with pool.acquire() as conn:
        if organization_id is not None:
            return await conn.fetchrow(
                f"SELECT {_JOB_COLS} FROM data_source_ingestion_jobs "
                "WHERE id = $1::uuid AND organization_id = $2::uuid",
                job_id,
                organization_id,
            )
        return await conn.fetchrow(
            f"SELECT {_JOB_COLS} FROM data_source_ingestion_jobs WHERE id = $1::uuid",
            job_id,
        )


async def list_jobs(
    pool: asyncpg.Pool,
    connection_id: str,
    *,
    limit: int,
    before: Optional[datetime] = None,
) -> list[asyncpg.Record]:
    """List a connection's jobs newest-first, with an optional created_at cursor."""
    async with pool.acquire() as conn:
        if before is not None:
            return await conn.fetch(
                f"SELECT {_JOB_COLS} FROM data_source_ingestion_jobs "
                "WHERE connection_id = $1::uuid AND created_at < $2 "
                "ORDER BY created_at DESC LIMIT $3",
                connection_id,
                before,
                limit,
            )
        return await conn.fetch(
            f"SELECT {_JOB_COLS} FROM data_source_ingestion_jobs "
            "WHERE connection_id = $1::uuid ORDER BY created_at DESC LIMIT $2",
            connection_id,
            limit,
        )


async def find_orphaned_jobs(
    pool: asyncpg.Pool, *, threshold_seconds: int, limit: int
) -> list[asyncpg.Record]:
    """Pending/running jobs whose heartbeat is stale — candidates for recovery."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT {_JOB_COLS}
            FROM data_source_ingestion_jobs
            WHERE status IN ('pending', 'running')
              AND COALESCE(heartbeat_at, started_at, created_at)
                  < NOW() - make_interval(secs => $1)
            ORDER BY created_at ASC
            LIMIT $2
            """,
            threshold_seconds,
            limit,
        )
