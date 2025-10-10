"""Unit tests for OCPP message handling."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# Import OCPP handler
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint


class TestOCPPHandler:
    """Test OCPP message handling."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_device_component = AsyncMock()
        client.store_device_variable = AsyncMock()
        client.get_device_variables = AsyncMock()
        client.store_charging_profile = AsyncMock()
        client.get_active_charging_profiles = AsyncMock()
        client.store_transaction = AsyncMock()
        client.get_transaction = AsyncMock()
        client.store_certificate = AsyncMock()
        client.get_certificate = AsyncMock()
        client.store_monitoring_report = AsyncMock()
        client.store_display_message = AsyncMock()
        client.store_tariff = AsyncMock()
        client.store_customer_information_request = AsyncMock()
        return client

    @pytest.fixture
    def ocpp_handler(self, mock_timescale_client):
        """Create EnhancedOCPPChargePoint instance."""
        return EnhancedOCPPChargePoint("TEST_STATION", mock_timescale_client)

    @pytest.mark.asyncio
    async def test_boot_notification(self, ocpp_handler, mock_timescale_client):
        """Test BootNotification handling."""
        from ocpp.v21.datatypes import ChargingStationType
        from ocpp.v21.enums import RegistrationStatusEnumType

        charging_station = ChargingStationType(
            model="TestModel",
            vendor_name="TestVendor",
            serial_number="SN123456",
            firmware_version="1.0.0"
        )

        result = await ocpp_handler.on_boot_notification(
            charging_station, "1.6", "2023-01-01T00:00:00Z"
        )

        assert result["status"] == RegistrationStatusEnumType.accepted
        assert "currentTime" in result
        assert "interval" in result

    @pytest.mark.asyncio
    async def test_heartbeat(self, ocpp_handler):
        """Test Heartbeat handling."""
        result = await ocpp_handler.on_heartbeat()

        assert "currentTime" in result
        assert isinstance(result["currentTime"], str)

    @pytest.mark.asyncio
    async def test_status_notification(self, ocpp_handler, mock_timescale_client):
        """Test StatusNotification handling."""
        from ocpp.v21.enums import ConnectorStatusEnumType, ChargePointStatusEnumType

        result = await ocpp_handler.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status=ConnectorStatusEnumType.available,
            timestamp="2023-01-01T00:00:00Z",
            info="Test status"
        )

        assert result is None  # StatusNotification has no response

    @pytest.mark.asyncio
    async def test_meter_values(self, ocpp_handler, mock_timescale_client):
        """Test MeterValues handling."""
        from ocpp.v21.datatypes import MeterValueType, SampledValueType
        from ocpp.v21.enums import ReadingContextEnumType, MeasurandEnumType, UnitOfMeasureEnumType

        sampled_value = SampledValueType(
            value="22.5",
            context=ReadingContextEnumType.sample_periodic,
            format="Raw",
            measurand=MeasurandEnumType.energy_active_import_register,
            phase="L1",
            location="Outlet",
            unit_of_measure=UnitOfMeasureEnumType.kwh
        )

        meter_value = MeterValueType(
            timestamp="2023-01-01T00:00:00Z",
            sampled_value=[sampled_value]
        )

        result = await ocpp_handler.on_meter_values(
            evse_id=1,
            meter_value=[meter_value],
            transaction_id="TXN123"
        )

        assert result is None  # MeterValues has no response

    @pytest.mark.asyncio
    async def test_get_variables(self, ocpp_handler, mock_timescale_client):
        """Test GetVariables handling."""
        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "VendorName",
                "actual_value": "TestVendor"
            }
        ]

        result = await ocpp_handler.on_get_variables(
            get_variable_data=[{
                "component": {"name": "ChargingStation"},
                "variable": {"name": "VendorName"}
            }]
        )

        assert result["status"] == "Accepted"
        assert len(result["getVariableResult"]) == 1
        assert result["getVariableResult"][0]["attributeValue"] == "TestVendor"

    @pytest.mark.asyncio
    async def test_set_variables(self, ocpp_handler, mock_timescale_client):
        """Test SetVariables handling."""
        mock_timescale_client.store_device_variable.return_value = None

        result = await ocpp_handler.on_set_variables(
            set_variable_data=[{
                "component": {"name": "ChargingStation"},
                "variable": {"name": "HeartbeatInterval"},
                "attributeValue": "300"
            }]
        )

        assert result["status"] == "Accepted"
        assert len(result["setVariableResult"]) == 1

    @pytest.mark.asyncio
    async def test_set_charging_profile(self, ocpp_handler, mock_timescale_client):
        """Test SetChargingProfile handling."""
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

        result = await ocpp_handler.on_set_charging_profile(
            evse_id=1,
            charging_profile=profile
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_get_charging_profiles(self, ocpp_handler, mock_timescale_client):
        """Test GetChargingProfiles handling."""
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

        result = await ocpp_handler.on_get_charging_profiles(
            request_id=1,
            evse_id=1,
            charging_profile_purpose="TxDefaultProfile",
            stack_level=0
        )

        assert result["status"] == "Accepted"
        assert "chargingProfile" in result

    @pytest.mark.asyncio
    async def test_request_start_transaction(self, ocpp_handler, mock_timescale_client):
        """Test RequestStartTransaction handling."""
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType, AuthorizationStatusEnumType

        mock_timescale_client.get_id_token_info.return_value = {
            "status": AuthorizationStatusEnumType.accepted,
            "cache_timeout": datetime.now(timezone.utc) + timedelta(hours=1)
        }

        id_token = IdTokenType(id_token="AUTH123", type=IdTokenEnumType.key_code)

        result = await ocpp_handler.on_request_start_transaction(
            evse_id=1,
            id_token=id_token,
            remote_start_id=1,
            charging_profile=None
        )

        assert result["status"] == "Accepted"
        assert "transactionId" in result

    @pytest.mark.asyncio
    async def test_transaction_event(self, ocpp_handler, mock_timescale_client):
        """Test TransactionEvent handling."""
        from ocpp.v21.datatypes import IdTokenType, MeterValueType
        from ocpp.v21.enums import IdTokenEnumType, TransactionEventEnumType, TriggerReasonEnumType

        id_token = IdTokenType(id_token="AUTH123", type=IdTokenEnumType.key_code)

        result = await ocpp_handler.on_transaction_event(
            event_type=TransactionEventEnumType.started,
            timestamp="2023-01-01T00:00:00Z",
            trigger_reason=TriggerReasonEnumType.authorized,
            seq_no=1,
            transaction_info={
                "transactionId": "TXN123",
                "chargingState": "Charging"
            },
            id_token=id_token,
            meter_value=[],
            evse=None,
            cable_max_current=None,
            reservation_id=None,
            offline=None,
            number_of_phases_used=None,
            ocpp_central_system_requested=None
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_reset(self, ocpp_handler, mock_timescale_client):
        """Test Reset handling."""
        from ocpp.v21.enums import ResetEnumType, ResetStatusEnumType

        result = await ocpp_handler.on_reset(
            type=ResetEnumType.immediate
        )

        assert result["status"] == ResetStatusEnumType.accepted

    @pytest.mark.asyncio
    async def test_change_availability(self, ocpp_handler, mock_timescale_client):
        """Test ChangeAvailability handling."""
        from ocpp.v21.enums import OperationalStatusEnumType, AvailabilityStatusEnumType

        result = await ocpp_handler.on_change_availability(
            operational_status=OperationalStatusEnumType.operative
        )

        assert result["status"] == AvailabilityStatusEnumType.accepted

    @pytest.mark.asyncio
    async def test_get_monitoring_report(self, ocpp_handler, mock_timescale_client):
        """Test GetMonitoringReport handling."""
        mock_timescale_client.get_monitoring_report.return_value = {
            "report_id": "REPORT_001",
            "monitoring_data": []
        }

        result = await ocpp_handler.on_get_monitoring_report(
            request_id=1,
            monitoring_criteria=[],
            component_variable=[]
        )

        assert result["status"] == "Accepted"
        assert "monitoringReport" in result

    @pytest.mark.asyncio
    async def test_set_display_message(self, ocpp_handler, mock_timescale_client):
        """Test SetDisplayMessage handling."""
        from ocpp.v21.datatypes import MessageInfoType, MessageContentType
        from ocpp.v21.enums import MessagePriorityEnumType, MessageStateEnumType, MessageFormatEnumType

        message_content = MessageContentType(
            format=MessageFormatEnumType.utf8,
            language="en",
            content="Test message"
        )

        message_info = MessageInfoType(
            id=1,
            priority=MessagePriorityEnumType.normal_cycle,
            state=MessageStateEnumType.charging,
            start_date_time="2023-01-01T00:00:00Z",
            end_date_time="2023-01-01T23:59:59Z",
            message=message_content
        )

        result = await ocpp_handler.on_set_display_message(
            message=message_info
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_customer_information(self, ocpp_handler, mock_timescale_client):
        """Test CustomerInformation handling."""
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType, CustomerInformationStatusEnumType

        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)

        result = await ocpp_handler.on_customer_information(
            request_id=1,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123"
        )

        assert result["status"] == CustomerInformationStatusEnumType.accepted


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
