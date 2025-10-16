"""Enhanced integration tests with database-backed flows."""

import pytest
import asyncio
import tempfile
import shutil
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Import test dependencies
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
from websocket_handler.config import Config, TimescaleConfig, SupabaseConfig
from websocket_handler.connection_manager import ConnectionManager
from websocket_handler.timescale_client import TimescaleClient
from websocket_handler.timescale_schema import create_tables
from websocket_handler.optimization_engine import OptimizationEngine
from websocket_handler.price_feeder import PriceFeederService
from websocket_handler.monitoring_manager import MonitoringManager
from websocket_handler.privacy_manager import PrivacyManager
from websocket_handler.tariff_manager import TariffManager


class TestDatabaseIntegration:
    """Integration tests with real database operations."""

    @pytest.fixture
    async def temp_db_client(self):
        """Create temporary database client for testing."""
        # Use in-memory SQLite for testing
        client = TimescaleClient(
            host=":memory:",
            port=0,
            database="test_db",
            user="test",
            password="test"
        )
        
        try:
            await client.initialize()
            await create_tables(client)
            yield client
        finally:
            await client.close()

    @pytest.fixture
    def test_config(self):
        """Create test configuration."""
        return Config(
            timescale=TimescaleConfig(
                service_url="sqlite:///:memory:",
                host=":memory:",
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
    def connection_manager(self):
        """Create connection manager."""
        return ConnectionManager()

    @pytest.fixture
    def ocpp_handler(self, test_config, temp_db_client, connection_manager):
        """Create OCPP handler with real database."""
        return EnhancedOCPPChargePoint(
            "TEST_STATION_001",
            test_config,
            temp_db_client,
            connection_manager
        )

    @pytest.mark.asyncio
    async def test_complete_charging_session_with_database(self, ocpp_handler, temp_db_client):
        """Test complete charging session with database persistence."""
        
        # 1. Boot Notification
        from ocpp.v21.datatypes import ChargingStationType
        from ocpp.v21.enums import RegistrationStatusEnumType

        charging_station = ChargingStationType(
            model="TestModel",
            vendor_name="TestVendor",
            serial_number="SN123456",
            firmware_version="1.0.0"
        )

        boot_result = await ocpp_handler.on_boot_notification(
            charging_station, "2.0.1", "2023-01-01T00:00:00Z"
        )
        assert boot_result["status"] == RegistrationStatusEnumType.accepted

        # Verify device model was stored
        variables = await temp_db_client.get_device_variables(
            "TEST_STATION_001", "ChargingStation", "VendorName"
        )
        assert len(variables) > 0
        assert variables[0]["actual_value"] == "TestVendor"

        # 2. Status Notification
        from ocpp.v21.enums import ConnectorStatusEnumType

        await ocpp_handler.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status=ConnectorStatusEnumType.available,
            timestamp="2023-01-01T00:00:00Z"
        )

        # 3. Request Start Transaction
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType

        id_token = IdTokenType(id_token="AUTH123", type=IdTokenEnumType.key_code)

        start_result = await ocpp_handler.on_request_start_transaction(
            evse_id=1,
            id_token=id_token,
            remote_start_id=1
        )
        assert start_result["status"] == "Accepted"
        transaction_id = start_result["transactionId"]

        # Verify transaction was stored
        transaction = await temp_db_client.get_transaction(transaction_id)
        assert transaction is not None
        assert transaction["station_id"] == "TEST_STATION_001"

        # 4. Transaction Event - Started
        from ocpp.v21.enums import TransactionEventEnumType, TriggerReasonEnumType

        transaction_started = await ocpp_handler.on_transaction_event(
            event_type=TransactionEventEnumType.started,
            timestamp="2023-01-01T00:00:00Z",
            trigger_reason=TriggerReasonEnumType.authorized,
            seq_no=1,
            transaction_info={
                "transactionId": transaction_id,
                "chargingState": "Charging"
            },
            id_token=id_token
        )
        assert transaction_started["status"] == "Accepted"

        # 5. Meter Values
        from ocpp.v21.datatypes import MeterValueType, SampledValueType
        from ocpp.v21.enums import ReadingContextEnumType, MeasurandEnumType, UnitOfMeasureEnumType

        sampled_value = SampledValueType(
            value="22.5",
            context=ReadingContextEnumType.sample_periodic,
            format="Raw",
            measurand=MeasurandEnumType.energy_active_import_register,
            unit_of_measure=UnitOfMeasureEnumType.kwh
        )

        meter_value = MeterValueType(
            timestamp="2023-01-01T00:00:00Z",
            sampled_value=[sampled_value]
        )

        await ocpp_handler.on_meter_values(
            evse_id=1,
            meter_value=[meter_value],
            transaction_id=transaction_id
        )

        # 6. Transaction Event - Ended
        transaction_ended = await ocpp_handler.on_transaction_event(
            event_type=TransactionEventEnumType.ended,
            timestamp="2023-01-01T01:00:00Z",
            trigger_reason=TriggerReasonEnumType.ev_disconnected,
            seq_no=2,
            transaction_info={
                "transactionId": transaction_id,
                "chargingState": "EVDisconnected"
            },
            id_token=id_token
        )
        assert transaction_ended["status"] == "Accepted"

        # Verify final transaction state
        final_transaction = await temp_db_client.get_transaction(transaction_id)
        assert final_transaction["charging_state"] == "EVDisconnected"

    @pytest.mark.asyncio
    async def test_charging_profile_management_with_database(self, ocpp_handler, temp_db_client):
        """Test charging profile management with database persistence."""
        
        # 1. Set Charging Profile
        from ocpp.v21.datatypes import ChargingProfileType, ChargingScheduleType, ChargingSchedulePeriodType
        from ocpp.v21.enums import ChargingProfilePurposeEnumType, ChargingProfileKindEnumType, ChargingRateUnitEnumType

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

        set_profile_result = await ocpp_handler.on_set_charging_profile(
            evse_id=1,
            charging_profile=profile
        )
        assert set_profile_result["status"] == "Accepted"

        # Verify profile was stored
        profiles = await temp_db_client.get_active_charging_profiles("TEST_STATION_001", 1)
        assert len(profiles) > 0
        assert profiles[0]["profile_id"] == 1

        # 2. Get Charging Profiles
        get_profiles_result = await ocpp_handler.on_get_charging_profiles(
            request_id=1,
            evse_id=1,
            charging_profile_purpose="TxDefaultProfile",
            stack_level=0
        )
        assert get_profiles_result["status"] == "Accepted"

        # 3. Get Composite Schedule
        composite_result = await ocpp_handler.on_get_composite_schedule(
            request_id=2,
            evse_id=1,
            duration=3600,
            charging_rate_unit="W"
        )
        assert composite_result["status"] == "Accepted"

        # 4. Clear Charging Profile
        clear_result = await ocpp_handler.on_clear_charging_profile(
            charging_profile_id=1,
            charging_profile_purpose="TxDefaultProfile",
            evse_id=1
        )
        assert clear_result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_monitoring_and_alerting_with_database(self, ocpp_handler, temp_db_client):
        """Test monitoring and alerting with database persistence."""
        
        # 1. Set Variable Monitoring
        set_monitoring_result = await ocpp_handler.on_set_variable_monitoring(
            monitoring_data=[{
                "component": {"name": "ChargingStation"},
                "variable": {"name": "HeartbeatInterval"},
                "monitoring": {
                    "type": "ThresholdMonitoring",
                    "severity": 1,
                    "threshold": {"monitor": "GT", "value": "600"}
                }
            }]
        )
        assert set_monitoring_result["status"] == "Accepted"

        # Verify monitoring configuration was stored
        # Note: This would require implementing get_variable_monitoring in TimescaleClient

        # 2. Get Monitoring Report
        monitoring_report_result = await ocpp_handler.on_get_monitoring_report(
            request_id=1,
            monitoring_criteria=[],
            component_variable=[]
        )
        assert monitoring_report_result["status"] == "Accepted"

        # 3. Notify Monitoring Report
        notify_monitoring_result = await ocpp_handler.on_notify_monitoring_report(
            request_id=1,
            generated_at="2023-01-01T00:00:00Z",
            seq_no=1,
            monitoring_data=[]
        )
        assert notify_monitoring_result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_privacy_compliance_with_database(self, ocpp_handler, temp_db_client):
        """Test privacy compliance with database persistence."""
        
        # 1. Customer Information Request
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType, CustomerInformationStatusEnumType

        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)

        customer_info_result = await ocpp_handler.on_customer_information(
            request_id=1,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123"
        )
        assert customer_info_result["status"] == CustomerInformationStatusEnumType.accepted

        # Verify privacy request was stored
        # Note: This would require implementing get_customer_information_requests in TimescaleClient

        # 2. Delete Customer Information Request
        delete_customer_result = await ocpp_handler.on_delete_customer_information(
            request_id=2,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123"
        )
        assert delete_customer_result["status"] == CustomerInformationStatusEnumType.accepted


