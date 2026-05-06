"""Unit tests for src.websocket_handler.liveness_notifier."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.liveness_notifier import (
    LIVENESS_NOTIFY_CHANNEL,
    LivenessNotifier,
)


def _ts() -> MagicMock:
    """A timescale_client stand-in whose pg_pool yields a recording conn."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="SELECT 1")

    class _Acquire:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *exc):
            return False

    pool = MagicMock()
    pool.acquire = lambda: _Acquire()
    ts = MagicMock()
    ts.pg_pool = pool
    ts._conn = conn  # exposed for assertions
    return ts


@pytest.mark.asyncio
async def test_notify_publishes_pg_notify_call() -> None:
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=10.0)

    await notifier.maybe_notify("CP-1", "00000000-0000-0000-0000-000000000001")

    assert ts._conn.execute.await_count == 1
    args = ts._conn.execute.await_args.args
    assert args[0] == "SELECT pg_notify($1, $2)"
    assert args[1] == LIVENESS_NOTIFY_CHANNEL
    # Payload includes the station_id.
    assert "CP-1" in args[2]
    assert "00000000-0000-0000-0000-000000000001" in args[2]


@pytest.mark.asyncio
async def test_rate_limit_suppresses_within_interval() -> None:
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=10.0)
    org = "00000000-0000-0000-0000-000000000001"

    await notifier.maybe_notify("CP-1", org)
    await notifier.maybe_notify("CP-1", org)
    await notifier.maybe_notify("CP-1", org)

    # First call publishes; subsequent two within the 10 s window are
    # suppressed.
    assert ts._conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_rate_limit_resets_after_interval() -> None:
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=0.05)  # 50 ms for fast test
    org = "00000000-0000-0000-0000-000000000001"

    await notifier.maybe_notify("CP-1", org)
    time.sleep(0.06)
    await notifier.maybe_notify("CP-1", org)

    assert ts._conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_rate_limit_is_per_station() -> None:
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=10.0)
    org = "00000000-0000-0000-0000-000000000001"

    await notifier.maybe_notify("CP-1", org)
    await notifier.maybe_notify("CP-2", org)
    await notifier.maybe_notify("CP-3", org)

    # Each distinct station gets its own first-call publish.
    assert ts._conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_disabled_notifier_skips_publish() -> None:
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=10.0, enabled=False)

    await notifier.maybe_notify("CP-1", "00000000-0000-0000-0000-000000000001")

    ts._conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_organization_id_is_a_noop() -> None:
    """Without org scope the API can't fan out — skip the notify entirely."""
    ts = _ts()
    notifier = LivenessNotifier(ts, interval_s=10.0)

    await notifier.maybe_notify("CP-1", None)
    await notifier.maybe_notify("CP-1", "")

    ts._conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_pg_notify_error_is_swallowed() -> None:
    """A DB error must never propagate into OCPP frame handling."""
    ts = _ts()
    ts._conn.execute = AsyncMock(side_effect=RuntimeError("pg gone"))
    notifier = LivenessNotifier(ts, interval_s=10.0)

    # Must not raise.
    await notifier.maybe_notify("CP-1", "00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_rate_limit_advances_even_on_db_failure() -> None:
    """A failing notify still consumes the 10 s window — we don't want a
    failing pool to retry on every single OCPP frame."""
    ts = _ts()
    ts._conn.execute = AsyncMock(side_effect=RuntimeError("pg gone"))
    notifier = LivenessNotifier(ts, interval_s=10.0)
    org = "00000000-0000-0000-0000-000000000001"

    await notifier.maybe_notify("CP-1", org)
    await notifier.maybe_notify("CP-1", org)

    # Both calls would attempt to publish — but the second is rate-limited
    # away regardless of whether the first succeeded. We advance the clock
    # before awaiting the pool.
    assert ts._conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_no_pg_pool_attribute_is_a_safe_noop() -> None:
    """Some test fixtures pass a stub TimescaleClient without ``pg_pool``."""
    ts = MagicMock(spec=[])  # no attributes at all
    notifier = LivenessNotifier(ts, interval_s=10.0)

    # Must not raise.
    await notifier.maybe_notify("CP-1", "00000000-0000-0000-0000-000000000001")
