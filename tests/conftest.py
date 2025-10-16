"""Shared test fixtures and utilities."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timezone, timedelta

# Import config
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from websocket_handler.config import Config, TimescaleConfig, SupabaseConfig


@pytest.fixture
def mock_timescale_client():
    """Mock TimescaleClient with common methods."""
    client = Mock()
    
    # Device model methods
    client.store_device_component = AsyncMock()
    client.store_device_variable = AsyncMock()
    client.get_device_variables = AsyncMock()
    client.get_all_device_variables = AsyncMock()
    
    # Charging profile methods
    client.store_charging_profile = AsyncMock()
    client.get_active_charging_profiles = AsyncMock()
    client.remove_charging_profile = AsyncMock()
    client.store_reported_charging_profile = AsyncMock()
    
    # Transaction methods
    client.store_transaction = AsyncMock()
    client.get_transaction = AsyncMock()
    client.update_transaction = AsyncMock()
    client.store_transaction_event = AsyncMock()
    client.get_id_token_info = AsyncMock()
    client.calculate_transaction_cost = AsyncMock()
    
    # Certificate methods
    client.store_certificate = AsyncMock()
    client.get_certificate = AsyncMock()
    client.delete_certificate = AsyncMock()
    client.get_installed_certificate_ids = AsyncMock()
    
    # Monitoring methods
    client.store_monitoring_report = AsyncMock()
    client.store_variable_monitoring = AsyncMock()
    client.store_notified_monitoring_report = AsyncMock()
    client.store_alert_rule = AsyncMock()
    client.store_alert = AsyncMock()
    client.get_alert_rules = AsyncMock()
    client.get_periodic_monitoring_data = AsyncMock()
    client.get_active_alerts = AsyncMock()
    
    # Display methods
    client.store_display_message = AsyncMock()
    client.get_display_messages = AsyncMock()
    client.clear_display_message = AsyncMock()
    client.store_display_message_history = AsyncMock()
    
    # Tariff methods
    client.store_tariff = AsyncMock()
    client.get_active_tariffs = AsyncMock()
    client.store_tariff_element = AsyncMock()
    client.store_tou_period = AsyncMock()
    client.store_cost_update = AsyncMock()
    client.calculate_tou_multiplier = AsyncMock()
    
    # Privacy methods
    client.store_customer_information_request = AsyncMock()
    client.update_customer_information_status = AsyncMock()
    client.store_data_retention_policy = AsyncMock()
    client.store_consent_record = AsyncMock()
    client.anonymize_customer_data = AsyncMock()
    
    # Error handling methods
    client.store_circuit_breaker_state = AsyncMock()
    client.store_dead_letter_message = AsyncMock()
    client.store_retry_attempt = AsyncMock()
    client.store_health_check_result = AsyncMock()
    client.get_degradation_rules = AsyncMock()
    client.get_retryable_dlq_messages = AsyncMock()
    
    # Price and optimization methods
    client.store_electricity_prices = AsyncMock()
    client.get_latest_prices = AsyncMock()
    client.get_active_charging_sessions = AsyncMock()
    client.store_charging_schedule = AsyncMock()
    client.store_optimization_decision = AsyncMock()
    
    # Health check method
    client.health_check = AsyncMock(return_value={"status": "healthy"})
    
    return client


@pytest.fixture
def mock_supabase_client():
    """Mock SupabaseClient with common methods."""
    client = Mock()
    
    # User and organization methods
    client.get_user = AsyncMock()
    client.create_user = AsyncMock()
    client.update_user = AsyncMock()
    client.delete_user = AsyncMock()
    client.get_organization = AsyncMock()
    client.create_organization = AsyncMock()
    
    # Station and fleet methods
    client.get_station = AsyncMock()
    client.create_station = AsyncMock()
    client.update_station = AsyncMock()
    client.get_fleet_stations = AsyncMock()
    
    # Analytics methods
    client.get_analytics_data = AsyncMock()
    client.store_analytics_event = AsyncMock()
    
    # Health check method
    client.health_check = AsyncMock(return_value={"status": "healthy"})
    
    return client


@pytest.fixture
def mock_connection_manager():
    """Mock ConnectionManager with common methods."""
    manager = Mock()
    
    # Connection methods
    manager.add_connection = AsyncMock()
    manager.remove_connection = AsyncMock()
    manager.get_connection = AsyncMock()
    manager.get_all_connections = AsyncMock()
    manager.send_message = AsyncMock()
    manager.broadcast_message = AsyncMock()
    
    # Health check method
    manager.get_health_status = Mock(return_value={"status": "healthy"})
    
    return manager


@pytest.fixture
def test_config():
    """Create test configuration."""
    return Config(
        timescale=TimescaleConfig(
            service_url="postgres://test:test@localhost:5432/test",
            host="localhost",
            user="test",
            password="test"
        ),
        supabase=SupabaseConfig(
            url="https://test.supabase.co",
            anon_key="test_anon_key",
            service_key="test_service_key",
            db_host="test.db.host",
            db_user="test_user",
            db_password="test_password"
        )
    )


@pytest.fixture
def test_station_id():
    """Test station ID."""
    return "TEST_STATION_001"


@pytest.fixture
def test_evse_id():
    """Test EVSE ID."""
    return 1


@pytest.fixture
def test_connector_id():
    """Test connector ID."""
    return 1


@pytest.fixture
def test_transaction_id():
    """Test transaction ID."""
    return "TXN123456"


@pytest.fixture
def test_timestamp():
    """Test timestamp."""
    return datetime.now(timezone.utc)


@pytest.fixture
def test_timestamp_future():
    """Test future timestamp."""
    return datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.fixture
def test_timestamp_past():
    """Test past timestamp."""
    return datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.fixture
def sample_charging_station_data():
    """Sample charging station data for testing."""
    return {
        "model": "TestModel",
        "vendor_name": "TestVendor",
        "serial_number": "SN123456",
        "firmware_version": "1.0.0",
        "max_power": 22.0,
        "v2x_capable": True,
        "num_evses": 2,
        "num_connectors": 4
    }


@pytest.fixture
def sample_charging_profile_data():
    """Sample charging profile data for testing."""
    return {
        "id": 1,
        "stack_level": 0,
        "charging_profile_purpose": "TxDefaultProfile",
        "charging_profile_kind": "Absolute",
        "charging_schedule": {
            "id": 1,
            "charging_rate_unit": "W",
            "charging_schedule_period": [
                {
                    "start_period": 0,
                    "limit": 22.0
                }
            ],
            "duration": 3600
        }
    }


@pytest.fixture
def sample_transaction_data():
    """Sample transaction data for testing."""
    return {
        "transaction_id": "TXN123456",
        "station_id": "TEST_STATION_001",
        "evse_id": 1,
        "connector_id": 1,
        "id_token": "AUTH123",
        "started_at": datetime.now(timezone.utc),
        "charging_state": "Charging",
        "energy_kwh": 22.5
    }


@pytest.fixture
def sample_meter_value_data():
    """Sample meter value data for testing."""
    return {
        "timestamp": datetime.now(timezone.utc),
        "evse_id": 1,
        "connector_id": 1,
        "transaction_id": "TXN123456",
        "sampled_value": [
            {
                "value": "22.5",
                "context": "Sample.Periodic",
                "format": "Raw",
                "measurand": "Energy.Active.Import.Register",
                "unit_of_measure": "kWh"
            }
        ]
    }


@pytest.fixture
def sample_price_data():
    """Sample price data for testing."""
    return {
        "time": datetime.now(timezone.utc),
        "node_id": "TH_SP15_GEN-APND",
        "market_type": "DAM",
        "lmp_price_mwh": 100.0,
        "energy_component_mwh": 95.0,
        "congestion_component_mwh": 3.0,
        "loss_component_mwh": 2.0,
        "ghg_adder_mwh": 0.0
    }


@pytest.fixture
def sample_optimization_session():
    """Sample optimization session data for testing."""
    return {
        "station_id": "TEST_STATION_001",
        "evse_id": 1,
        "connector_id": 1,
        "start_time": datetime.now(timezone.utc),
        "end_time": datetime.now(timezone.utc) + timedelta(hours=2),
        "start_soc_percent": 50.0
    }


@pytest.fixture
def sample_alert_rule_data():
    """Sample alert rule data for testing."""
    return {
        "rule_id": "RULE_001",
        "station_id": "TEST_STATION_001",
        "component_name": "ChargingStation",
        "variable_name": "Temperature",
        "monitoring_criterion": "ThresholdMonitoring",
        "threshold": 80.0,
        "severity": "high",
        "enabled": True
    }


@pytest.fixture
def sample_display_message_data():
    """Sample display message data for testing."""
    return {
        "id": 1,
        "priority": "NormalCycle",
        "state": "Charging",
        "start_date_time": datetime.now(timezone.utc),
        "end_date_time": datetime.now(timezone.utc) + timedelta(hours=1),
        "message": {
            "format": "UTF8",
            "language": "en",
            "content": "Charging in progress"
        }
    }


@pytest.fixture
def sample_tariff_data():
    """Sample tariff data for testing."""
    return {
        "tariff_id": "TARIFF_001",
        "description": "Test Tariff",
        "currency": "USD",
        "priority": 1,
        "elements": [
            {
                "type": "Energy",
                "price_per_unit": 0.20,
                "unit": "kWh",
                "currency": "USD"
            }
        ]
    }


@pytest.fixture
def sample_privacy_request_data():
    """Sample privacy request data for testing."""
    return {
        "request_id": 1,
        "customer_identifier": "CUSTOMER123",
        "id_token": {
            "id_token": "CUSTOMER123",
            "type": "KeyCode"
        },
        "request_type": "data_access"
    }


class AsyncMockWrapper:
    """Wrapper to make Mock objects work better with async/await."""
    
    def __init__(self, mock_obj):
        self.mock_obj = mock_obj
    
    def __getattr__(self, name):
        attr = getattr(self.mock_obj, name)
        if callable(attr):
            return AsyncMock(side_effect=attr.side_effect, return_value=attr.return_value)
        return attr


@pytest.fixture
def async_mock():
    """Create an async mock wrapper."""
    return AsyncMockWrapper(Mock())


def create_mock_ocpp_message(action, payload, message_id=1):
    """Create a mock OCPP message."""
    return [2, str(message_id), action, payload]


def create_mock_ocpp_response(action, payload, message_id=1):
    """Create a mock OCPP response."""
    return [3, str(message_id), action, payload]


def create_mock_ocpp_error(action, error_code, error_description, message_id=1):
    """Create a mock OCPP error response."""
    return [4, str(message_id), error_code, error_description, {}]


@pytest.fixture
def mock_boot_notification():
    """Mock BootNotification message."""
    return create_mock_ocpp_message("BootNotification", {
        "chargingStation": {
            "model": "TestModel",
            "vendorName": "TestVendor",
            "serialNumber": "SN123456",
            "firmwareVersion": "1.0.0"
        },
        "reason": "PowerUp"
    })


@pytest.fixture
def mock_boot_notification_response():
    """Mock BootNotification response."""
    return create_mock_ocpp_response("BootNotification", {
        "status": "Accepted",
        "currentTime": datetime.now(timezone.utc).isoformat(),
        "interval": 300
    })


@pytest.fixture
def mock_heartbeat():
    """Mock Heartbeat message."""
    return create_mock_ocpp_message("Heartbeat", {})


@pytest.fixture
def mock_heartbeat_response():
    """Mock Heartbeat response."""
    return create_mock_ocpp_response("Heartbeat", {
        "currentTime": datetime.now(timezone.utc).isoformat()
    })


@pytest.fixture
def mock_status_notification():
    """Mock StatusNotification message."""
    return create_mock_ocpp_message("StatusNotification", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "connectorStatus": "Available",
        "evseId": 1,
        "connectorId": 1,
        "errorCode": "NoError"
    })


@pytest.fixture
def mock_meter_values():
    """Mock MeterValues message."""
    return create_mock_ocpp_message("MeterValues", {
        "evseId": 1,
        "meterValue": [{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sampledValue": [{
                "value": "22.5",
                "context": "Sample.Periodic",
                "format": "Raw",
                "measurand": "Energy.Active.Import.Register",
                "unitOfMeasure": "kWh"
            }]
        }]
    })


@pytest.fixture
def mock_transaction_event():
    """Mock TransactionEvent message."""
    return create_mock_ocpp_message("TransactionEvent", {
        "eventType": "Started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "triggerReason": "Authorized",
        "seqNo": 1,
        "transactionInfo": {
            "transactionId": "TXN123456",
            "chargingState": "Charging"
        },
        "idToken": {
            "idToken": "AUTH123",
            "type": "KeyCode"
        }
    })


@pytest.fixture
def mock_transaction_event_response():
    """Mock TransactionEvent response."""
    return create_mock_ocpp_response("TransactionEvent", {
        "status": "Accepted"
    })


# Pytest configuration
def pytest_configure(config):
    """Configure pytest."""
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as an end-to-end test"
    )
    config.addinivalue_line(
        "markers", "load: mark test as a load test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection."""
    for item in items:
        # Add slow marker to tests that take longer than 1 second
        if "slow" in item.name or "integration" in item.name or "e2e" in item.name:
            item.add_marker(pytest.mark.slow)
        
        # Add unit marker to unit tests
        if "test_" in item.name and not any(marker in item.name for marker in ["integration", "e2e", "load"]):
            item.add_marker(pytest.mark.unit)
