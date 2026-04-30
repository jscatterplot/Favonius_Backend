"""Unit tests for the dispatcher's startup glue.

The full Application orchestrator is integration-tested via docker; this
file just pins the WEB_CONCURRENCY trip-wire and the LISTEN-fallback
behavior so a regression in those code paths doesn't slip through.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.notifications.dispatcher import AlertDispatcher
from src.notifications.email_client import FakeEmailClient


class TestSingleWorkerWarning:
    def test_warns_critical_when_web_concurrency_above_one(self, monkeypatch, caplog):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with caplog.at_level(logging.CRITICAL, logger="src.notifications.dispatcher"):
            AlertDispatcher._check_single_worker()
        assert any(
            "WEB_CONCURRENCY=4" in rec.message and rec.levelname == "CRITICAL"
            for rec in caplog.records
        )

    def test_silent_when_web_concurrency_one(self, monkeypatch, caplog):
        monkeypatch.setenv("WEB_CONCURRENCY", "1")
        with caplog.at_level(logging.WARNING, logger="src.notifications.dispatcher"):
            AlertDispatcher._check_single_worker()
        assert all("WEB_CONCURRENCY" not in rec.message for rec in caplog.records)

    def test_silent_when_web_concurrency_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        with caplog.at_level(logging.WARNING, logger="src.notifications.dispatcher"):
            AlertDispatcher._check_single_worker()
        assert all("WEB_CONCURRENCY" not in rec.message for rec in caplog.records)

    def test_invalid_web_concurrency_does_not_raise(self, monkeypatch, caplog):
        monkeypatch.setenv("WEB_CONCURRENCY", "not-a-number")
        # Should fall back to 1 silently rather than crashing the dispatcher.
        AlertDispatcher._check_single_worker()


class TestStartFallsBackOnListenerFailure:
    @pytest.mark.asyncio
    async def test_listener_failure_does_not_block_startup(self, caplog):
        """If LISTEN setup fails the dispatcher must still start in
        polling-only mode rather than failing application boot."""
        pool = MagicMock()
        # acquire() raises so add_listener never gets called
        pool.acquire = AsyncMock(side_effect=RuntimeError("LISTEN unsupported"))
        # release() must exist for stop() teardown but won't be called here
        pool.release = AsyncMock()

        disp = AlertDispatcher(
            pool=pool,
            email_client=FakeEmailClient(),
            default_from="x@y.com",
            poll_interval_s=300.0,
        )

        with caplog.at_level(logging.WARNING, logger="src.notifications.dispatcher"):
            await disp.start()

        try:
            assert disp._main_task is not None
            assert disp._listener_conn is None
            assert any("polling-only" in rec.message for rec in caplog.records)
        finally:
            await disp.stop()
