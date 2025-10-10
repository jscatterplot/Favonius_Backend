"""Unit tests for core managers."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# Import managers
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.device_model import DeviceModel, ComponentType, VariableType
from websocket_handler.charging_profile_manager import ChargingProfileManager
from websocket_handler.transaction_manager import TransactionManager
from websocket_handler.certificate_manager import CertificateManager
from websocket_handler.monitoring_manager import MonitoringManager
from websocket_handler.display_manager import DisplayManager
from websocket_handler.tariff_manager import TariffManager
from websocket_handler.privacy_manager import PrivacyManager
from websocket_handler.error_handler import CircuitBreaker, RetryManager, DeadLetterQueue


class TestDeviceModel:
    """Test DeviceModel functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_device_component = AsyncMock()
        client.store_device_variable = AsyncMock()
        client.get_device_variables = AsyncMock()
        return client

    @pytest.fixture
    def device_model(self, mock_timescale_client):
        """Create DeviceModel instance."""
        return DeviceModel(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_initialize_complete_device_model(self, device_model, mock_timescale_client):
        """Test complete device model initialization."""
        station_id = "TEST_STATION_001"
        station_info = {
            "vendor_name": "TestVendor",
            "model": "TestModel",
            "serial_number": "SN123456",
            "firmware_version": "1.0.0",
            "max_power": 22.0,
            "v2x_capable": True,
            "num_evses": 2,
            "num_connectors": 4
        }

        await device_model.initialize_complete_device_model(station_id, station_info)

        # Verify component creation calls
        assert mock_timescale_client.store_device_component.call_count >= 10  # Multiple components
        assert mock_timescale_client.store_device_variable.call_count >= 50  # Many variables

    @pytest.mark.asyncio
    async def test_get_variables(self, device_model, mock_timescale_client):
        """Test variable retrieval."""
        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "VendorName",
                "actual_value": "TestVendor"
            }
        ]

        result = await device_model.get_variables("TEST_STATION", [{
            "component": {"name": "ChargingStation"},
            "variable": {"name": "VendorName"}
        }])

        assert result["status"] == "Accepted"
        assert len(result["getVariableResult"]) == 1
        assert result["getVariableResult"][0]["attributeValue"] == "TestVendor"

    @pytest.mark.asyncio
    async def test_set_variables(self, device_model, mock_timescale_client):
        """Test variable setting."""
        mock_timescale_client.store_device_variable.return_value = None

        result = await device_model.set_variables("TEST_STATION", [{
            "component": {"name": "ChargingStation"},
            "variable": {"name": "HeartbeatInterval"},
            "attributeValue": "300"
        }])

        assert result["status"] == "Accepted"
        mock_timescale_client.store_device_variable.assert_called_once()


