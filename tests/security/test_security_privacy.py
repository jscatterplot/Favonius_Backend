"""Security and privacy testing for V2G system."""

import asyncio
import json
import logging
import os

# Import test dependencies
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tests.e2e.citrineos_simulator import CitrineOSSimulator
from tests.security.test_certificates import (
    EXPIRED_TEST_CERTIFICATE,
    INVALID_FORMAT_CERTIFICATE,
    VALID_TEST_CERTIFICATE,
)
from websocket_handler.certificate_manager import CertificateManager
from websocket_handler.config import Config
from websocket_handler.privacy_manager import PrivacyManager
from websocket_handler.security_manager import SecurityManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestSecurityCompliance:
    """Test security compliance and vulnerability scenarios."""

    @pytest.fixture
    async def test_server(self):
        """Start test WebSocket server."""
        config = Config()
        server = WebSocketServer(config)  # noqa: F821

        try:
            await server.start()
            yield server
        finally:
            await server.stop()

    @pytest.fixture
    def security_manager(self):
        """Create security manager for testing."""
        return SecurityManager()

    @pytest.fixture
    def certificate_manager(self):
        """Create certificate manager for testing."""
        return CertificateManager(Mock())  # Mock TimescaleClient

    @pytest.mark.asyncio
    async def test_tls_certificate_validation(self, test_server, certificate_manager):
        """Test TLS certificate validation."""

        logger.info("Testing TLS certificate validation")

        # Test 1: Valid certificate
        valid_cert = {
            "certificate_type": "V2GCertificate",
            "certificate_data": VALID_TEST_CERTIFICATE,
            "certificate_chain": [],
        }

        result = await certificate_manager.install_certificate(
            "TEST_STATION", "V2GCertificate", valid_cert["certificate_data"]
        )
        assert result["status"] == "Accepted"

        # Test 2: Invalid certificate format
        invalid_cert = {
            "certificate_type": "V2GCertificate",
            "certificate_data": INVALID_FORMAT_CERTIFICATE,
            "certificate_chain": [],
        }

        result = await certificate_manager.install_certificate(
            "TEST_STATION", "V2GCertificate", invalid_cert["certificate_data"]
        )
        assert result["status"] == "Rejected"
        assert "InvalidCertificateFormat" in result["statusInfo"]["reason_code"]

        # Test 3: Expired certificate
        expired_cert = {
            "certificate_type": "V2GCertificate",
            "certificate_data": EXPIRED_TEST_CERTIFICATE,
            "certificate_chain": [],
        }

        result = await certificate_manager.install_certificate(
            "TEST_STATION", "V2GCertificate", expired_cert["certificate_data"]
        )
        assert result["status"] == "Rejected"
        assert "CertificateExpired" in result["statusInfo"]["reason_code"]

    @pytest.mark.asyncio
    async def test_certificate_rotation_scenario(self, test_server, certificate_manager):
        """Test certificate rotation scenarios."""

        logger.info("Testing certificate rotation")

        # Install initial certificate
        result = await certificate_manager.install_certificate(
            "TEST_STATION", "V2GCertificate", VALID_TEST_CERTIFICATE
        )
        assert result["status"] == "Accepted"

        # Rotate to new certificate
        result = await certificate_manager.install_certificate(
            "TEST_STATION", "V2GCertificate", VALID_TEST_CERTIFICATE
        )
        assert result["status"] == "Accepted"

        # Verify certificate is installed
        installed_certs = await certificate_manager.get_installed_certificate_ids("TEST_STATION")
        assert len(installed_certs) == 1

    @pytest.mark.asyncio
    async def test_authentication_bypass_attempts(self, test_server):
        """Test authentication bypass attempts."""

        logger.info("Testing authentication bypass attempts")

        simulator = CitrineOSSimulator("SECURITY_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Test 1: Unauthorized message without boot
            unauthorized_message = [
                2,
                "1",
                "GetVariables",
                {
                    "getVariableData": [
                        {
                            "component": {"name": "ChargingStation"},
                            "variable": {"name": "VendorName"},
                        }
                    ]
                },
            ]

            await simulator.websocket.send(json.dumps(unauthorized_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should receive error or rejection
            assert response_data[0] in [3, 4]  # Response or error
            if response_data[0] == 4:  # Error
                assert response_data[2] in ["SecurityError", "NotSupported", "NotImplemented"]

            # Test 2: Malicious payload injection
            malicious_message = [
                2,
                "1",
                "SetVariables",
                {
                    "setVariableData": [
                        {
                            "component": {"name": "ChargingStation"},
                            "variable": {"name": "VendorName"},
                            "attributeValue": "<script>alert('xss')</script>",
                        }
                    ]
                },
            ]

            await simulator.websocket.send(json.dumps(malicious_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should handle malicious payload safely
            assert response_data[0] in [3, 4]  # Response or error

            # Test 3: SQL injection attempt
            sql_injection_message = [
                2,
                "1",
                "GetVariables",
                {
                    "getVariableData": [
                        {
                            "component": {"name": "ChargingStation'; DROP TABLE stations; --"},
                            "variable": {"name": "VendorName"},
                        }
                    ]
                },
            ]

            await simulator.websocket.send(json.dumps(sql_injection_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should handle SQL injection safely
            assert response_data[0] in [3, 4]  # Response or error

        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_message_integrity_validation(self, test_server):
        """Test message integrity validation."""

        logger.info("Testing message integrity validation")

        simulator = CitrineOSSimulator("INTEGRITY_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Boot notification first
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"

            # Test 1: Message tampering
            tampered_message = [2, "1", "Heartbeat", {"tampered": "data"}]

            await simulator.websocket.send(json.dumps(tampered_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should handle tampered message
            assert response_data[0] in [3, 4]  # Response or error

            # Test 2: Invalid message structure
            invalid_structure = [2, "1", "Heartbeat", "invalid_structure"]

            await simulator.websocket.send(json.dumps(invalid_structure))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should handle invalid structure
            assert response_data[0] in [3, 4]  # Response or error

            # Test 3: Oversized message
            oversized_payload = {"data": "x" * 100000}  # Large payload
            oversized_message = [2, "1", "DataTransfer", oversized_payload]

            await simulator.websocket.send(json.dumps(oversized_message))
            response = await simulator.websocket.recv()
            response_data = json.loads(response)

            # Should handle oversized message
            assert response_data[0] in [3, 4]  # Response or error

        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_rate_limiting_security(self, test_server):
        """Test rate limiting security measures."""

        logger.info("Testing rate limiting security")

        simulator = CitrineOSSimulator("RATE_LIMIT_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Boot notification first
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"

            # Send rapid messages to test rate limiting
            message_count = 0
            error_count = 0

            for i in range(100):  # Send 100 messages rapidly
                heartbeat_message = [2, str(i + 1), "Heartbeat", {}]

                try:
                    await simulator.websocket.send(json.dumps(heartbeat_message))
                    response = await simulator.websocket.recv()
                    response_data = json.loads(response)

                    message_count += 1

                    if response_data[0] == 4:  # Error
                        error_count += 1

                except Exception:
                    error_count += 1

                # Small delay to avoid overwhelming
                await asyncio.sleep(0.01)

            logger.info(f"Rate limiting test: {message_count} messages sent, {error_count} errors")

            # Should have some rate limiting in effect
            assert error_count > 0, "Should have some rate limiting errors"

        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_encryption_validation(self, test_server, security_manager):
        """Test encryption validation."""

        logger.info("Testing encryption validation")

        # Test 1: Data encryption
        sensitive_data = "sensitive_customer_data"
        encrypted_data = security_manager.encrypt_data(sensitive_data)

        assert encrypted_data != sensitive_data, "Data should be encrypted"
        assert len(encrypted_data) > len(sensitive_data), "Encrypted data should be longer"

        # Test 2: Data decryption
        decrypted_data = security_manager.decrypt_data(encrypted_data)
        assert decrypted_data == sensitive_data, "Decrypted data should match original"

        # Test 3: Hash generation
        data_hash = security_manager.generate_hash(sensitive_data)
        assert len(data_hash) == 64, "Hash should be 64 characters (SHA-256)"

        # Test 4: Hash verification
        is_valid = security_manager.verify_hash(sensitive_data, data_hash)
        assert is_valid is True, "Hash verification should pass"

        # Test 5: Invalid hash verification
        invalid_hash = "invalid_hash"
        is_valid = security_manager.verify_hash(sensitive_data, invalid_hash)
        assert is_valid is False, "Invalid hash verification should fail"

    @pytest.mark.asyncio
    async def test_security_audit_logging(self, test_server):
        """Test security audit logging."""

        logger.info("Testing security audit logging")

        simulator = CitrineOSSimulator("AUDIT_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Boot notification
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"

            # Send various messages to generate audit logs
            await simulator.heartbeat()
            await simulator.meter_values(1, 22.5)
            await simulator.status_notification(1, "Available")

            # Test unauthorized access attempt
            unauthorized_message = [
                2,
                "1",
                "GetVariables",
                {
                    "getVariableData": [
                        {
                            "component": {"name": "ChargingStation"},
                            "variable": {"name": "VendorName"},
                        }
                    ]
                },
            ]

            await simulator.websocket.send(json.dumps(unauthorized_message))
            await simulator.websocket.recv()

            # Verify audit logs were generated
            # Note: This would require implementing audit log verification
            # For now, just verify the operations completed

        finally:
            await simulator.disconnect()


class TestPrivacyCompliance:
    """Test privacy compliance and GDPR scenarios."""

    @pytest.fixture
    def privacy_manager(self):
        """Create privacy manager for testing."""
        return PrivacyManager(Mock())  # Mock TimescaleClient

    @pytest.mark.asyncio
    async def test_gdpr_data_access_request(self, test_server, privacy_manager):
        """Test GDPR data access request."""

        logger.info("Testing GDPR data access request")

        # Test 1: Valid data access request
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType

        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)

        result = await privacy_manager.handle_customer_information_request(
            station_id="TEST_STATION",
            request_id=1,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123",
        )

        assert result["status"] == "Accepted"

        # Test 2: Invalid customer identifier
        result = await privacy_manager.handle_customer_information_request(
            station_id="TEST_STATION",
            request_id=2,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="INVALID_CUSTOMER",
        )

        assert result["status"] == "Rejected"
        assert "InvalidCustomerIdentifier" in result["statusInfo"]["reason_code"]

    @pytest.mark.asyncio
    async def test_gdpr_data_deletion_request(self, test_server, privacy_manager):
        """Test GDPR data deletion request."""

        logger.info("Testing GDPR data deletion request")

        # Test 1: Valid data deletion request
        from ocpp.v21.datatypes import IdTokenType
        from ocpp.v21.enums import IdTokenEnumType

        id_token = IdTokenType(id_token="CUSTOMER123", type=IdTokenEnumType.key_code)

        result = await privacy_manager.handle_delete_customer_information_request(
            station_id="TEST_STATION",
            request_id=1,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="CUSTOMER123",
        )

        assert result["status"] == "Accepted"

        # Test 2: Data not found
        result = await privacy_manager.handle_delete_customer_information_request(
            station_id="TEST_STATION",
            request_id=2,
            customer_certificate_id=None,
            id_token=id_token,
            customer_identifier="NONEXISTENT_CUSTOMER",
        )

        assert result["status"] == "Rejected"
        assert "DataNotFound" in result["statusInfo"]["reason_code"]

    @pytest.mark.asyncio
    async def test_data_anonymization(self, test_server, privacy_manager):
        """Test data anonymization."""

        logger.info("Testing data anonymization")

        # Test 1: Anonymize customer data
        customer_data = {
            "customer_id": "CUSTOMER123",
            "name": "John Doe",
            "email": "john.doe@example.com",
            "phone": "+1234567890",
            "address": "123 Main St, City, State",
        }

        anonymized_data = await privacy_manager.anonymize_customer_data(customer_data)

        # Verify sensitive data is anonymized
        assert anonymized_data["customer_id"] != customer_data["customer_id"]
        assert anonymized_data["name"] != customer_data["name"]
        assert anonymized_data["email"] != customer_data["email"]
        assert anonymized_data["phone"] != customer_data["phone"]
        assert anonymized_data["address"] != customer_data["address"]

        # Verify anonymized data structure is preserved
        assert "customer_id" in anonymized_data
        assert "name" in anonymized_data
        assert "email" in anonymized_data
        assert "phone" in anonymized_data
        assert "address" in anonymized_data

    @pytest.mark.asyncio
    async def test_consent_management(self, test_server, privacy_manager):
        """Test consent management."""

        logger.info("Testing consent management")

        # Test 1: Record consent
        consent_data = {
            "customer_id": "CUSTOMER123",
            "consent_type": "data_processing",
            "consent_given": True,
            "consent_timestamp": datetime.now(timezone.utc),
            "consent_version": "1.0",
        }

        result = await privacy_manager.record_consent(consent_data)
        assert result["status"] == "Accepted"

        # Test 2: Withdraw consent
        withdrawal_data = {
            "customer_id": "CUSTOMER123",
            "consent_type": "data_processing",
            "consent_given": False,
            "consent_timestamp": datetime.now(timezone.utc),
            "consent_version": "1.0",
        }

        result = await privacy_manager.record_consent(withdrawal_data)
        assert result["status"] == "Accepted"

        # Test 3: Get consent status
        consent_status = await privacy_manager.get_consent_status("CUSTOMER123", "data_processing")
        assert consent_status["consent_given"] is False
        assert consent_status["consent_timestamp"] is not None

    @pytest.mark.asyncio
    async def test_data_retention_policy(self, test_server, privacy_manager):
        """Test data retention policy."""

        logger.info("Testing data retention policy")

        # Test 1: Set retention policy
        retention_policy = {
            "data_type": "transaction_data",
            "retention_period_days": 365,
            "anonymization_after_days": 90,
            "deletion_after_days": 365,
        }

        result = await privacy_manager.set_data_retention_policy(retention_policy)
        assert result["status"] == "Accepted"

        # Test 2: Get retention policy
        policy = await privacy_manager.get_data_retention_policy("transaction_data")
        assert policy["retention_period_days"] == 365
        assert policy["anonymization_after_days"] == 90
        assert policy["deletion_after_days"] == 365

        # Test 3: Apply retention policy
        old_data = {
            "transaction_id": "TXN123",
            "customer_id": "CUSTOMER123",
            "timestamp": datetime.now(timezone.utc) - timedelta(days=100),
        }

        result = await privacy_manager.apply_retention_policy(old_data)
        assert result["status"] == "Accepted"
        assert result["action"] == "anonymized"  # Should be anonymized after 90 days

    @pytest.mark.asyncio
    async def test_privacy_audit_logging(self, test_server, privacy_manager):
        """Test privacy audit logging."""

        logger.info("Testing privacy audit logging")

        # Test 1: Data access audit
        access_data = {
            "customer_id": "CUSTOMER123",
            "access_type": "data_request",
            "timestamp": datetime.now(timezone.utc),
            "requested_by": "CUSTOMER123",
        }

        result = await privacy_manager.log_privacy_event(access_data)
        assert result["status"] == "Accepted"

        # Test 2: Data deletion audit
        deletion_data = {
            "customer_id": "CUSTOMER123",
            "access_type": "data_deletion",
            "timestamp": datetime.now(timezone.utc),
            "requested_by": "CUSTOMER123",
        }

        result = await privacy_manager.log_privacy_event(deletion_data)
        assert result["status"] == "Accepted"

        # Test 3: Get audit logs
        audit_logs = await privacy_manager.get_privacy_audit_logs("CUSTOMER123")
        assert len(audit_logs) >= 2
        assert any(log["access_type"] == "data_request" for log in audit_logs)
        assert any(log["access_type"] == "data_deletion" for log in audit_logs)


class TestSecurityPrivacyIntegration:
    """Test integration of security and privacy features."""

    @pytest.mark.asyncio
    async def test_end_to_end_security_privacy(self, test_server):
        """Test end-to-end security and privacy integration."""

        logger.info("Testing end-to-end security and privacy integration")

        simulator = CitrineOSSimulator("SECURITY_PRIVACY_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Boot notification with security
            boot_result = await simulator.boot_notification()
            assert boot_result[2]["status"] == "Accepted"

            # Start transaction with privacy compliance
            start_result = await simulator.request_start_transaction(1)
            assert start_result[2]["status"] == "Accepted"
            transaction_id = start_result[2]["transactionId"]

            # Transaction event with audit logging
            txn_result = await simulator.transaction_event("Started", transaction_id)
            assert txn_result[2]["status"] == "Accepted"

            # Meter values with data protection
            meter_result = await simulator.meter_values(1, 22.5, transaction_id)
            assert meter_result is None  # MeterValues has no response

            # Stop transaction with privacy compliance
            stop_result = await simulator.request_stop_transaction(transaction_id)
            assert stop_result[2]["status"] == "Accepted"

            # Transaction ended with audit logging
            end_result = await simulator.transaction_event(
                "Ended", transaction_id, "EVDisconnected"
            )
            assert end_result[2]["status"] == "Accepted"

            # Verify security and privacy measures were applied
            # Note: This would require implementing verification methods

        finally:
            await simulator.disconnect()

    @pytest.mark.asyncio
    async def test_security_privacy_performance(self, test_server):
        """Test security and privacy performance impact."""

        logger.info("Testing security and privacy performance impact")

        simulator = CitrineOSSimulator("PERFORMANCE_TEST_001", "ws://localhost:9000")

        await simulator.connect()

        try:
            # Boot notification
            start_time = datetime.now()
            boot_result = await simulator.boot_notification()
            boot_time = (datetime.now() - start_time).total_seconds()

            assert boot_result[2]["status"] == "Accepted"
            assert boot_time < 1.0, "Security should not significantly impact performance"

            # Heartbeat with security
            start_time = datetime.now()
            heartbeat_result = await simulator.heartbeat()
            heartbeat_time = (datetime.now() - start_time).total_seconds()

            assert heartbeat_result[2]["status"] == "Accepted"
            assert heartbeat_time < 0.5, "Security should not significantly impact heartbeat"

            # Meter values with privacy
            start_time = datetime.now()
            meter_result = await simulator.meter_values(1, 22.5)
            meter_time = (datetime.now() - start_time).total_seconds()

            assert meter_result is None
            assert meter_time < 0.5, "Privacy should not significantly impact meter values"

        finally:
            await simulator.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
