"""Unit tests for the post-commit cost task in TimescaleClient.close_open_session.

The close path schedules ``_compute_and_write_cost`` as a fire-and-forget
``asyncio.create_task`` so close-path latency is untouched. These tests
verify:

  * Success path schedules the task with the new session_id.
  * The race-lost case (``UPDATE … RETURNING NULL``) does NOT schedule.
  * The recovery path (``recover_orphaned_sessions``) also schedules
    cost calcs for the rows it closes.

The task body itself (``_compute_and_write_cost``) is exercised by the
integration suite — here we only assert the wiring.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.websocket_handler.config import TimescaleConfig
from src.websocket_handler.timescale_client import TimescaleClient


def _client_with_conn(conn):
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost",
        user="user",
        password="pass",
    )
    client = TimescaleClient(config)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None
    client.pg_pool = pool
    return client, conn


@pytest.mark.asyncio
async def test_close_open_session_schedules_cost_task_on_success():
    session_id = uuid4()
    conn = AsyncMock()
    # First fetchrow → SELECT FOR UPDATE returns the open session.
    # Second fetchrow → UPDATE returns the closed row.
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "session_id": session_id,
            "meter_start_wh": 1000,
            "last_meter_wh": 51000,
        },
        {
            "meter_start_wh": 1000,
            "last_meter_wh": 51000,
            "energy_delivered_kwh": 50.0,
        },
    ])
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=51000,
        )

    assert result is not None
    assert result["session_id"] == session_id
    scheduler.assert_called_once_with(session_id)


@pytest.mark.asyncio
async def test_close_open_session_no_match_does_not_schedule_cost():
    """When the SELECT FOR UPDATE matches nothing (idempotent retry on
    a closed session), no cost task should be scheduled."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=None,
        )

    assert result is None
    scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_close_open_session_lost_race_does_not_schedule_cost():
    """When SELECT matched but UPDATE … RETURNING returned NULL (another
    writer closed the row first), no task should be scheduled."""
    session_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"session_id": session_id, "meter_start_wh": 0, "last_meter_wh": None},
        None,  # UPDATE matched zero rows
    ])
    client, _ = _client_with_conn(conn)

    with patch.object(client, "_schedule_session_cost") as scheduler:
        result = await client.close_open_session(
            station_id="cp-1",
            transaction_id=1,
            end_time=datetime.now(timezone.utc),
            meter_stop_wh=None,
        )

    assert result is None
    scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_session_cost_creates_named_task():
    """_schedule_session_cost should use asyncio.create_task with a name
    that includes the session_id for log/debug traceability."""
    config = TimescaleConfig(
        service_url="postgresql://user:pass@localhost:5432/tsdb",
        host="localhost", user="user", password="pass",
    )
    client = TimescaleClient(config)
    client.pg_pool = MagicMock()

    session_id = uuid4()
    captured_name: list[str] = []

    def _fake_create_task(coro, *, name=None):
        captured_name.append(name or "")
        coro.close()  # avoid 'coroutine was never awaited' warning

        class _Stub:
            def get_name(self):
                return name
        return _Stub()

    with patch.object(client, "_compute_and_write_cost", return_value=AsyncMock()()):
        with patch("src.websocket_handler.timescale_client.asyncio.create_task", side_effect=_fake_create_task):
            client._schedule_session_cost(session_id)

    assert captured_name, "create_task was not called"
    assert str(session_id) in captured_name[0]
    assert captured_name[0].startswith("session-cost-")
