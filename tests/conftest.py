"""Shared test fixtures and utilities."""

import pytest
import asyncio
from uuid import uuid4
from unittest.mock import Mock, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Import config
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Optional: Import websocket_handler config if available
# Note: Supabase removed - PRD specifies TimescaleDB only
try:
    from websocket_handler.config import Config, TimescaleConfig
except ImportError:
    Config = None
    TimescaleConfig = None

# Core model imports
try:
    from src.core.models import DepotConfig, DepotState, OptimizationResult
    from src.core.controller_config import ControllerConfig
    CORE_MODELS_AVAILABLE = True
except ImportError:
    CORE_MODELS_AVAILABLE = False

# asyncpg import
try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False


# ============ Core Test Fixtures for Favonius ============

@pytest.fixture
def sample_depot_id():
    """Generate a sample depot ID."""
    return str(uuid4())


@pytest.fixture
def sample_vehicle_ids():
    """Sample vehicle IDs."""
    return ['bus_1', 'bus_2', 'bus_3']


@pytest.fixture
def mock_asyncpg_pool():
    """Mock asyncpg connection pool for database tests.
    
    Usage:
        pool, conn = mock_asyncpg_pool
        # Configure conn.fetch, conn.execute, etc.
    """
    if ASYNCPG_AVAILABLE:
        pool = MagicMock(spec=asyncpg.Pool)
    else:
        pool = MagicMock()
    
    conn = AsyncMock()
    # Ensure fetchrow returns a proper dict-like object, not a coroutine
    conn.fetchrow = AsyncMock(return_value=None)  # Default: no rows
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def mock_db_pool():
    """Alias for mock_asyncpg_pool for backward compatibility."""
    if ASYNCPG_AVAILABLE:
        pool = MagicMock(spec=asyncpg.Pool)
    else:
        pool = MagicMock()
    
    conn = AsyncMock()
    # Ensure fetchrow returns a proper dict-like object, not a coroutine
    conn.fetchrow = AsyncMock(return_value=None)  # Default: no rows
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def sample_depot_config():
    """Sample DepotConfig for testing optimization and control logic."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    vehicle_ids = ['bus_1', 'bus_2', 'bus_3']
    return DepotConfig(
        vehicle_capacities={
            'bus_1': 324.0,
            'bus_2': 324.0,
            'bus_3': 250.0,
        },
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 5},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        battery_efficiency=0.92,
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=800.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def sample_depot_config_small():
    """Small DepotConfig for faster tests."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    return DepotConfig(
        vehicle_capacities={'bus_1': 324.0},
        vehicle_max_charge_kw={'bus_1': 80.0},
        charger_groups={80.0: 1},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        battery_efficiency=0.92,
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=400.0,
        delta_t=0.25,
        n_timesteps=48,  # 12 hours
    )


@pytest.fixture
def sample_depot_state(sample_depot_config):
    """Sample DepotState for testing."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    n_t = sample_depot_config.n_timesteps
    vehicles = list(sample_depot_config.vehicle_capacities.keys())
    
    return DepotState(
        vehicle_socs={v: 0.3 + 0.1 * i for i, v in enumerate(vehicles)},
        battery_soc=0.5,
        prices=[0.10 + 0.02 * (i % 24) for i in range(n_t)],
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={v: [True] * n_t for v in vehicles},
        energy_requirements={v: 150.0 + 50.0 * i for i, v in enumerate(vehicles)},
        departure_times={v: 48 + 12 * i for i, v in enumerate(vehicles)},
        building_power=[50.0] * n_t,
    )


@pytest.fixture
def sample_optimization_result(sample_depot_config):
    """Sample OptimizationResult for testing."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    n_t = sample_depot_config.n_timesteps
    vehicles = list(sample_depot_config.vehicle_capacities.keys())
    
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            v: {
                'charging_power': [80.0 if i < 48 else 0.0 for i in range(n_t)],
                'soc': [0.3 + 0.01 * i for i in range(n_t)],
            }
            for v in vehicles
        },
        battery_dispatch=[0.0] * n_t,
        grid_power=[200.0] * n_t,
        peak_demand=250.0,
        objective_value=1500.0,
        solve_time=5.0,
        status='completed',
    )


