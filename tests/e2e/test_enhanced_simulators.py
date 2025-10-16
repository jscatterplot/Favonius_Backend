"""Enhanced end-to-end tests with CitrineOS simulator and failure scenarios."""

import pytest
import asyncio
import json
import websockets
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta
import logging

# Import test dependencies
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from tests.e2e.citrineos_simulator import CitrineOSSimulator, CitrineOSFleetSimulator
from websocket_handler.server import WebSocketServer
from websocket_handler.config import Config


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestCitrineOSEnhanced:
    """Enhanced CitrineOS simulation tests with failure scenarios."""

    @pytest.fixture
    async def test_server(self):
        """Start test WebSocket server."""
        config = Config()
        server = WebSocketServer(config)
        
        try:
            await server.start()
            yield server
        finally:
            await server.stop()

    @pytest.fixture
    def simulator(self):
        """Create CitrineOS simulator."""
        return CitrineOSSimulator("ENHANCED_TEST_001", "ws://localhost:9000")

    @pytest.mark.asyncio
    async def test_connection_failure_scenarios(self, test_server):
        """Test various connection failure scenarios."""
        
        # Test 1: Connection with invalid URL
        invalid_simulator = CitrineOSSimulator("INVALID_TEST", "ws://localhost:9999")
        
        with pytest.raises(Exception):
            await invalid_simulator.connect()
        
        # Test 2: Connection timeout
        timeout_simulator = CitrineOSSimulator("TIMEOUT_TEST", "ws://localhost:9000")
        
        # Mock websockets.connect to simulate timeout
        with patch('websockets.connect', side_effect=asyncio.TimeoutError()):
            with pytest.raises(asyncio.TimeoutError):
                await timeout_simulator.connect()
        
        # Test 3: Connection refused
        refused_simulator = CitrineOSSimulator("REFUSED_TEST", "ws://localhost:9001")
        
        with pytest.raises(Exception):
            await refused_simulator.connect()

    @pytest.mark.asyncio
    async def test_message_failure_scenarios(self, test_server, simulator):
        """Test various message failure scenarios."""
        
        await simulator.connect()
        
        try:
            # Test 1: Invalid message format
            invalid_message = "invalid json"
            await simulator.websocket.send(invalid_message)
            
            # Should not crash the server
            assert simulator.connected is True
            
            # Test 2: Missing required fields
            incomplete_message = [2, "1", "BootNotification", {}]  # Missing chargingStation
            await simulator.websocket.send(json.dumps(incomplete_message))
            
            # Should receive error response
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            assert response_data[0] == 4  # Error message type
            
            # Test 3: Invalid message type
            invalid_type_message = [99, "1", "InvalidAction", {}]
            await simulator.websocket.send(json.dumps(invalid_type_message))
            
            # Should receive error response
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            assert response_data[0] == 4  # Error message type
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_protocol_compliance_scenarios(self, test_server, simulator):
        """Test OCPP protocol compliance scenarios."""
        
        await simulator.connect()
        
        try:
            # Test 1: Boot notification with different OCPP versions
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"
            
            # Test 2: Heartbeat compliance
            heartbeat_result = await simulator.heartbeat()
            assert heartbeat_result[2]["status"] == "Accepted"
            assert "currentTime" in heartbeat_result[2]
            
            # Test 3: Status notification compliance
            status_result = await simulator.status_notification(1, "Available")
            assert status_result is None  # StatusNotification has no response
            
            # Test 4: Meter values compliance
            meter_result = await simulator.meter_values(1, 22.5)
            assert meter_result is None  # MeterValues has no response
            
            # Test 5: Transaction event compliance
            transaction_result = await simulator.transaction_event("Started", "TXN123")
            assert transaction_result[2]["status"] == "Accepted"
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_error_recovery_scenarios(self, test_server, simulator):
        """Test error recovery scenarios."""
        
        await simulator.connect()
        
        try:
            # Test 1: Recover from invalid message
            invalid_message = "invalid json"
            await simulator.websocket.send(invalid_message)
            
            # Should still be able to send valid messages
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"
            
            # Test 2: Recover from connection interruption
            # Simulate connection interruption by closing and reopening
            await simulator.disconnect()
            await asyncio.sleep(0.1)  # Small delay
            await simulator.connect()
            
            # Should be able to continue normal operation
            heartbeat_result = await simulator.heartbeat()
            assert heartbeat_result[2]["status"] == "Accepted"
            
            # Test 3: Recover from server restart
            # This would require restarting the server, which is complex in tests
            # Instead, test that the simulator handles disconnection gracefully
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_concurrent_connections(self, test_server):
        """Test multiple concurrent connections."""
        
        # Create multiple simulators
        simulators = []
        for i in range(5):
            simulator = CitrineOSSimulator(f"CONCURRENT_TEST_{i+1:03d}", "ws://localhost:9000")
            simulators.append(simulator)
        
        try:
            # Connect all simulators concurrently
            await asyncio.gather(*[sim.connect() for sim in simulators])
            
            # Boot all simulators concurrently
            boot_results = await asyncio.gather(*[sim.boot_notification() for sim in simulators])
            
            # Verify all boots were successful
            for result in boot_results:
                assert result[2]["status"] == "Accepted"
            
            # Send heartbeats concurrently
            heartbeat_results = await asyncio.gather(*[sim.heartbeat() for sim in simulators])
            
            # Verify all heartbeats were successful
            for result in heartbeat_results:
                assert result[2]["status"] == "Accepted"
            
            # Test concurrent meter values
            meter_results = await asyncio.gather(*[
                sim.meter_values(1, 22.5 + i) for i, sim in enumerate(simulators)
            ])
            
            # Meter values should not return responses
            for result in meter_results:
                assert result is None
            
        finally:
            # Disconnect all simulators
            await asyncio.gather(*[sim.disconnect() for sim in simulators])

    @pytest.mark.asyncio
    async def test_scaling_scenarios(self, test_server):
        """Test scaling scenarios with many connections."""
        
        # Create fleet simulator with many stations
        fleet = CitrineOSFleetSimulator(num_stations=20, server_url="ws://localhost:9000")
        
        try:
            # Connect all stations
            await fleet.connect_all()
            
            # Boot all stations
            boot_results = await fleet.boot_all_stations()
            successful_boots = sum(1 for result in boot_results if result[2].get("status") == "Accepted")
            
            # Should have high success rate
            assert successful_boots >= 18  # Allow for some failures
            
            # Test concurrent operations
            await fleet.test_device_configuration()
            await fleet.test_charging_profiles()
            await fleet.start_charging_sessions(10)
            
            # Send meter values from all stations
            energy_values = [22.5 + i for i in range(20)]
            await fleet.send_meter_values(energy_values)
            
            # Test monitoring on all stations
            await fleet.test_monitoring()
            
            # Test display messages on all stations
            await fleet.test_display_messages()
            
            # Test privacy compliance on all stations
            await fleet.test_privacy_compliance()
            
            # Send heartbeats from all stations
            await fleet.heartbeat_all()
            
        finally:
            await fleet.disconnect_all()

    @pytest.mark.asyncio
    async def test_stress_scenarios(self, test_server):
        """Test stress scenarios with rapid message sending."""
        
        simulator = CitrineOSSimulator("STRESS_TEST_001", "ws://localhost:9000")
        
        await simulator.connect()
        
        try:
            # Boot notification
            await simulator.boot_notification()
            
            # Send rapid heartbeats
            for i in range(10):
                heartbeat_result = await simulator.heartbeat()
                assert heartbeat_result[2]["status"] == "Accepted"
                await asyncio.sleep(0.1)  # Small delay between heartbeats
            
            # Send rapid meter values
            for i in range(20):
                meter_result = await simulator.meter_values(1, 22.5 + i)
                assert meter_result is None
                await asyncio.sleep(0.05)  # Small delay between meter values
            
            # Send rapid status notifications
            for i in range(5):
                status_result = await simulator.status_notification(1, "Available")
                assert status_result is None
                await asyncio.sleep(0.1)  # Small delay between status notifications
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_data_integrity_scenarios(self, test_server, simulator):
        """Test data integrity scenarios."""
        
        await simulator.connect()
        
        try:
            # Boot notification
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"
            
            # Test transaction flow with data integrity
            start_result = await simulator.request_start_transaction(1)
            assert start_result[2]["status"] == "Accepted"
            transaction_id = start_result[2]["transactionId"]
            
            # Transaction event with correct transaction ID
            txn_result = await simulator.transaction_event("Started", transaction_id)
            assert txn_result[2]["status"] == "Accepted"
            
            # Meter values with correct transaction ID
            meter_result = await simulator.meter_values(1, 22.5, transaction_id)
            assert meter_result is None
            
            # Stop transaction with correct transaction ID
            stop_result = await simulator.request_stop_transaction(transaction_id)
            assert stop_result[2]["status"] == "Accepted"
            
            # Transaction ended with correct transaction ID
            end_result = await simulator.transaction_event("Ended", transaction_id, "EVDisconnected")
            assert end_result[2]["status"] == "Accepted"
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_security_scenarios(self, test_server, simulator):
        """Test security scenarios."""
        
        await simulator.connect()
        
        try:
            # Test 1: Unauthorized access attempts
            # Try to access without proper authentication
            unauthorized_message = [2, "1", "GetVariables", {
                "getVariableData": [{
                    "component": {"name": "ChargingStation"},
                    "variable": {"name": "VendorName"}
                }]
            }]
            
            await simulator.websocket.send(json.dumps(unauthorized_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            
            # Should receive error or rejection
            assert response_data[0] in [3, 4]  # Response or error
            
            # Test 2: Malicious payload attempts
            malicious_message = [2, "1", "SetVariables", {
                "setVariableData": [{
                    "component": {"name": "ChargingStation"},
                    "variable": {"name": "VendorName"},
                    "attributeValue": "<script>alert('xss')</script>"
                }]
            }]
            
            await simulator.websocket.send(json.dumps(malicious_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            
            # Should handle malicious payload safely
            assert response_data[0] in [3, 4]  # Response or error
            
            # Test 3: Oversized message
            oversized_payload = {"data": "x" * 100000}  # Large payload
            oversized_message = [2, "1", "DataTransfer", oversized_payload]
            
            await simulator.websocket.send(json.dumps(oversized_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            
            # Should handle oversized message
            assert response_data[0] in [3, 4]  # Response or error
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_performance_scenarios(self, test_server):
        """Test performance scenarios."""
        
        simulator = CitrineOSSimulator("PERFORMANCE_TEST_001", "ws://localhost:9000")
        
        await simulator.connect()
        
        try:
            # Boot notification
            start_time = datetime.now()
            boot_result = await simulator.boot_notification()
            boot_time = (datetime.now() - start_time).total_seconds()
            
            assert boot_result[2]["status"] == "Accepted"
            assert boot_time < 1.0  # Should respond within 1 second
            
            # Heartbeat performance
            start_time = datetime.now()
            heartbeat_result = await simulator.heartbeat()
            heartbeat_time = (datetime.now() - start_time).total_seconds()
            
            assert heartbeat_result[2]["status"] == "Accepted"
            assert heartbeat_time < 0.5  # Should respond within 500ms
            
            # Meter values performance
            start_time = datetime.now()
            meter_result = await simulator.meter_values(1, 22.5)
            meter_time = (datetime.now() - start_time).total_seconds()
            
            assert meter_result is None
            assert meter_time < 0.5  # Should respond within 500ms
            
            # Status notification performance
            start_time = datetime.now()
            status_result = await simulator.status_notification(1, "Available")
            status_time = (datetime.now() - start_time).total_seconds()
            
            assert status_result is None
            assert status_time < 0.5  # Should respond within 500ms
            
        finally:
            await simulator.disconnect()


class TestEVerestSimulation:
    """Test EVerest simulator integration."""

    @pytest.fixture
    async def test_server(self):
        """Start test WebSocket server."""
        config = Config()
        server = WebSocketServer(config)
        
        try:
            await server.start()
            yield server
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_everest_log_parsing(self, test_server):
        """Test EVerest log parsing and message extraction."""
        
        # Mock EVerest log data
        mock_log_data = [
            {
                "timestamp": "2023-01-01T00:00:00Z",
                "message_type": "BootNotification",
                "payload": {
                    "chargingStation": {
                        "model": "EVerest-Sim",
                        "vendorName": "EVerest",
                        "serialNumber": "EV001",
                        "firmwareVersion": "1.0.0"
                    },
                    "reason": "PowerUp"
                }
            },
            {
                "timestamp": "2023-01-01T00:00:30Z",
                "message_type": "Heartbeat",
                "payload": {}
            },
            {
                "timestamp": "2023-01-01T00:01:00Z",
                "message_type": "StatusNotification",
                "payload": {
                    "timestamp": "2023-01-01T00:01:00Z",
                    "connectorStatus": "Available",
                    "evseId": 1,
                    "connectorId": 1,
                    "errorCode": "NoError"
                }
            }
        ]
        
        # Create simulator and connect
        simulator = CitrineOSSimulator("EVEREST_TEST_001", "ws://localhost:9000")
        await simulator.connect()
        
        try:
            # Replay log messages
            for log_entry in mock_log_data:
                message = [
                    2,  # OCPP 2.0.1 message type
                    "1",
                    log_entry["message_type"],
                    log_entry["payload"]
                ]
                
                await simulator.websocket.send(json.dumps(message))
                
                # Wait for response
                response = await simulator.websocket.recv()
                response_data = json.loads(response)
                
                # Verify response
                assert response_data[0] in [3, 4]  # Response or error
                
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_everest_fleet_simulation(self, test_server):
        """Test EVerest fleet simulation."""
        
        # Mock EVerest fleet data
        fleet_data = []
        for i in range(5):
            station_data = {
                "station_id": f"EVEREST_STATION_{i+1:03d}",
                "model": "EVerest-Fleet",
                "vendor": "EVerest",
                "serial": f"EV{i+1:03d}",
                "firmware": "1.0.0"
            }
            fleet_data.append(station_data)
        
        # Create simulators for fleet
        simulators = []
        for station_data in fleet_data:
            simulator = CitrineOSSimulator(station_data["station_id"], "ws://localhost:9000")
            simulators.append(simulator)
        
        try:
            # Connect all simulators
            await asyncio.gather(*[sim.connect() for sim in simulators])
            
            # Boot all stations
            boot_results = await asyncio.gather(*[sim.boot_notification() for sim in simulators])
            
            # Verify all boots were successful
            for result in boot_results:
                assert result[2]["status"] == "Accepted"
            
            # Send heartbeats from all stations
            heartbeat_results = await asyncio.gather(*[sim.heartbeat() for sim in simulators])
            
            # Verify all heartbeats were successful
            for result in heartbeat_results:
                assert result[2]["status"] == "Accepted"
            
        finally:
            # Disconnect all simulators
            await asyncio.gather(*[sim.disconnect() for sim in simulators])


class TestMobileHouseSimulation:
    """Test MobileHouse Python OCPP library simulation."""

    @pytest.fixture
    async def test_server(self):
        """Start test WebSocket server."""
        config = Config()
        server = WebSocketServer(config)
        
        try:
            await server.start()
            yield server
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_mobilehouse_message_generation(self, test_server):
        """Test MobileHouse message generation."""
        
        # Mock MobileHouse message generation
        def generate_boot_notification(station_id):
            return [2, "1", "BootNotification", {
                "chargingStation": {
                    "model": "MobileHouse-Sim",
                    "vendorName": "MobileHouse",
                    "serialNumber": station_id,
                    "firmwareVersion": "1.0.0"
                },
                "reason": "PowerUp"
            }]
        
        def generate_heartbeat():
            return [2, "2", "Heartbeat", {}]
        
        def generate_meter_values(evse_id, energy):
            return [2, "3", "MeterValues", {
                "evseId": evse_id,
                "meterValue": [{
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [{
                        "value": str(energy),
                        "context": "Sample.Periodic",
                        "format": "Raw",
                        "measurand": "Energy.Active.Import.Register",
                        "unitOfMeasure": "kWh"
                    }]
                }]
            }]
        
        # Create simulator and connect
        simulator = CitrineOSSimulator("MOBILEHOUSE_TEST_001", "ws://localhost:9000")
        await simulator.connect()
        
        try:
            # Generate and send messages
            boot_message = generate_boot_notification("MH001")
            await simulator.websocket.send(json.dumps(boot_message))
            
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            assert response_data[2]["status"] == "Accepted"
            
            # Heartbeat
            heartbeat_message = generate_heartbeat()
            await simulator.websocket.send(json.dumps(heartbeat_message))
            
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            assert response_data[2]["status"] == "Accepted"
            
            # Meter values
            meter_message = generate_meter_values(1, 22.5)
            await simulator.websocket.send(json.dumps(meter_message))
            
            response = await simulator.websocket.recv()
            response_data = json.loads(response)
            assert response_data[2]["status"] == "Accepted"
            
        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_mobilehouse_async_simulation(self, test_server):
        """Test MobileHouse async simulation."""
        
        async def simulate_station(station_id, server_url):
            """Simulate a single station."""
            simulator = CitrineOSSimulator(station_id, server_url)
            
            try:
                await simulator.connect()
                
                # Boot notification
                boot_result = await simulator.boot_notification()
                assert boot_result[2]["status"] == "Accepted"
                
                # Heartbeat
                heartbeat_result = await simulator.heartbeat()
                assert heartbeat_result[2]["status"] == "Accepted"
                
                # Meter values
                meter_result = await simulator.meter_values(1, 22.5)
                assert meter_result is None
                
            finally:
                await simulator.disconnect()
        
        # Simulate multiple stations concurrently
        tasks = []
        for i in range(3):
            task = simulate_station(f"MH_STATION_{i+1:03d}", "ws://localhost:9000")
            tasks.append(task)
        
        # Run all simulations concurrently
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
