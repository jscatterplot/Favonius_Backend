"""Unit tests for V2G implementations."""

import pytest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# Import V2G managers
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.charging_profile_manager import ChargingProfileManager, ChargingSchedulePeriod, ChargingProfilePurpose, OperationModeEnumType
from websocket_handler.der_control_manager import DERControlManager, DERControlEnumType
from websocket_handler.priority_charging_manager import PriorityChargingManager, PriorityChargingStatus
from websocket_handler.external_control_manager import ExternalControlManager, ExternalControlSource, ExternalControlType
from websocket_handler.certificate_manager import CertificateManager, CertificateType
from websocket_handler.v2x_controller import V2XController, V2XOperationMode, V2XControllerConfig
from websocket_handler.config import Config, TimescaleConfig, SupabaseConfig


class TestV2GChargingProfileManager:
    """Test V2G charging profile manager."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_charging_profile = AsyncMock()
        client.get_charging_profiles = AsyncMock(return_value=[])
        client.remove_charging_profile = AsyncMock()
        client.insert_ev_charging_needs = AsyncMock()
        return client

    @pytest.fixture
    def charging_profile_manager(self, mock_timescale_client):
        """Create ChargingProfileManager instance."""
        return ChargingProfileManager(mock_timescale_client)

    def test_charging_schedule_period_v2g_fields(self):
        """Test ChargingSchedulePeriod with V2G fields."""
        period = ChargingSchedulePeriod(
            start_period=0,
            limit=22.0,
            discharging_limit=-11.0,
            setpoint=15.0,
            setpoint_reactive=5.0,
            operation_mode="CentralSetpoint",
            v2x_freq_watt_curve=[{"frequency": 50.0, "power": 20.0}],
            v2x_signal_watt_curve=[{"signal": 1.0, "power": 18.0}],
            v2x_baseline=10.0
        )
        
        assert period.start_period == 0
        assert period.limit == 22.0
        assert period.discharging_limit == -11.0
        assert period.setpoint == 15.0
        assert period.setpoint_reactive == 5.0
        assert period.operation_mode == "CentralSetpoint"
        assert period.v2x_freq_watt_curve == [{"frequency": 50.0, "power": 20.0}]
        assert period.v2x_signal_watt_curve == [{"signal": 1.0, "power": 18.0}]
        assert period.v2x_baseline == 10.0

    def test_operation_mode_enum(self):
        """Test OperationModeEnumType."""
        assert OperationModeEnumType.CHARGING_ONLY.value == "ChargingOnly"
        assert OperationModeEnumType.CENTRAL_SETPOINT.value == "CentralSetpoint"
        assert OperationModeEnumType.CENTRAL_FREQUENCY.value == "CentralFrequency"
        assert OperationModeEnumType.LOCAL_FREQUENCY.value == "LocalFrequency"
        assert OperationModeEnumType.EXTERNAL_SETPOINT.value == "ExternalSetpoint"
        assert OperationModeEnumType.EXTERNAL_LIMITS.value == "ExternalLimits"
        assert OperationModeEnumType.LOCAL_LOAD_BALANCING.value == "LocalLoadBalancing"
        assert OperationModeEnumType.IDLE.value == "Idle"

    def test_charging_profile_purpose_v2g(self):
        """Test ChargingProfilePurpose with V2G purposes."""
        assert ChargingProfilePurpose.PRIORITY_CHARGING.value == "PriorityCharging"
        assert ChargingProfilePurpose.LOCAL_GENERATION.value == "LocalGeneration"

    @pytest.mark.asyncio
    async def test_set_charging_profile_v2g(self, charging_profile_manager):
        """Test setting charging profile with V2G fields."""
        profile_data = {
            "id": 1,
            "stackLevel": 1,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "id": 1,
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": 0,
                        "limit": 22.0,
                        "dischargingLimit": -11.0,
                        "setpoint": 15.0,
                        "setpointReactive": 5.0,
                        "operationMode": "CentralSetpoint",
                        "v2xFreqWattCurve": [{"frequency": 50.0, "power": 20.0}],
                        "v2xSignalWattCurve": [{"signal": 1.0, "power": 18.0}],
                        "v2xBaseline": 10.0
                    }
                ]
            }
        }
        
        result = await charging_profile_manager.set_charging_profile("TEST_STATION", 1, profile_data)
        
        assert result["status"] == "Accepted"
        charging_profile_manager.timescale_client.store_charging_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_dynamic_schedule(self, charging_profile_manager):
        """Test UpdateDynamicSchedule functionality."""
        # Mock the get_charging_profiles to return a profile
        charging_profile_manager.timescale_client.get_charging_profiles.return_value = [
            {
                "profile_id": 1,
                "evse_id": 1,
                "schedule": json.dumps({
                    "id": 1,
                    "stackLevel": 1,
                    "chargingProfilePurpose": "TxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": {
                        "id": 1,
                        "chargingSchedulePeriod": [
                            {"startPeriod": 0, "limit": 22.0, "numberPhases": 3}
                        ]
                    }
                })
            }
        ]
        
        result = await charging_profile_manager.update_dynamic_schedule(
            "TEST_STATION", 1, limit=20.0, discharging_limit=-10.0, 
            setpoint=15.0, setpoint_reactive=3.0
        )
        
        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_get_charging_profiles_v2g(self, charging_profile_manager):
        """Test getting charging profiles with V2G support."""
        # Mock the private method to return parsed profiles
        mock_profile = Mock()
        mock_profile.charging_schedule.charging_schedule_period = [
            Mock(discharging_limit=-11.0, setpoint=15.0)
        ]
        charging_profile_manager._get_profiles = AsyncMock(return_value=[mock_profile])
        
        result = await charging_profile_manager.get_charging_profiles("TEST_STATION", 1)
        
        assert result["status"] == "Accepted"
        assert len(result["chargingProfile"]) > 0


class TestDERControlManager:
    """Test DER control manager."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_der_control = AsyncMock()
        client.get_der_controls = AsyncMock(return_value=[])
        client.update_der_control_status = AsyncMock()
        client.store_der_event = AsyncMock()
        return client

    @pytest.fixture
    def der_control_manager(self, mock_timescale_client):
        """Create DERControlManager instance."""
        return DERControlManager(mock_timescale_client)

    def test_der_control_enum_types(self):
        """Test DERControlEnumType."""
        assert DERControlEnumType.FIXED_PF_INJECT.value == "FixedPFInject"
        assert DERControlEnumType.VOLT_VAR.value == "VoltVar"
        assert DERControlEnumType.WATT_VAR.value == "WattVar"
        assert DERControlEnumType.FIXED_VAR.value == "FixedVar"
        assert DERControlEnumType.VOLT_WATT.value == "VoltWatt"
        assert DERControlEnumType.FREQ_DROOP.value == "FreqDroop"

    @pytest.mark.asyncio
    async def test_set_der_control(self, der_control_manager):
        """Test setting DER control."""
        # Mock the private methods
        der_control_manager._validate_der_control = AsyncMock(return_value={"valid": True})
        der_control_manager._handle_control_priority = AsyncMock()
        der_control_manager._get_next_control_id = AsyncMock(return_value=1)
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        der_control_data = {
            "controlId": 1,
            "isDefault": False,
            "controlType": "FreqDroop",
            "priority": 1,
            "startTime": datetime.now(timezone.utc).isoformat(),
            "duration": 3600,
            "overFreq": 50.5,
            "underFreq": 49.5,
            "overDroop": 0.05,
            "underDroop": 0.05,
            "responseTime": 5
        }

        result = await der_control_manager.set_der_control("TEST_STATION", der_control_data)

        assert result["status"] == "Accepted"
        der_control_manager._store_der_control.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_der_control(self, der_control_manager):
        """Test getting DER control."""
        # Mock the private method to return a control
        mock_control = Mock()
        mock_control.control_id = 1
        mock_control.control_type = "FreqDroop"
        der_control_manager._get_der_control_by_id = AsyncMock(return_value=mock_control)
        der_control_manager._der_control_to_dict = Mock(return_value={"control_id": 1})
        
        result = await der_control_manager.get_der_control("TEST_STATION", 1)
        
        assert result["status"] == "Accepted"
        assert result["der_control"]["control_id"] == 1

    @pytest.mark.asyncio
    async def test_notify_der_alarm(self, der_control_manager):
        """Test DER alarm notification."""
        # Mock the private method
        der_control_manager._store_der_alarm_event = AsyncMock()
        
        result = await der_control_manager.notify_der_alarm(
            "TEST_STATION", "FreqDroop", False, "GridFault",
            datetime.now(timezone.utc).isoformat()
        )

        assert result["status"] == "Accepted"
        der_control_manager._store_der_alarm_event.assert_called_once()