@pytest.fixture
def sample_controller_config():
    """Sample ControllerConfig for testing."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=5,
        max_optimization_failures=3,
        dispatch_retry_attempts=2,
        dispatch_retry_delay_seconds=1.0,
        shutdown_timeout_seconds=5.0,
    )


@pytest.fixture
def sample_controller_config_fast():
    """Fast ControllerConfig for integration tests."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    return ControllerConfig(
        optimization_horizon_hours=12,
        hourly_optimization_start=0,
        hourly_optimization_end=24,
        optimization_timeout=10.0,
        trigger_cooldown_minutes=0,  # No cooldown for tests
        max_optimization_failures=2,
        dispatch_retry_attempts=1,
        dispatch_retry_delay_seconds=0.1,
        shutdown_timeout_seconds=1.0,
    )


@pytest.fixture
def mock_ocpp_server():
    """Mock OCPPServer for testing dispatch and control loop."""
    server = AsyncMock()
    server.get_charge_point = MagicMock(return_value=None)  # Default: no chargers
    server.start = AsyncMock()
    server.stop = AsyncMock()
    return server


@pytest.fixture
def mock_charge_point():
    """Mock FleetChargePoint for testing OCPP dispatch."""
    cp = AsyncMock()
    cp.set_charging_profile = AsyncMock(return_value=True)
    cp.clear_charging_profile = AsyncMock(return_value=True)
    cp.get_composite_schedule = AsyncMock(return_value=None)
    cp.remote_start_transaction = AsyncMock(return_value={'status': 'Accepted'})
    cp.remote_stop_transaction = AsyncMock(return_value={'status': 'Accepted'})
    return cp


@pytest.fixture
def mock_ocpp_server_with_charger(mock_ocpp_server, mock_charge_point):
    """Mock OCPPServer with a connected charge point."""
    mock_ocpp_server.get_charge_point = MagicMock(return_value=mock_charge_point)
    return mock_ocpp_server, mock_charge_point


@pytest.fixture
def sample_prices_flat():
    """Sample flat electricity prices (96 timesteps)."""
    return [0.10] * 96


@pytest.fixture
def sample_prices_tou():
    """Sample time-of-use electricity prices (96 timesteps)."""
    prices = []
    for i in range(96):
        hour = (i * 15 // 60) % 24
        if 16 <= hour < 21:  # Peak 4pm-9pm
            prices.append(0.25)
        elif 9 <= hour < 16 or 21 <= hour < 23:  # Mid-peak
            prices.append(0.15)
        else:  # Off-peak
            prices.append(0.08)
    return prices


@pytest.fixture
def sample_prices_spike():
    """Sample prices with a spike (96 timesteps)."""
    prices = [0.10] * 96
    # Spike at hours 17-19 (timesteps 68-76)
    for i in range(68, 77):
        prices[i] = 0.50
    return prices


# ============ Async Test Utilities ============

@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


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
    
    # Telemetry methods
    client.insert_telemetry_batch = AsyncMock()  # Add missing method
    
    # Health check method
    client.health_check = AsyncMock(return_value={"status": "healthy"})
    
    return client


# Supabase removed - PRD specifies TimescaleDB only
# @pytest.fixture
# def mock_supabase_client():
#     """Mock SupabaseClient with common methods."""
#     ...


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
    # Note: Config requires all fields, but we only need timescale for tests
    # If Config initialization fails, we'll skip tests that need it
    try:
        return Config(
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test"
            ),
            # Supabase removed - PRD specifies TimescaleDB only
            # Other fields use defaults from Config class
        )
    except (ImportError, AttributeError):
        # If Config or TimescaleConfig not available, return None
        # Tests using this fixture should handle None case
        return None


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


# ============ Real Database Fixtures ============

@pytest.fixture(scope="session")
def test_database_url():
    """Get test database URL from environment."""
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test"
    )


@pytest.fixture(scope="session")
async def real_db_pool(test_database_url):
    """Create a real database pool for integration tests.
    
    Requires TEST_DATABASE_URL environment variable or running docker-compose.test.yml.
    """
    if not ASYNCPG_AVAILABLE:
        pytest.skip("asyncpg not available")
    
    try:
        pool = await asyncpg.create_pool(
            test_database_url,
            min_size=2,
            max_size=10,
            command_timeout=60,
        )
        yield pool
        await pool.close()
    except Exception as e:
        pytest.skip(f"Database not available: {e}")