class TestCrossManagerIntegration:
    """Test integration between different managers."""

    @pytest.fixture
    async def temp_db_client(self):
        """Create temporary database client for testing."""
        client = TimescaleClient(
            host=":memory:",
            port=0,
            database="test_db",
            user="test",
            password="test"
        )
        
        try:
            await client.initialize()
            await create_tables(client)
            yield client
        finally:
            await client.close()

    @pytest.fixture
    def test_config(self):
        """Create test configuration."""
        return Config(
            timescale=TimescaleConfig(
                service_url="sqlite:///:memory:",
                host=":memory:",
                user="test",
                password="test"
            )
        )

    @pytest.mark.asyncio
    async def test_charging_profiles_with_tariffs(self, temp_db_client, test_config):
        """Test charging profiles integrated with tariff management."""
        
        # Create tariff manager
        tariff_manager = TariffManager(temp_db_client)
        
        # Set tariff
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
        
        tariff_result = await tariff_manager.set_tariff("TEST_STATION_001", tariff_data)
        assert tariff_result["status"] == "Accepted"
        
        # Verify tariff was stored
        tariffs = await temp_db_client.get_active_tariffs("TEST_STATION_001")
        assert len(tariffs) > 0
        assert tariffs[0]["tariff_id"] == "TARIFF_001"
        
        # Calculate transaction cost
        cost_result = await tariff_manager.calculate_transaction_cost(
            "TEST_STATION_001", "TXN123", 22.0, 3600
        )
        assert cost_result["total_cost"] == 4.4  # 22 kWh * $0.20
        assert cost_result["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_optimization_with_price_feeder(self, temp_db_client, test_config):
        """Test optimization engine integrated with price feeder."""
        
        # Create price feeder
        price_feeder = PriceFeederService(test_config.price_feeder, temp_db_client)
        
        # Create optimization engine
        optimization_engine = OptimizationEngine(
            test_config.optimization,
            temp_db_client,
            Mock(),  # Supabase client
            Mock()   # Connection manager
        )
        
        # Set optimization engine in price feeder
        price_feeder.set_optimization_engine(optimization_engine)
        
        # Mock price data
        price_data = [
            {
                "time": datetime.now(timezone.utc),
                "node_id": "TH_SP15_GEN-APND",
                "lmp_price_mwh": 100.0,
                "energy_component_mwh": 95.0,
                "congestion_component_mwh": 3.0,
                "loss_component_mwh": 2.0,
                "ghg_adder_mwh": 0.0
            }
        ]
        
        # Store price data
        await temp_db_client.store_electricity_prices(price_data)
        
        # Mock charging session
        session_data = {
            "station_id": "TEST_STATION_001",
            "evse_id": 1,
            "connector_id": 1,
            "start_time": datetime.now(timezone.utc),
            "end_time": datetime.now(timezone.utc) + timedelta(hours=2),
            "start_soc_percent": 50.0
        }
        
        # Store charging session
        await temp_db_client.store_transaction(session_data)
        
        # Run optimization
        await optimization_engine._run_optimization("price_update")
        
        # Verify optimization decision was stored
        # Note: This would require implementing get_optimization_decisions in TimescaleClient

    @pytest.mark.asyncio
    async def test_monitoring_with_privacy(self, temp_db_client, test_config):
        """Test monitoring integrated with privacy management."""
        
        # Create monitoring manager
        monitoring_manager = MonitoringManager(temp_db_client)
        
        # Create privacy manager
        privacy_manager = PrivacyManager(temp_db_client)
        
        # Set variable monitoring
        monitoring_result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION_001",
            component_name="ChargingStation",
            variable_name="Temperature",
            monitoring_criterion="ThresholdMonitoring",
            threshold=80.0
        )
        assert monitoring_result["status"] == "Accepted"
        
        # Create privacy request
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType
        
        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)
        
        privacy_result = await privacy_manager.handle_customer_information_request(
            station_id="TEST_STATION_001",
            request_id=1,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123"
        )
        assert privacy_result["status"] == "Accepted"
        
        # Verify both operations were stored
        # Note: This would require implementing get methods for both managers