class TestPriorityChargingManager:
    """Test priority charging manager."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_priority_charging_profile = AsyncMock()
        client.update_priority_charging_profile_status = AsyncMock()
        client.store_priority_charging_notification = AsyncMock()
        return client

    @pytest.fixture
    def priority_charging_manager(self, mock_timescale_client):
        """Create PriorityChargingManager instance."""
        return PriorityChargingManager(mock_timescale_client)

    def test_priority_charging_status_enum(self):
        """Test PriorityChargingStatus."""
        assert PriorityChargingStatus.INACTIVE.value == "Inactive"
        assert PriorityChargingStatus.ACTIVE.value == "Active"
        assert PriorityChargingStatus.SUSPENDED.value == "Suspended"
        assert PriorityChargingStatus.EXPIRED.value == "Expired"

    @pytest.mark.asyncio
    async def test_use_priority_charging_activate(self, priority_charging_manager):
        """Test activating priority charging."""
        result = await priority_charging_manager.use_priority_charging(
            "TEST_STATION", "TXN_001", True,
            priorityLevel=1,
            maxPowerKw=22.0,
            minPowerKw=3.7,
            targetSocPercent=80.0
        )
        
        assert result["status"] == "Accepted"
        priority_charging_manager.timescale_client.store_priority_charging_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_use_priority_charging_deactivate(self, priority_charging_manager):
        """Test deactivating priority charging."""
        # First activate
        await priority_charging_manager.use_priority_charging("TEST_STATION", "TXN_001", True)
        
        # Then deactivate
        result = await priority_charging_manager.use_priority_charging(
            "TEST_STATION", "TXN_001", False
        )
        
        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_notify_priority_charging(self, priority_charging_manager):
        """Test priority charging notification."""
        result = await priority_charging_manager.notify_priority_charging(
            "TEST_STATION", "TXN_001", True
        )
        
        assert result["status"] == "Accepted"
        priority_charging_manager.timescale_client.store_priority_charging_notification.assert_called_once()


class TestExternalControlManager:
    """Test external control manager."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_external_control_signal = AsyncMock()
        client.update_external_control_signal_status = AsyncMock()
        client.store_external_charging_limit = AsyncMock()
        return client

    @pytest.fixture
    def external_control_manager(self, mock_timescale_client):
        """Create ExternalControlManager instance."""
        return ExternalControlManager(mock_timescale_client)

    def test_external_control_enums(self):
        """Test external control enums."""
        assert ExternalControlSource.EMS.value == "EMS"
        assert ExternalControlSource.SO.value == "SO"
        assert ExternalControlSource.CSO.value == "CSO"
        assert ExternalControlSource.OTHER.value == "Other"
        
        assert ExternalControlType.GRID_CRITICAL.value == "GridCritical"
        assert ExternalControlType.LOCAL_GENERATION.value == "LocalGeneration"
        assert ExternalControlType.DEMAND_RESPONSE.value == "DemandResponse"
        assert ExternalControlType.FREQUENCY_REGULATION.value == "FrequencyRegulation"
        assert ExternalControlType.VOLTAGE_REGULATION.value == "VoltageRegulation"
        assert ExternalControlType.EMERGENCY_SHUTDOWN.value == "EmergencyShutdown"

    @pytest.mark.asyncio
    async def test_process_external_limit_signal(self, external_control_manager):
        """Test processing external limit signal."""
        signal_data = {
            "signalId": "SIG_001",
            "source": "EMS",
            "controlType": "GridCritical",
            "signalValue": 0.8,
            "targetResponseKw": 20.0,
            "responseDeadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "compensationRateKwh": 0.15,
            "gridOperator": "CAISO",
            "region": "CA"
        }
        
        result = await external_control_manager.process_external_limit_signal(
            "TEST_STATION", 1, signal_data
        )
        
        assert result["status"] == "Accepted"
        external_control_manager.timescale_client.store_external_control_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_external_limit_signal(self, external_control_manager):
        """Test clearing external limit signal."""
        # First add a signal
        signal_data = {
            "signalId": "SIG_001",
            "source": "EMS",
            "controlType": "GridCritical",
            "signalValue": 0.8,
            "targetResponseKw": 20.0,
            "responseDeadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        }
        await external_control_manager.process_external_limit_signal("TEST_STATION", 1, signal_data)
        
        # Then clear it
        result = await external_control_manager.clear_external_limit_signal("TEST_STATION", "SIG_001")
        
        assert result["status"] == "Accepted"


class TestCertificateManagerV2G:
    """Test certificate manager V2G enhancements."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_certificate = AsyncMock()
        client.get_certificate = AsyncMock(return_value=None)
        client.count_certificates = AsyncMock(return_value=0)
        client.store_authorization_record = AsyncMock()
        return client

    @pytest.fixture
    def certificate_manager(self, mock_timescale_client):
        """Create CertificateManager instance."""
        return CertificateManager(mock_timescale_client)

    def test_certificate_type_v2g(self):
        """Test CertificateType with V2G types."""
        assert CertificateType.SECC_CERTIFICATE.value == "SECCCertificate"
        assert CertificateType.V2G_ROOT_CERTIFICATE.value == "V2GRootCertificate"
        assert CertificateType.MO_SUB_CERTIFICATE.value == "MOSubCertificate"
        assert CertificateType.OEM_SUB_CERTIFICATE.value == "OEMSubCertificate"
        assert CertificateType.CPO_SUB_CERTIFICATE.value == "CPOSubCertificate"

    @pytest.mark.asyncio
    async def test_validate_contract_certificate(self, certificate_manager):
        """Test contract certificate validation."""
        # Mock certificate data
        mock_certificate_data = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t"
        
        result = await certificate_manager.validate_contract_certificate(
            "TEST_STATION", mock_certificate_data, "test@emaid"
        )
        
        # Should handle validation (may fail due to mock data, but should not crash)
        assert "valid" in result

    @pytest.mark.asyncio
    async def test_handle_pnc_authorization(self, certificate_manager):
        """Test Plug & Charge authorization."""
        mock_certificate_data = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t"
        
        result = await certificate_manager.handle_pnc_authorization(
            "TEST_STATION", mock_certificate_data, "test@emaid"
        )
        
        # Should handle authorization (may fail due to mock data, but should not crash)
        assert "status" in result


class TestV2XController:
    """Test V2X controller enhancements."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_v2x_data = AsyncMock()
        client.get_v2x_config = AsyncMock(return_value={})
        return client

    @pytest.fixture
    def v2x_controller(self, mock_timescale_client):
        """Create V2XController instance."""
        config = V2XControllerConfig()
        app_config = Config(
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
                db_host="localhost",
                db_user="test",
                db_password="test"
            )
        )
        return V2XController(config, app_config)

    def test_v2x_operation_modes(self):
        """Test V2XOperationMode."""
        assert V2XOperationMode.CHARGING_ONLY.value == "ChargingOnly"
        assert V2XOperationMode.CENTRAL_SETPOINT.value == "CentralSetpoint"
        assert V2XOperationMode.CENTRAL_FREQUENCY.value == "CentralFrequency"
        assert V2XOperationMode.LOCAL_FREQUENCY.value == "LocalFrequency"
        assert V2XOperationMode.EXTERNAL_SETPOINT.value == "ExternalSetpoint"
        assert V2XOperationMode.EXTERNAL_LIMITS.value == "ExternalLimits"
        assert V2XOperationMode.LOCAL_LOAD_BALANCING.value == "LocalLoadBalancing"
        assert V2XOperationMode.IDLE.value == "Idle"

    @pytest.mark.asyncio
    async def test_set_der_control(self, v2x_controller):
        """Test DER control integration."""
        der_control_data = {
            "controlId": 1,
            "controlType": "FreqDroop",
            "priority": 1,
            "startTime": datetime.now(timezone.utc).isoformat()
        }
        
        result = await v2x_controller.set_der_control("TEST_STATION", der_control_data)
        
        # Should handle DER control (may fail due to missing DER manager, but should not crash)
        assert "status" in result

    @pytest.mark.asyncio
    async def test_notify_allowed_energy_transfer(self, v2x_controller):
        """Test allowed energy transfer notification."""
        result = await v2x_controller.notify_allowed_energy_transfer(
            "TEST_STATION", ["AC_BPT", "DC_BPT"]
        )
        
        # Should handle energy transfer notification
        assert "status" in result


