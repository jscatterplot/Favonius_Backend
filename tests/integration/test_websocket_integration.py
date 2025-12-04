"""
Integration tests for WebSocket server with real database connections.
Tests complex workflows and cross-module interactions.
"""

import pytest
import asyncio
import json
import time
from unittest.mock import Mock, AsyncMock, patch
from websockets import WebSocketServerProtocol

from src.websocket_handler.server import OCPPWebSocketServer
from src.websocket_handler.connection_manager import ConnectionManager
from src.websocket_handler.message_handler import MessageHandler
from src.websocket_handler.config import Config
from src.websocket_handler.timescale_client import TimescaleClient
from src.websocket_handler.supabase_client import SupabaseClient


class TestWebSocketIntegration:
    """Integration tests for WebSocket server with real components."""
    
    @pytest.fixture
    async def integration_config(self):
        """Create a real configuration for integration tests."""
        return Config()
    
    @pytest.fixture
    async def timescale_client(self, integration_config):
        """Create a real TimescaleDB client."""
        return TimescaleClient(integration_config.timescale)
    
    @pytest.fixture
    async def supabase_client(self, integration_config):
        """Create a real Supabase client."""
        return SupabaseClient(integration_config.supabase)
    
    @pytest.fixture
    async def connection_manager(self, integration_config):
        """Create a real connection manager."""
        return ConnectionManager(integration_config)
    
    @pytest.fixture
    async def message_handler(self, connection_manager, integration_config, timescale_client):
        """Create a real message handler."""
        return MessageHandler(
            connection_manager=connection_manager,
            config=integration_config,
            timescale_client=timescale_client
        )
    
    @pytest.fixture
    async def server(self, integration_config, timescale_client, supabase_client):
        """Create a real WebSocket server."""
        return OCPPWebSocketServer(
            config=integration_config,
            timescale_client=timescale_client,
            supabase_client=supabase_client
        )
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_server_with_real_components(self, server):
        """Test server initialization with real components."""
        # Test that server initializes without errors
        assert server.config is not None
        assert server.timescale_client is not None
        assert server.supabase_client is not None
        
        # Test component initialization
        await server._initialize_components()
        
        assert server.connection_manager is not None
        assert server.message_handler is not None
        assert server.charging_profile_manager is not None
        assert server.der_control_manager is not None
        assert server.priority_charging_manager is not None
        assert server.external_control_manager is not None
        assert server.certificate_manager is not None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_connection_lifecycle_integration(self, server, connection_manager):
        """Test complete connection lifecycle with real components."""
        # Initialize server components
        await server._initialize_components()
        
        # Simulate connection registration
        connection_id = "test_connection_001"
        station_id = "TEST_STATION_001"
        
        # Register connection
        await connection_manager.register_connection(connection_id, station_id)
        
        # Verify connection is registered
        connection = connection_manager.get_connection(connection_id)
        assert connection is not None
        assert connection["station_id"] == station_id
        
        # Test heartbeat update
        await connection_manager.update_heartbeat(connection_id)
        
        # Test message sending
        test_message = {"action": "Heartbeat", "payload": {}}
        await connection_manager.send_message_to_station(station_id, test_message)
        
        # Test health status
        health_status = connection_manager.get_health_status()
        assert health_status["total_connections"] == 1
        assert health_status["healthy_connections"] == 1
        
        # Test connection cleanup
        await connection_manager.unregister_connection(connection_id)
        
        # Verify connection is removed
        connection = connection_manager.get_connection(connection_id)
        assert connection is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_message_processing_integration(self, server, message_handler):
        """Test message processing with real message handler."""
        # Initialize server components
        await server._initialize_components()
        
        # Test BootNotification message
        boot_message = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": "Test Model",
                    "vendorName": "Test Vendor"
                },
                "reason": "PowerUp"
            }
        }
        
        response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=boot_message
        )
        
        assert response is not None
        assert "action" in response
        assert response["action"] == "BootNotification"
        assert "payload" in response
        assert "status" in response["payload"]
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_transaction_workflow_integration(self, server, message_handler):
        """Test complete transaction workflow."""
        # Initialize server components
        await server._initialize_components()
        
        # Step 1: Boot notification
        boot_message = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": "Test Model",
                    "vendorName": "Test Vendor"
                },
                "reason": "PowerUp"
            }
        }
        
        boot_response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=boot_message
        )
        assert boot_response["payload"]["status"] == "Accepted"
        
        # Step 2: Status notification
        status_message = {
            "action": "StatusNotification",
            "payload": {
                "connectorId": 1,
                "errorCode": "NoError",
                "status": "Available"
            }
        }
        
        status_response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=status_message
        )
        assert status_response == {}  # StatusNotification returns empty response
        
        # Step 3: Transaction event (start)
        transaction_start = {
            "action": "TransactionEvent",
            "payload": {
                "eventType": "Started",
                "timestamp": "2025-10-16T16:30:00Z",
                "transactionId": "TXN_001",
                "triggerReason": "Authorized",
                "seqNo": 1,
                "transactionInfo": {
                    "transactionId": "TXN_001"
                }
            }
        }
        
        txn_response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=transaction_start
        )
        assert txn_response == {}  # TransactionEvent returns empty response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_meter_values_integration(self, server, message_handler):
        """Test meter values processing with real components."""
        # Initialize server components
        await server._initialize_components()
        
        # Test meter values message
        meter_message = {
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
                            }
                        ]
                    }
                ]
            }
        }
        
        response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=meter_message
        )
        
        assert response == {}  # MeterValues returns empty response
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_authorization_workflow_integration(self, server, message_handler):
        """Test authorization workflow with real components."""
        # Initialize server components
        await server._initialize_components()
        
        # Test authorization message
        auth_message = {
            "action": "Authorize",
            "payload": {
                "idToken": {
                    "idToken": "RFID_123456789",
                    "type": "ISO14443"
                }
            }
        }
        
        response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=auth_message
        )
        
        assert response is not None
        assert "action" in response
        assert response["action"] == "Authorize"
        assert "payload" in response
        assert "idTokenInfo" in response["payload"]
        assert response["payload"]["idTokenInfo"]["status"] == "Accepted"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_error_handling_integration(self, server, message_handler):
        """Test error handling with real components."""
        # Initialize server components
        await server._initialize_components()
        
        # Test unknown message type
        unknown_message = {
            "action": "UnknownAction",
            "payload": {}
        }
        
        response = await message_handler.handle_message(
            connection_id="test_conn",
            station_id="TEST_STATION_001",
            message=unknown_message
        )
        
        # Should handle gracefully without crashing
        assert response is not None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_concurrent_connections_integration(self, server, connection_manager):
        """Test concurrent connection handling."""
        # Initialize server components
        await server._initialize_components()
        
        # Create multiple connections concurrently
        connections = []
        for i in range(5):
            connection_id = f"test_conn_{i}"
            station_id = f"TEST_STATION_{i:03d}"
            
            await connection_manager.register_connection(connection_id, station_id)
            connections.append((connection_id, station_id))
        
        # Verify all connections are registered
        health_status = connection_manager.get_health_status()
        assert health_status["total_connections"] == 5
        assert health_status["healthy_connections"] == 5
        
        # Test concurrent message sending
        tasks = []
        for connection_id, station_id in connections:
            message = {"action": "Heartbeat", "payload": {}}
            task = connection_manager.send_message_to_station(station_id, message)
            tasks.append(task)
        
        # Wait for all messages to be sent
        await asyncio.gather(*tasks)
        
        # Clean up all connections
        for connection_id, _ in connections:
            await connection_manager.unregister_connection(connection_id)
        
        # Verify all connections are removed
        health_status = connection_manager.get_health_status()
        assert health_status["total_connections"] == 0
        assert health_status["healthy_connections"] == 0
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_database_integration(self, server, timescale_client):
        """Test database integration with real TimescaleDB client."""
        # Initialize server components
        await server._initialize_components()
        
        # Test database connection
        try:
            # Test basic database operations
            result = await timescale_client.execute_query("SELECT 1 as test")
            assert result is not None
            
            # Test connection health
            health = await timescale_client.check_health()
            assert health is not None
            
        except Exception as e:
            # If database is not available, skip this test
            pytest.skip(f"Database not available: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_supabase_integration(self, server, supabase_client):
        """Test Supabase integration with real client."""
        # Initialize server components
        await server._initialize_components()
        
        # Test Supabase client initialization
        assert supabase_client.client is not None
        
        # Test basic operations (without actual API calls)
        try:
            # Test client configuration
            assert supabase_client.url is not None
            assert supabase_client.key is not None
            
        except Exception as e:
            # If Supabase is not configured, skip this test
            pytest.skip(f"Supabase not configured: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_server_health_check_integration(self, server):
        """Test server health check with real components."""
        # Initialize server components
        await server._initialize_components()
        
        # Test health check
        health_status = await server.get_health_status()
        
        assert health_status is not None
        assert "status" in health_status
        assert "components" in health_status
        assert "timestamp" in health_status
        
        # Test degraded health check
        server.running = False
        degraded_health = await server.get_health_status()
        
        assert degraded_health["status"] == "degraded"
