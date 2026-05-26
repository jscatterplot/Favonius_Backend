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
_RECOVERY_PAGE_SIZE = int(os.getenv("DATA_SOURCES_RECOVERY_PAGE", "100"))


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
    """Re-kick non-terminal ingestion jobs during startup recovery.

    Run once at startup. Pages through every job with a keyset cursor so a
    large backlog is fully recovered, not just the first page — leftover
    non-terminal rows would otherwise keep blocking fresh enqueues via the
    one-active-job index. Resumes the existing row (the overlap unique index
    forbids a duplicate); ingestion idempotency makes resume safe.
    """
    total = 0
    after_created_at = None
    after_id = None
    while True:
        rows = await repo.find_orphaned_jobs(
            static_pool,
            limit=_RECOVERY_PAGE_SIZE,
            after_created_at=after_created_at,
            after_id=after_id,
        )
        if not rows:
            break
        for row in rows:
            logger.warning(
                "Recovering orphaned data-source job %s (status=%s, connection=%s)",
                row["id"],
                row["status"],
                row["connection_id"],
            )
            spawn(
                run_ingestion_job(
                    static_pool,
                    ts_pool,
                    job_id=row["id"],
                    allow_stale_running_claim=True,
                    expected_running_lease_at=(
                        row.get("heartbeat_at") or row.get("started_at") or row.get("created_at")
                    ),
                )
            )
        total += len(rows)
        if len(rows) < _RECOVERY_PAGE_SIZE:
            break
        after_created_at = rows[-1]["created_at"]
        after_id = rows[-1]["id"]
    if total:
        logger.info("Re-kicked %d orphaned data-source job(s)", total)
    return total


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
            # Spawn the just-inserted job before the non-critical reschedule write so
            # a transient failure there can't strand it 'pending' and block the
            # connection behind the one-active-job index.
            spawn(run_ingestion_job(static_pool, ts_pool, job_id=job["id"]))
            await repo.set_next_sync_now_plus_interval(static_pool, connection_id)
            logger.info(
                "Scheduled sync enqueued for connection %s (job %s)",
                connection_id,
                job["id"],
            )
        except Exception:  # noqa: BLE001 — one bad connection must not abort the batch.
            logger.exception("Scheduler failed to enqueue connection %s; skipping", connection_id)