class TestErrorHandlingIntegration:
    """Test error handling integration scenarios."""

    @pytest.fixture
    async def temp_db_client(self):
        """Create temporary database client for testing."""
        client = TimescaleClient(
            host=":memory:",
            port=0,
            database="test_db",
            user="test",
            password="test"
        )
        
        try:
            await client.initialize()
            await create_tables(client)
            yield client
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_with_database_errors(self, temp_db_client):
        """Test circuit breaker with database errors."""
        
        # Mock database client that fails
        failing_client = Mock()
        failing_client.store_transaction = AsyncMock(side_effect=Exception("Database error"))
        
        # Create OCPP handler with failing client
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
        from websocket_handler.config import Config
        
        config = Config()
        connection_manager = ConnectionManager()
        
        handler = EnhancedOCPPChargePoint(
            "TEST_STATION_001",
            config,
            failing_client,
            connection_manager
        )
        
        # Test circuit breaker functionality
        circuit_breaker = handler.error_handler.get_circuit_breaker("database")
        
        # Should fail multiple times before opening
        for i in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_client.store_transaction, {"test": "data"})
        
        # Circuit should now be open
        assert circuit_breaker.state.value == "open"

    @pytest.mark.asyncio
    async def test_retry_manager_with_database_recovery(self, temp_db_client):
        """Test retry manager with database recovery."""
        
        # Mock database client that fails then succeeds
        call_count = 0
        
        async def flaky_store_transaction(data):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary database error")
            return "success"
        
        flaky_client = Mock()
        flaky_client.store_transaction = AsyncMock(side_effect=flaky_store_transaction)
        
        # Create OCPP handler with flaky client
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
        from websocket_handler.config import Config
        
        config = Config()
        connection_manager = ConnectionManager()
        
        handler = EnhancedOCPPChargePoint(
            "TEST_STATION_001",
            config,
            flaky_client,
            connection_manager
        )
        
        # Test retry manager
        retry_manager = handler.error_handler.retry_manager
        
        result = await retry_manager.execute_with_retry(
            flaky_client.store_transaction, 
            {"test": "data"},
            max_retries=3
        )
        
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_dead_letter_queue_with_database_persistence(self, temp_db_client):
        """Test dead letter queue with database persistence."""
        
        # Create OCPP handler
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
        from websocket_handler.config import Config
        
        config = Config()
        connection_manager = ConnectionManager()
        
        handler = EnhancedOCPPChargePoint(
            "TEST_STATION_001",
            config,
            temp_db_client,
            connection_manager
        )
        
        # Test dead letter queue
        dlq = handler.error_handler.dlq
        
        # Add message to DLQ
        original_message = {"test": "data"}
        error = Exception("Test error")
        
        await dlq.add_message(original_message, error)
        
        # Verify message was added
        assert len(dlq.queue) == 1
        assert dlq.queue[0]["message"] == original_message
        assert dlq.queue[0]["error"] == "Test error"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
