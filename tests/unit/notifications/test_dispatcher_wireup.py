"""Unit tests for the dispatcher's startup glue.

The full Application orchestrator is integration-tested via docker; this
file just pins the WEB_CONCURRENCY trip-wire and the LISTEN-fallback
behavior so a regression in those code paths doesn't slip through.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.notifications.dispatcher import AlertDispatcher
from src.notifications.email_client import FakeEmailClient


def _make_pool_with_fetchval(return_value):
    """Build a pool mock whose ``acquire()`` yields a conn returning ``return_value``."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=return_value)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool.release = AsyncMock()
    return pool, conn


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


class TestSchemaPreflight:
    """Pin the migration-022 missing-schema guard.

    The dispatcher's main loop queries notification_alerts every poll_interval_s.
    If the table is missing (migration 022 not applied), the prior behavior was
    a perpetual UndefinedTableError storm in the logs. The preflight check must
    detect the missing table at start time and refuse to launch the loop.
    """

    @pytest.mark.asyncio
    async def test_start_aborts_when_schema_missing(self, caplog):
        pool, conn = _make_pool_with_fetchval(False)
        disp = AlertDispatcher(
            pool=pool,
            email_client=FakeEmailClient(),
            default_from="x@y.com",
            poll_interval_s=300.0,
        )

        with caplog.at_level(logging.ERROR, logger="src.notifications.dispatcher"):
            await disp.start()

        try:
            assert disp._main_task is None
            assert disp._listener_conn is None
            conn.fetchval.assert_awaited_once()
            assert any(
                "schema is missing" in rec.message and "022" in rec.message
                for rec in caplog.records
            )
        finally:
            await disp.stop()

    @pytest.mark.asyncio
    async def test_start_proceeds_when_schema_present(self):
        pool, conn = _make_pool_with_fetchval(True)
        # Subsequent _start_listener acquire raises so we don't actually
        # spawn the LISTEN task — the preflight result is what we're asserting.
        original_acquire = pool.acquire

        @asynccontextmanager
        async def acquire_then_fail():
            # First call (preflight) yields conn; subsequent calls aren't made
            # in this test because we cancel before the loop's first tick.
            async with original_acquire() as c:
                yield c

        pool.acquire = acquire_then_fail
        disp = AlertDispatcher(
            pool=pool,
            email_client=FakeEmailClient(),
            default_from="x@y.com",
            poll_interval_s=300.0,
        )

        try:
            await disp.start()
            assert disp._main_task is not None
        finally:
            await disp.stop()

    @pytest.mark.asyncio
    async def test_preflight_failure_assumes_present(self, caplog):
        """A transient pool error at startup must NOT permanently disable
        the dispatcher — fall through to the (resilient) main loop instead."""
        pool = MagicMock()
        pool.acquire = AsyncMock(side_effect=RuntimeError("transient"))
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
            # Listener also fails because acquire() raises, but main_task
            # is still scheduled (polling-only fallback).
            assert disp._main_task is not None
        finally:
            await disp.stop()
