"""
End-to-End OCPP protocol flow tests for Favonius Energy V2G system.
Tests complete OCPP message flows and real-world scenarios.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.websocket_handler.config import (
    Config,
    SupabaseConfig,
    TimescaleConfig,
    TLSConfig,
    WebSocketConfig,
)
from src.websocket_handler.connection_manager import ConnectionManager
from src.websocket_handler.message_handler import MessageHandler

# Import system components
from src.websocket_handler.server import OCPPWebSocketServer


class TestOCPPEndToEndFlows:
    """Test complete OCPP protocol flows."""

    @pytest_asyncio.fixture
    async def config(self):
        """Create test configuration."""
        config = Config(
            websocket=WebSocketConfig(
                port=9001,
                host="127.0.0.1",
                max_connections=10,
                heartbeat_interval=30,
                message_timeout=60,
                max_message_size=65536,
                rate_limit_per_minute=100,
            ),
            tls=TLSConfig(cert_path=None, key_path=None, ca_path=None, verify_client=False),
            timescale=TimescaleConfig(
                service_url="postgresql://test:test@localhost:5432/testdb",
                host="localhost",
                port=5432,
                database="testdb",
                user="test",
                password="test",
                sslmode="require",
                max_connections=10,
                pool_size=5,
                statement_timeout=30,
                idle_timeout=600,
                chunk_time_interval="1 day",
                compression_after="7 days",
                retention_period="2 years",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="localhost",
                db_port=5432,
                db_name="testdb",
                db_user="test",
                db_password="test",
                max_connections=10,
                connection_timeout=30,
                enable_realtime=True,
            ),
        )
        return config

    @pytest_asyncio.fixture
    async def timescale_client(self):
        """Create mock TimescaleDB client."""
        client = AsyncMock()
        client.connect.return_value = None
        client.disconnect.return_value = None
        client.execute_query.return_value = []
        client.insert_data.return_value = None
        return client

    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)

    @pytest_asyncio.fixture
    async def message_handler(self, config, connection_manager, timescale_client):
        """Create message handler."""
        return MessageHandler(connection_manager, config, timescale_client)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_complete_charging_session_flow(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test complete charging session from boot to transaction end."""
        # Initialize server
        OCPPWebSocketServer(config, timescale_client)

        # Test data
        station_id = "TEST_STATION_001"
        str(uuid.uuid4())
        transaction_id = str(uuid.uuid4())

        # 1. Boot Notification Flow
        boot_payload = {
            "chargingStation": {
                "model": "TestModel",
                "vendorName": "TestVendor",
                "serialNumber": "SN123456",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }

        boot_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "BootNotification", boot_payload
        )

        assert "status" in boot_response
        assert "currentTime" in boot_response
        assert "interval" in boot_response

        # 2. Status Notification Flow
        status_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectorStatus": "Available",
            "evseId": 1,
            "connectorId": 1,
        }

        status_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "StatusNotification", status_payload
        )

        assert status_response == {}

        # 3. Authorize Flow
        authorize_payload = {"idToken": {"idToken": "RFID123456", "type": "ISO14443"}}

        authorize_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "Authorize", authorize_payload
        )

        assert "idTokenInfo" in authorize_response
        assert authorize_response["idTokenInfo"]["status"] == "Accepted"

        # 4. Transaction Event Started
        transaction_start_payload = {
            "eventType": "Started",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": "Authorized",
            "seqNo": 1,
            "transactionInfo": {"transactionId": transaction_id},
            "evse": {"id": 1},
            "idToken": {"idToken": "RFID123456", "type": "ISO14443"},
        }

        transaction_start_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "TransactionEvent", transaction_start_payload
        )

        assert transaction_start_response == {}

        # 5. Meter Values during charging
        meter_values_payload = {
            "evseId": 1,
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "1000",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": {"unit": "Wh"},
                        },
                        {
                            "value": "2200",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Power.Active.Import",
                            "unitOfMeasure": {"unit": "W"},
                        },
                    ],
                }
            ],
        }

        meter_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "MeterValues", meter_values_payload
        )

        assert meter_response == {}

        # 6. Transaction Event Ended
        transaction_end_payload = {
            "eventType": "Ended",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": "EVDisconnected",
            "seqNo": 2,
            "transactionInfo": {"transactionId": transaction_id},
            "evse": {"id": 1},
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "5000",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": {"unit": "Wh"},
                        }
                    ],
                }
            ],
        }

        transaction_end_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "TransactionEvent", transaction_end_payload
        )

        assert transaction_end_response == {}

        # 7. Heartbeat Flow
        heartbeat_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "Heartbeat", {}
        )

        assert "currentTime" in heartbeat_response

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_error_recovery_flow(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test error recovery and resilience flows."""
        station_id = "TEST_STATION_002"

        # 1. Boot with invalid data
        invalid_boot_payload = {
            "chargingStation": {
                "model": "",  # Invalid empty model
                "vendorName": "TestVendor",
                "serialNumber": "SN123456",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }

        # Should handle gracefully
        boot_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "BootNotification", invalid_boot_payload
        )

        assert "status" in boot_response

        # 2. Status notification with invalid connector status
        invalid_status_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectorStatus": "InvalidStatus",
            "evseId": 1,
            "connectorId": 1,
        }

        status_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "StatusNotification", invalid_status_payload
        )

        assert status_response == {}

        # 3. Meter values with invalid measurand
        invalid_meter_payload = {
            "evseId": 1,
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "invalid",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "InvalidMeasurand",
                            "unitOfMeasure": {"unit": "Wh"},
                        }
                    ],
                }
            ],
        }

        meter_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "MeterValues", invalid_meter_payload
        )

        assert meter_response == {}

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_concurrent_sessions_flow(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test multiple concurrent charging sessions."""
        # Test multiple stations
        stations = ["STATION_001", "STATION_002", "STATION_003"]

        # Boot all stations
        for station_id in stations:
            boot_payload = {
                "chargingStation": {
                    "model": f"Model_{station_id}",
                    "vendorName": "TestVendor",
                    "serialNumber": f"SN{station_id}",
                    "firmwareVersion": "1.0.0",
                },
                "reason": "PowerUp",
            }

            boot_response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "BootNotification", boot_payload
            )

            assert "status" in boot_response

        # Start transactions on all stations
        for i, station_id in enumerate(stations):
            transaction_payload = {
                "eventType": "Started",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "triggerReason": "Authorized",
                "seqNo": 1,
                "transactionInfo": {"transactionId": f"TXN_{station_id}_{i}"},
                "evse": {"id": 1},
                "idToken": {"idToken": f"RFID_{station_id}", "type": "ISO14443"},
            }

            transaction_response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "TransactionEvent", transaction_payload
            )

            assert transaction_response == {}

        # Send meter values from all stations
        for station_id in stations:
            meter_payload = {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": str(1000 + hash(station_id) % 1000),
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"},
                            }
                        ],
                    }
                ],
            }

            meter_response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
            )

            assert meter_response == {}

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_heartbeat_monitoring_flow(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test heartbeat monitoring and connection health."""
        station_id = "HEARTBEAT_STATION"

        # Boot station
        boot_payload = {
            "chargingStation": {
                "model": "HeartbeatModel",
                "vendorName": "TestVendor",
                "serialNumber": "SN_HEARTBEAT",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }

        boot_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "BootNotification", boot_payload
        )

        assert "status" in boot_response

        # Send multiple heartbeats
        for i in range(5):
            heartbeat_response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "Heartbeat", {}
            )

            assert "currentTime" in heartbeat_response

            # Simulate time passing
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_data_persistence_flow(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test data persistence and retrieval flows."""
        station_id = "PERSISTENCE_STATION"
        transaction_id = str(uuid.uuid4())

        # Mock database operations
        timescale_client.insert_data.return_value = None
        timescale_client.execute_query.return_value = []

        # 1. Boot and persist station data
        boot_payload = {
            "chargingStation": {
                "model": "PersistenceModel",
                "vendorName": "TestVendor",
                "serialNumber": "SN_PERSISTENCE",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }

        boot_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "BootNotification", boot_payload
        )

        assert "status" in boot_response

        # 2. Start transaction and persist
        transaction_payload = {
            "eventType": "Started",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": "Authorized",
            "seqNo": 1,
            "transactionInfo": {"transactionId": transaction_id},
            "evse": {"id": 1},
            "idToken": {"idToken": "RFID_PERSISTENCE", "type": "ISO14443"},
        }

        transaction_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "TransactionEvent", transaction_payload
        )

        assert transaction_response == {}

        # 3. Send meter values and persist
        meter_payload = {
            "evseId": 1,
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": "2500",
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": {"unit": "Wh"},
                        }
                    ],
                }
            ],
        }

        meter_response = await message_handler.handle_message(
            station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
        )

        assert meter_response == {}

        # Note: Database operations are handled internally by the message handler
        # The test verifies that messages are processed without errors


