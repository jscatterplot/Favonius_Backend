"""Unit tests for OCPP message handler."""

import asyncio
import os

# Import test dependencies
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import Config
from websocket_handler.message_handler import MessageHandler


class TestMessageHandler:
    """Test OCPP message handler functionality."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        config = Mock(spec=Config)
        config.websocket = Mock()
        config.websocket.heartbeat_interval = 30
        return config

    @pytest.fixture
    def mock_connection_manager(self):
        """Create mock connection manager."""
        manager = Mock()
        manager.send_message = AsyncMock()
        manager.get_connection = AsyncMock()
        return manager

    @pytest.fixture
    def mock_timescale_client(self):
        """Create mock TimescaleDB client."""
        client = Mock()
        client.store_device_component = AsyncMock()
        client.store_device_variable = AsyncMock()
        client.store_transaction = AsyncMock()
        client.store_transaction_event = AsyncMock()
        client.store_meter_value = AsyncMock()
        client.store_charging_profile = AsyncMock()
        client.lookup_id_tag = AsyncMock(
            return_value={
                "source": "rfid_card",
                "vehicle_id": "vehicle-1",
                "card_id": "card-1",
                "depot_id": "depot-1",
            }
        )
        return client

    @pytest.fixture
    def message_handler(self, mock_connection_manager, mock_config, mock_timescale_client):
        """Create message handler instance."""
        return MessageHandler(mock_connection_manager, mock_config, mock_timescale_client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_message_handler_initialization(self, message_handler):
        """Test message handler initialization."""
        assert message_handler.connection_manager is not None
        assert message_handler.config is not None
        assert message_handler.timescale_client is not None
        assert len(message_handler.handlers) > 0
        assert "BootNotification" in message_handler.handlers
        assert "StatusNotification" in message_handler.handlers
        assert "TransactionEvent" in message_handler.handlers

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_boot_notification(self, message_handler):
        """Test BootNotification message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "chargingStation": {
                "model": "TestModel",
                "vendorName": "TestVendor",
                "serialNumber": "SN123456",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }
        unique_id = "1"

        response = await message_handler._handle_boot_notification(station_id, payload, unique_id)

        assert response["status"] == "Accepted"
        assert "currentTime" in response
        assert response["interval"] == 30
        assert response["statusInfo"]["reasonCode"] == "NoError"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_status_notification(self, message_handler):
        """Test StatusNotification message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectorStatus": "Available",
            "evseId": 1,
            "connectorId": 1,
            "errorCode": "NoError",
        }
        unique_id = "1"

        response = await message_handler._handle_status_notification(station_id, payload, unique_id)

        assert response == {}  # StatusNotification returns empty dict

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_transaction_event(self, message_handler):
        """Test TransactionEvent message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "eventType": "Started",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": "Authorized",
            "seqNo": 1,
            "transactionInfo": {"transactionId": "TXN123456", "chargingState": "Charging"},
            "idToken": {"idToken": "AUTH123", "type": "KeyCode"},
        }
        unique_id = "1"

        response = await message_handler._handle_transaction_event(station_id, payload, unique_id)

        assert response == {}  # TransactionEvent returns empty dict

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_meter_values(self, message_handler):
        """Test MeterValues message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "evseId": 1,
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "22.5",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": {"unit": "kWh"},
                        }
                    ],
                }
            ],
        }
        unique_id = "1"

        response = await message_handler._handle_meter_values(station_id, payload, unique_id)

        assert response == {}  # MeterValues returns empty dict

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_heartbeat(self, message_handler):
        """Test Heartbeat message handling."""
        station_id = "TEST_STATION_001"
        payload = {}
        unique_id = "1"

        response = await message_handler._handle_heartbeat(station_id, payload, unique_id)

        assert response["currentTime"] is not None
        assert isinstance(response["currentTime"], str)

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_authorize(self, message_handler):
        """Test Authorize message handling."""
        station_id = "TEST_STATION_001"
        payload = {"idToken": {"idToken": "AUTH123", "type": "KeyCode"}}
        unique_id = "1"

        response = await message_handler._handle_authorize(station_id, payload, unique_id)

        assert "idTokenInfo" in response
        assert response["idTokenInfo"]["status"] == "Accepted"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_ev_charging_needs(self, message_handler):
        """Test NotifyEVChargingNeeds message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "evseId": 1,
            "chargingNeeds": {
                "acChargingParameters": {
                    "energyAmount": 22.5,
                    "evMinCurrent": 6,
                    "evMaxCurrent": 32,
                    "evMaxVoltage": 400,
                },
                "dcChargingParameters": {
                    "energyAmount": 22.5,
                    "evMaxCurrent": 200,
                    "evMaxVoltage": 800,
                },
            },
        }
        unique_id = "1"

        response = await message_handler._handle_ev_charging_needs(station_id, payload, unique_id)

        assert response["status"] == "Accepted"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_ev_charging_schedule(self, message_handler):
        """Test NotifyEVChargingSchedule message handling."""
        station_id = "TEST_STATION_001"
        payload = {
            "evseId": 1,
            "chargingSchedule": {
                "id": 1,
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 22.0}],
            },
        }
        unique_id = "1"

        response = await message_handler._handle_ev_charging_schedule(
            station_id, payload, unique_id
        )

        assert response["status"] == "Accepted"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_data_transfer(self, message_handler):
        """Test DataTransfer message handling."""
        station_id = "TEST_STATION_001"
        payload = {"vendorId": "TestVendor", "messageId": "TestMessage", "data": "Test data"}
        unique_id = "1"

        response = await message_handler._handle_data_transfer(station_id, payload, unique_id)

        assert response["status"] == "Accepted"
        assert "data" in response

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_unknown_message(self, message_handler):
        """Test handling of unknown message types."""
        station_id = "TEST_STATION_001"
        message_type_id = 2
        unique_id = "1"
        action = "UnknownAction"
        payload = {}

        response = await message_handler.handle_message(
            station_id, message_type_id, unique_id, action, payload
        )

        assert response["status"] == "Rejected"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_non_call_message(self, message_handler):
        """Test handling of non-CALL messages."""
        station_id = "TEST_STATION_001"
        message_type_id = 3  # CALLRESULT
        unique_id = "1"
        action = "BootNotification"
        payload = {}

        response = await message_handler.handle_message(
            station_id, message_type_id, unique_id, action, payload
        )

        assert response is None

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_message_processing_error_handling(self, message_handler):
        """Test error handling in message processing."""
        station_id = "TEST_STATION_001"
        message_type_id = 2
        unique_id = "1"
        action = "BootNotification"
        payload = {}

        # Mock handler to raise exception
        original_handler = message_handler.handlers["BootNotification"]
        message_handler.handlers["BootNotification"] = AsyncMock(
            side_effect=Exception("Test error")
        )

        try:
            response = await message_handler.handle_message(
                station_id, message_type_id, unique_id, action, payload
            )
            assert response["status"] == "Rejected"
        finally:
            # Restore original handler
            message_handler.handlers["BootNotification"] = original_handler

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_log_message_event(self, message_handler):
        """Test message event logging."""
        station_id = "TEST_STATION_001"
        action = "BootNotification"
        payload = {"test": "data"}
        response = {"status": "Accepted"}

        # Should not raise exception
        await message_handler._log_message_event(station_id, action, payload, response)

        # Verify it completes without error
        assert True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_transaction_event_with_meter_values(self, message_handler):
        """Test TransactionEvent with meter values."""
        station_id = "TEST_STATION_001"
        payload = {
            "eventType": "Updated",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": "MeterValuePeriodic",
            "seqNo": 2,
            "transactionInfo": {"transactionId": "TXN123456", "chargingState": "Charging"},
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "25.0",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": {"unit": "kWh"},
                        }
                    ],
                }
            ],
        }
        unique_id = "1"

        response = await message_handler._handle_transaction_event(station_id, payload, unique_id)

        assert response == {}  # TransactionEvent returns empty dict

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authorize_with_certificate(self, message_handler):
        """Test Authorize with certificate-based authentication."""
        station_id = "TEST_STATION_001"
        payload = {
            "idToken": {"idToken": "CERT123", "type": "Certificate"},
            "certificateHashData": {
                "hashAlgorithm": "SHA256",
                "issuerNameHash": "hash1",
                "issuerKeyHash": "hash2",
                "serialNumber": "SN123",
            },
        }
        unique_id = "1"

        response = await message_handler._handle_authorize(station_id, payload, unique_id)

        assert "idTokenInfo" in response
        assert response["idTokenInfo"]["status"] == "Accepted"

    @pytest.mark.asyncio
    @pytest.mark.timeout(15)
    async def test_concurrent_message_handling(self, message_handler):
        """Test concurrent message handling."""
        station_id = "TEST_STATION_001"

        # Create multiple messages
        messages = []
        for i in range(5):
            message = {
                "station_id": station_id,
                "message_type_id": 2,
                "unique_id": str(i),
                "action": "Heartbeat",
                "payload": {},
            }
            messages.append(message)

        # Process messages concurrently
        tasks = []
        for msg in messages:
            task = asyncio.create_task(
                message_handler.handle_message(
                    msg["station_id"],
                    msg["message_type_id"],
                    msg["unique_id"],
                    msg["action"],
                    msg["payload"],
                )
            )
            tasks.append(task)

        responses = await asyncio.gather(*tasks)

        # All messages should be processed successfully
        assert len(responses) == 5
        for response in responses:
            assert response is not None
            assert "currentTime" in response

    def test_handler_registration(self, message_handler):
        """Test that all expected handlers are registered."""
        expected_handlers = [
            "BootNotification",
            "StatusNotification",
            "TransactionEvent",
            "MeterValues",
            "NotifyEVChargingNeeds",
            "NotifyEVChargingSchedule",
            "Heartbeat",
            "Authorize",
            "DataTransfer",
        ]

        for handler_name in expected_handlers:
            assert handler_name in message_handler.handlers
            assert callable(message_handler.handlers[handler_name])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