class TestChargingProfileManager:
    """Test ChargingProfileManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_charging_profile = AsyncMock()
        client.get_active_charging_profiles = AsyncMock()
        client.remove_charging_profile = AsyncMock()
        return client

    @pytest.fixture
    def profile_manager(self, mock_timescale_client):
        """Create ChargingProfileManager instance."""
        return ChargingProfileManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_set_charging_profile(self, profile_manager, mock_timescale_client):
        """Test charging profile setting."""
        from ocpp.v21.datatypes import ChargingProfileType, ChargingScheduleType, ChargingSchedulePeriodType
        from ocpp.v21.enums import ChargingProfilePurposeEnumType, ChargingProfileKindEnumType, ChargingRateUnitEnumType

        # Create test profile
        schedule_period = ChargingSchedulePeriodType(
            start_period=0,
            limit=22.0
        )
        
        schedule = ChargingScheduleType(
            id=1,
            charging_rate_unit=ChargingRateUnitEnumType.w,
            charging_schedule_period=[schedule_period],
            duration=3600
        )
        
        profile = ChargingProfileType(
            id=1,
            stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeEnumType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindEnumType.absolute,
            charging_schedule=schedule
        )

        result = await profile_manager.set_charging_profile("TEST_STATION", 1, profile)

        assert result["status"] == "Accepted"
        mock_timescale_client.store_charging_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_composite_schedule(self, profile_manager, mock_timescale_client):
        """Test composite schedule calculation."""
        mock_timescale_client.get_active_charging_profiles.return_value = [
            {
                "profile_id": 1,
                "stack_level": 0,
                "schedule": {
                    "id": 1,
                    "chargingRateUnit": "W",
                    "chargingSchedulePeriod": [
                        {"startPeriod": 0, "limit": 22.0}
                    ],
                    "duration": 3600
                }
            }
        ]

        result = await profile_manager.get_composite_schedule("TEST_STATION", 1, 3600)

        assert result["status"] == "Accepted"
        assert "chargingSchedule" in result


class TestTransactionManager:
    """Test TransactionManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_transaction = AsyncMock()
        client.get_transaction = AsyncMock()
        client.update_transaction = AsyncMock()
        client.store_transaction_event = AsyncMock()
        client.get_id_token_info = AsyncMock()
        return client

    @pytest.fixture
    def transaction_manager(self, mock_timescale_client):
        """Create TransactionManager instance."""
        return TransactionManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_request_start_transaction(self, transaction_manager, mock_timescale_client):
        """Test transaction start request."""
        from ocpp.v21.datatypes import IdToken
        from ocpp.v21.enums import IdTokenEnumType, AuthorizationStatusEnumType

        # Mock authorization
        mock_timescale_client.get_id_token_info.return_value = {
            "status": AuthorizationStatusEnumType.accepted,
            "cache_timeout": datetime.now(timezone.utc) + timedelta(hours=1)
        }

        id_token = IdToken(id_token="AUTH123", type=IdTokenEnumType.key_code)

        result = await transaction_manager.request_start_transaction(
            "TEST_STATION", 1, None, id_token, None
        )

        assert result["status"] == "Accepted"
        assert "transactionId" in result
        mock_timescale_client.store_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_calculate_transaction_cost(self, transaction_manager, mock_timescale_client):
        """Test transaction cost calculation."""
        mock_timescale_client.get_transaction.return_value = {
            "transaction_id": "TXN123",
            "started_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "charging_state": "Charging"
        }
        mock_timescale_client.get_transaction_energy.return_value = {
            "energy_kwh": 22.0
        }
        mock_timescale_client.get_tariff.return_value = {
            "tariff_element": [
                {
                    "priceComponent": {
                        "type": "Energy",
                        "price": 0.20
                    }
                }
            ],
            "currency": "USD"
        }

        result = await transaction_manager.calculate_transaction_cost("TEST_STATION", "TXN123")

        assert result["total_cost"] == 4.4  # 22 kWh * $0.20
        assert result["currency"] == "USD"


class TestCircuitBreaker:
    """Test CircuitBreaker functionality."""

    @pytest.fixture
    def circuit_breaker(self):
        """Create CircuitBreaker instance."""
        return CircuitBreaker("test_service", failure_threshold=3, timeout_seconds=60)

    @pytest.mark.asyncio
    async def test_circuit_breaker_closed_state(self, circuit_breaker):
        """Test circuit breaker in closed state."""
        async def success_func():
            return "success"

        result = await circuit_breaker.call(success_func)
        assert result == "success"
        assert circuit_breaker.state.value == "closed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_failures(self, circuit_breaker):
        """Test circuit breaker opens after threshold failures."""
        async def failing_func():
            raise Exception("Test failure")

        # Should fail 3 times before opening
        for i in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)

        # Circuit should now be open
        assert circuit_breaker.state.value == "open"

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_when_open(self, circuit_breaker):
        """Test circuit breaker blocks calls when open."""
        # Force circuit to open state
        circuit_breaker.state = circuit_breaker.state.__class__("open")
        circuit_breaker.failure_count = 5

        async def any_func():
            return "should not be called"

        with pytest.raises(Exception):  # CircuitBreakerOpenError
            await circuit_breaker.call(any_func)


