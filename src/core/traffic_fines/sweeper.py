"""Periodic deadline re-check for traffic fines.

A fine uploaded well before its deadline is ``not_yet`` at upload and raises no
alert. This loop re-evaluates parsed fines whose deadline has since entered the
alert window and raises the alert — deterministically, with NO LLM call (it
reuses the stored extraction). Idempotent: the ``notification_alerts`` dedup_key
collapses repeat raises, and the row flips to ``alerted`` so it leaves the
candidate set. Wired into the FastAPI lifespan, gated by the feature flag.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import ValidationError

from src.core.traffic_fines import config, repository
from src.core.traffic_fines.alerting import raise_fine_alert
from src.core.traffic_fines.evaluator import evaluate_early_payment
from src.core.traffic_fines.models import TrafficFineExtraction

logger = logging.getLogger(__name__)


async def run_one_sweep(
    ts_pool: Any,
    static_pool: Any,
    *,
    now: Optional[datetime] = None,
    window_hours: Optional[float] = None,
) -> int:
    """Re-evaluate due fines once; return the number of alerts raised.

    Pure of the LLM — uses each fine's stored extraction. Returns the count so
    tests and metrics can assert progress.
    """
    now = now or datetime.now(timezone.utc)
    window = window_hours if window_hours is not None else config.alert_window_hours()
    candidates = await repository.sweep_candidates(ts_pool, window_hours=window)

    raised = 0
    tz_cache: dict[UUID, Optional[str]] = {}
    for row in candidates:
        fine_id: UUID = row["id"]
        try:
            extraction = TrafficFineExtraction.model_validate(
                repository.coerce_jsonb(row["extraction"])
            )
        except ValidationError:
            logger.warning("sweep: fine %s has unparseable extraction; skipping", fine_id)
            continue

        depot_id: UUID = row["depot_id"]
        if depot_id not in tz_cache:
            tz_cache[depot_id] = await repository.get_depot_timezone(static_pool, depot_id)

        ev = evaluate_early_payment(
            extraction,
            now=now,
            depot_tz=tz_cache[depot_id],
            fallback_fine_id=str(fine_id)[:8],
            window_hours=window,
        )
        if not ev.within_window or not ev.message:
            continue

        try:
            async with ts_pool.acquire() as conn:
                async with conn.transaction():
                    alert_id = await raise_fine_alert(
                        conn,
                        organization_id=row["organization_id"],
                        depot_id=depot_id,
                        fine_id=fine_id,
                        evaluation=ev,
                        extraction=extraction,
                    )
                    await repository.mark_alerted(conn, fine_id, alert_id=alert_id, evaluation=ev)
            raised += 1
        except Exception:  # noqa: BLE001 — isolate one fine's failure from the batch
            logger.exception("sweep: failed to raise alert for fine %s", fine_id)

    if raised:
        logger.info("traffic-fine sweep raised %d alert(s)", raised)
    return raised


async def run_traffic_fine_deadline_sweep(
    ts_pool: Any,
    static_pool: Any,
    *,
    interval_s: Optional[int] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> None:
    """Background loop: re-check fine deadlines every ``interval_s`` seconds."""
    interval = interval_s if interval_s is not None else config.sweep_interval_s()
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    logger.info("traffic-fine deadline sweep started (interval=%ss)", interval)
    while True:
        try:
            await run_one_sweep(ts_pool, static_pool, now=clock())
        except asyncio.CancelledError:
            logger.info("traffic-fine deadline sweep cancelled")
            raise
        except Exception:  # noqa: BLE001 — never let one iteration kill the loop
            logger.exception("traffic-fine deadline sweep iteration failed")
        await asyncio.sleep(interval)
