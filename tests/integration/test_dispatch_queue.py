"""Integration tests for the queue-mediated SetChargingProfile dispatch.

Session 3 replaces the in-process ``set_charging_profile`` push with a
``charging_command_queue`` row that the legacy WebSocket handler drains.
These tests pin down two end-to-end behaviours:

  * ``dispatch_charging_profiles`` writes a queue row even when no charger
    is currently connected — the legacy boot replay path will pick it up.
  * ``ChargingCommandQueueConsumer`` drains pending rows, calls the
    charger session's ``send_charging_profile``, and marks the row as
    ``sent`` (or ``failed``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.adapters.ocpp.dispatch import dispatch_charging_profiles
from src.core.models import OptimizationResult


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes — the queue sits behind asyncpg, but we don't need a live DB to
# verify the dispatch contract: it is "rows in, send_charging_profile out".
# A small in-memory queue + pool fake is enough.
# ---------------------------------------------------------------------------


class FakeQueue:
    """In-memory stand-in for charging_command_queue.

    Exposes only the columns dispatch.py / the consumer touch.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next_id = 1

    def insert(self, charge_point_id: str, connector_id: int, payload: dict) -> int:
        queue_id = self._next_id
        self._next_id += 1
        self.rows.append(
            {
                "queue_id": queue_id,
                "charge_point_id": charge_point_id,
                "connector_id": connector_id,
                "command_type": "set_charging_profile",
                "payload": payload,
                "status": "pending",
                "attempt_count": 0,
                "last_error": None,
            }
        )
        return queue_id

    def pending(self) -> list[dict]:
        return [r for r in self.rows if r["status"] == "pending"]

    def by_id(self, queue_id: int) -> dict:
        return next(r for r in self.rows if r["queue_id"] == queue_id)


class FakeConn:
    """Minimal asyncpg.Connection shim for the dispatch INSERTs."""

    def __init__(self, queue: FakeQueue) -> None:
        self.queue = queue

    async def fetchval(self, sql: str, *args):
        # dispatch._enqueue: cp_id, connector_id, json_payload, expires_in_min
        cp_id, connector_id, payload_json, _expires = args
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        return self.queue.insert(cp_id, connector_id, payload)

    async def execute(self, sql: str, *args):
        # Audit-table write — we just swallow it.
        return "INSERT 0 1"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakePool:
    def __init__(self, queue: FakeQueue) -> None:
        self.queue = queue

    def acquire(self):
        return FakeConn(self.queue)


@pytest.fixture
def fake_queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def fake_pools(fake_queue: FakeQueue):
    """DatabasePools shim with the same .ts/.static attributes."""
    pool = FakePool(fake_queue)
    return SimpleNamespace(static=pool, ts=pool)


