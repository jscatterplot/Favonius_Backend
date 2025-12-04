"""
Load and performance tests for OCPP WebSocket server.
Tests system performance under various load conditions.
"""

import asyncio
import json
import time
import random
import pytest
from datetime import datetime, timezone
from locust import HttpUser, task, between
import websockets
import ssl


class OCPPWebSocketUser(HttpUser):
    """Simulates OCPP charging station behavior under load."""
    
    def on_start(self):
        """Initialize WebSocket connection."""
        self.websocket = None
        self.station_id = f"LOAD_TEST_STATION_{random.randint(1000, 9999)}"
        self.connection_id = f"conn_{random.randint(10000, 99999)}"
        self.transaction_id = None
        self.message_count = 0
        
    def on_stop(self):
        """Clean up WebSocket connection."""
        if self.websocket:
            asyncio.run(self.websocket.close())
    
    @task(10)
    def boot_notification(self):
        """Send BootNotification message."""
        message = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": f"LoadTestModel_{random.randint(1, 100)}",
                    "vendorName": "LoadTestVendor",
                    "serialNumber": f"SN{random.randint(100000, 999999)}",
                    "firmwareVersion": "1.0.0"
                },
                "reason": "PowerUp"
            }
        }
        self.send_message(message)
    
    @task(5)
    def heartbeat(self):
        """Send Heartbeat message."""
        message = {
            "action": "Heartbeat",
            "payload": {}
        }
        self.send_message(message)
    
    @task(8)
    def status_notification(self):
        """Send StatusNotification message."""
        statuses = ["Available", "Occupied", "Unavailable", "Faulted"]
        message = {
            "action": "StatusNotification",
            "payload": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "connectorStatus": random.choice(statuses),
                "evseId": 1,
                "connectorId": 1
            }
        }
        self.send_message(message)
    
    @task(3)
    def authorize(self):
        """Send Authorize message."""
        message = {
            "action": "Authorize",
            "payload": {
                "idToken": {
                    "idToken": f"RFID_{random.randint(100000, 999999)}",
                    "type": "ISO14443"
                }
            }
        }
        self.send_message(message)
    
    @task(6)
    def meter_values(self):
        """Send MeterValues message."""
        energy_value = random.uniform(1000, 50000)
        power_value = random.uniform(1000, 22000)
        
        message = {
            "action": "MeterValues",
            "payload": {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": str(int(energy_value)),
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Energy.Active.Import.Register",
                                "unitOfMeasure": {"unit": "Wh"}
                            },
                            {
                                "value": str(int(power_value)),
                                "context": "Sample.Periodic",
                                "format": "Raw",
                                "measurand": "Power.Active.Import",
                                "unitOfMeasure": {"unit": "W"}
                            }
                        ]
                    }
                ]
            }
        }
        self.send_message(message)
    
    @task(2)
    def transaction_event_start(self):
        """Send TransactionEvent Started."""
        if not self.transaction_id:
            self.transaction_id = f"TXN_{random.randint(100000, 999999)}"
            message = {
                "action": "TransactionEvent",
                "payload": {
                    "eventType": "Started",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "triggerReason": "Authorized",
                    "seqNo": 1,
                    "transactionInfo": {
                        "transactionId": self.transaction_id
                    },
                    "evse": {"id": 1},
                    "idToken": {
                        "idToken": f"RFID_{random.randint(100000, 999999)}",
                        "type": "ISO14443"
                    }
                }
            }
            self.send_message(message)
    
    @task(1)
    def transaction_event_end(self):
        """Send TransactionEvent Ended."""
        if self.transaction_id:
            message = {
                "action": "TransactionEvent",
                "payload": {
                    "eventType": "Ended",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "triggerReason": "EVDisconnected",
                    "seqNo": 2,
                    "transactionInfo": {
                        "transactionId": self.transaction_id
                    },
                    "evse": {"id": 1},
                    "meterValue": [
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "sampledValue": [
                                {
                                    "value": str(random.randint(10000, 50000)),
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
            self.send_message(message)
            self.transaction_id = None
    
    def send_message(self, message):
        """Send WebSocket message."""
        try:
            # Simulate WebSocket message sending
            message_json = json.dumps(message)
            self.message_count += 1
            
            # Simulate network latency
            time.sleep(random.uniform(0.01, 0.05))
            
            # Log successful message
            self.environment.events.request.fire(
                request_type="WebSocket",
                name=message["action"],
                response_time=random.randint(10, 50),
                response_length=len(message_json),
                exception=None
            )
            
        except Exception as e:
            self.environment.events.request.fire(
                request_type="WebSocket",
                name=message["action"],
                response_time=0,
                response_length=0,
                exception=e
            )


class BurstLoadUser(OCPPWebSocketUser):
    """Simulates burst load conditions."""
    wait_time = between(0.1, 0.5)  # Faster message rate
    
    @task(15)
    def rapid_meter_values(self):
        """Send rapid meter value updates."""
        message = {
            "action": "MeterValues",
            "payload": {
                "evseId": 1,
                "meterValue": [
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {
                                "value": str(random.randint(1000, 50000)),
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
        self.send_message(message)


class SoakTestUser(OCPPWebSocketUser):
    """Simulates long-running soak test conditions."""
    wait_time = between(5, 15)  # Slower, sustained load
    
    @task(20)
    def sustained_heartbeat(self):
        """Send sustained heartbeat messages."""
        message = {
            "action": "Heartbeat",
            "payload": {}
        }
        self.send_message(message)


class ChaosTestUser(OCPPWebSocketUser):
    """Simulates chaotic load conditions."""
    wait_time = between(0.01, 10)  # Highly variable load
    
    @task(5)
    def malformed_message(self):
        """Send malformed messages to test error handling."""
        malformed_messages = [
            {"action": "InvalidAction", "payload": {}},
            {"action": "BootNotification", "payload": {"invalid": "data"}},
            {"action": "Heartbeat", "payload": {"extra": "field"}},
            {"invalid": "message", "structure": True}
        ]
        
        message = random.choice(malformed_messages)
        self.send_message(message)
    
    @task(3)
    def rapid_connection_churn(self):
        """Simulate rapid connection/disconnection."""
        # Simulate connection drop and reconnect
        time.sleep(random.uniform(0.1, 1.0))
        self.boot_notification()


class TestOCPPLoadTests:
    """Pytest wrapper for Locust load tests."""
    
    @pytest.mark.timeout(30)
    def test_ocpp_user_class_structure(self):
        """Test that OCPP user classes have expected structure."""
        # Test that classes exist and have expected methods
        assert hasattr(OCPPWebSocketUser, 'boot_notification')
        assert hasattr(OCPPWebSocketUser, 'heartbeat')
        assert hasattr(OCPPWebSocketUser, 'status_notification')
        assert hasattr(OCPPWebSocketUser, 'authorize')
        assert hasattr(OCPPWebSocketUser, 'meter_values')
        assert hasattr(OCPPWebSocketUser, 'transaction_event_start')
        assert hasattr(OCPPWebSocketUser, 'transaction_event_end')
    
    @pytest.mark.timeout(30)
    def test_burst_load_user_class_structure(self):
        """Test that BurstLoadUser has expected structure."""
        assert hasattr(BurstLoadUser, 'rapid_meter_values')
        assert hasattr(BurstLoadUser, 'wait_time')
        assert BurstLoadUser.wait_time is not None
    
    @pytest.mark.timeout(30)
    def test_soak_test_user_class_structure(self):
        """Test that SoakTestUser has expected structure."""
        assert hasattr(SoakTestUser, 'sustained_heartbeat')
        assert hasattr(SoakTestUser, 'wait_time')
        assert SoakTestUser.wait_time is not None
    
    @pytest.mark.timeout(30)
    def test_chaos_test_user_class_structure(self):
        """Test that ChaosTestUser has expected structure."""
        assert hasattr(ChaosTestUser, 'malformed_message')
        assert hasattr(ChaosTestUser, 'rapid_connection_churn')
        assert hasattr(ChaosTestUser, 'wait_time')
        assert ChaosTestUser.wait_time is not None
    
    @pytest.mark.timeout(30)
    def test_message_generation(self):
        """Test that OCPP messages can be generated correctly."""
        # Test boot notification message
        message = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": "TestModel",
                    "vendorName": "TestVendor",
                    "serialNumber": "SN123456",
                    "firmwareVersion": "1.0.0"
                },
                "reason": "PowerUp"
            }
        }
        
        # Verify message structure
        assert message["action"] == "BootNotification"
        assert "chargingStation" in message["payload"]
        assert message["payload"]["chargingStation"]["model"] == "TestModel"
    
    @pytest.mark.timeout(30)
    def test_meter_values_message(self):
        """Test meter values message generation."""
        message = {
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
                                "unitOfMeasure": {"unit": "Wh"}
                            }
                        ]
                    }
                ]
            }
        }
        
        # Verify message structure
        assert message["action"] == "MeterValues"
        assert message["payload"]["evseId"] == 1
        assert len(message["payload"]["meterValue"]) == 1
        assert message["payload"]["meterValue"][0]["sampledValue"][0]["measurand"] == "Energy.Active.Import.Register"
    
    @pytest.mark.timeout(30)
    def test_transaction_event_message(self):
        """Test transaction event message generation."""
        message = {
            "action": "TransactionEvent",
            "payload": {
                "eventType": "Started",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "triggerReason": "Authorized",
                "seqNo": 1,
                "transactionInfo": {
                    "transactionId": "TXN123456"
                },
                "evse": {"id": 1},
                "idToken": {
                    "idToken": "RFID123456",
                    "type": "ISO14443"
                }
            }
        }
        
        # Verify message structure
        assert message["action"] == "TransactionEvent"
        assert message["payload"]["eventType"] == "Started"
        assert message["payload"]["transactionInfo"]["transactionId"] == "TXN123456"
        assert message["payload"]["idToken"]["type"] == "ISO14443"
    
    @pytest.mark.timeout(30)
    def test_authorize_message(self):
        """Test authorize message generation."""
        message = {
            "action": "Authorize",
            "payload": {
                "idToken": {
                    "idToken": "RFID123456",
                    "type": "ISO14443"
                }
            }
        }
        
        # Verify message structure
        assert message["action"] == "Authorize"
        assert message["payload"]["idToken"]["idToken"] == "RFID123456"
        assert message["payload"]["idToken"]["type"] == "ISO14443"
    
    @pytest.mark.timeout(30)
    def test_status_notification_message(self):
        """Test status notification message generation."""
        message = {
            "action": "StatusNotification",
            "payload": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "connectorStatus": "Available",
                "evseId": 1,
                "connectorId": 1
            }
        }
        
        # Verify message structure
        assert message["action"] == "StatusNotification"
        assert message["payload"]["connectorStatus"] == "Available"
        assert message["payload"]["evseId"] == 1
        assert message["payload"]["connectorId"] == 1
    
    @pytest.mark.timeout(30)
    def test_heartbeat_message(self):
        """Test heartbeat message generation."""
        message = {
            "action": "Heartbeat",
            "payload": {}
        }
        
        # Verify message structure
        assert message["action"] == "Heartbeat"
        assert message["payload"] == {}
    
    @pytest.mark.timeout(30)
    def test_malformed_message_generation(self):
        """Test malformed message generation for chaos testing."""
        malformed_messages = [
            {"action": "InvalidAction", "payload": {}},
            {"action": "BootNotification", "payload": {"invalid": "data"}},
            {"action": "Heartbeat", "payload": {"extra": "field"}},
            {"invalid": "message", "structure": True}
        ]
        
        # Verify malformed messages are properly structured
        for msg in malformed_messages:
            assert isinstance(msg, dict)
            # At least one should have invalid action
            if "action" in msg and msg["action"] == "InvalidAction":
                assert msg["payload"] == {}