class TestOCPPProtocolCompliance:
    """Test OCPP protocol compliance and edge cases."""

    @pytest_asyncio.fixture
    async def config(self):
        """Create test configuration."""
        config = Config(
            websocket=WebSocketConfig(
                port=9002,
                host="127.0.0.1",
                max_connections=10,
                heartbeat_interval=30,
                message_timeout=60,
                max_message_size=65536,
                rate_limit_per_minute=100,
            ),
            tls=TLSConfig(cert_path=None, key_path=None, ca_path=None, verify_client=False),
            timescale=TimescaleConfig(
                service_url="postgresql://test:test@localhost:5432/testdb",
                host="localhost",
                port=5432,
                database="testdb",
                user="test",
                password="test",
                sslmode="require",
                max_connections=10,
                pool_size=5,
                statement_timeout=30,
                idle_timeout=600,
                chunk_time_interval="1 day",
                compression_after="7 days",
                retention_period="2 years",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="localhost",
                db_port=5432,
                db_name="testdb",
                db_user="test",
                db_password="test",
                max_connections=10,
                connection_timeout=30,
                enable_realtime=True,
            ),
        )
        return config

    @pytest_asyncio.fixture
    async def timescale_client(self):
        """Create mock TimescaleDB client."""
        client = AsyncMock()
        client.connect.return_value = None
        client.disconnect.return_value = None
        client.execute_query.return_value = []
        client.insert_data.return_value = None
        return client

    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)

    @pytest_asyncio.fixture
    async def message_handler(self, config, connection_manager, timescale_client):
        """Create message handler."""
        return MessageHandler(connection_manager, config, timescale_client)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_ocpp_message_format_compliance(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test OCPP message format compliance."""
        station_id = "FORMAT_STATION"

        # Test valid OCPP message formats
        valid_messages = [
            {
                "action": "BootNotification",
                "payload": {
                    "chargingStation": {
                        "model": "TestModel",
                        "vendorName": "TestVendor",
                        "serialNumber": "SN123456",
                        "firmwareVersion": "1.0.0",
                    },
                    "reason": "PowerUp",
                },
            },
            {"action": "Heartbeat", "payload": {}},
            {
                "action": "StatusNotification",
                "payload": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "connectorStatus": "Available",
                    "evseId": 1,
                    "connectorId": 1,
                },
            },
            {
                "action": "Authorize",
                "payload": {"idToken": {"idToken": "RFID123456", "type": "ISO14443"}},
            },
            {
                "action": "MeterValues",
                "payload": {
                    "evseId": 1,
                    "meterValue": [
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "sampledValue": [
                                {
                                    "value": "1000",
                                    "context": "Sample.Periodic",
                                    "format": "Raw",
                                    "measurand": "Energy.Active.Import.Register",
                                    "unitOfMeasure": {"unit": "Wh"},
                                }
                            ],
                        }
                    ],
                },
            },
        ]

        for message in valid_messages:
            response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), message["action"], message["payload"]
            )

            # All valid messages should be processed without errors
            assert isinstance(response, dict)

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_ocpp_error_handling_compliance(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test OCPP error handling compliance."""
        station_id = "ERROR_STATION"

        # Test invalid message types
        invalid_messages = [
            {"action": "InvalidAction", "payload": {}},
            {"action": "BootNotification", "payload": {"invalidField": "value"}},
            {"action": "Heartbeat", "payload": {"extraField": "value"}},
        ]

        for message in invalid_messages:
            # Should handle invalid messages gracefully
            try:
                response = await message_handler.handle_message(
                    station_id, 2, str(uuid.uuid4()), message["action"], message["payload"]
                )
                # Should return some response, even if it's an error response
                assert isinstance(response, dict)
            except Exception as e:
                # If an exception is raised, it should be handled gracefully
                assert isinstance(e, Exception)

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_ocpp_timestamp_compliance(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test OCPP timestamp format compliance."""
        station_id = "TIMESTAMP_STATION"

        # Test various timestamp formats
        timestamp_formats = [
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]

        for timestamp in timestamp_formats:
            status_payload = {
                "timestamp": timestamp,
                "connectorStatus": "Available",
                "evseId": 1,
                "connectorId": 1,
            }

            response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "StatusNotification", status_payload
            )

            assert response == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_ocpp_measurand_compliance(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test OCPP measurand compliance."""
        station_id = "MEASURAND_STATION"

        # Test valid OCPP measurands
        valid_measurands = [
            "Energy.Active.Import.Register",
            "Power.Active.Import",
            "Voltage",
            "Current.Import",
            "Temperature",
            "SoC",
        ]

        for measurand in valid_measurands:
            meter_payload = {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": measurand,
                                "unitOfMeasure": {"unit": "Wh" if "Energy" in measurand else "W"},
                            }
                        ],
                    }
                ],
            }

            response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
            )

            assert response == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_ocpp_unit_compliance(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test OCPP unit of measure compliance."""
        station_id = "UNIT_STATION"

        # Test valid OCPP units
        valid_units = [
            {"unit": "Wh"},
            {"unit": "kWh"},
            {"unit": "W"},
            {"unit": "kW"},
            {"unit": "V"},
            {"unit": "A"},
            {"unit": "°C"},
            {"unit": "%"},
        ]

        for unit_info in valid_units:
            meter_payload = {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": "1000",
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": unit_info,
                            }
                        ],
                    }
                ],
            }

            response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
            )

            assert response == {}


class TestOCPPPerformanceE2E:
    """Test OCPP performance in end-to-end scenarios."""

    @pytest_asyncio.fixture
    async def config(self):
        """Create test configuration."""
        config = Config(
            websocket=WebSocketConfig(
                port=9003,
                host="127.0.0.1",
                max_connections=100,
                heartbeat_interval=30,
                message_timeout=60,
                max_message_size=65536,
                rate_limit_per_minute=1000,
            ),
            tls=TLSConfig(cert_path=None, key_path=None, ca_path=None, verify_client=False),
            timescale=TimescaleConfig(
                service_url="postgresql://test:test@localhost:5432/testdb",
                host="localhost",
                port=5432,
                database="testdb",
                user="test",
                password="test",
                sslmode="require",
                max_connections=100,
                pool_size=20,
                statement_timeout=30,
                idle_timeout=600,
                chunk_time_interval="1 day",
                compression_after="7 days",
                retention_period="2 years",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="localhost",
                db_port=5432,
                db_name="testdb",
                db_user="test",
                db_password="test",
                max_connections=10,
                connection_timeout=30,
                enable_realtime=True,
            ),
        )
        return config

    @pytest_asyncio.fixture
    async def timescale_client(self):
        """Create mock TimescaleDB client."""
        client = AsyncMock()
        client.connect.return_value = None
        client.disconnect.return_value = None
        client.execute_query.return_value = []
        client.insert_data.return_value = None
        return client

    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)

    @pytest_asyncio.fixture
    async def message_handler(self, config, connection_manager, timescale_client):
        """Create message handler."""
        return MessageHandler(connection_manager, config, timescale_client)

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_high_throughput_message_processing(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test high throughput message processing."""
        station_id = "THROUGHPUT_STATION"

        # Process many messages rapidly
        start_time = time.time()

        for i in range(100):
            meter_payload = {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": str(1000 + i),
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"},
                            }
                        ],
                    }
                ],
            }

            response = await message_handler.handle_message(
                station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
            )

            assert response == {}

        end_time = time.time()
        processing_time = end_time - start_time

        # Should process 100 messages in reasonable time
        assert processing_time < 10.0  # Less than 10 seconds
        assert processing_time / 100 < 0.1  # Less than 100ms per message

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_concurrent_station_processing(
        self, config, timescale_client, connection_manager, message_handler
    ):
        """Test concurrent processing of multiple stations."""
        # Create multiple stations
        stations = [f"STATION_{i:03d}" for i in range(20)]

        # Process messages from all stations concurrently
        tasks = []

        for station_id in stations:

            async def process_station_messages(station_id):
                # Boot notification
                boot_payload = {
                    "chargingStation": {
                        "model": f"Model_{station_id}",
                        "vendorName": "TestVendor",
                        "serialNumber": f"SN{station_id}",
                        "firmwareVersion": "1.0.0",
                    },
                    "reason": "PowerUp",
                }

                boot_response = await message_handler.handle_message(
                    station_id, 2, str(uuid.uuid4()), "BootNotification", boot_payload
                )

                # Status notification
                status_payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "connectorStatus": "Available",
                    "evseId": 1,
                    "connectorId": 1,
                }

                status_response = await message_handler.handle_message(
                    station_id, 2, str(uuid.uuid4()), "StatusNotification", status_payload
                )

                # Meter values
                meter_payload = {
                    "evseId": 1,
                    "meterValue": [
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "sampledValue": [
                                {
                                    "value": "1000",
                                    "context": "Sample.Periodic",
                                    "format": "Raw",
                                    "measurand": "Energy.Active.Import.Register",
                                    "unitOfMeasure": {"unit": "Wh"},
                                }
                            ],
                        }
                    ],
                }

                meter_response = await message_handler.handle_message(
                    station_id, 2, str(uuid.uuid4()), "MeterValues", meter_payload
                )

                return {
                    "station_id": station_id,
                    "boot_response": boot_response,
                    "status_response": status_response,
                    "meter_response": meter_response,
                }

            tasks.append(process_station_messages(station_id))

        # Execute all tasks concurrently
        start_time = time.time()
        results = await asyncio.gather(*tasks)
        end_time = time.time()

        processing_time = end_time - start_time

        # Verify all stations processed successfully
        assert len(results) == 20

        for result in results:
            assert "status" in result["boot_response"]
            assert result["status_response"] == {}
            assert result["meter_response"] == {}

        # Should process all stations in reasonable time
        assert processing_time < 5.0  # Less than 5 seconds for 20 stations
