"""Security audit logging for NKSC compliance.

Provides tamper-evident audit logging to the security_audit_log
TimescaleDB hypertable. Used by both the Main API and WebSocket Handler.

Features:
- Async batch writer (buffer → flush every N seconds or M events)
- Tamper-evident sequential numbering via PostgreSQL sequence
- Structured event types for auditor queries
- Shared by both services (both write to same TimescaleDB)

Event types:
    AUTH_SUCCESS, AUTH_FAILURE, GEO_BLOCK, RATE_LIMIT,
    ACCESS_DENIED, CONFIG_CHANGE, CONNECTION_REJECT,
    STATION_LOCKOUT, INCIDENT_DETECT
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    """A security audit event to be logged.

    Attributes:
        event_type: Category of the event (e.g. AUTH_FAILURE, GEO_BLOCK).
        source_ip: IP address of the request origin.
        user_id: Authenticated user ID (if available).
        station_id: OCPP station ID (for WebSocket Handler events).
        resource: The resource being accessed (e.g. endpoint path, station ID).
        country_code: Resolved country code (for geo-blocking events).
        details: Additional structured data about the event.
        service: Which service generated this event (main_api or websocket_handler).
        timestamp: When the event occurred (defaults to now).
    """

    event_type: str
    source_ip: Optional[str] = None
    user_id: Optional[str] = None
    station_id: Optional[str] = None
    resource: Optional[str] = None
    country_code: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    service: str = "unknown"
    timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        """Set timestamp if not provided."""
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


class AuditLogger:
    """Async batch writer for security audit events.

    Buffers events in an asyncio.Queue and flushes to the database
    periodically or when the buffer reaches a threshold. This avoids
    adding latency to the request path.

    The tamper-evident sequential numbering is handled by the PostgreSQL
    sequence (security_audit_seq) — no application-level sequencing needed.

    Usage:
        audit_logger = AuditLogger(db_pool=pool, service="main_api")
        await audit_logger.start()
        await audit_logger.log(AuditEvent(event_type="GEO_BLOCK", ...))
        # ... on shutdown:
        await audit_logger.stop()
    """

    def __init__(
        self,
        db_pool: Any = None,
        service: str = "unknown",
        flush_interval_seconds: float = 5.0,
        flush_threshold: int = 100,
    ) -> None:
        self._pool = db_pool
        self._service = service
        self._flush_interval = flush_interval_seconds
        self._flush_threshold = flush_threshold
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue()
        self._buffer: list[AuditEvent] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the background flush task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info("Audit logger started (service=%s)", self._service)

    async def stop(self) -> None:
        """Stop the flush task and flush remaining events."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final flush of any remaining events
        await self._drain_queue()
        if self._buffer:
            await self._flush_to_db()
        logger.info("Audit logger stopped (service=%s)", self._service)

    async def log(self, event: AuditEvent) -> None:
        """Queue an audit event for writing.

        This method is non-blocking and safe to call from the request path.
        """
        event.service = self._service
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "Audit log queue full, dropping event: %s", event.event_type
            )

    async def log_event(
        self,
        event_type: str,
        source_ip: Optional[str] = None,
        user_id: Optional[str] = None,
        station_id: Optional[str] = None,
        resource: Optional[str] = None,
        country_code: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        """Convenience method to log an event without creating AuditEvent manually."""
        await self.log(
            AuditEvent(
                event_type=event_type,
                source_ip=source_ip,
                user_id=user_id,
                station_id=station_id,
                resource=resource,
                country_code=country_code,
                details=details or {},
            )
        )

    async def _flush_loop(self) -> None:
        """Background loop that periodically flushes buffered events."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._drain_queue()
                if self._buffer:
                    await self._flush_to_db()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Audit flush error: %s", e, exc_info=True)

    async def _drain_queue(self) -> None:
        """Move all queued events into the buffer."""
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                self._buffer.append(event)
            except asyncio.QueueEmpty:
                break

        # Also flush if buffer exceeds threshold
        if len(self._buffer) >= self._flush_threshold:
            await self._flush_to_db()

    async def _flush_to_db(self) -> None:
        """Write buffered events to the database in a batch."""
        if not self._buffer:
            return

        if self._pool is None:
            logger.warning(
                "Audit logger has no DB pool — dropping %d events", len(self._buffer)
            )
            self._buffer.clear()
            return

        events = self._buffer.copy()
        self._buffer.clear()

        try:
            async with self._pool.acquire() as conn:
                # Use executemany for batch insert efficiency
                await conn.executemany(
                    """
                    INSERT INTO security_audit_log
                        (timestamp, event_type, source_ip, user_id, station_id,
                         resource, country_code, details, service)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                    """,
                    [
                        (
                            event.timestamp,
                            event.event_type,
                            event.source_ip,
                            event.user_id,
                            event.station_id,
                            event.resource,
                            event.country_code,
                            _serialize_details(event.details),
                            event.service,
                        )
                        for event in events
                    ],
                )
                logger.debug("Flushed %d audit events to database", len(events))
        except Exception as e:
            logger.error(
                "Failed to flush %d audit events: %s — events lost",
                len(events),
                e,
            )


def _serialize_details(details: dict) -> str:
    """Serialize event details dict to JSON string for JSONB column."""
    import json

    return json.dumps(details, default=str)


# ============ Module-level Singleton ============

_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> Optional[AuditLogger]:
    """Get the module-level audit logger instance.

    Returns None if not initialized (e.g. during tests or before startup).
    """
    return _audit_logger


def set_audit_logger(logger_instance: AuditLogger) -> None:
    """Set the module-level audit logger instance.

    Called during application startup after the DB pool is available.
    """
    global _audit_logger
    _audit_logger = logger_instance


async def audit_log_event(
    event_type: str,
    source_ip: Optional[str] = None,
    user_id: Optional[str] = None,
    station_id: Optional[str] = None,
    resource: Optional[str] = None,
    country_code: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Convenience function to log an audit event.

    Safe to call even if the audit logger is not initialized (no-op).
    """
    audit = get_audit_logger()
    if audit is not None:
        await audit.log_event(
            event_type=event_type,
            source_ip=source_ip,
            user_id=user_id,
            station_id=station_id,
            resource=resource,
            country_code=country_code,
            details=details,
        )
