"""Tests for VDV 463 handler."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from adapters.vdv463.handler import VDV463Handler
from adapters.vdv463.messages import ValidationMode, VDVMessageEnvelope
from adapters.vdv463.vehicle_resolver import VehicleResolver


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
    assert handler.running == False


@pytest.mark.asyncio
async def test_handle_provide_charging_requests(mock_websocket, mock_connection_manager, mock_config):
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
                    "arrivalTime": "2025-01-28T10:00:00Z",
                    "departureTime": "2025-01-28T14:00:00Z",
                    "minTargetSoc": 0.2,
                    "maxTargetSoc": 0.95,
                }
            ]
        },
    ]
    
    import json
    raw_message = json.dumps(message)
    
    # Mock websocket iteration
    async def mock_iter():
        yield raw_message
    
    mock_websocket.__aiter__ = mock_iter
    
    # Run handler (will process one message then exit)
    try:
        await asyncio.wait_for(handler.run(), timeout=1.0)
    except asyncio.TimeoutError:
        pass  # Expected - handler runs indefinitely
    
    # Verify response was sent
    assert mock_websocket.send.called


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
    
    assert handler.running == False
    assert mock_connection_manager.unregister_connection.called
