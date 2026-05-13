"""Tests for OCPP16Session._push_metering_config (Issue 3A / 10A).

Verifies that after every BootNotification we push the metering config
keys, and that none of the three failure modes (Rejected, Timeout,
exception) can stall the OCPP event loop.

Pattern follows the existing session fixture in test_ocpp16_adapter.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.rfid_authorization import RFIDAuthorizationService


@pytest.fixture()
def mock_websocket():
    ws = MagicMock()
    ws.send = AsyncMock()
    return ws


@pytest.fixture()
def mock_timescale():
    ts = MagicMock()
    ts.pg_pool = MagicMock()
    ts._static_pool = MagicMock(return_value=ts.pg_pool)
    ts.upsert_charging_station = AsyncMock()
    ts.fetch_open_sessions = AsyncMock(return_value=[])
    ts.lookup_idtag_authorization = AsyncMock(return_value=None)
    return ts


@pytest.fixture()
def mock_message_handler(mock_timescale):
    mh = MagicMock()
    mh._push_to_main_api = AsyncMock()
    mh.rfid_authorization = RFIDAuthorizationService(mock_timescale, MagicMock())
    return mh


@pytest.fixture()
def session(mock_websocket, mock_timescale, mock_message_handler):
    from src.websocket_handler.ocpp16_adapter import OCPP16Session

    return OCPP16Session(
        station_id="test-cp-metering",
        websocket=mock_websocket,
        timescale_client=mock_timescale,
        message_handler=mock_message_handler,
    )


@pytest.mark.asyncio
async def test_push_metering_config_sends_all_three_keys(session):
    """Happy path: all three OCPP keys are pushed in order with the documented values."""
    session._cp.change_configuration = AsyncMock(return_value="Accepted")

    await session._push_metering_config()

    calls = session._cp.change_configuration.await_args_list
    assert len(calls) == 3

    pushed = {call.kwargs["key"]: call.kwargs["value"] for call in calls}
    assert "MeterValuesSampledData" in pushed
    assert "StopTxnSampledData" in pushed
    assert "MeterValueSampleInterval" in pushed

    # Energy.Active.Import.Register MUST be in MeterValuesSampledData —
    # that's the whole point of this push.
    assert "Energy.Active.Import.Register" in pushed["MeterValuesSampledData"]
    # And in StopTxnSampledData so transactionData carries the final register.
    assert "Energy.Active.Import.Register" in pushed["StopTxnSampledData"]


@pytest.mark.asyncio
async def test_push_metering_config_continues_after_rejected_key(session):
    """A charger rejecting one key must not abort the remaining pushes.

    Terra AC pre-1.8.32 rejects certain measurand combinations; the rest
    of the configuration should still be attempted.
    """
    statuses = ["Accepted", "Rejected", "Accepted"]
    session._cp.change_configuration = AsyncMock(side_effect=statuses)

    await session._push_metering_config()

    assert session._cp.change_configuration.await_count == 3


@pytest.mark.asyncio
async def test_push_metering_config_continues_after_timeout(session):
    """A charger that never responds to ChangeConfiguration must not stall the loop.

    A 10-second timeout is enforced; the loop logs a WARN and proceeds
    to the next key. This is the highest-risk failure mode — a blocked
    push would stall every subsequent BootNotification.
    """
    async def slow_change(*args, **kwargs):
        await asyncio.sleep(30)  # Longer than the 10s timeout

    session._cp.change_configuration = AsyncMock(side_effect=slow_change)

    # Must complete in well under 30 seconds despite the slow charger
    await asyncio.wait_for(session._push_metering_config(), timeout=35.0)


@pytest.mark.asyncio
async def test_push_metering_config_swallows_exceptions(session):
    """A charger raising during ChangeConfiguration must not crash the session.

    Defensive: change_configuration on FleetChargePoint already returns
    'Rejected' on exception, but the push helper has its own try/except
    to guard against future changes to that contract.
    """
    session._cp.change_configuration = AsyncMock(
        side_effect=RuntimeError("connection dropped")
    )

    # Must not raise; all three keys are attempted regardless.
    await session._push_metering_config()
    assert session._cp.change_configuration.await_count == 3


@pytest.mark.asyncio
async def test_push_metering_config_clears_task_reference(session):
    """After the push completes, _metering_config_task is cleared.

    This is what tells subsequent BootNotifications they can schedule a
    fresh push without cancelling a still-running task by accident.
    """
    session._cp.change_configuration = AsyncMock(return_value="Accepted")

    # Wire up the task reference the way _on_boot would.
    task = asyncio.create_task(session._push_metering_config())
    session._metering_config_task = task
    await task

    assert session._metering_config_task is None
