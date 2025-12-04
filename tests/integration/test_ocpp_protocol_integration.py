"""
OCPP protocol integration tests.
Tests complete OCPP 2.0.1 protocol flows with real message processing.
"""

import pytest
import pytest_asyncio
import asyncio
import json
import uuid
from datetime import datetime, timezone

from src.websocket_handler.server import OCPPWebSocketServer
from src.websocket_handler.message_handler import MessageHandler
from src.websocket_handler.connection_manager import ConnectionManager
from src.websocket_handler.config import Config


class TestOCPPProtocolIntegration:
    """Integration tests for OCPP protocol flows."""
    
    @pytest_asyncio.fixture
    async def config(self):
        """Create configuration for OCPP tests."""
        return Config(
            timescale={
                "service_url": "postgresql://test:test@localhost:5432/test",
                "host": "localhost",
                "user": "test",
                "password": "test",
                "database": "test",
                "port": 5432
            },
            supabase={
                "url": "https://test.supabase.co",
                "anon_key": "test_anon_key",
                "service_key": "test_service_key",
                "db_host": "localhost",
                "db_port": 5432,
                "db_name": "postgres",
                "db_user": "test",
                "db_password": "test"
            }
        )
    
    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)
    
    @pytest_asyncio.fixture
    async def message_handler(self, connection_manager, config):
        """Create message handler."""
        return MessageHandler(
            connection_manager=connection_manager,
            config=config,
            timescale_client=None  # Mock for protocol tests
        )
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_boot_notification_flow(self, message_handler):
        """Test complete BootNotification flow."""
        # BootNotification request
        boot_request = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": "Test Model",
                    "vendorName": "Test Vendor",
                    "serialNumber": "SN123456",
                    "firmwareVersion": "1.0.0"
                },
                "reason": "PowerUp"
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,  # CALL message
            unique_id="test_unique_id",
            action="BootNotification",
            payload=boot_request["payload"]
        )
        
        assert response is not None
        assert response["status"] == "Accepted"
        assert "currentTime" in response
        assert "interval" in response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_heartbeat_flow(self, message_handler):
        """Test Heartbeat flow."""
        # Heartbeat request
        heartbeat_request = {
            "action": "Heartbeat",
            "payload": {}
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id="test_unique_id",
            action="Heartbeat",
            payload={}
        )
        
        assert response is not None
        assert "currentTime" in response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_status_notification_flow(self, message_handler):
        """Test StatusNotification flow."""
        # StatusNotification request
        status_request = {
            "action": "StatusNotification",
            "payload": {
                "timestamp": "2025-10-16T16:30:00Z",
                "connectorStatus": "Available",
                "evseId": 1,
                "connectorId": 1
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id="test_unique_id",
            action="StatusNotification",
            payload=status_request["payload"]
        )
        
        assert response == {}  # StatusNotification returns empty response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_transaction_event_flow(self, message_handler):
        """Test complete TransactionEvent flow."""
        # Transaction started
        txn_start = {
            "action": "TransactionEvent",
            "payload": {
                "eventType": "Started",
                "timestamp": "2025-10-16T16:30:00Z",
                "triggerReason": "Authorized",
                "seqNo": 1,
                "transactionInfo": {
                    "transactionId": "TXN_001"
                },
                "evse": {
                    "id": 1
                },
                "idToken": {
                    "idToken": "RFID_123456789",
                    "type": "ISO14443"
                }
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="TransactionEvent",
            payload=txn_start["payload"]
        )
        
        assert response == {}  # TransactionEvent returns empty response
        
        # Transaction updated
        txn_update = {
            "action": "TransactionEvent",
            "payload": {
                "eventType": "Updated",
                "timestamp": "2025-10-16T16:35:00Z",
                "triggerReason": "MeterValuePeriodic",
                "seqNo": 2,
                "transactionInfo": {
                    "transactionId": "TXN_001"
                },
                "evse": {
                    "id": 1
                },
                "meterValue": [
                    {
                        "timestamp": "2025-10-16T16:35:00Z",
                        "sampledValue": [
                            {
                                "value": "5000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"}
                            }
                        ]
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="TransactionEvent",
            payload=txn_update["payload"]
        )
        
        assert response == {}  # TransactionEvent returns empty response
        
        # Transaction ended
        txn_end = {
            "action": "TransactionEvent",
            "payload": {
                "eventType": "Ended",
                "timestamp": "2025-10-16T17:00:00Z",
                "triggerReason": "EVDisconnected",
                "seqNo": 3,
                "transactionInfo": {
                    "transactionId": "TXN_001"
                },
                "evse": {
                    "id": 1
                },
                "meterValue": [
                    {
                        "timestamp": "2025-10-16T17:00:00Z",
                        "sampledValue": [
                            {
                                "value": "15000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"}
                            }
                        ]
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="TransactionEvent",
            payload=txn_end["payload"]
        )
        
        assert response == {}  # TransactionEvent returns empty response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_meter_values_flow(self, message_handler):
        """Test MeterValues flow."""
        # MeterValues request
        meter_request = {
            "action": "MeterValues",
            "payload": {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": "2025-10-16T16:30:00Z",
                        "sampledValue": [
                            {
                                "value": "15000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "phase": "L1",
                                "location": "Outlet",
                                "unitOfMeasure": {"unit": "Wh"}
                            },
                            {
                                "value": "7500",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Power.Active.Import",
                                "phase": "L1",
                                "location": "Outlet",
                                "unitOfMeasure": {"unit": "W"}
                            }
                        ]
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="MeterValues",
            payload=meter_request["payload"]
        )
        
        assert response == {}  # MeterValues returns empty response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_authorize_flow(self, message_handler):
        """Test Authorize flow."""
        # Authorize request
        auth_request = {
            "action": "Authorize",
            "payload": {
                "idToken": {
                    "idToken": "RFID_123456789",
                    "type": "ISO14443"
                }
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="Authorize",
            payload=auth_request["payload"]
        )
        
        assert response is not None
        assert "idTokenInfo" in response
        assert response["idTokenInfo"]["status"] == "Accepted"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_variables_flow(self, message_handler):
        """Test GetVariables flow."""
        # GetVariables request
        get_vars_request = {
            "action": "GetVariables",
            "payload": {
                "getVariableData": [
                    {
                        "component": {
                            "name": "EVSE"
                        },
                        "variable": {
                            "name": "AvailabilityState"
                        }
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="GetVariables",
            payload=get_vars_request["payload"]
        )
        
        assert response is not None
        assert response["status"] == "Rejected"  # GetVariables not implemented
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_set_variables_flow(self, message_handler):
        """Test SetVariables flow."""
        # SetVariables request
        set_vars_request = {
            "action": "SetVariables",
            "payload": {
                "setVariableData": [
                    {
                        "component": {
                            "name": "EVSE"
                        },
                        "variable": {
                            "name": "AvailabilityState"
                        },
                        "attributeValue": "Available"
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="SetVariables",
            payload=set_vars_request["payload"]
        )
        
        assert response is not None
        assert response["status"] == "Rejected"  # SetVariables not implemented
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_complete_charging_session_flow(self, message_handler):
        """Test complete charging session flow."""
        # Step 1: Boot notification
        boot_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="BootNotification",
            payload={
                "chargingStation": {
                    "model": "Test Model",
                    "vendorName": "Test Vendor"
                },
                "reason": "PowerUp"
            }
        )
        assert "status" in boot_response
        assert boot_response["status"] == "Accepted"
        
        # Step 2: Status notification (Available)
        status_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="StatusNotification",
            payload={
                "timestamp": "2025-10-16T16:30:00Z",
                "connectorStatus": "Available",
                "evseId": 1,
                "connectorId": 1
            }
        )
        assert status_response == {}
        
        # Step 3: Authorize
        auth_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="Authorize",
            payload={
                "idToken": {
                    "idToken": "RFID_123456789",
                    "type": "ISO14443"
                }
            }
        )
        assert "idTokenInfo" in auth_response
        assert auth_response["idTokenInfo"]["status"] == "Accepted"
        
        # Step 4: Status notification (Occupied)
        status_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="StatusNotification",
            payload={
                "timestamp": "2025-10-16T16:31:00Z",
                "connectorStatus": "Occupied",
                "evseId": 1,
                "connectorId": 1
            }
        )
        assert status_response == {}
        
        # Step 5: Transaction started
        txn_start_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="TransactionEvent",
            payload={
                "eventType": "Started",
                "timestamp": "2025-10-16T16:31:00Z",
                "triggerReason": "Authorized",
                "seqNo": 1,
                "transactionInfo": {
                    "transactionId": "TXN_SESSION_001"
                },
                "evse": {"id": 1},
                "idToken": {
                    "idToken": "RFID_123456789",
                    "type": "ISO14443"
                }
            }
        )
        assert txn_start_response == {}
        
        # Step 6: Meter values during charging
        meter_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="MeterValues",
            payload={
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": "2025-10-16T16:35:00Z",
                        "sampledValue": [
                            {
                                "value": "5000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"}
                            }
                        ]
                    }
                ]
            }
        )
        assert meter_response == {}
        
        # Step 7: Transaction ended
        txn_end_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="TransactionEvent",
            payload={
                "eventType": "Ended",
                "timestamp": "2025-10-16T17:00:00Z",
                "triggerReason": "EVDisconnected",
                "seqNo": 2,
                "transactionInfo": {
                    "transactionId": "TXN_SESSION_001"
                },
                "evse": {"id": 1},
                "meterValue": [
                    {
                        "timestamp": "2025-10-16T17:00:00Z",
                        "sampledValue": [
                            {
                                "value": "15000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"}
                            }
                        ]
                    }
                ]
            }
        )
        assert txn_end_response == {}
        
        # Step 8: Status notification (Available)
        status_response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="StatusNotification",
            payload={
                "timestamp": "2025-10-16T17:00:00Z",
                "connectorStatus": "Available",
                "evseId": 1,
                "connectorId": 1
            }
        )
        assert status_response == {}
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_error_handling_flow(self, message_handler):
        """Test error handling in OCPP flows."""
        # Test malformed message
        malformed_message = {
            "action": "BootNotification",
            "payload": {
                # Missing required fields
            }
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="BootNotification",
            payload={}
        )
        
        # Should handle gracefully
        assert response is not None
        
        # Test unknown action
        unknown_message = {
            "action": "UnknownAction",
            "payload": {}
        }
        
        response = await message_handler.handle_message(
            station_id="TEST_STATION_001",
            message_type_id=2,
            unique_id=str(uuid.uuid4()),
            action="UnknownAction",
            payload={}
        )
        
        # Should handle gracefully
        assert response is not None
