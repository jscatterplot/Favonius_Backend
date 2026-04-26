"""Cross-restart recovery tests for the legacy OCPP WebSocket handler.

These tests run against a real TimescaleDB (docker-compose.test.yml) so they
exercise migration 013 and the SQL written by the recovery helpers. They do
NOT spin up a real OCPP charger — they unit-stub the FleetChargePoint
WebSocket layer and drive ``OCPP16Session`` callbacks directly.

Coverage targets (per session 2 done criteria):
  - test_boot_reloads_open_sessions
  - test_close_marks_connector_unavailable
  - test_offline_profile_is_queued_then_replayed_on_boot
  - test_status_notification_persisted_on_legacy_handler
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import TimescaleConfig
from websocket_handler.ocpp16_adapter import OCPP16Session
from websocket_handler.timescale_client import TimescaleClient


pytestmark = [pytest.mark.integration, pytest.mark.database]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )


@pytest_asyncio.fixture
async def db_pool(test_database_url):
    """Module-scoped pool would race with ``conftest.real_db_pool``; use a
    fresh, function-scoped pool so we can also tear down test rows cleanly."""
    try:
        pool = await asyncpg.create_pool(
            test_database_url, min_size=1, max_size=4, command_timeout=15
        )
    except Exception as exc:  # pragma: no cover — environment guard
        pytest.skip(f"Test DB not reachable: {exc}")
    try:
        # Belt-and-braces: ensure migration 013 has been applied. If the
        # DB was provisioned before this branch landed, run the file now.
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass('public.charging_command_queue')"
            )
            if exists is None:
                migration_path = os.path.join(
                    os.path.dirname(__file__),
                    "..",
                    "..",
                    "migrations",
                    "013_recovery.sql",
                )
                with open(migration_path, "r", encoding="utf-8") as f:
                    await conn.execute(f.read())
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def timescale_client(db_pool):
    """A ``TimescaleClient`` whose ``pg_pool`` points at the test DB.

    The recovery helpers only need ``pg_pool``; we skip ``connect()`` (which
    also wires sqlalchemy + the enhanced pool) to keep the test surface
    small.
    """
    # The recovery helpers only touch ``pg_pool``; we hand-build a config
    # with the bare minimum Pydantic requires and skip ``connect()`` (which
    # would also wire SQLAlchemy + the enhanced pool).
    cfg = TimescaleConfig(
        service_url="postgresql://favonius_test:test_password@localhost:5432/favonius_test",
        host="localhost",
        port=5432,
        user="favonius_test",
        password="test_password",
        database="favonius_test",
        sslmode="disable",
    )
    client = TimescaleClient(cfg)
    client.pg_pool = db_pool
    client.connected = True
    return client


@pytest_asyncio.fixture
async def cleanup_station(db_pool):
    """Yield a per-test station_id and wipe its rows on teardown."""
    station_id = f"test_recovery_{uuid.uuid4().hex[:8]}"
    yield station_id
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM charging_command_queue WHERE charge_point_id = $1",
            station_id,
        )
        await conn.execute(
            "DELETE FROM charging_sessions WHERE station_id = $1", station_id
        )
        await conn.execute(
            "DELETE FROM connector_status WHERE station_id = $1", station_id
        )


def _make_session(station_id: str, timescale: TimescaleClient) -> OCPP16Session:
    """Construct an ``OCPP16Session`` with WS and message-handler stubs.

    The real WebSocket layer is not exercised here — we drive callbacks
    directly. ``MessageHandler._push_to_main_api`` is mocked so the test
    does not need a running main API.
    """
    fake_ws = MagicMock()
    fake_handler = MagicMock()
    fake_handler._push_to_main_api = AsyncMock(return_value=None)
    return OCPP16Session(
        station_id=station_id,
        websocket=fake_ws,
        timescale_client=timescale,
        message_handler=fake_handler,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_reloads_open_sessions(
    db_pool, timescale_client, cleanup_station
):
    """An open ``charging_sessions`` row is rehydrated into FleetChargePoint
    on BootNotification so a subsequent StopTransaction is recognised."""
    station_id = cleanup_station

    tx_id = int(
        await db_pool.fetchval("SELECT nextval('ocpp_transaction_id')")
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                station_id, transaction_id, evse_id, connector_id,
                id_token, start_time
            ) VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '5 minutes')
            """,
            station_id,
            tx_id,
            1,
            1,
            "TAG_BOOT_RELOAD",
        )

    session = _make_session(station_id, timescale_client)
    assert session._cp.transactions == {}

    await session._on_boot(
        cp_id=station_id,
        vendor="ACME",
        model="X1",
        serial_number="SN-1",
        firmware_version="1.0",
    )

    # Replay scheduling is async; wait long enough for the no-op replay loop
    # to complete (queue is empty here) so the test cleanup is deterministic.
    await asyncio.sleep(0.05)
    assert session._cp.transactions[1] == tx_id