@pytest.fixture
async def db_connection(real_db_pool):
    """Get a single database connection from the pool."""
    async with real_db_pool.acquire() as conn:
        yield conn


@pytest.fixture
async def clean_db(db_connection):
    """Clean database tables before each test."""
    # Clean tables in dependency order
    await db_connection.execute("DELETE FROM charging_commands")
    await db_connection.execute("DELETE FROM optimization_runs")
    await db_connection.execute("DELETE FROM interdepot_messages")
    await db_connection.execute("DELETE FROM telemetry")
    await db_connection.execute("DELETE FROM prices")
    yield db_connection


@pytest.fixture
def unique_test_id():
    """Generate a unique test identifier."""
    return f"test_{uuid4().hex[:8]}"


# ============ Depot Factory Fixtures ============

@pytest.fixture
def depot_config_factory():
    """Factory for creating DepotConfig with customizable parameters."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    def _create(
        n_vehicles: int = 3,
        n_chargers: int = 5,
        charger_power: float = 80.0,  # For backward compatibility, converted to charger_groups
        battery_capacity: float = 500.0,
        max_site_power: float = 800.0,
        n_timesteps: int = 96,
        charger_groups: Optional[dict[float, int]] = None,  # New: explicit charger groups
    ) -> DepotConfig:
        # Convert legacy charger_power/n_chargers to charger_groups if not provided
        if charger_groups is None:
            charger_groups = {charger_power: n_chargers}
        
        # Build vehicle capacities and max_charge_kw
        vehicle_ids = [f'bus_{i:02d}' for i in range(1, n_vehicles + 1)]
        vehicle_capacities = {vid: 324.0 for vid in vehicle_ids}
        vehicle_max_charge_kw = {vid: charger_power for vid in vehicle_ids}
        
        return DepotConfig(
            vehicle_capacities=vehicle_capacities,
            vehicle_max_charge_kw=vehicle_max_charge_kw,
            charger_groups=charger_groups,
            charger_efficiency=0.95,
            charger_vehicle_access={},  # All vehicles can access all chargers (simple case)
            battery_capacity=battery_capacity,
            battery_power=100.0,
            battery_efficiency=0.92,
            battery_soc_min=0.2,
            battery_soc_max=0.8,
            max_site_power=max_site_power,
            delta_t=0.25,
            n_timesteps=n_timesteps,
        )
    return _create


# ============ Helper Functions for DepotConfig Migration ============

def get_total_chargers(config: DepotConfig) -> int:
    """Get total number of chargers from charger_groups.
    
    Helper function for migrating from n_chargers property.
    
    Args:
        config: DepotConfig instance
        
    Returns:
        Total number of chargers (sum of all charger group counts)
    """
    return sum(config.charger_groups.values())


def get_charger_power(config: DepotConfig) -> float:
    """Get charger power for single-group configs (most tests).
    
    Helper function for migrating from charger_power property.
    For single-group configs, returns the rated_kw.
    For multi-group configs, raises ValueError.
    
    Args:
        config: DepotConfig instance
        
    Returns:
        Charger power (rated_kw) for single-group configs
        
    Raises:
        ValueError: If config has multiple charger groups
    """
    if len(config.charger_groups) == 1:
        return list(config.charger_groups.keys())[0]
    raise ValueError(
        f"Multiple charger groups found: {config.charger_groups}. "
        "Use charger_groups directly for multi-group configs."
    )


def create_depot_config_legacy(
    vehicle_capacities: dict[str, float],
    charger_power: float,
    n_chargers: int,
    charger_efficiency: float = 0.95,
    battery_capacity: float = 500.0,
    battery_power: float = 100.0,
    max_site_power: float = 800.0,
    delta_t: float = 0.25,
    n_timesteps: int = 96,
    **kwargs
) -> DepotConfig:
    """Create DepotConfig using legacy API (charger_power, n_chargers).
    
    This is a helper function for backward compatibility during migration.
    It converts legacy parameters to the new charger_groups format.
    
    Args:
        vehicle_capacities: Dict mapping vehicle_id to battery capacity (kWh)
        charger_power: Charger rated power (kW) - converted to charger_groups
        n_chargers: Number of chargers - converted to charger_groups
        charger_efficiency: Charger efficiency (default 0.95)
        battery_capacity: Stationary battery capacity (kWh)
        battery_power: Stationary battery power (kW)
        max_site_power: Maximum site power (kW)
        delta_t: Time step duration (hours)
        n_timesteps: Number of time steps
        **kwargs: Additional fields (vehicle_max_charge_kw, charger_vehicle_access, etc.)
        
    Returns:
        DepotConfig instance with charger_groups set appropriately
    """
    if not CORE_MODELS_AVAILABLE:
        raise ImportError("Core models not available")
    
    vehicle_ids = list(vehicle_capacities.keys())
    
    # Convert legacy charger_power/n_chargers to charger_groups
    charger_groups = {charger_power: n_chargers}
    
    # Set vehicle_max_charge_kw if not provided
    vehicle_max_charge_kw = kwargs.get(
        'vehicle_max_charge_kw',
        {vid: charger_power for vid in vehicle_ids}
    )
    
    # Set charger_vehicle_access if not provided (empty = all accessible)
    charger_vehicle_access = kwargs.get('charger_vehicle_access', {})
    
    return DepotConfig(
        vehicle_capacities=vehicle_capacities,
        vehicle_max_charge_kw=vehicle_max_charge_kw,
        charger_groups=charger_groups,
        charger_efficiency=charger_efficiency,
        charger_vehicle_access=charger_vehicle_access,
        battery_capacity=battery_capacity,
        battery_power=battery_power,
        battery_efficiency=kwargs.get('battery_efficiency', 0.92),
        battery_soc_min=kwargs.get('battery_soc_min', 0.2),
        battery_soc_max=kwargs.get('battery_soc_max', 0.8),
        max_site_power=max_site_power,
        delta_t=delta_t,
        n_timesteps=n_timesteps,
    )


@pytest.fixture
def depot_state_factory(depot_config_factory):
    """Factory for creating DepotState with customizable parameters."""
    if not CORE_MODELS_AVAILABLE:
        pytest.skip("Core models not available")
    
    def _create(
        config: DepotConfig = None,
        initial_socs: Dict[str, float] = None,
        prices: List[float] = None,
        price_pattern: str = 'flat',
    ) -> DepotState:
        if config is None:
            config = depot_config_factory()
        
        n_t = config.n_timesteps
        vehicles = list(config.vehicle_capacities.keys())
        
        # Generate prices based on pattern
        if prices is None:
            if price_pattern == 'flat':
                prices = [0.10] * n_t
            elif price_pattern == 'tou':
                prices = []
                for i in range(n_t):
                    hour = (i * 0.25) % 24
                    if 16 <= hour < 21:
                        prices.append(0.30)
                    elif 9 <= hour < 16:
                        prices.append(0.15)
                    else:
                        prices.append(0.08)
            elif price_pattern == 'spike':
                prices = [0.10] * n_t
                for i in range(68, 77):  # Peak hours
                    prices[i] = 0.50
            else:
                prices = [0.10] * n_t
        
        # Set initial SoCs
        if initial_socs is None:
            initial_socs = {v: 0.3 + 0.05 * i for i, v in enumerate(vehicles)}
        
        return DepotState(
            vehicle_socs=initial_socs,
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={v: [True] * n_t for v in vehicles},
            energy_requirements={v: 180.0 + 20.0 * i for i, v in enumerate(vehicles)},
            departure_times={v: 48 + 12 * i for i, v in enumerate(vehicles)},
            building_power=[50.0] * n_t,
        )
    return _create


# ============ OCPP Test Utilities ============

@pytest.fixture
def ocpp_server_factory():
    """Factory for creating mock OCPP servers with chargers."""
    def _create(charger_ids: List[str] = None, all_accept: bool = True):
        server = AsyncMock()
        charge_points = {}
        
        if charger_ids is None:
            charger_ids = [f'charger_{i}' for i in range(1, 6)]
        
        for cp_id in charger_ids:
            cp = AsyncMock()
            cp.set_charging_profile = AsyncMock(return_value=all_accept)
            cp.remote_start_transaction = AsyncMock(return_value=True)
            cp.remote_stop_transaction = AsyncMock(return_value=True)
            charge_points[cp_id] = cp
        
        server.charge_points = charge_points
        server.get_charge_point = MagicMock(side_effect=lambda x: charge_points.get(x))
        server.is_connected = MagicMock(side_effect=lambda x: x in charge_points)
        server.start = AsyncMock()
        server.stop = AsyncMock()
        
        return server
    return _create


# ============ Timing Utilities ============

class TimingContext:
    """Context manager for timing code blocks."""
    
    def __init__(self):
        self.start_time = None
        self.end_time = None
        self.elapsed = None
    
    def __enter__(self):
        import time
        self.start_time = time.time()
        return self
    
    def __exit__(self, *args):
        import time
        self.end_time = time.time()
        self.elapsed = self.end_time - self.start_time


@pytest.fixture
def timing():
    """Fixture for timing operations."""
    return TimingContext


# ============ Assertion Helpers ============

def assert_schedule_valid(result: 'OptimizationResult', config: 'DepotConfig'):
    """Assert optimization result schedule is valid."""
    assert result.status == 'completed'
    assert result.schedule is not None
    
    for vehicle_id in config.vehicle_capacities:
        assert vehicle_id in result.schedule, f"Missing schedule for {vehicle_id}"
        schedule = result.schedule[vehicle_id]
        assert 'charging_power' in schedule
        assert 'soc' in schedule
        assert len(schedule['charging_power']) == config.n_timesteps
        assert len(schedule['soc']) == config.n_timesteps


def assert_departures_met(
    result: 'OptimizationResult',
    state: 'DepotState',
    min_soc: float = 0.98,
):
    """Assert all vehicle departure SoC requirements are met."""
    violations = []
    for vehicle_id, schedule in result.schedule.items():
        departure_t = state.departure_times.get(vehicle_id, len(schedule['soc']) - 1)
        if departure_t < len(schedule['soc']):
            final_soc = schedule['soc'][departure_t]
            if final_soc < min_soc:
                violations.append({
                    'vehicle': vehicle_id,
                    'soc': final_soc,
                    'required': min_soc,
                })
    
    if violations:
        pytest.fail(f"Departure SoC violations: {violations}")


def assert_solve_time_within_limit(result: 'OptimizationResult', limit: float = 30.0):
    """Assert optimization solve time is within limit."""
    assert result.solve_time < limit, (
        f"Solve time {result.solve_time:.2f}s exceeds {limit}s limit"
    )


# ============ Test Data Generators ============

def generate_vehicle_telemetry(
    vehicle_id: str,
    n_points: int = 100,
    start_soc: float = 0.3,
    charging: bool = True,
):
    """Generate synthetic vehicle telemetry data."""
    from datetime import datetime, timezone, timedelta
    
    data = []
    current_soc = start_soc
    base_time = datetime.now(timezone.utc) - timedelta(hours=n_points / 4)
    
    for i in range(n_points):
        if charging and current_soc < 1.0:
            current_soc = min(1.0, current_soc + 0.01)
        
        data.append({
            'time': base_time + timedelta(minutes=15 * i),
            'vehicle_id': vehicle_id,
            'soc': current_soc,
            'charging_power': 80.0 if charging and current_soc < 1.0 else 0.0,
            'status': 'Charging' if charging and current_soc < 1.0 else 'Available',
        })
    
    return data


def generate_price_series(
    n_timesteps: int = 96,
    pattern: str = 'tou',
    base_price: float = 0.10,
):
    """Generate synthetic price series."""
    from datetime import datetime, timezone, timedelta
    import random
    
    data = []
    base_time = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    for i in range(n_timesteps):
        hour = (i * 0.25) % 24
        
        if pattern == 'flat':
            price = base_price
        elif pattern == 'tou':
            if 16 <= hour < 21:
                price = base_price * 3
            elif 9 <= hour < 16:
                price = base_price * 1.5
            else:
                price = base_price
        elif pattern == 'volatile':
            random.seed(i)  # Reproducible
            price = base_price * (1 + random.uniform(-0.3, 0.5))
        else:
            price = base_price
        
        data.append({
            'time': base_time + timedelta(minutes=15 * i),
            'node_id': 'TEST_NODE',
            'price': price,
        })
    
    return data


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
    config.addinivalue_line(
        "markers", "performance: mark test as a performance benchmark"
    )
    config.addinivalue_line(
        "markers", "benchmark: mark test for pytest-benchmark"
    )
    config.addinivalue_line(
        "markers", "database: mark test as requiring real database"
    )
    config.addinivalue_line(
        "markers", "acceptance: PRD acceptance criteria tests (AT-01 through AT-07)"
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
