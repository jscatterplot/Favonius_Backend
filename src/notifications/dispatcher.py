"""Alert dispatcher.

Single-worker loop that claims pending notification_alerts rows, fans them
out to matching recipients, renders email templates, and pushes via the
EmailDeliveryClient. Subscribes to the 'notification_alerts_new' pg_notify
channel for sub-second reaction; falls back to a 30s poll for safety
(see decision 4.4 in docs/plans/alerts-pipeline.md).

Worker safety (decision 4.1): in-memory `_currently_sending` set prevents
the same alert from being processed twice within one dispatcher. A startup
check warns if WEB_CONCURRENCY > 1 — multi-worker safety would require
SELECT FOR UPDATE SKIP LOCKED, not implemented yet.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional
from uuid import UUID

from . import alerts as alerts_repo
from . import recipients as recipients_repo
from .alerts import Alert
from .email_client import EmailDeliveryClient
from .renderer import render_alert

logger = logging.getLogger(__name__)


_NOTIFY_CHANNEL = "notification_alerts_new"


class AlertDispatcher:
    """Drains notification_alerts → email."""

    def __init__(
        self,
        *,
        pool: Any,
        email_client: EmailDeliveryClient,
        default_from: str,
        poll_interval_s: float = 30.0,
        resend_interval_s: int = 3600,
        batch_size: int = 50,
    ) -> None:
        self._pool = pool
        self._email_client = email_client
        self._default_from = default_from
        self._poll_interval_s = poll_interval_s
        self._resend_interval_s = resend_interval_s
        self._batch_size = batch_size

        self._currently_sending: set[UUID] = set()
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._main_task: Optional[asyncio.Task] = None
        self._listener_conn: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background loop and the LISTEN connection."""
        self._check_single_worker()
        if not await self._preflight_schema():
            # Migration 022 hasn't run on this database. Without notification_alerts
            # the dispatcher would log a 'relation does not exist' UndefinedTableError
            # every poll interval forever. Skip startup loudly so the operator knows
            # to run scripts/run_migrations.py — but don't crash the whole WS handler.
            logger.error(
                "alerts: dispatcher disabled — required schema is missing. "
                "Run `python scripts/run_migrations.py` so migration 022 "
                "(alerts pipeline) is applied, then restart this service."
            )
            return
        try:
            await self._start_listener()
        except Exception as exc:
            # NOTIFY is a fast-path optimization. If LISTEN fails (e.g. PG
            # not supporting it, transient connection issue), fall back to
            # polling-only mode rather than failing startup.
            logger.warning("alerts: LISTEN setup failed, polling-only: %s", exc)
        self._main_task = asyncio.create_task(self._run(), name="alert-dispatcher")
        logger.info(
            "alerts: dispatcher started (poll=%.1fs, resend=%ds, batch=%d)",
            self._poll_interval_s,
            self._resend_interval_s,
            self._batch_size,
        )

    async def _preflight_schema(self) -> bool:
        """Return True iff the alerts pipeline tables exist.

        Probes ``notification_alerts`` via ``to_regclass`` so a missing
        migration produces a clean boolean rather than a per-tick crash.
        Treats an unexpected pool error as 'present' so a transient DB
        glitch at startup doesn't permanently disable the dispatcher.
        """
        try:
            async with self._pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT to_regclass('public.notification_alerts') IS NOT NULL"
                )
        except Exception:
            logger.exception(
                "alerts: schema preflight failed; assuming schema present "
                "and starting dispatcher anyway"
            )
            return True
        return bool(exists)

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._main_task is not None:
            try:
                await asyncio.wait_for(self._main_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._main_task.cancel()
        if self._listener_conn is not None:
            try:
                await self._listener_conn.remove_listener(_NOTIFY_CHANNEL, self._on_notify)
            except Exception:
                logger.debug("alerts: remove_listener failed (already torn down)", exc_info=True)
            try:
                await self._pool.release(self._listener_conn)
            except Exception:
                logger.debug("alerts: pool.release failed", exc_info=True)
        logger.info("alerts: dispatcher stopped")

    @staticmethod
    def _check_single_worker() -> None:
        """Warn loudly if running with WEB_CONCURRENCY > 1.

        Decision 4.1: in-memory dedup set is single-worker-only. Multiple
        workers would double-send. This is the trip-wire for revisiting the
        choice — see docs/plans/alerts-pipeline.md.
        """
        try:
            wc = int(os.getenv("WEB_CONCURRENCY", "1"))
        except ValueError:
            wc = 1
        if wc > 1:
            logger.critical(
                "alerts: WEB_CONCURRENCY=%d but the dispatcher relies on a "
                "single-worker assumption; expect duplicate notifications. "
                "See docs/plans/alerts-pipeline.md decision 4.1.",
                wc,
            )

    # ------------------------------------------------------------------
    # NOTIFY listener
    # ------------------------------------------------------------------

    async def _start_listener(self) -> None:
        self._listener_conn = await self._pool.acquire()
        await self._listener_conn.add_listener(_NOTIFY_CHANNEL, self._on_notify)

    def _on_notify(self, *_args: Any) -> None:
        # asyncpg calls this in the event loop; no await needed. Just wake
        # the main loop so it runs another tick immediately.
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("alerts: dispatcher tick failed")

            # Wait either for a NOTIFY (wake_event) or the poll interval —
            # whichever fires first. Reconciliation poll is the safety net
            # against missed NOTIFYs (decision 4.4).
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._poll_interval_s
                )
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()

    # ------------------------------------------------------------------
    # Tick: claim → process → record
    # ------------------------------------------------------------------

    async def _tick(self) -> int:
        """Run one dispatcher cycle. Returns the number of alerts processed."""
        async with self._pool.acquire() as conn:
            alerts = await alerts_repo.claim_pending_alerts(
                conn,
                resend_interval_s=self._resend_interval_s,
                limit=self._batch_size,
            )

        pending = [a for a in alerts if a.id not in self._currently_sending]
        if not pending:
            return 0

        results = await asyncio.gather(
            *(self._process_alert(a) for a in pending),
            return_exceptions=True,
        )
        for alert, result in zip(pending, results):
            if isinstance(result, Exception):
                logger.exception(
                    "alerts: process_alert failed for %s", alert.id, exc_info=result
                )
        return len(pending)

    async def _process_alert(self, alert: Alert) -> None:
        if alert.id in self._currently_sending:
            return  # racing tick; another in-flight processor owns it
        self._currently_sending.add(alert.id)
        try:
            await self._send_and_record(alert)
        finally:
            self._currently_sending.discard(alert.id)

    async def _send_and_record(self, alert: Alert) -> None:
        async with self._pool.acquire() as conn:
            recipients = await recipients_repo.list_for_alert(
                conn,
                organization_id=alert.organization_id,
                alert_type=alert.alert_type,
                severity=alert.severity,
            )

            if not recipients:
                # Bump anyway so the alert isn't re-claimed every tick. The
                # operator might see it in the alerts UI but doesn't get an
                # email until they configure recipients.
                await alerts_repo.mark_notified(conn, alert.id)
                logger.info(
                    "alerts: no recipients for org=%s type=%s severity=%s",
                    alert.organization_id,
                    alert.alert_type,
                    alert.severity.value,
                )
                return

            # Bump BEFORE sending (decision 4.1 / risk-tradeoff): a crash
            # mid-tick means failed recipients wait one resend interval
            # rather than getting duplicate emails on every retry.
            new_count = await alerts_repo.mark_notified(conn, alert.id)

        # Render once; reuse for every recipient.
        for recipient in recipients:
            try:
                message = render_alert(
                    alert,
                    recipient_email=recipient.email,
                    from_address=self._default_from,
                )
                result = await self._email_client.send(message)
            except Exception as exc:  # render or transport beyond client's retry
                logger.exception(
                    "alerts: send failed for alert=%s recipient=%s",
                    alert.id,
                    recipient.id,
                )
                async with self._pool.acquire() as conn:
                    await alerts_repo.record_delivery(
                        conn,
                        alert_id=alert.id,
                        recipient_id=recipient.id,
                        notified_count=new_count,
                        provider_message_id=None,
                        status="failed",
                        status_detail={"error": "exception", "message": str(exc)},
                    )
                continue

            async with self._pool.acquire() as conn:
                await alerts_repo.record_delivery(
                    conn,
                    alert_id=alert.id,
                    recipient_id=recipient.id,
                    notified_count=new_count,
                    provider_message_id=result.provider_message_id,
                    status=result.status,
                    status_detail=result.detail,
                )


__all__ = ["AlertDispatcher"]