@pytest.mark.asyncio
async def test_close_marks_connector_unavailable(
    db_pool, timescale_client, cleanup_station
):
    """The WS close hook must (a) append an Unavailable row to
    ``connector_status`` and (b) stamp ``last_seen_at`` on every open
    ``charging_sessions`` row at the station."""
    station_id = cleanup_station

    async with db_pool.acquire() as conn:
        # Prior status: connector 1 is Available (sets up the source row
        # that ``mark_connectors_unavailable`` copies forward).
        await conn.execute(
            """
            INSERT INTO connector_status (
                station_id, connector_id, status, error_code, timestamp
            ) VALUES ($1, 1, 'Available', 'NoError', NOW() - INTERVAL '1 minute')
            """,
            station_id,
        )
        # Open session row that should pick up ``last_seen_at``.
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                station_id, transaction_id, evse_id, connector_id,
                id_token, start_time
            ) VALUES ($1, $2, 1, 1, 'TAG_CLOSE', NOW())
            """,
            station_id,
            int(await db_pool.fetchval("SELECT nextval('ocpp_transaction_id')")),
        )

    await timescale_client.mark_connectors_unavailable(station_id)
    await timescale_client.mark_sessions_seen(station_id)

    async with db_pool.acquire() as conn:
        latest = await conn.fetchrow(
            """
            SELECT status, error_code FROM connector_status
             WHERE station_id = $1 AND connector_id = 1
             ORDER BY timestamp DESC LIMIT 1
            """,
            station_id,
        )
        assert latest["status"] == "Unavailable"
        assert latest["error_code"] == "ConnectionLost"

        seen = await conn.fetchval(
            """
            SELECT last_seen_at FROM charging_sessions
             WHERE station_id = $1 AND end_time IS NULL
            """,
            station_id,
        )
        assert seen is not None
        assert (datetime.now(timezone.utc) - seen) < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_offline_profile_is_queued_then_replayed_on_boot(
    db_pool, timescale_client, cleanup_station
):
    """``send_charging_profile`` against a disconnected charger writes a
    pending row to ``charging_command_queue``; replaying drains it and
    marks the row ``acked``."""
    station_id = cleanup_station

    session = _make_session(station_id, timescale_client)

    # FleetChargePoint catches transport failures internally and returns False.
    session._cp.set_charging_profile = AsyncMock(return_value=False)

    profile = {
        "chargingProfileId": 4242,
        "stackLevel": 0,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind": "Absolute",
        "chargingSchedule": {
            "chargingRateUnit": "W",
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 7000}],
        },
    }

    ok = await session.send_charging_profile(1, profile)
    assert ok is False

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT queue_id, connector_id, status, attempt_count
              FROM charging_command_queue
             WHERE charge_point_id = $1
            """,
            station_id,
        )
        assert row is not None
        assert row["status"] == "pending"
        assert row["connector_id"] == 1
        assert row["attempt_count"] == 0

    # Now the charger reconnects. Swap in a working push and replay.
    session._cp.set_charging_profile = AsyncMock(return_value=True)

    sent = await session.replay_queued_commands()
    assert sent == 1

    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            """
            SELECT status FROM charging_command_queue
             WHERE charge_point_id = $1
            """,
            station_id,
        )
        assert status == "acked"


@pytest.mark.asyncio
async def test_status_notification_persisted_on_legacy_handler(
    db_pool, timescale_client, cleanup_station
):
    """Every StatusNotification through ``OCPP16Session._on_status_change``
    must land in ``connector_status`` (PRD §7.1: alerts depend on this)."""
    station_id = cleanup_station
    session = _make_session(station_id, timescale_client)

    ts = "2026-04-26T12:00:00Z"
    await session._on_status_change(
        cp_id=station_id,
        connector_id=2,
        status="Charging",
        error_code="NoError",
        timestamp=ts,
    )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT status, error_code, connector_id
              FROM connector_status
             WHERE station_id = $1
             ORDER BY timestamp DESC LIMIT 1
            """,
            station_id,
        )
        assert row is not None
        assert row["status"] == "Charging"
        assert row["error_code"] == "NoError"
        assert row["connector_id"] == 2