class TestRetryManager:
    """Test RetryManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_retry_attempt = AsyncMock()
        return client

    @pytest.fixture
    def retry_manager(self, mock_timescale_client):
        """Create RetryManager instance."""
        return RetryManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self, retry_manager):
        """Test retry manager succeeds on first attempt."""
        async def success_func():
            return "success"

        result = await retry_manager.execute_with_retry(success_func, max_retries=3)
        assert result == "success"

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_failures(self, retry_manager):
        """Test retry manager succeeds after some failures."""
        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary failure")
            return "success"

        result = await retry_manager.execute_with_retry(flaky_func, max_retries=3)
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausts_attempts(self, retry_manager):
        """Test retry manager exhausts all attempts."""
        async def always_failing_func():
            raise Exception("Permanent failure")

        with pytest.raises(Exception):
            await retry_manager.execute_with_retry(always_failing_func, max_retries=2)


class TestTariffManager:
    """Test TariffManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_tariff = AsyncMock()
        client.get_active_tariffs = AsyncMock()
        client.store_tariff_element = AsyncMock()
        client.store_tou_period = AsyncMock()
        client.calculate_tou_multiplier = AsyncMock()
        return client

    @pytest.fixture
    def tariff_manager(self, mock_timescale_client):
        """Create TariffManager instance."""
        return TariffManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_set_tariff(self, tariff_manager, mock_timescale_client):
        """Test tariff setting."""
        tariff_data = {
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

        result = await tariff_manager.set_tariff("TEST_STATION", tariff_data)

        assert result["status"] == "Accepted"
        mock_timescale_client.store_tariff.assert_called_once()

    @pytest.mark.asyncio
    async def test_calculate_transaction_cost(self, tariff_manager, mock_timescale_client):
        """Test transaction cost calculation."""
        mock_timescale_client.get_active_tariffs.return_value = [
            {
                "tariff_id": "TARIFF_001",
                "tariff_currency": "USD"
            }
        ]
        mock_timescale_client.get_tariff_elements.return_value = [
            {
                "element_type": "Energy",
                "price_per_unit": Decimal('0.20'),
                "unit": "kWh",
                "currency": "USD"
            }
        ]
        mock_timescale_client.calculate_tou_multiplier.return_value = 1.0

        result = await tariff_manager.calculate_transaction_cost(
            "TEST_STATION", "TXN123", 22.0, 3600
        )

        assert result["total_cost"] == 4.4  # 22 kWh * $0.20
        assert result["currency"] == "USD"


class TestPrivacyManager:
    """Test PrivacyManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_customer_information_request = AsyncMock()
        client.update_customer_information_status = AsyncMock()
        client.store_data_retention_policy = AsyncMock()
        client.store_consent_record = AsyncMock()
        client.anonymize_customer_data = AsyncMock()
        return client

    @pytest.fixture
    def privacy_manager(self, mock_timescale_client):
        """Create PrivacyManager instance."""
        return PrivacyManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_handle_customer_information_request(self, privacy_manager, mock_timescale_client):
        """Test customer information request handling."""
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType

        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)

        result = await privacy_manager.handle_customer_information_request(
            "TEST_STATION", 1, None, id_token, "CUSTOMER123"
        )

        assert result["status"] == "Accepted"
        mock_timescale_client.store_customer_information_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_data_retention_policy(self, privacy_manager, mock_timescale_client):
        """Test data retention policy creation."""
        policy_id = await privacy_manager.create_data_retention_policy(
            "transaction_data", 2555, True, "anonymize", "Financial records retention"
        )

        assert policy_id is not None
        mock_timescale_client.store_data_retention_policy.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_consent(self, privacy_manager, mock_timescale_client):
        """Test consent recording."""
        consent_id = await privacy_manager.record_consent(
            "CUSTOMER123", "data_processing", "granted", "explicit", "web_portal"
        )

        assert consent_id is not None
        mock_timescale_client.store_consent_record.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
