"""WorkflowScheduler — runs depot-agent workflows on a time schedule.

Per PRD §6.1 the daily readiness check fires ``lead_time_min`` minutes
before each depot's earliest scheduled departure. The scheduler:

1. Wakes on a polling cadence (default 60 s).
2. For each depot, computes ``earliest_departure_in_window`` from the
   ``schedules`` table over the lookahead horizon.
3. If ``earliest_departure - lead_time_min ≤ now`` and the depot has
   no fresh Decision row inside ``lead_time_min``, executes the
   readiness workflow via :func:`execute_readiness_workflow`.
4. Persists the resulting Decision row and records the
   scheduler-to-completion latency.

The router's idempotency cache and the scheduler's
"already-ran-this-window" check both consult the same
``workflow_decisions`` table, so a manual ``POST /today/{depot_id}``
inside the lead-time window suppresses the scheduled run and vice
versa.

Lifecycle parity with :class:`ControllerManager` — owned by the
FastAPI lifespan and stopped before the DB pool closes. Errors per
depot are caught and logged; a failure on depot A never blocks
depot B.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from .workflows import WORKFLOW_NAME as READINESS_WORKFLOW_NAME
from .workflows.orchestrator import execute_readiness_workflow, persist_decision
from .workflows.tools import DatabaseToolBundle
from ..db.pools import DatabasePools
from ..monitoring.metrics import (
    AGENT_WORKFLOW_RUNS,
    AGENT_WORKFLOW_SCHEDULER_LATENCY,
)

logger = logging.getLogger(__name__)


@dataclass
class WorkflowSchedulerConfig:
    """Tunables for the scheduler."""

    # How often the scheduler wakes to check pending depots (seconds).
    poll_interval_seconds: int = 60
    # How far ahead of "now" to look for the earliest departure.
    departure_lookahead_hours: int = 24
    # Lead time in minutes before the earliest departure when the
    # readiness check fires.
    lead_time_minutes: int = 60
    # Minimum window for the readiness check itself (after the
    # earliest departure). 4 hours covers a typical morning shift.
    window_hours: int = 4
    # Per-depot deduplication: a Decision row written inside this
    # many seconds is treated as "already ran this window".
    dedupe_window_seconds: int = 30 * 60

    @classmethod
    def from_env(cls) -> "WorkflowSchedulerConfig":
        return cls(
            poll_interval_seconds=int(
                os.environ.get("DEPOT_AGENT_SCHEDULER_POLL_INTERVAL_S", "60")
            ),
            departure_lookahead_hours=int(
                os.environ.get("DEPOT_AGENT_SCHEDULER_LOOKAHEAD_H", "24")
            ),
            lead_time_minutes=int(os.environ.get("DEPOT_AGENT_SCHEDULER_LEAD_TIME_MIN", "60")),
            window_hours=int(os.environ.get("DEPOT_AGENT_SCHEDULER_WINDOW_H", "4")),
            dedupe_window_seconds=int(
                os.environ.get("DEPOT_AGENT_SCHEDULER_DEDUPE_S", "1800")
            ),
        )


class WorkflowScheduler:
    """Per-depot daily-readiness-check scheduler.

    Owns one ``asyncio.Task`` polling on the configured cadence. The
    task is started via :meth:`start` and stopped via :meth:`stop`;
    the FastAPI lifespan wires these into application startup and
    shutdown so the scheduler stays in step with the rest of the app.
    """

    def __init__(
        self,
        pools: DatabasePools,
        *,
        config: Optional[WorkflowSchedulerConfig] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.pools = pools
        self.config = config or WorkflowSchedulerConfig.from_env()
        # `clock` is a zero-arg callable returning a timezone-aware
        # datetime; tests inject a fixed clock so the scheduler runs
        # deterministically.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the polling loop. Idempotent."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever())
        logger.info(
            "WorkflowScheduler started (poll=%ss lead_time=%smin window=%sh)",
            self.config.poll_interval_seconds,
            self.config.lead_time_minutes,
            self.config.window_hours,
        )

    async def stop(self) -> None:
        """Stop the polling loop and await the task. Idempotent."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                logger.exception("WorkflowScheduler stop encountered an error")
            self._task = None
        logger.info("WorkflowScheduler stopped")

    # ── Polling loop ────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("WorkflowScheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.poll_interval_seconds,
                )
                # If the event fires we exit; otherwise we hit timeout
                # and loop again.
                if self._stop_event.is_set():
                    return
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> int:
        """Single pass: returns the number of depots that ran this tick.

        Public so unit tests can drive the scheduler one step at a
        time without spawning the background task.
        """
        depots = await self._list_depots()
        ran = 0
        for depot in depots:
            depot_id = UUID(str(depot["depot_id"]))
            try:
                fired = await self._maybe_run_for_depot(depot_id, tz=str(depot.get("timezone") or "UTC"))
                if fired:
                    ran += 1
            except Exception:
                logger.exception("scheduler failed for depot %s", depot_id)
        return ran

    # ── Per-depot logic ─────────────────────────────────────────────────

    async def _maybe_run_for_depot(self, depot_id: UUID, *, tz: str) -> bool:
        """Run the readiness workflow if its trigger condition holds.

        Trigger: ``earliest_departure_in_window - lead_time_min ≤ now``
        AND no Decision row exists for this depot in the last
        ``dedupe_window_seconds``.
        """
        now_utc = self._clock()
        earliest = await self._earliest_departure_in_window(
            depot_id,
            now_utc=now_utc,
            lookahead=timedelta(hours=self.config.departure_lookahead_hours),
        )
        if earliest is None:
            return False

        trigger_at = earliest - timedelta(minutes=self.config.lead_time_minutes)
        if trigger_at > now_utc:
            return False

        if await self._has_fresh_decision(depot_id, now_utc=now_utc):
            return False

        organization_id = await self._fetch_org_id(depot_id)

        start_ts = time.monotonic()
        window_start_utc = earliest
        window_end_utc = earliest + timedelta(hours=self.config.window_hours)
        tools = DatabaseToolBundle(pools=self.pools)
        decision = await execute_readiness_workflow(
            depot_id=depot_id,
            organization_id=organization_id,
            triggered_by="scheduler",
            triggered_by_user_id=None,
            tools=tools,
            depot_timezone=tz,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
        )
        try:
            await persist_decision(self.pools.ts, decision)
        except Exception:
            logger.exception("scheduler failed to persist decision for depot %s", depot_id)

        AGENT_WORKFLOW_RUNS.labels(
            workflow_name=decision.workflow_name,
            triggered_by="scheduler",
            status=decision.status,
        ).inc()
        # Scheduler latency is measured from the *planned trigger*
        # (lead_time before departure) — not from the start of the
        # tick — so it captures any drift introduced by busy event
        # loops or DB latency.
        drift = (now_utc - trigger_at).total_seconds() + (time.monotonic() - start_ts)
        AGENT_WORKFLOW_SCHEDULER_LATENCY.labels(
            workflow_name=decision.workflow_name
        ).observe(max(0.0, drift))
        logger.info(
            "scheduler ran daily_readiness_check for depot=%s exceptions=%d",
            depot_id,
            len(decision.output.exceptions),
        )
        return True

    # ── DB helpers ─────────────────────────────────────────────────────

    async def _list_depots(self) -> list[dict[str, Any]]:
        async with self.pools.static.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id::text AS depot_id, timezone FROM sites ORDER BY created_at"
            )
        return [dict(r) for r in rows]

    async def _fetch_org_id(self, depot_id: UUID) -> Optional[UUID]:
        async with self.pools.static.acquire() as conn:
            org = await conn.fetchval(
                "SELECT organization_id::text FROM sites WHERE id = $1::uuid",
                str(depot_id),
            )
        return UUID(org) if org else None

    async def _earliest_departure_in_window(
        self,
        depot_id: UUID,
        *,
        now_utc: datetime,
        lookahead: timedelta,
    ) -> Optional[datetime]:
        end = now_utc + lookahead
        async with self.pools.static.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT MIN(s.departure_time) AS earliest
                FROM schedules s
                JOIN vehicles v ON v.id = s.vehicle_id
                WHERE v.site_id = $1::uuid
                  AND s.departure_time >= $2
                  AND s.departure_time < $3
                """,
                str(depot_id),
                now_utc,
                end,
            )
        earliest = row["earliest"] if row else None
        if earliest is None:
            return None
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        return earliest

    async def _has_fresh_decision(self, depot_id: UUID, *, now_utc: datetime) -> bool:
        async with self.pools.ts.acquire() as conn:
            cnt = await conn.fetchval(
                """
                SELECT 1
                FROM workflow_decisions
                WHERE workflow_name = $1
                  AND depot_id = $2::uuid
                  AND created_at > $3
                LIMIT 1
                """,
                READINESS_WORKFLOW_NAME,
                str(depot_id),
                now_utc - timedelta(seconds=self.config.dedupe_window_seconds),
            )
        return cnt is not None
