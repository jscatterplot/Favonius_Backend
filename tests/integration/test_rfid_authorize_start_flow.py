"""Integration-style coverage for RFID authorize -> start flow on legacy OCPP path."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocpp.v16.enums import AuthorizationStatus

from src.websocket_handler.ocpp16_adapter import OCPP16Session


pytestmark = pytest.mark.asyncio


def _build_session(timescale_client: object) -> OCPP16Session:
    websocket = SimpleNamespace()
    message_handler = MagicMock()
    message_handler._push_to_main_api = AsyncMock()
    return OCPP16Session(
        station_id="LEGACY-CP-1",
        websocket=websocket,
        timescale_client=timescale_client,
        message_handler=message_handler,
    )


async def test_known_rfid_authorize_then_start_sets_identity_and_persists_open_session() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(
        return_value={
            "source": "rfid_card",
            "vehicle_id": "veh-123",
            "driver_id": "drv-001",
            "card_id": "card-xyz",
            "depot_id": "dep-1",
        }
    )
    timescale.next_transaction_id = AsyncMock(return_value=98765)
    timescale.insert_open_session = AsyncMock()
    timescale.next_charging_profile_id = AsyncMock(return_value=10)
    timescale.insert_telemetry_batch = AsyncMock()

    session = _build_session(timescale)

    authorize = await session._on_authorize("LEGACY-CP-1", "CARD-001")
    assert authorize == AuthorizationStatus.accepted

    start = await session._on_transaction_start(
        cp_id="LEGACY-CP-1",
        connector_id=1,
        id_tag="CARD-001",
        meter_start=0,
        timestamp="2026-01-01T00:00:00Z",
    )
    assert start == AuthorizationStatus.accepted
    assert session._pending_start["vehicle_id"] == "veh-123"
    assert session._pending_start["driver_id"] == "drv-001"
    assert session._pending_start["card_id"] == "card-xyz"

    tx_id = await session._next_transaction_id()
    assert tx_id == 98765
    timescale.insert_open_session.assert_awaited_once()
    kwargs = timescale.insert_open_session.await_args.kwargs
    assert kwargs["station_id"] == "LEGACY-CP-1"
    assert kwargs["vehicle_id"] == "veh-123"
    assert kwargs["driver_id"] == "drv-001"
    assert kwargs["card_id"] == "card-xyz"
    assert isinstance(kwargs["start_time"], datetime)
    assert kwargs["start_time"].tzinfo == timezone.utc


async def test_unknown_rfid_denied_for_authorize_and_start() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    timescale.next_transaction_id = AsyncMock(return_value=111)
    timescale.insert_open_session = AsyncMock()
    timescale.next_charging_profile_id = AsyncMock(return_value=10)
    timescale.insert_telemetry_batch = AsyncMock()

    session = _build_session(timescale)

    authorize = await session._on_authorize("LEGACY-CP-1", "UNKNOWN")
    assert authorize == AuthorizationStatus.invalid

    start = await session._on_transaction_start(
        cp_id="LEGACY-CP-1",
        connector_id=1,
        id_tag="UNKNOWN",
        meter_start=0,
        timestamp="2026-01-01T00:00:00Z",
    )
    assert start == AuthorizationStatus.invalid
    timescale.next_transaction_id.assert_not_awaited()
