"""Integration tests for OCPP message flows."""

import pytest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# Import OCPP handler and managers
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
from websocket_handler.config import Config, TimescaleConfig, SupabaseConfig
from websocket_handler.connection_manager import ConnectionManager
from websocket_handler.timescale_client import TimescaleClient


class TestOCPPIntegration:
    """Integration tests for OCPP message flows."""

    @pytest.fixture
    async def timescale_client(self):
        """Create real TimescaleClient for integration tests."""
        # Use test database configuration
        config = TimescaleConfig(
            service_url="postgresql://postgres:password@localhost:5432/test_favonius",
            host="localhost",
            port=5432,
            database="test_favonius",
            user="postgres",
            password="password"
        )
        client = TimescaleClient(config)
        await client.connect()
        yield client
        await client.disconnect()

    @pytest.fixture
    async def ocpp_handler(self, timescale_client):
        """Create EnhancedOCPPChargePoint instance."""
        config = Config(
            timescale=TimescaleConfig(
                service_url="postgresql://test:test@localhost:5432/test",
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
        connection_manager = ConnectionManager(config)
        mock_connection = Mock()
        client = await timescale_client.__anext__()
        return EnhancedOCPPChargePoint("TEST_STATION_001", mock_connection, config, client, connection_manager)

    @pytest.mark.asyncio
    async def test_complete_charging_session_flow(self, ocpp_handler, timescale_client):
        """Test complete charging session from boot to transaction end."""
        
        # Await the async fixtures
        handler = await ocpp_handler
        client = await timescale_client.__anext__()
        
        # 1. Boot Notification
        from ocpp.v21.datatypes import ChargingStationType
        from ocpp.v21.enums import RegistrationStatusEnumType
        
        charging_station = ChargingStationType(
            model="TestModel",
            vendor_name="TestVendor",
            serial_number="SN123456",
            firmware_version="1.0.0"
        )
        
        boot_result = handler.on_boot_notification(
            charging_station, "PowerUp"
        )
        assert boot_result["status"] == RegistrationStatusEnumType.accepted

        # 2. Status Notification - Available
        from ocpp.v21.enums import ConnectorStatusEnumType

        status_result = await ocpp_handler.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status=ConnectorStatusEnumType.available,
            timestamp="2023-01-01T00:00:00Z"
        )
        assert status_result is None

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

        # 5. Meter Values - Energy consumption
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

        meter_result = await ocpp_handler.on_meter_values(
            evse_id=1,
            meter_value=[meter_value],
            transaction_id=transaction_id
        )
        assert meter_result is None

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

        # 7. Status Notification - Available
        final_status = await ocpp_handler.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status=ConnectorStatusEnumType.available,
            timestamp="2023-01-01T01:00:00Z"
        )
        assert final_status is None

    @pytest.mark.asyncio
    async def test_charging_profile_management_flow(self, ocpp_handler, timescale_client):
        """Test charging profile management flow."""
        
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
    async def test_device_configuration_flow(self, ocpp_handler, timescale_client):
        """Test device configuration management flow."""
        
        # 1. Get Base Report
        base_report_result = await ocpp_handler.on_get_base_report(
            request_id=1,
            report_base="ConfigurationInventory"
        )
        assert base_report_result["status"] == "Accepted"

        # 2. Get Variables
        get_vars_result = await ocpp_handler.on_get_variables(
            get_variable_data=[{
                "component": {"name": "ChargingStation"},
                "variable": {"name": "VendorName"}
            }]
        )
        assert get_vars_result["status"] == "Accepted"

        # 3. Set Variables
        set_vars_result = await ocpp_handler.on_set_variables(
            set_variable_data=[{
                "component": {"name": "ChargingStation"},
                "variable": {"name": "HeartbeatInterval"},
                "attributeValue": "300"
            }]
        )
        assert set_vars_result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_monitoring_and_alerting_flow(self, ocpp_handler, timescale_client):
        """Test monitoring and alerting flow."""
        
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
    async def test_display_message_flow(self, ocpp_handler, timescale_client):
        """Test display message management flow."""
        
        # 1. Set Display Message
        from ocpp.v21.datatypes import MessageInfoType, MessageContentType
        from ocpp.v21.enums import MessagePriorityEnumType, MessageStateEnumType, MessageFormatEnumType

        message_content = MessageContentType(
            format=MessageFormatEnumType.utf8,
            language="en",
            content="Charging in progress"
        )

        message_info = MessageInfoType(
            id=1,
            priority=MessagePriorityEnumType.normal_cycle,
            state=MessageStateEnumType.charging,
            start_date_time="2023-01-01T00:00:00Z",
            end_date_time="2023-01-01T23:59:59Z",
            message=message_content
        )

        set_message_result = await ocpp_handler.on_set_display_message(
            message=message_info
        )
        assert set_message_result["status"] == "Accepted"

        # 2. Clear Display Message
        clear_message_result = await ocpp_handler.on_clear_display_message(
            message_id=1
        )
        assert clear_message_result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_error_handling_flow(self, ocpp_handler, timescale_client):
        """Test error handling and resilience flow."""
        
        # Test circuit breaker functionality
        circuit_breaker = ocpp_handler.error_handler.get_circuit_breaker("test_service")
        
        # Test successful call
        async def success_func():
            return "success"
        
        result = await circuit_breaker.call(success_func)
        assert result == "success"
        assert circuit_breaker.state.value == "closed"

        # Test retry manager
        retry_manager = ocpp_handler.error_handler.retry_manager
        
        call_count = 0
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Temporary failure")
            return "success"
        
        result = await retry_manager.execute_with_retry(flaky_func, max_retries=3)
        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_privacy_compliance_flow(self, ocpp_handler, timescale_client):
        """Test privacy and GDPR compliance flow."""
        
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

        # 2. Delete Customer Information Request
        delete_customer_result = await ocpp_handler.on_delete_customer_information(
            request_id=2,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123"
        )
        assert delete_customer_result["status"] == CustomerInformationStatusEnumType.accepted


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
