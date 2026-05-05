"""Tests for security audit logging.

Tests cover:
- AuditEvent creation and defaults
- AuditLogger queuing and flushing
- Batch write behavior
- Service name tagging
- Graceful handling of missing DB pool
- Module-level singleton management
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.security.audit_log import (
    AuditEvent,
    AuditLogger,
    _serialize_details,
    audit_log_event,
    get_audit_logger,
    set_audit_logger,
)


# ============ Tests: AuditEvent ============


class TestAuditEvent:
    """Test AuditEvent dataclass."""

    def test_creates_with_defaults(self):
        """Event has timestamp and empty details by default."""
        event = AuditEvent(event_type="AUTH_FAILURE")
        assert event.event_type == "AUTH_FAILURE"
        assert event.timestamp is not None
        assert isinstance(event.timestamp, datetime)
        assert event.details == {}
        assert event.service == "unknown"

    def test_creates_with_all_fields(self):
        """Event accepts all fields."""
        ts = datetime(2026, 3, 30, tzinfo=timezone.utc)
        event = AuditEvent(
            event_type="GEO_BLOCK",
            source_ip="93.184.216.34",
            user_id=None,
            station_id=None,
            resource="/optimize",
            country_code="RU",
            details={"reason": "blocked_country"},
            service="main_api",
            timestamp=ts,
        )
        assert event.event_type == "GEO_BLOCK"
        assert event.source_ip == "93.184.216.34"
        assert event.country_code == "RU"
        assert event.details["reason"] == "blocked_country"
        assert event.timestamp == ts

    def test_auto_timestamps_in_utc(self):
        """Auto-generated timestamps are UTC."""
        event = AuditEvent(event_type="TEST")
        assert event.timestamp.tzinfo is not None


# ============ Tests: AuditLogger ============


class TestAuditLogger:
    """Test AuditLogger batch writer."""

    @pytest.mark.asyncio
    async def test_log_event_queued(self):
        """Events are queued without blocking."""
        logger = AuditLogger(service="test")
        event = AuditEvent(event_type="TEST")
        await logger.log(event)
        assert logger._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_service_name_applied(self):
        """Service name is set on logged events."""
        logger = AuditLogger(service="main_api")
        event = AuditEvent(event_type="TEST")
        await logger.log(event)
        # Drain and check
        await logger._drain_queue()
        assert logger._buffer[0].service == "main_api"

    @pytest.mark.asyncio
    async def test_drain_moves_queue_to_buffer(self):
        """Drain moves all queued events to the buffer."""
        logger = AuditLogger(service="test")
        for i in range(5):
            await logger.log(AuditEvent(event_type=f"TEST_{i}"))
        assert logger._queue.qsize() == 5
        assert len(logger._buffer) == 0

        await logger._drain_queue()
        assert logger._queue.qsize() == 0
        assert len(logger._buffer) == 5

    @pytest.mark.asyncio
    async def test_flush_to_db_with_pool(self):
        """Flush writes events to the database."""
        mock_conn = AsyncMock()

        # Create a proper async context manager for pool.acquire()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        logger = AuditLogger(db_pool=mock_pool, service="test")
        logger._buffer = [
            AuditEvent(event_type="AUTH_FAILURE", source_ip="1.2.3.4"),
            AuditEvent(event_type="GEO_BLOCK", source_ip="5.6.7.8"),
        ]

        await logger._flush_to_db()

        mock_conn.executemany.assert_called_once()
        call_args = mock_conn.executemany.call_args
        assert "INSERT INTO security_audit_log" in call_args[0][0]
        assert len(call_args[0][1]) == 2  # 2 events
        assert len(logger._buffer) == 0  # Buffer cleared

    @pytest.mark.asyncio
    async def test_flush_without_pool_clears_buffer(self):
        """Flush without DB pool drops events gracefully."""
        logger = AuditLogger(db_pool=None, service="test")
        logger._buffer = [AuditEvent(event_type="TEST")]

        await logger._flush_to_db()
        assert len(logger._buffer) == 0  # Cleared, not stuck

    @pytest.mark.asyncio
    async def test_flush_on_threshold(self):
        """Buffer auto-flushes when threshold is reached."""
        mock_conn = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        logger = AuditLogger(db_pool=mock_pool, service="test", flush_threshold=3)

        # Queue 3 events (at threshold)
        for i in range(3):
            await logger.log(AuditEvent(event_type=f"TEST_{i}"))

        # Drain triggers flush because buffer >= threshold
        await logger._drain_queue()

        # Buffer should have been flushed
        mock_conn.executemany.assert_called_once()
        assert len(logger._buffer) == 0

    @pytest.mark.asyncio
    async def test_log_event_convenience_method(self):
        """Convenience method creates and queues event."""
        logger = AuditLogger(service="test")
        await logger.log_event(
            event_type="RATE_LIMIT",
            source_ip="8.8.8.8",
            resource="/optimize",
            details={"limit": 10},
        )
        assert logger._queue.qsize() == 1
        await logger._drain_queue()
        event = logger._buffer[0]
        assert event.event_type == "RATE_LIMIT"
        assert event.source_ip == "8.8.8.8"
        assert event.details["limit"] == 10

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """Start creates background task, stop cancels it."""
        logger = AuditLogger(service="test", flush_interval_seconds=0.1)
        await logger.start()
        assert logger._running is True
        assert logger._task is not None

        await logger.stop()
        assert logger._running is False

    @pytest.mark.asyncio
    async def test_db_error_does_not_crash(self):
        """Database error during flush is handled gracefully."""
        mock_pool = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(
            side_effect=RuntimeError("DB connection failed")
        )
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        logger = AuditLogger(db_pool=mock_pool, service="test")
        logger._buffer = [AuditEvent(event_type="TEST")]

        # Should not raise
        await logger._flush_to_db()
        assert len(logger._buffer) == 0  # Buffer cleared even on error


# ============ Tests: Serialization ============


class TestSerialization:
    """Test JSON serialization of event details."""

    def test_serialize_simple_dict(self):
        """Simple dict serializes to JSON."""
        result = _serialize_details({"key": "value", "count": 42})
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["count"] == 42

    def test_serialize_datetime(self):
        """Datetime in details is serialized via str()."""
        dt = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        result = _serialize_details({"timestamp": dt})
        parsed = json.loads(result)
        assert "2026-03-30" in parsed["timestamp"]

    def test_serialize_empty_dict(self):
        """Empty dict serializes to '{}'."""
        result = _serialize_details({})
        assert result == "{}"


# ============ Tests: Module Singleton ============


class TestModuleSingleton:
    """Test module-level singleton management."""

    def test_get_returns_none_initially(self):
        """Before initialization, returns None."""
        # Reset global state
        import src.security.audit_log as mod

        original = mod._audit_logger
        mod._audit_logger = None
        try:
            assert get_audit_logger() is None
        finally:
            mod._audit_logger = original

    def test_set_and_get(self):
        """Set then get returns the same instance."""
        import src.security.audit_log as mod

        original = mod._audit_logger
        try:
            logger = AuditLogger(service="test")
            set_audit_logger(logger)
            assert get_audit_logger() is logger
        finally:
            mod._audit_logger = original

    @pytest.mark.asyncio
    async def test_convenience_function_noop_when_none(self):
        """audit_log_event is a no-op when logger is not set."""
        import src.security.audit_log as mod

        original = mod._audit_logger
        mod._audit_logger = None
        try:
            # Should not raise
            await audit_log_event(event_type="TEST")
        finally:
            mod._audit_logger = original

    @pytest.mark.asyncio
    async def test_convenience_function_logs_when_set(self):
        """audit_log_event queues event when logger is set."""
        import src.security.audit_log as mod

        original = mod._audit_logger
        try:
            logger = AuditLogger(service="test")
            set_audit_logger(logger)
            await audit_log_event(event_type="GEO_BLOCK", source_ip="1.2.3.4")
            assert logger._queue.qsize() == 1
        finally:
            mod._audit_logger = original
