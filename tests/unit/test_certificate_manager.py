"""
Unit tests for CertificateManager - ISO 15118 Certificate Management for V2G Plug & Charge.
"""

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from src.websocket_handler.certificate_manager import (
    CertificateInfo,
    CertificateInstallationResult,
    CertificateManager,
    CertificateStatus,
    CertificateType,
)
from src.websocket_handler.timescale_client import TimescaleClient


class TestCertificateManager:
    """Test the CertificateManager class."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        mock_client = Mock(spec=TimescaleClient)
        return mock_client

    @pytest.fixture
    def certificate_manager(self, mock_timescale_client):
        """Create CertificateManager instance."""
        return CertificateManager(mock_timescale_client)

    @pytest.mark.timeout(10)
    def test_certificate_manager_initialization(self, mock_timescale_client):
        """Test CertificateManager initialization."""
        manager = CertificateManager(mock_timescale_client)

        assert manager.timescale_client == mock_timescale_client
        assert manager.certificate_cache == {}
        assert manager.validation_cache == {}
        assert manager.root_cas == {}
        assert len(manager.certificate_limits) > 0
        assert CertificateType.V2G_ROOT_CA in manager.certificate_limits
        assert manager.certificate_limits[CertificateType.V2G_ROOT_CA] == 5

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_15118_ev_certificate_success(self, certificate_manager):
        """Test getting ISO 15118 EV certificate successfully."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA

        # Mock certificate data
        mock_certificate = CertificateInfo(
            certificate_type=certificate_type,
            certificate_data=base64.b64encode(b"test_certificate_data").decode(),
            issuer_name="Test CA",
            subject_name="Test Certificate",
            serial_number="123456789",
            valid_from=datetime.now(timezone.utc),
            valid_to=datetime.now(timezone.utc) + timedelta(days=365),
            status=CertificateStatus.VALID,
        )

        with patch.object(certificate_manager, "_get_certificate", return_value=mock_certificate):
            result = await certificate_manager.get_15118_ev_certificate(
                station_id, certificate_type
            )

            assert result["status"] == "Accepted"
            assert "exiResponse" in result
            assert result["exiResponse"] == mock_certificate.certificate_data
            assert result["statusInfo"]["reasonCode"] == "NoError"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_15118_ev_certificate_not_found(self, certificate_manager):
        """Test getting ISO 15118 EV certificate when not found."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA

        with patch.object(certificate_manager, "_get_certificate", return_value=None):
            result = await certificate_manager.get_15118_ev_certificate(
                station_id, certificate_type
            )

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "UnknownCertificate"
            assert "not found" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_15118_ev_certificate_exception(self, certificate_manager):
        """Test getting ISO 15118 EV certificate with exception."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA

        with patch.object(
            certificate_manager, "_get_certificate", side_effect=Exception("Test error")
        ):
            result = await certificate_manager.get_15118_ev_certificate(
                station_id, certificate_type
            )

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "InternalError"
            assert "Test error" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_certificate_signed_success(self, certificate_manager):
        """Test handling CertificateSigned message successfully."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA
        certificate_chain = ["cert1", "cert2", "cert3"]
        exi_response = base64.b64encode(b"test_response").decode()

        with (
            patch.object(
                certificate_manager, "_validate_certificate_chain", return_value={"valid": True}
            ),
            patch.object(
                certificate_manager,
                "_parse_certificate",
                return_value=CertificateInfo(
                    certificate_type=certificate_type,
                    certificate_data=exi_response,
                    issuer_name="Test CA",
                    subject_name="Test Certificate",
                    serial_number="123456789",
                    valid_from=datetime.now(timezone.utc),
                    valid_to=datetime.now(timezone.utc) + timedelta(days=365),
                    status=CertificateStatus.VALID,
                ),
            ),
            patch.object(certificate_manager, "_check_certificate_limits", return_value=True),
            patch.object(
                certificate_manager, "_install_certificate", return_value={"status": "Accepted"}
            ),
        ):

            result = await certificate_manager.certificate_signed(
                station_id, certificate_type, certificate_chain, exi_response
            )

            assert result["status"] == "Accepted"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_certificate_signed_invalid_chain(self, certificate_manager):
        """Test handling CertificateSigned message with invalid chain."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA
        certificate_chain = ["cert1", "cert2", "cert3"]
        exi_response = base64.b64encode(b"test_response").decode()

        with patch.object(
            certificate_manager,
            "_validate_certificate_chain",
            return_value={
                "valid": False,
                "reason_code": "InvalidCertificateChain",
                "message": "Invalid chain",
            },
        ):
            result = await certificate_manager.certificate_signed(
                station_id, certificate_type, certificate_chain, exi_response
            )

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "InvalidCertificateChain"
            assert "Invalid chain" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_certificate_signed_storage_failure(self, certificate_manager):
        """Test handling CertificateSigned message with storage failure."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA
        certificate_chain = ["cert1", "cert2", "cert3"]
        exi_response = base64.b64encode(b"test_response").decode()

        with (
            patch.object(
                certificate_manager, "_validate_certificate_chain", return_value={"valid": True}
            ),
            patch.object(
                certificate_manager,
                "_parse_certificate",
                return_value=CertificateInfo(
                    certificate_type=certificate_type,
                    certificate_data=exi_response,
                    issuer_name="Test CA",
                    subject_name="Test Certificate",
                    serial_number="123456789",
                    valid_from=datetime.now(timezone.utc),
                    valid_to=datetime.now(timezone.utc) + timedelta(days=365),
                    status=CertificateStatus.VALID,
                ),
            ),
            patch.object(certificate_manager, "_check_certificate_limits", return_value=True),
            patch.object(
                certificate_manager,
                "_install_certificate",
                return_value={
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "InternalError",
                        "additionalInfo": "Failed to store certificate",
                    },
                },
            ),
        ):

            result = await certificate_manager.certificate_signed(
                station_id, certificate_type, certificate_chain, exi_response
            )

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "InternalError"
            assert "Failed to store certificate" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_delete_certificate_success(self, certificate_manager):
        """Test deleting a certificate successfully."""
        station_id = "STATION_001"
        certificate_hash_data = [
            {
                "certificateType": "V2GRootCertificate",
                "hashAlgorithm": "SHA256",
                "issuerNameHash": "abcd1234",
                "issuerKeyHash": "efgh5678",
                "serialNumber": "123456789",
            }
        ]

        with (
            patch.object(certificate_manager, "_find_certificate_by_hash", return_value="cert123"),
            patch.object(certificate_manager, "_delete_certificate_by_id", return_value=None),
        ):

            result = await certificate_manager.delete_certificate(station_id, certificate_hash_data)

            assert result["status"] == "Accepted"
            assert result["statusInfo"]["reasonCode"] == "NoError"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_delete_certificate_not_found(self, certificate_manager):
        """Test deleting a non-existent certificate."""
        station_id = "STATION_001"
        certificate_hash_data = [
            {
                "certificateType": "V2GRootCertificate",
                "hashAlgorithm": "SHA256",
                "issuerNameHash": "abcd1234",
                "issuerKeyHash": "efgh5678",
                "serialNumber": "123456789",
            }
        ]

        with patch.object(certificate_manager, "_find_certificate_by_hash", return_value=None):
            result = await certificate_manager.delete_certificate(station_id, certificate_hash_data)

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "UnknownCertificate"
            assert "No matching certificates found" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_delete_certificate_exception(self, certificate_manager):
        """Test deleting a certificate with exception."""
        station_id = "STATION_001"
        certificate_hash_data = [
            {
                "certificateType": "V2GRootCertificate",
                "hashAlgorithm": "SHA256",
                "issuerNameHash": "abcd1234",
                "issuerKeyHash": "efgh5678",
                "serialNumber": "123456789",
            }
        ]

        with patch.object(
            certificate_manager, "_find_certificate_by_hash", side_effect=Exception("Test error")
        ):
            result = await certificate_manager.delete_certificate(station_id, certificate_hash_data)

            assert result["status"] == "Rejected"
            assert result["statusInfo"]["reasonCode"] == "InternalError"
            assert "Test error" in result["statusInfo"]["additionalInfo"]

    @pytest.mark.timeout(10)
    def test_certificate_info_creation(self):
        """Test CertificateInfo creation."""
        cert_info = CertificateInfo(
            certificate_type=CertificateType.V2G_ROOT_CA,
            certificate_data=base64.b64encode(b"test_certificate_data").decode(),
            issuer_name="Test CA",
            subject_name="Test Certificate",
            serial_number="123456789",
            valid_from=datetime.now(timezone.utc),
            valid_to=datetime.now(timezone.utc) + timedelta(days=365),
            status=CertificateStatus.VALID,
        )

        assert cert_info.certificate_type == CertificateType.V2G_ROOT_CA
        assert cert_info.issuer_name == "Test CA"
        assert cert_info.subject_name == "Test Certificate"
        assert cert_info.serial_number == "123456789"
        assert cert_info.status == CertificateStatus.VALID
        assert cert_info.certificate_chain is None
        assert cert_info.installation_date is None

    @pytest.mark.timeout(10)
    def test_certificate_info_with_chain(self):
        """Test CertificateInfo creation with certificate chain."""
        cert_info = CertificateInfo(
            certificate_type=CertificateType.V2G_CERTIFICATE_CHAIN,
            certificate_data=base64.b64encode(b"test_certificate_data").decode(),
            certificate_chain=["cert1", "cert2", "cert3"],
            issuer_name="Test CA",
            subject_name="Test Certificate",
            serial_number="123456789",
            valid_from=datetime.now(timezone.utc),
            valid_to=datetime.now(timezone.utc) + timedelta(days=365),
            status=CertificateStatus.VALID,
            installation_date=datetime.now(timezone.utc),
        )

        assert cert_info.certificate_type == CertificateType.V2G_CERTIFICATE_CHAIN
        assert len(cert_info.certificate_chain) == 3
        assert cert_info.certificate_chain[0] == "cert1"
        assert cert_info.installation_date is not None

    @pytest.mark.timeout(10)
    def test_certificate_installation_result_creation(self):
        """Test CertificateInstallationResult creation."""
        result = CertificateInstallationResult(
            status="Accepted", status_info={"reasonCode": "NoError"}
        )

        assert result.status == "Accepted"
        assert result.status_info["reasonCode"] == "NoError"

    @pytest.mark.timeout(10)
    def test_certificate_installation_result_with_error(self):
        """Test CertificateInstallationResult creation with error."""
        result = CertificateInstallationResult(
            status="Rejected",
            status_info={"reasonCode": "InvalidCertificate", "additionalInfo": "Test error"},
        )

        assert result.status == "Rejected"
        assert result.status_info["reasonCode"] == "InvalidCertificate"
        assert result.status_info["additionalInfo"] == "Test error"

    @pytest.mark.timeout(10)
    def test_certificate_manager_certificate_limits(self, certificate_manager):
        """Test certificate limits configuration."""
        limits = certificate_manager.certificate_limits

        # Test specific limits
        assert limits[CertificateType.V2G_ROOT_CA] == 5
        assert limits[CertificateType.MO_SUB_CA_1] == 10
        assert limits[CertificateType.MO_SUB_CA_2] == 10
        assert limits[CertificateType.OEM_SUB_CA_1] == 20
        assert limits[CertificateType.OEM_SUB_CA_2] == 20
        assert limits[CertificateType.CPO_SUB_CA_1] == 20
        assert limits[CertificateType.CPO_SUB_CA_2] == 20
        assert limits[CertificateType.V2G_CSMS_CERTIFICATE] == 10
        assert limits[CertificateType.V2G_CERTIFICATE_CHAIN] == 50
        assert limits[CertificateType.CONTRACT_CERTIFICATE] == 100
        assert limits[CertificateType.CHARGING_STATION_CERTIFICATE] == 5

    @pytest.mark.timeout(10)
    def test_certificate_manager_cache_initialization(self, certificate_manager):
        """Test certificate manager cache initialization."""
        assert certificate_manager.certificate_cache == {}
        assert certificate_manager.validation_cache == {}
        assert certificate_manager.root_cas == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_certificate_manager_with_exi_request(self, certificate_manager):
        """Test getting certificate with EXI request."""
        station_id = "STATION_001"
        certificate_type = CertificateType.V2G_ROOT_CA
        exi_request = base64.b64encode(b"test_exi_request").decode()

        mock_certificate = CertificateInfo(
            certificate_type=certificate_type,
            certificate_data=base64.b64encode(b"test_certificate_data").decode(),
            issuer_name="Test CA",
            subject_name="Test Certificate",
            serial_number="123456789",
            valid_from=datetime.now(timezone.utc),
            valid_to=datetime.now(timezone.utc) + timedelta(days=365),
            status=CertificateStatus.VALID,
        )

        with patch.object(certificate_manager, "_get_certificate", return_value=mock_certificate):
            result = await certificate_manager.get_15118_ev_certificate(
                station_id, certificate_type, exi_request
            )

            assert result["status"] == "Accepted"
            assert "exiResponse" in result
            assert result["exiResponse"] == mock_certificate.certificate_data


class TestCertificateEnums:
    """Test certificate-related enums."""

    @pytest.mark.timeout(10)
    def test_certificate_type_enum(self):
        """Test CertificateType enum values."""
        assert CertificateType.V2G_ROOT_CA.value == "V2GRootCertificate"
        assert CertificateType.MO_SUB_CA_1.value == "MOSubCA1Certificate"
        assert CertificateType.MO_SUB_CA_2.value == "MOSubCA2Certificate"
        assert CertificateType.OEM_SUB_CA_1.value == "OEMSubCA1Certificate"
        assert CertificateType.OEM_SUB_CA_2.value == "OEMSubCA2Certificate"
        assert CertificateType.CPO_SUB_CA_1.value == "CPOSubCA1Certificate"
        assert CertificateType.CPO_SUB_CA_2.value == "CPOSubCA2Certificate"
        assert CertificateType.V2G_CSMS_CERTIFICATE.value == "V2GCertificate"
        assert CertificateType.V2G_CERTIFICATE_CHAIN.value == "V2GCertificateChain"
        assert CertificateType.CONTRACT_CERTIFICATE.value == "ContractCertificate"
        assert CertificateType.CHARGING_STATION_CERTIFICATE.value == "ChargingStationCertificate"
        assert CertificateType.SECC_CERTIFICATE.value == "SECCCertificate"

    @pytest.mark.timeout(10)
    def test_certificate_status_enum(self):
        """Test CertificateStatus enum values."""
        assert CertificateStatus.VALID.value == "Valid"
        assert CertificateStatus.EXPIRED.value == "Expired"
        assert CertificateStatus.REVOKED.value == "Revoked"
        assert CertificateStatus.PENDING.value == "Pending"
        assert CertificateStatus.INVALID.value == "Invalid"

    @pytest.mark.timeout(10)
    def test_certificate_type_aliases(self):
        """Test CertificateType alias values."""
        assert CertificateType.V2G_ROOT_CERTIFICATE.value == "V2GRootCertificate"
        assert CertificateType.MO_SUB_CERTIFICATE.value == "MOSubCertificate"
        assert CertificateType.OEM_SUB_CERTIFICATE.value == "OEMSubCertificate"
        assert CertificateType.CPO_SUB_CERTIFICATE.value == "CPOSubCertificate"