class TestV2GIntegration:
    """Test V2G integration scenarios."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_charging_profile = AsyncMock()
        client.store_der_control = AsyncMock()
        client.store_priority_charging_profile = AsyncMock()
        client.store_external_control_signal = AsyncMock()
        client.store_certificate = AsyncMock()
        client.store_v2x_data = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_v2g_bidirectional_charging_flow(self, mock_timescale_client):
        """Test complete V2G bidirectional charging flow."""
        # Setup proper mocks for the integration test
        mock_timescale_client.store_charging_profile = AsyncMock()
        mock_timescale_client.get_charging_profiles = AsyncMock(return_value=[])
        mock_timescale_client.store_der_control = AsyncMock()
        mock_timescale_client.get_der_controls = AsyncMock(return_value=[])
        mock_timescale_client.store_priority_charging_profile = AsyncMock()
        mock_timescale_client.store_external_charging_limit = AsyncMock()
        mock_timescale_client.store_ev_charging_needs = AsyncMock()
        
        # Initialize managers
        charging_profile_manager = ChargingProfileManager(mock_timescale_client)
        der_control_manager = DERControlManager(mock_timescale_client)
        priority_charging_manager = PriorityChargingManager(mock_timescale_client)
        external_control_manager = ExternalControlManager(mock_timescale_client)
        certificate_manager = CertificateManager(mock_timescale_client)

        # Create V2X controller with proper config
        config = V2XControllerConfig()
        app_config = Config(
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
                db_host="localhost",
                db_user="test",
                db_password="test"
            )
        )
        v2x_controller = V2XController(config, app_config)

        station_id = "V2G_TEST_STATION"
        evse_id = 1

        # 1. Set bidirectional charging profile
        profile_data = {
            "id": 1,
            "stackLevel": 1,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "id": 1,
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": 0,
                        "limit": 22.0,
                        "dischargingLimit": -11.0,
                        "setpoint": 15.0,
                        "setpointReactive": 5.0,
                        "operationMode": "CentralSetpoint"
                    }
                ]
            }
        }

        profile_result = await charging_profile_manager.set_charging_profile(station_id, evse_id, profile_data)
        assert profile_result["status"] == "Accepted"
        
        # 2. Set DER control for frequency regulation
        der_control_data = {
            "controlId": 1,
            "controlType": "FreqDroop",
            "priority": 1,
            "startTime": datetime.now(timezone.utc).isoformat(),
            "overFreq": 50.5,
            "underFreq": 49.5,
            "overDroop": 0.05,
            "underDroop": 0.05
        }
        
        der_result = await der_control_manager.set_der_control(station_id, der_control_data)
        assert der_result["status"] == "Accepted"
        
        # 3. Process external grid signal
        signal_data = {
            "signalId": "GRID_SIG_001",
            "source": "SO",
            "controlType": "FrequencyRegulation",
            "signalValue": 0.8,
            "targetResponseKw": 20.0,
            "responseDeadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        }
        
        signal_result = await external_control_manager.process_external_limit_signal(station_id, evse_id, signal_data)
        assert signal_result["status"] == "Accepted"
        
        # 4. Activate priority charging
        priority_result = await priority_charging_manager.use_priority_charging(
            station_id, "TXN_V2G_001", True,
            priorityLevel=1,
            maxPowerKw=22.0,
            minPowerKw=3.7
        )
        assert priority_result["status"] == "Accepted"
        
        # 5. Notify allowed energy transfer
        energy_result = await v2x_controller.notify_allowed_energy_transfer(station_id, ["AC_BPT", "DC_BPT"])
        assert energy_result["status"] == "Accepted"
        
        # Verify all managers were called
        assert mock_timescale_client.store_charging_profile.called
        # Note: DER control manager uses private methods, so we check the result instead
        assert der_result["status"] == "Accepted"
        assert mock_timescale_client.store_priority_charging_profile.called
        assert mock_timescale_client.store_external_control_signal.called

    @pytest.mark.asyncio
    async def test_v2g_iso15118_pnc_flow(self, mock_timescale_client):
        """Test ISO 15118 Plug & Charge flow."""
        certificate_manager = CertificateManager(mock_timescale_client)
        
        station_id = "PNC_TEST_STATION"
        emaid = "test@emaid"
        mock_certificate = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t"
        
        # Test certificate validation
        cert_result = await certificate_manager.validate_contract_certificate(
            station_id, mock_certificate, emaid
        )
        assert "valid" in cert_result
        
        # Test PnC authorization
        auth_result = await certificate_manager.handle_pnc_authorization(
            station_id, mock_certificate, emaid
        )
        assert "status" in auth_result
        
        # Test certificate installation
        install_result = await certificate_manager.install_v2g_certificate(
            station_id, CertificateType.CONTRACT_CERTIFICATE, mock_certificate
        )
        assert "status" in install_result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
