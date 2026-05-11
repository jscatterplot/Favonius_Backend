"""Unit tests for src.api.liveness_hub.LivenessHub."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from src.api.liveness_hub import LivenessHub


def _make_hub(pool: MagicMock = None) -> LivenessHub:
    """A LivenessHub with a stub asyncpg pool that won't actually LISTEN."""
    pool = pool or MagicMock()
    return LivenessHub(pool)


@pytest.mark.asyncio
async def test_subscribe_returns_a_queue_and_records_subscriber() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")
    assert isinstance(q, asyncio.Queue)
    assert q in hub._subscribers["org-A"]


@pytest.mark.asyncio
async def test_unsubscribe_removes_queue_and_cleans_empty_bucket() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")
    hub.unsubscribe("org-A", q)
    assert "org-A" not in hub._subscribers


@pytest.mark.asyncio
async def test_notification_fans_out_to_all_subscribers_for_org() -> None:
    hub = _make_hub()
    q1 = hub.subscribe("org-A")
    q2 = hub.subscribe("org-A")

    payload = json.dumps(
        {
            "station_id": "CP-1",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T10:00:00Z",
            "server_time": "2026-05-06T10:00:00Z",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    e1 = await asyncio.wait_for(q1.get(), timeout=0.1)
    e2 = await asyncio.wait_for(q2.get(), timeout=0.1)
    assert (
        e1
        == e2
        == {
            "station_id": "CP-1",
            "last_interaction_at": "2026-05-06T10:00:00Z",
            "server_time": "2026-05-06T10:00:00Z",
        }
    )


@pytest.mark.asyncio
async def test_notification_does_not_leak_across_organizations() -> None:
    hub = _make_hub()
    q_a = hub.subscribe("org-A")
    q_b = hub.subscribe("org-B")

    payload = json.dumps(
        {
            "station_id": "CP-1",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T10:00:00Z",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    a = await asyncio.wait_for(q_a.get(), timeout=0.1)
    assert a["station_id"] == "CP-1"
    # org-B receives nothing for org-A's notify.
    assert q_b.empty()


@pytest.mark.asyncio
async def test_invalid_json_payload_is_logged_and_dropped() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")

    # Must not raise.
    hub._on_notify(MagicMock(), 0, "charger_liveness", "not json {{")

    assert q.empty()


@pytest.mark.asyncio
async def test_valid_json_non_object_payload_is_dropped() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")

    # Must not raise even though JSON is valid but not an object.
    hub._on_notify(MagicMock(), 0, "charger_liveness", '["not", "an", "object"]')

    assert q.empty()


@pytest.mark.asyncio
async def test_payload_missing_organization_id_is_dropped() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")

    payload = json.dumps({"station_id": "CP-1", "last_interaction_at": "x"})
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    assert q.empty()


@pytest.mark.asyncio
async def test_full_subscriber_queue_drops_oldest_event() -> None:
    """Slow subscribers must not block the listener — drop oldest, keep newest."""
    hub = _make_hub()
    q = hub.subscribe("org-A")
    # Fill the queue.
    for _ in range(50):
        q.put_nowait("filler")

    payload = json.dumps(
        {
            "station_id": "CP-1",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T10:00:00Z",
            "server_time": "2026-05-06T10:00:00Z",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    # Queue should still have 50 entries (oldest dropped, new event added).
    assert q.qsize() == 50
    # The first item now is what was second before — the new event lands
    # at the tail.
    drained: list = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert drained[-1] == {
        "station_id": "CP-1",
        "last_interaction_at": "2026-05-06T10:00:00Z",
        "server_time": "2026-05-06T10:00:00Z",
    }


@pytest.mark.asyncio
async def test_server_time_field_propagates_to_subscribers() -> None:
    """Frontend uses ``server_time`` to compute clock-skew offset.

    Producer always emits the field; the hub forwards it verbatim.
    """
    hub = _make_hub()
    q = hub.subscribe("org-A")

    payload = json.dumps(
        {
            "station_id": "CP-7",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T11:23:45.678+00:00",
            "server_time": "2026-05-06T11:23:45.678+00:00",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    event = await asyncio.wait_for(q.get(), timeout=0.1)
    assert event["server_time"] == "2026-05-06T11:23:45.678+00:00"
    assert event["last_interaction_at"] == "2026-05-06T11:23:45.678+00:00"


@pytest.mark.asyncio
async def test_notification_with_no_matching_subscribers_is_silent() -> None:
    hub = _make_hub()

    payload = json.dumps(
        {
            "station_id": "CP-1",
            "organization_id": "org-orphan",
            "last_interaction_at": "x",
        }
    )
    # Must not raise.
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)


@pytest.mark.asyncio
async def test_stop_enqueues_shutdown_sentinel_even_when_queue_is_full() -> None:
    hub = _make_hub()
    q = hub.subscribe("org-A")
    # Saturate queue so stop() must evict to enqueue sentinel.
    for _ in range(50):
        q.put_nowait("filler")

    hub._running = True
    await hub.stop()

    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert drained[-1] is None


# ----- Per-station last_interaction cache (REST initial-load fix) -----


@pytest.mark.asyncio
async def test_get_last_interaction_returns_none_for_unknown_station() -> None:
    """Cache cold: REST endpoint falls back to connector_status MAX."""
    hub = _make_hub()
    assert hub.get_last_interaction("unknown-cp") is None


@pytest.mark.asyncio
async def test_notify_populates_last_interaction_cache() -> None:
    """Every NOTIFY updates the per-station cache as a parsed datetime."""
    from datetime import datetime, timezone

    hub = _make_hub()
    payload = json.dumps(
        {
            "station_id": "CP-7",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T12:34:56.789+00:00",
            "server_time": "2026-05-06T12:34:56.789+00:00",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    cached = hub.get_last_interaction("CP-7")
    assert cached == datetime(2026, 5, 6, 12, 34, 56, 789000, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_cache_overwrites_with_newer_notify() -> None:
    """Subsequent NOTIFYs replace the cached value."""
    from datetime import datetime, timezone

    hub = _make_hub()
    for ts in ("2026-05-06T12:00:00+00:00", "2026-05-06T12:05:00+00:00"):
        payload = json.dumps(
            {
                "station_id": "CP-7",
                "organization_id": "org-A",
                "last_interaction_at": ts,
                "server_time": ts,
            }
        )
        hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    cached = hub.get_last_interaction("CP-7")
    assert cached == datetime(2026, 5, 6, 12, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_cache_populated_even_without_subscribers() -> None:
    """REST initial-load works even when no SSE clients are connected."""
    from datetime import datetime, timezone

    hub = _make_hub()
    # No subscribe() call — cache should still populate from NOTIFYs.
    payload = json.dumps(
        {
            "station_id": "CP-7",
            "organization_id": "org-A",
            "last_interaction_at": "2026-05-06T12:00:00+00:00",
            "server_time": "2026-05-06T12:00:00+00:00",
        }
    )
    hub._on_notify(MagicMock(), 0, "charger_liveness", payload)

    assert hub.get_last_interaction("CP-7") == datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
