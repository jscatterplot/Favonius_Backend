"""Scheduled-sync loop and startup recovery for data-source ingestion.

Both run inside the FastAPI lifespan (single web worker assumed, like the
monthly report scheduler and the alerts dispatcher). The DB-level overlap guard
(``uq_dsij_one_active_per_conn``) means duplicate jobs are impossible even under
WEB_CONCURRENCY > 1, so the worst case of a multi-worker deploy is redundant
SELECTs, not double-ingestion.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

import asyncpg

from . import repository as repo
from .ingestion import run_ingestion_job

logger = logging.getLogger(__name__)

Spawn = Callable[[Awaitable[Any]], None]

_SCHEDULER_INTERVAL_S = int(os.getenv("DATA_SOURCES_SCHEDULER_INTERVAL_S", "300"))
_ORPHAN_THRESHOLD_S = int(os.getenv("DATA_SOURCES_ORPHAN_THRESHOLD_S", "1800"))
_BATCH_LIMIT = int(os.getenv("DATA_SOURCES_SCHEDULER_BATCH", "50"))


def check_single_worker() -> None:
    """Warn loudly if running multiple web workers (parity with the dispatcher)."""
    try:
        concurrency = int(os.getenv("WEB_CONCURRENCY", "1"))
    except ValueError:
        concurrency = 1
    if concurrency > 1:
        logger.critical(
            "WEB_CONCURRENCY=%s > 1: the data-source scheduler runs per worker. "
            "Duplicate jobs are still prevented by the DB overlap guard, but "
            "redundant scheduler ticks will run. Prefer a single web worker.",
            concurrency,
        )


async def recover_orphaned_data_source_jobs(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    *,
    spawn: Spawn,
) -> int:
    """Re-kick pending/running jobs whose heartbeat is stale. Returns the count.

    Run once at startup. Resumes the existing row (the overlap unique index
    forbids a duplicate); ingestion idempotency makes resume safe.
    """
    rows = await repo.find_orphaned_jobs(
        static_pool, threshold_seconds=_ORPHAN_THRESHOLD_S, limit=100
    )
    for row in rows:
        logger.warning(
            "Recovering orphaned data-source job %s (status=%s, connection=%s)",
            row["id"],
            row["status"],
            row["connection_id"],
        )
        spawn(run_ingestion_job(static_pool, ts_pool, job_id=row["id"]))
    if rows:
        logger.info("Re-kicked %d orphaned data-source job(s)", len(rows))
    return len(rows)


async def run_data_source_scheduler(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    *,
    spawn: Spawn,
) -> None:
    """Periodically enqueue + kick syncs for connections that are due."""
    logger.info("Data-source scheduler loop started (interval=%ss)", _SCHEDULER_INTERVAL_S)
    while True:
        try:
            await _tick(static_pool, ts_pool, spawn=spawn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die.
            logger.exception("Data-source scheduler tick failed")
        await asyncio.sleep(_SCHEDULER_INTERVAL_S)


async def _tick(static_pool: asyncpg.Pool, ts_pool: asyncpg.Pool, *, spawn: Spawn) -> None:
    due = await repo.find_due_connections(static_pool, limit=_BATCH_LIMIT)
    for connection in due:
        connection_id = connection["id"]
        try:
            job = await repo.enqueue_job(
                static_pool,
                connection_id=connection_id,
                organization_id=connection["organization_id"],
                site_id=connection["site_id"],
                provider_key=connection["provider_key"],
                trigger="scheduled",
                triggered_by=None,
            )
        except asyncpg.UniqueViolationError:
            # A manual sync won the race for this connection; just advance.
            await repo.set_next_sync_now_plus_interval(static_pool, connection_id)
            continue
        await repo.set_next_sync_now_plus_interval(static_pool, connection_id)
        spawn(run_ingestion_job(static_pool, ts_pool, job_id=job["id"]))
        logger.info(
            "Scheduled sync enqueued for connection %s (job %s)",
            connection_id,
            job["id"],
        )