@pytest.fixture
def optimization_result() -> OptimizationResult:
    """Two-vehicle schedule with positive charging power on each."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            "bus_1": {"charging_power": [22.0, 22.0, 0.0, 0.0]},
            "bus_2": {"charging_power": [11.0, 11.0, 11.0, 0.0]},
        },
        battery_dispatch=[],
        grid_power=[],
        peak_demand_kw=44.0,
        objective_value=0.0,
        solve_time_s=0.05,
        solver_used="highs",
        status="optimal",
    )


# ---------------------------------------------------------------------------
# 1. dispatch_charging_profiles enqueues even when chargers are offline
# ---------------------------------------------------------------------------


async def test_dispatch_writes_queue_row_when_charger_offline(
    fake_queue: FakeQueue, fake_pools, optimization_result: OptimizationResult
) -> None:
    """The optimizer never knows whether a charger is online; dispatch must
    still write the row so the boot-time replay path can deliver it."""
    vehicle_to_charger_map = {
        "bus_1": ("CHARGER_001", 1),
        "bus_2": ("CHARGER_002", 1),
    }

    results = await dispatch_charging_profiles(
        optimization_result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map=vehicle_to_charger_map,
    )

    assert results == {"bus_1": True, "bus_2": True}
    assert len(fake_queue.rows) == 2

    row1, row2 = fake_queue.rows
    assert row1["charge_point_id"] == "CHARGER_001"
    assert row1["connector_id"] == 1
    assert row1["status"] == "pending"
    # Payload must carry the OCPP envelope a charger can consume directly.
    assert row1["payload"]["chargingSchedule"]["chargingSchedulePeriod"]
    assert row2["charge_point_id"] == "CHARGER_002"


async def test_dispatch_skips_unmapped_vehicles(
    fake_queue: FakeQueue, fake_pools, optimization_result: OptimizationResult
) -> None:
    """No mapping → row is NOT enqueued and dispatch reports False."""
    results = await dispatch_charging_profiles(
        optimization_result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map={"bus_1": ("CHARGER_001", 1)},  # bus_2 missing
    )

    assert results["bus_1"] is True
    assert results["bus_2"] is False
    assert len(fake_queue.rows) == 1
    assert fake_queue.rows[0]["charge_point_id"] == "CHARGER_001"


async def test_dispatch_skips_zero_power_schedule(
    fake_queue: FakeQueue, fake_pools
) -> None:
    """All-zero charging power means nothing to dispatch."""
    result = OptimizationResult(
        run_id=uuid4(),
        schedule={"bus_1": {"charging_power": [0.0, 0.0, 0.0]}},
        battery_dispatch=[],
        grid_power=[],
        peak_demand_kw=0.0,
        objective_value=0.0,
        solve_time_s=0.01,
        solver_used="highs",
        status="optimal",
    )

    results = await dispatch_charging_profiles(
        result,
        pools=fake_pools,
        depot_id="depot_a",
        vehicle_to_charger_map={"bus_1": ("CHARGER_001", 1)},
    )

    assert results["bus_1"] is False
    assert fake_queue.rows == []


# ---------------------------------------------------------------------------
# 2. ChargingCommandQueueConsumer drains rows to 'sent' / 'failed'
# ---------------------------------------------------------------------------


class FakeTimescaleClient:
    """Just enough of TimescaleClient for the consumer to drain rows."""

    def __init__(self, queue: FakeQueue) -> None:
        self.queue = queue
        # Real client exposes pg_pool; consumer falls back to polling-only
        # if it's missing, which is the path we want for unit tests.
        self.pg_pool = None

    async def fetch_pending_commands_all(
        self, limit: int = 200, exclude_charge_point_ids: list[str] | None = None
    ):
        excluded = set(exclude_charge_point_ids or [])
        pending = [r for r in self.queue.pending() if r["charge_point_id"] not in excluded]
        return [dict(r) for r in pending[:limit]]

    async def mark_command_sent(self, queue_id: int) -> None:
        row = self.queue.by_id(queue_id)
        if row["status"] == "pending":
            row["status"] = "sent"
            row["attempt_count"] += 1

    async def mark_command_failed(self, queue_id: int, error: str) -> None:
        row = self.queue.by_id(queue_id)
        if row["status"] == "pending":
            row["status"] = "failed"
            row["last_error"] = error
            row["attempt_count"] += 1

    async def expire_overdue_commands(self) -> int:
        return 0

    async def queue_depth_by_status(self):
        return {"pending": len(self.queue.pending())}


async def test_queue_consumer_drains_pending_to_sent(fake_queue: FakeQueue) -> None:
    """Consumer.drain_once pushes via send_charging_profile, marks 'sent'."""
    from src.websocket_handler.charging_profile_manager import (
        ChargingCommandQueueConsumer,
    )

    fake_queue.insert(
        "CHARGER_001",
        1,
        {
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Absolute",
            "stackLevel": 0,
            "chargingSchedule": {
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": 22000, "numberPhases": 3}
                ],
            },
        },
    )

    # In-memory charger session: exposes send_charging_profile that returns True.
    fake_session = MagicMock()
    fake_session.send_charging_profile = AsyncMock(return_value=True)

    def cp_lookup(cp_id: str):
        return fake_session if cp_id == "CHARGER_001" else None

    timescale = FakeTimescaleClient(fake_queue)
    consumer = ChargingCommandQueueConsumer(timescale, cp_lookup)

    processed = await consumer.drain_once()

    assert processed == 1
    fake_session.send_charging_profile.assert_awaited_once()
    args, kwargs = fake_session.send_charging_profile.call_args
    assert args[0] == 1  # connector_id
    assert "chargingSchedule" in args[1]
    # Consumer probes the signature; OCPP16Session takes allow_enqueue, our
    # fake doesn't, so the consumer falls back to the 2-arg form.
    assert "allow_enqueue" not in kwargs

    assert fake_queue.rows[0]["status"] == "sent"


async def test_queue_consumer_marks_failed_on_charger_reject(
    fake_queue: FakeQueue,
) -> None:
    fake_queue.insert("CHARGER_001", 1, {"chargingSchedule": {}})

    fake_session = MagicMock()
    fake_session.send_charging_profile = AsyncMock(return_value=False)

    timescale = FakeTimescaleClient(fake_queue)
    consumer = __import__(
        "src.websocket_handler.charging_profile_manager",
        fromlist=["ChargingCommandQueueConsumer"],
    ).ChargingCommandQueueConsumer(timescale, lambda _cp: fake_session)

    await consumer.drain_once()

    assert fake_queue.rows[0]["status"] == "failed"
    assert fake_queue.rows[0]["last_error"]


async def test_queue_consumer_leaves_offline_rows_pending(
    fake_queue: FakeQueue,
) -> None:
    """No connected session → row stays pending for the boot replay path."""
    fake_queue.insert("CHARGER_OFFLINE", 1, {"chargingSchedule": {}})

    timescale = FakeTimescaleClient(fake_queue)
    consumer = __import__(
        "src.websocket_handler.charging_profile_manager",
        fromlist=["ChargingCommandQueueConsumer"],
    ).ChargingCommandQueueConsumer(timescale, lambda _cp: None)

    await consumer.drain_once()

    assert fake_queue.rows[0]["status"] == "pending"


async def test_queue_consumer_emits_profile_push_latency_metric(
    fake_queue: FakeQueue,
) -> None:
    """profile_push_latency_seconds.labels(station,outcome).observe is called."""
    from src.websocket_handler.charging_profile_manager import (
        ChargingCommandQueueConsumer,
    )
    from src.websocket_handler import monitoring as m

    fake_queue.insert("CHARGER_001", 1, {"chargingSchedule": {}})
    fake_session = MagicMock()
    fake_session.send_charging_profile = AsyncMock(return_value=True)

    # Snapshot the histogram sample count so we can detect that .observe ran.
    samples_before = _collect_profile_push_count("CHARGER_001", "sent")

    consumer = ChargingCommandQueueConsumer(
        FakeTimescaleClient(fake_queue), lambda _cp: fake_session
    )
    await consumer.drain_once()

    samples_after = _collect_profile_push_count("CHARGER_001", "sent")
    assert samples_after == samples_before + 1


def _collect_profile_push_count(station_id: str, outcome: str) -> int:
    """Read the *_count sample of profile_push_latency_seconds for one labelset."""
    from src.websocket_handler import monitoring as m

    metric = m.PROFILE_PUSH_LATENCY
    if not hasattr(metric, "collect"):
        return 0
    for collected in metric.collect():
        for sample in collected.samples:
            if (
                sample.name.endswith("_count")
                and sample.labels.get("station_id") == station_id
                and sample.labels.get("outcome") == outcome
            ):
                return int(sample.value)
    return 0
