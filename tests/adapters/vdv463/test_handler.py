"""Tests for VDV 463 handler."""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

pytest.importorskip("websockets")

from adapters.vdv463.handler import VDV463Handler
from adapters.vdv463.messages import ValidationMode


@pytest.fixture
def mock_websocket():
    """Create mock WebSocket."""
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 12345)
    ws.closed = False
    return ws


@pytest.fixture
def mock_connection_manager():
    """Create mock connection manager."""
    cm = AsyncMock()
    cm.check_message_rate_limit = AsyncMock(return_value=(True, ""))
    cm.record_message_received = AsyncMock()
    cm.register_connection = AsyncMock()
    cm.unregister_connection = AsyncMock()
    return cm


@pytest.fixture
def mock_config():
    """Create mock config."""
    config = MagicMock()
    config.vdv463.validation_mode = "hard"
    config.vdv463.default_depot_id = "depot_001"
    config.default_depot_id = "depot_001"
    config.websocket.max_message_size = 1048576
    return config


@pytest.mark.asyncio
async def test_handler_initialization(mock_websocket, mock_connection_manager, mock_config):
    """Test handler initialization."""
    handler = VDV463Handler(
        presystem_id="presystem_001",
        websocket=mock_websocket,
        connection_manager=mock_connection_manager,
        config=mock_config,
    )

    assert handler.presystem_id == "presystem_001"
    assert handler.validation_mode == ValidationMode.HARD
    assert not handler.running


@pytest.mark.asyncio
async def test_handle_provide_charging_requests(
    mock_websocket, mock_connection_manager, mock_config
):
    """Test handling ProvideChargingRequests message."""
    handler = VDV463Handler(
        presystem_id="presystem_001",
        websocket=mock_websocket,
        connection_manager=mock_connection_manager,
        config=mock_config,
    )

    message = [
        1,  # Request
        "BMS",
        "presystem_001",
        datetime.utcnow().isoformat() + "Z",
        "msg-123",
        "ProvideChargingRequests",
        {
            "chargingRequestList": [
                {
                    "vehicleId": "bus_101",
                    "chargingRequestId": "cr-001",
                    "chargingRequestData": {
                        "expectedArrivalTimeAtChargingPoint": "2025-01-28T10:00:00Z",
                        "requestedTimeForDeparture": "2025-01-28T14:00:00Z",
                        "minTargetSoc": 20,
                        "maxTargetSoc": 95,
                    },
                    "chargingPriority": 1,
                }
            ]
        },
    ]

    import json

    raw_message = json.dumps(message)

    # Mock websocket recv loop: process one message then close cleanly
    mock_websocket.recv = AsyncMock(
        side_effect=[
            raw_message,
            websockets.exceptions.ConnectionClosedOK(
                None,
                None,
            ),
        ]
    )

    # Run handler (will process one message then exit on simulated close)
    await asyncio.wait_for(handler.run(), timeout=1.0)

    # Verify response was sent
    assert mock_websocket.send.called


@pytest.mark.asyncio
async def test_message_too_large_rejected(mock_websocket, mock_connection_manager, mock_config):
    """Oversize message (> 1 MB) is rejected with MessageTooLarge error before parsing."""
    handler = VDV463Handler(
        presystem_id="presystem_001",
        websocket=mock_websocket,
        connection_manager=mock_connection_manager,
        config=mock_config,
    )
    # Message > 1 MB (PRD Section 10)
    big_payload = "x" * (1048576 + 1)
    await handler._handle_message(big_payload)
    assert mock_websocket.send.called
    sent = json.loads(mock_websocket.send.call_args[0][0])
    assert sent[0] == 3  # Error
    assert sent[6].get("errorCode") == "MessageTooLarge"


@pytest.mark.asyncio
async def test_rate_limit_exceeded(mock_websocket, mock_connection_manager, mock_config):
    """Test rate limit handling."""
    mock_connection_manager.check_message_rate_limit = AsyncMock(
        return_value=(False, "Rate limit exceeded")
    )

    handler = VDV463Handler(
        presystem_id="presystem_001",
        websocket=mock_websocket,
        connection_manager=mock_connection_manager,
        config=mock_config,
    )

    message = [
        1,
        "BMS",
        "presystem_001",
        datetime.utcnow().isoformat() + "Z",
        "msg-123",
        "ProvideChargingRequests",
        {"chargingRequestList": []},
    ]

    import json

    await handler._handle_message(json.dumps(message))

    # Should send error response
    assert mock_websocket.send.called


@pytest.mark.asyncio
async def test_cleanup(mock_websocket, mock_connection_manager, mock_config):
    """Test handler cleanup."""
    handler = VDV463Handler(
        presystem_id="presystem_001",
        websocket=mock_websocket,
        connection_manager=mock_connection_manager,
        config=mock_config,
    )

    handler.running = True
    handler._charging_info_task = asyncio.create_task(asyncio.sleep(60))

    await handler._cleanup()

    assert not handler.running
    assert mock_connection_manager.unregister_connection.called
