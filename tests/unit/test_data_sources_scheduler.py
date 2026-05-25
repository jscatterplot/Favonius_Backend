"""Unit tests for the data-source scheduler + recovery (repository mocked)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from src.core.data_sources import repository as repo
from src.core.data_sources import scheduler

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _spawner():
    """Return (spawn_fn, captured_list); closes coroutines to avoid warnings."""
    captured: list[Any] = []

    def spawn(coro: Any) -> None:
        captured.append(coro)
        coro.close()

    return spawn, captured


def _conn_row() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "organization_id": str(uuid4()),
        "site_id": str(uuid4()),
        "provider_key": "kempower",
    }


async def test_recovery_rekicks_each_orphan(monkeypatch):
    rows = [
        {"id": str(uuid4()), "status": "running", "connection_id": str(uuid4())} for _ in range(3)
    ]
    monkeypatch.setattr(repo, "find_orphaned_jobs", AsyncMock(return_value=rows))
    spawn, captured = _spawner()

    n = await scheduler.recover_orphaned_data_source_jobs(MagicMock(), MagicMock(), spawn=spawn)
    assert n == 3
    assert len(captured) == 3


async def test_recovery_noop_when_none(monkeypatch):
    monkeypatch.setattr(repo, "find_orphaned_jobs", AsyncMock(return_value=[]))
    spawn, captured = _spawner()
    n = await scheduler.recover_orphaned_data_source_jobs(MagicMock(), MagicMock(), spawn=spawn)
    assert n == 0 and captured == []


async def test_recovery_paginates_through_all(monkeypatch):
    import datetime as _dt

    monkeypatch.setattr(scheduler, "_RECOVERY_PAGE_SIZE", 2)
    page1 = [
        {
            "id": str(uuid4()),
            "status": "running",
            "connection_id": str(uuid4()),
            "created_at": _dt.datetime(2026, 5, 25, tzinfo=_dt.timezone.utc),
        }
        for _ in range(2)
    ]
    page2 = [
        {
            "id": str(uuid4()),
            "status": "pending",
            "connection_id": str(uuid4()),
            "created_at": _dt.datetime(2026, 5, 25, 1, tzinfo=_dt.timezone.utc),
        }
    ]
    find = AsyncMock(side_effect=[page1, page2])
    monkeypatch.setattr(repo, "find_orphaned_jobs", find)
    spawn, captured = _spawner()

    n = await scheduler.recover_orphaned_data_source_jobs(MagicMock(), MagicMock(), spawn=spawn)
    assert n == 3
    assert len(captured) == 3
    assert find.await_count == 2  # full first page → fetched again
    # Second fetch carried the keyset cursor from the last row of page 1.
    assert find.await_args_list[1].kwargs["after_id"] == page1[-1]["id"]


async def test_tick_enqueues_and_spawns(monkeypatch):
    conn = _conn_row()
    monkeypatch.setattr(repo, "find_due_connections", AsyncMock(return_value=[conn]))
    enqueue = AsyncMock(return_value={"id": str(uuid4())})
    advance = AsyncMock()
    monkeypatch.setattr(repo, "enqueue_job", enqueue)
    monkeypatch.setattr(repo, "set_next_sync_now_plus_interval", advance)
    spawn, captured = _spawner()

    await scheduler._tick(MagicMock(), MagicMock(), spawn=spawn)

    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["trigger"] == "scheduled"
    advance.assert_awaited_once()
    assert len(captured) == 1  # one job kicked


async def test_tick_overlap_race_advances_without_spawn(monkeypatch):
    conn = _conn_row()
    monkeypatch.setattr(repo, "find_due_connections", AsyncMock(return_value=[conn]))

    async def _raise(*a, **k):
        raise asyncpg.UniqueViolationError("dup")

    advance = AsyncMock()
    monkeypatch.setattr(repo, "enqueue_job", _raise)
    monkeypatch.setattr(repo, "set_next_sync_now_plus_interval", advance)
    spawn, captured = _spawner()

    await scheduler._tick(MagicMock(), MagicMock(), spawn=spawn)

    advance.assert_awaited_once()  # next_sync still advanced
    assert captured == []  # but no duplicate job kicked


def test_check_single_worker_warns(monkeypatch, caplog):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    import logging

    with caplog.at_level(logging.CRITICAL):
        scheduler.check_single_worker()
    assert any("WEB_CONCURRENCY" in r.message for r in caplog.records)
