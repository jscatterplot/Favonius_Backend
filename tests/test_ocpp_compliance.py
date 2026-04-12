"""OCPP 2.0.1 Compliance Test Suite."""

import asyncio
import os

# Test imports
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from websocket_handler.certificate_manager import CertificateManager, CertificateType
from websocket_handler.charging_profile_manager import ChargingProfileManager
from websocket_handler.device_model import DeviceModel
from websocket_handler.timescale_client import TimescaleClient
from websocket_handler.transaction_manager import IdToken, IdTokenType, TransactionManager


class TestDeviceModel:
    """Test Device Model functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock(spec=TimescaleClient)
        client.get_device_variable.return_value = None
        client.set_device_variable.return_value = None
        client.get_device_components.return_value = []
        client.get_device_variables.return_value = []
        client.store_device_report.return_value = None
        return client

    @pytest.fixture
    def device_model(self, mock_timescale_client):
        """Create DeviceModel instance."""
        return DeviceModel(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_get_variables_success(self, device_model, mock_timescale_client):
        """Test successful GetVariables."""
        # Mock database response
        mock_timescale_client.get_device_variable.return_value = {"value": "TestValue"}

        get_variable_data = [
            {
                "component": {"name": "ChargingStation", "instance": ""},
                "variable": {"name": "Model", "instance": ""},
                "attributeType": "Actual",
            }
        ]

        results = await device_model.get_variables("test_station", get_variable_data)

        assert len(results) == 1
        assert results[0]["attributeStatus"] == "Accepted"
        assert results[0]["attributeValue"] == "TestValue"
        assert results[0]["component"]["name"] == "ChargingStation"
        assert results[0]["variable"]["name"] == "Model"

    @pytest.mark.asyncio
    async def test_get_variables_not_found(self, device_model, mock_timescale_client):
        """Test GetVariables with unknown variable."""
        mock_timescale_client.get_device_variable.return_value = None

        get_variable_data = [
            {
                "component": {"name": "ChargingStation", "instance": ""},
                "variable": {"name": "UnknownVariable", "instance": ""},
                "attributeType": "Actual",
            }
        ]

        results = await device_model.get_variables("test_station", get_variable_data)

        assert len(results) == 1
        assert results[0]["attributeStatus"] == "UnknownVariable"
        assert "attributeValue" not in results[0]

    @pytest.mark.asyncio
    async def test_set_variables_success(self, device_model, mock_timescale_client):
        """Test successful SetVariables."""
        set_variable_data = [
            {
                "component": {"name": "ChargingStation", "instance": ""},
                "variable": {"name": "Model", "instance": ""},
                "attributeType": "Actual",
                "attributeValue": "NewModel",
            }
        ]

        results = await device_model.set_variables("test_station", set_variable_data)

        assert len(results) == 1
        assert results[0]["attributeStatus"] == "Accepted"
        assert results[0]["component"]["name"] == "ChargingStation"
        assert results[0]["variable"]["name"] == "Model"

        # Verify database call
        mock_timescale_client.set_device_variable.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_variables_readonly(self, device_model):
        """Test SetVariables with read-only variable."""
        set_variable_data = [
            {
                "component": {"name": "ChargingStation", "instance": ""},
                "variable": {"name": "SerialNumber", "instance": ""},  # ReadOnly variable
                "attributeType": "Actual",
                "attributeValue": "NewSerial",
            }
        ]

        results = await device_model.set_variables("test_station", set_variable_data)

        assert len(results) == 1
        assert results[0]["attributeStatus"] == "Rejected"
        assert results[0]["attributeStatusInfo"]["reasonCode"] == "WriteDenied"

    @pytest.mark.asyncio
    async def test_initialize_station_device_model(self, device_model, mock_timescale_client):
        """Test device model initialization."""
        station_info = {
            "model": "TestModel",
            "vendor_name": "TestVendor",
            "serial_number": "TEST123",
            "firmware_version": "1.0.0",
        }

        await device_model.initialize_station_device_model("test_station", station_info)

        # Verify components were created
        assert mock_timescale_client.create_device_component.call_count >= 6
        # Verify variables were set
        assert mock_timescale_client.set_device_variable.call_count >= 20


class TestChargingProfileManager:
    """Test Charging Profile Manager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock(spec=TimescaleClient)
        client.store_charging_profile.return_value = None
        client.remove_charging_profile.return_value = None
        client.get_charging_profiles.return_value = []
        client.get_active_charging_profiles.return_value = []
        client.store_reported_charging_profile.return_value = None
        return client

    @pytest.fixture
    def charging_profile_manager(self, mock_timescale_client):
        """Create ChargingProfileManager instance."""
        return ChargingProfileManager(mock_timescale_client)

    @pytest.fixture
    def sample_charging_profile(self):
        """Create sample charging profile."""
        return {
            "id": 1,
            "stackLevel": 1,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "id": 1,
                "startSchedule": datetime.now(timezone.utc).isoformat(),
                "duration": 3600,
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 22.0, "numberPhases": 3}],
            },
        }

    @pytest.mark.asyncio
    async def test_set_charging_profile_success(
        self, charging_profile_manager, sample_charging_profile
    ):
        """Test successful SetChargingProfile."""
        result = await charging_profile_manager.set_charging_profile(
            "test_station", 1, sample_charging_profile
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_set_charging_profile_validation_error(self, charging_profile_manager):
        """Test SetChargingProfile with validation error."""
        invalid_profile = {
            "id": 1,
            "stackLevel": -1,  # Invalid stack level
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {"id": 1, "chargingRateUnit": "W", "chargingSchedulePeriod": []},
        }

        result = await charging_profile_manager.set_charging_profile(
            "test_station", 1, invalid_profile
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "PropertyConstraintViolation"

    @pytest.mark.asyncio
    async def test_clear_charging_profile_success(self, charging_profile_manager):
        """Test successful ClearChargingProfile."""
        result = await charging_profile_manager.clear_charging_profile(
            "test_station", 1, charging_profile_id=1
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_get_composite_schedule_no_profiles(self, charging_profile_manager):
        """Test GetCompositeSchedule with no active profiles."""
        result = await charging_profile_manager.get_composite_schedule("test_station", 1, 3600)

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "NoActiveChargingProfile"

    @pytest.mark.asyncio
    async def test_get_composite_schedule_with_profiles(
        self, charging_profile_manager, mock_timescale_client
    ):
        """Test GetCompositeSchedule with active profiles."""
        # Mock active profiles
        mock_timescale_client.get_active_charging_profiles.return_value = [
            {
                "schedule": {
                    "id": 1,
                    "stackLevel": 1,
                    "chargingProfilePurpose": "TxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": {
                        "id": 1,
                        "chargingRateUnit": "W",
                        "chargingSchedulePeriod": [
                            {"startPeriod": 0, "limit": 22.0, "numberPhases": 3}
                        ],
                    },
                }
            }
        ]

        result = await charging_profile_manager.get_composite_schedule("test_station", 1, 3600)

        assert result["status"] == "Accepted"
        assert "chargingSchedule" in result
        assert result["connectorId"] == 1


class TestTransactionManager:
    """Test Transaction Manager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock(spec=TimescaleClient)
        client.get_id_token_info.return_value = None
        client.store_transaction.return_value = None
        client.update_transaction.return_value = None
        client.get_transaction.return_value = None
        client.store_transaction_event.return_value = None
        client.get_transaction_energy.return_value = {"energy_kwh": 0.0, "power_kw": 0.0}
        client.store_transaction_cost.return_value = None
        client.get_evse_status.return_value = {"status": "Available"}
        return client

    @pytest.fixture
    def transaction_manager(self, mock_timescale_client):
        """Create TransactionManager instance."""
        return TransactionManager(mock_timescale_client)

    @pytest.fixture
    def sample_id_token(self):
        """Create sample ID token."""
        return IdToken(id_token="test_token_123", type=IdTokenType.ISO14443)

    @pytest.mark.asyncio
    async def test_request_start_transaction_success(
        self, transaction_manager, sample_id_token, mock_timescale_client
    ):
        """Test successful RequestStartTransaction."""
        result = await transaction_manager.request_start_transaction(
            "test_station", 1, None, sample_id_token
        )

        assert result["status"] == "Accepted"
        assert "transactionId" in result
        assert result["idTokenInfo"]["status"] == "Unknown"  # Token not in cache

    @pytest.mark.asyncio
    async def test_request_start_transaction_evse_unavailable(
        self, transaction_manager, sample_id_token, mock_timescale_client
    ):
        """Test RequestStartTransaction with unavailable EVSE."""
        mock_timescale_client.get_evse_status.return_value = {"status": "Unavailable"}

        result = await transaction_manager.request_start_transaction(
            "test_station", 1, None, sample_id_token
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "EVSEUnavailable"

    @pytest.mark.asyncio
    async def test_request_stop_transaction_success(
        self, transaction_manager, mock_timescale_client
    ):
        """Test successful RequestStopTransaction."""
        # Mock existing transaction
        mock_timescale_client.get_transaction.return_value = {
            "transaction_id": "test_tx_123",
            "charging_state": "Charging",
            "evse_id": 1,
            "connector_id": 1,
        }

        result = await transaction_manager.request_stop_transaction(
            "test_station", "test_tx_123", "Remote"
        )

        assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    async def test_request_stop_transaction_not_found(
        self, transaction_manager, mock_timescale_client
    ):
        """Test RequestStopTransaction with unknown transaction."""
        mock_timescale_client.get_transaction.return_value = None

        result = await transaction_manager.request_stop_transaction(
            "test_station", "unknown_tx", "Remote"
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "UnknownTransaction"

    @pytest.mark.asyncio
    async def test_authorize_id_token_unknown(self, transaction_manager):
        """Test ID token authorization with unknown token."""
        id_token = IdToken(id_token="unknown_token", type=IdTokenType.ISO14443)

        result = await transaction_manager.authorize_id_token(id_token)

        assert result["status"] == "Unknown"
        assert result["cacheTimeout"] == 300


class TestCertificateManager:
    """Test Certificate Manager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock(spec=TimescaleClient)
        client.get_certificate.return_value = None
        client.count_certificates.return_value = 0
        client.store_certificate.return_value = None
        client.get_installed_certificates.return_value = []
        client.find_certificate_by_hash.return_value = None
        client.delete_certificate.return_value = None
        return client

    @pytest.fixture
    def certificate_manager(self, mock_timescale_client):
        """Create CertificateManager instance."""
        return CertificateManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_get_15118_ev_certificate_not_found(self, certificate_manager):
        """Test Get15118EVCertificate with no certificate."""
        result = await certificate_manager.get_15118_ev_certificate(
            "test_station", CertificateType.V2G_ROOT_CA
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "UnknownCertificate"

    @pytest.mark.asyncio
    async def test_get_15118_ev_certificate_found(self, certificate_manager, mock_timescale_client):
        """Test Get15118EVCertificate with existing certificate."""
        mock_timescale_client.get_certificate.return_value = {
            "certificate_data": "test_cert_data",
            "issuer_name": "Test CA",
            "subject_name": "Test Subject",
        }

        result = await certificate_manager.get_15118_ev_certificate(
            "test_station", CertificateType.V2G_ROOT_CA
        )

        assert result["status"] == "Accepted"
        assert result["exiResponse"] == "test_cert_data"

    @pytest.mark.asyncio
    async def test_certificate_signed_invalid_chain(self, certificate_manager):
        """Test CertificateSigned with invalid certificate chain."""
        invalid_chain = ["invalid_cert_data"]

        result = await certificate_manager.certificate_signed(
            "test_station", CertificateType.V2G_ROOT_CA, invalid_chain, "exi_response"
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "CertificateChainError"

    @pytest.mark.asyncio
    async def test_delete_certificate_no_match(self, certificate_manager):
        """Test DeleteCertificate with no matching certificates."""
        certificate_hash_data = [
            {
                "certificateType": "V2GRootCertificate",
                "hashAlgorithm": "SHA256",
                "issuerNameHash": "test_hash",
                "issuerKeyHash": "test_key_hash",
                "serialNumber": "12345",
            }
        ]

        result = await certificate_manager.delete_certificate("test_station", certificate_hash_data)

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "UnknownCertificate"

    @pytest.mark.asyncio
    async def test_get_installed_certificate_ids(self, certificate_manager):
        """Test GetInstalledCertificateIds."""
        result = await certificate_manager.get_installed_certificate_ids("test_station")

        assert result["status"] == "Accepted"
        assert "certificateHashData" in result
        assert isinstance(result["certificateHashData"], list)


class TestOCPPIntegration:
    """Integration tests for OCPP message flows."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client with all methods."""
        client = AsyncMock(spec=TimescaleClient)

        # Device model methods
        client.get_device_variable.return_value = None
        client.set_device_variable.return_value = None
        client.get_device_components.return_value = []
        client.get_device_variables.return_value = []
        client.store_device_report.return_value = None
        client.create_device_component.return_value = None

        # Charging profile methods
        client.store_charging_profile.return_value = None
        client.remove_charging_profile.return_value = None
        client.get_charging_profiles.return_value = []
        client.get_active_charging_profiles.return_value = []
        client.store_reported_charging_profile.return_value = None

        # Transaction methods
        client.get_id_token_info.return_value = None
        client.store_transaction.return_value = None
        client.update_transaction.return_value = None
        client.get_transaction.return_value = None
        client.store_transaction_event.return_value = None
        client.get_transaction_energy.return_value = {"energy_kwh": 0.0, "power_kw": 0.0}
        client.store_transaction_cost.return_value = None
        client.get_evse_status.return_value = {"status": "Available"}

        # Certificate methods
        client.get_certificate.return_value = None
        client.count_certificates.return_value = 0
        client.store_certificate.return_value = None
        client.get_installed_certificates.return_value = []
        client.find_certificate_by_hash.return_value = None
        client.delete_certificate.return_value = None

        # Device control methods
        client.store_reset_request.return_value = None
        client.store_availability_change.return_value = None
        client.store_trigger_message.return_value = None
        client.store_unlock_connector.return_value = None

        return client

    @pytest.mark.asyncio
    async def test_boot_notification_flow(self, mock_timescale_client):
        """Test complete boot notification flow."""
        try:
            from ocpp.v21.datatypes import ChargingStationType
        except ModuleNotFoundError:
            from ocpp.v201.datatypes import ChargingStationType

        from websocket_handler.config import Config
        from websocket_handler.connection_manager import ConnectionManager
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint

        # Mock connection and config
        mock_connection = AsyncMock()
        mock_config = MagicMock(spec=Config)
        mock_config.websocket = MagicMock()
        mock_config.websocket.heartbeat_interval = 300
        mock_config.timescale = MagicMock()
        mock_config.supabase = MagicMock()
        mock_connection_manager = AsyncMock(spec=ConnectionManager)

        # Create charge point
        charge_point = EnhancedOCPPChargePoint(
            "test_station",
            mock_connection,
            mock_config,
            mock_timescale_client,
            mock_connection_manager,
        )

        # Test boot notification
        charging_station = ChargingStationType(
            serial_number="TEST123", model="TestModel", vendor_name="TestVendor"
        )

        result = charge_point.on_boot_notification(charging_station, "PowerUp")

        assert result.status == "Accepted"
        assert result.interval == 300

        # Wait for async tasks to complete
        await asyncio.sleep(0.5)  # Increased wait time

        # Verify device model initialization was triggered
        # The initialize_complete_device_model method calls set_device_variable, not create_device_component
        mock_timescale_client.set_device_variable.assert_called()

    @pytest.mark.asyncio
    async def test_get_variables_flow(self, mock_timescale_client):
        """Test GetVariables message flow."""
        from websocket_handler.config import Config
        from websocket_handler.connection_manager import ConnectionManager
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint

        # Mock connection and config
        mock_connection = AsyncMock()
        mock_config = MagicMock(spec=Config)
        mock_connection_manager = AsyncMock(spec=ConnectionManager)

        # Create charge point
        charge_point = EnhancedOCPPChargePoint(
            "test_station",
            mock_connection,
            mock_config,
            mock_timescale_client,
            mock_connection_manager,
        )

        # Mock database response
        mock_timescale_client.get_device_variable.return_value = {"value": "TestValue"}

        get_variable_data = [
            {
                "component": {"name": "ChargingStation", "instance": ""},
                "variable": {"name": "Model", "instance": ""},
                "attributeType": "Actual",
            }
        ]

        result = charge_point.on_get_variables(get_variable_data)

        assert result is not None

        # Verify async handler was triggered
        await asyncio.sleep(0.1)
        mock_timescale_client.get_device_variable.assert_called()

    @pytest.mark.asyncio
    async def test_set_charging_profile_flow(self, mock_timescale_client):
        """Test SetChargingProfile message flow."""
        from websocket_handler.config import Config
        from websocket_handler.connection_manager import ConnectionManager
        from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint

        # Mock connection and config
        mock_connection = AsyncMock()
        mock_config = MagicMock(spec=Config)
        mock_connection_manager = AsyncMock(spec=ConnectionManager)

        # Create charge point
        charge_point = EnhancedOCPPChargePoint(
            "test_station",
            mock_connection,
            mock_config,
            mock_timescale_client,
            mock_connection_manager,
        )

        charging_profile = {
            "id": 1,
            "stackLevel": 1,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "id": 1,
                "startSchedule": datetime.now(timezone.utc).isoformat(),
                "duration": 3600,
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 22.0, "numberPhases": 3}],
            },
        }

        result = charge_point.on_set_charging_profile(1, charging_profile)

        assert result is not None

        # Verify async handler was triggered
        await asyncio.sleep(0.1)
        mock_timescale_client.store_charging_profile.assert_called()


class TestDatabaseSchema:
    """Test database schema and migrations."""

    def test_ocpp_schema_sql(self):
        """Test OCPP schema SQL generation."""
        from websocket_handler.ocpp_schema import get_ocpp_schema_sql

        schema_sql = get_ocpp_schema_sql()

        # Verify key tables are present
        assert "CREATE TABLE IF NOT EXISTS device_components" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS device_variables" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS charging_profiles" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS transactions" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS certificates" in schema_sql

        # Verify indexes are present
        assert "CREATE INDEX IF NOT EXISTS idx_device_components_station_id" in schema_sql
        assert "CREATE INDEX IF NOT EXISTS idx_charging_profiles_station_evse" in schema_sql
        assert "CREATE INDEX IF NOT EXISTS idx_certificates_station_id" in schema_sql

        # Verify hypertables
        assert "SELECT create_hypertable('device_reports'" in schema_sql
        assert "SELECT create_hypertable('transaction_events'" in schema_sql

    def test_standard_ocpp_variables(self):
        """Test standard OCPP variables definition."""
        from websocket_handler.ocpp_schema import get_standard_ocpp_variables

        variables = get_standard_ocpp_variables()

        # Verify key components
        assert "ChargingStation" in variables
        assert "EVSE" in variables
        assert "Connector" in variables
        assert "SmartCharging" in variables
        assert "V2XController" in variables
        assert "Security" in variables

        # Verify key variables
        assert any(v["name"] == "Model" for v in variables["ChargingStation"])
        assert any(v["name"] == "SerialNumber" for v in variables["ChargingStation"])
        assert any(v["name"] == "Status" for v in variables["EVSE"])
        assert any(v["name"] == "Power" for v in variables["EVSE"])

    def test_initial_device_data(self):
        """Test initial device data generation."""
        from websocket_handler.ocpp_schema import get_initial_device_data

        station_info = {
            "model": "TestModel",
            "vendor_name": "TestVendor",
            "serial_number": "TEST123",
            "firmware_version": "1.0.0",
        }

        device_data = get_initial_device_data("test_station", station_info)

        assert len(device_data) == 6  # 6 components

        # Verify ChargingStation component
        charging_station = next(c for c in device_data if c["component_name"] == "ChargingStation")
        assert charging_station["station_id"] == "test_station"
        assert len(charging_station["variables"]) >= 20

        # Verify EVSE component
        evse = next(c for c in device_data if c["component_name"] == "EVSE")
        assert evse["instance"] == "1"
        assert any(v["name"] == "Status" for v in evse["variables"])


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "--tb=short"])
