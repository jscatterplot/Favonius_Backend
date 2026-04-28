"""
Unit tests for SecurityManager - OCPP Security Profile 3 implementation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import jwt
import pytest

from src.websocket_handler.security_manager import (
    AuthenticationMethod,
    SecurityConfig,
    SecurityEvent,
    SecurityEventType,
    SecurityManager,
    SecurityProfile,
    StationAuthToken,
)
from src.websocket_handler.timescale_client import TimescaleClient


class TestSecurityManager:
    """Test the SecurityManager class."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        mock_client = Mock(spec=TimescaleClient)
        return mock_client

    @pytest.fixture
    def security_config(self):
        """Mock security configuration."""
        return SecurityConfig(
            security_profile=SecurityProfile.PROFILE_3,
            require_mtls=True,
            require_station_auth=True,
            token_expiry_hours=24,
            max_failed_auth_attempts=5,
            lockout_duration_minutes=30,
            enable_audit_logging=True,
            require_secure_websocket=True,
        )

    @pytest.fixture
    def security_manager(self, mock_timescale_client, security_config):
        """Create SecurityManager instance."""
        return SecurityManager(mock_timescale_client, security_config)

    @pytest.mark.timeout(10)
    def test_security_manager_initialization(self, mock_timescale_client, security_config):
        """Test SecurityManager initialization."""
        manager = SecurityManager(mock_timescale_client, security_config)

        assert manager.timescale_client == mock_timescale_client
        assert manager.config == security_config
        assert manager.token_cache == {}
        assert manager.failed_auth_attempts == {}
        assert manager.security_events == []
        assert manager.jwt_secret is not None
        assert manager.cert_validation_cache == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_station_success(self, security_manager):
        """Test successful station authentication."""
        station_id = "STATION_001"
        auth_data = {"token": "valid_token"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_authenticate_bearer_token", return_value=True),
            patch.object(security_manager, "_clear_failed_attempts", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):

            success, error = await security_manager.authenticate_station(station_id, auth_data)

            assert success is True
            assert error is None

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_station_locked_out(self, security_manager):
        """Test authentication when station is locked out."""
        station_id = "STATION_001"
        auth_data = {"token": "valid_token"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=True),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):

            success, error = await security_manager.authenticate_station(station_id, auth_data)

            assert success is False
            assert error == "Station is temporarily locked out"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_authenticate_station_failure(self, security_manager):
        """Test failed station authentication."""
        station_id = "STATION_001"
        auth_data = {"token": "invalid_token"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_authenticate_bearer_token", return_value=False),
            patch.object(security_manager, "_authenticate_api_key", return_value=False),
            patch.object(security_manager, "_authenticate_basic_auth", return_value=False),
            patch.object(security_manager, "_authenticate_client_certificate", return_value=False),
            patch.object(security_manager, "_record_failed_attempt", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):

            success, error = await security_manager.authenticate_station(station_id, auth_data)

            assert success is False
            assert error is not None

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_production_charger_requires_basic_auth(self, security_manager):
        """Provisioned production chargers reject non-Basic Auth methods."""
        station_id = "acme-berlin-001"
        auth_data = {"bearer_token": "valid_token"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_station_requires_basic_auth", return_value=True),
            patch.object(security_manager, "_authenticate_bearer_token", return_value=True),
            patch.object(security_manager, "_record_failed_attempt", return_value=None) as failed,
            patch.object(security_manager, "_log_security_event", return_value=None) as log_event,
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is False
        assert error == "Basic Auth credentials required"
        failed.assert_called_once_with(station_id)
        log_event.assert_called_once()
        assert log_event.call_args.args[3]["reason"] == "missing_basic_auth"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_missing_basic_auth_checker_allows_other_methods(self, security_manager):
        """Missing checker should not force Basic Auth-only authentication."""
        station_id = "test-station-001"
        auth_data = {"bearer_token": "valid_token"}
        security_manager.timescale_client.station_requires_basic_auth.side_effect = RuntimeError(
            "db unavailable"
        )

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_authenticate_client_certificate", return_value=False),
            patch.object(security_manager, "_authenticate_bearer_token", return_value=True),
            patch.object(security_manager, "_clear_failed_attempts", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is True
        assert error is None

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_generate_station_token(self, security_manager):
        """Test generating a station authentication token."""
        station_id = "STATION_001"

        with patch.object(security_manager, "_store_auth_token", return_value=None):
            token = await security_manager.generate_station_token(station_id)

            assert token is not None
            assert isinstance(token, StationAuthToken)
            assert token.station_id == station_id
            assert token.token_type == "Bearer"
            assert token.token is not None
            assert len(token.token) > 0

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_station_token_valid(self, security_manager):
        """Test validating a valid station token."""
        station_id = "STATION_001"
        token = "valid_token"

        mock_token = StationAuthToken(
            station_id=station_id,
            token=token,
            token_type="Bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            created_at=datetime.now(timezone.utc),
            usage_count=0,
        )

        security_manager.token_cache[station_id] = mock_token

        with patch.object(security_manager, "_update_token_usage", return_value=None):
            is_valid = await security_manager.validate_station_token(station_id, token)

            assert is_valid is True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_station_token_expired(self, security_manager):
        """Test validating an expired station token."""
        station_id = "STATION_001"
        token = "expired_token"

        mock_token = StationAuthToken(
            station_id=station_id,
            token=token,
            token_type="Bearer",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
            created_at=datetime.now(timezone.utc) - timedelta(hours=25),
            usage_count=0,
        )

        security_manager.token_cache[station_id] = mock_token

        is_valid = await security_manager.validate_station_token(station_id, token)

        assert is_valid is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_station_token_not_found(self, security_manager):
        """Test validating a non-existent station token."""
        station_id = "STATION_001"
        token = "nonexistent_token"

        # Clear cache to simulate token not found
        security_manager.token_cache.clear()

        with patch("jwt.decode", side_effect=jwt.InvalidTokenError("Invalid token")):
            is_valid = await security_manager.validate_station_token(station_id, token)

            assert is_valid is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_revoke_station_token(self, security_manager):
        """Test revoking a station token."""
        station_id = "STATION_001"

        # Add token to cache
        mock_token = StationAuthToken(
            station_id=station_id,
            token="test_token",
            token_type="Bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            created_at=datetime.now(timezone.utc),
            usage_count=0,
        )
        security_manager.token_cache[station_id] = mock_token

        with (
            patch.object(security_manager, "_revoke_auth_token", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):

            result = await security_manager.revoke_station_token(station_id)

            assert result is True
            assert station_id not in security_manager.token_cache

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_security_event_notification(self, security_manager):
        """Test handling security event notification."""
        station_id = "STATION_001"
        event_type = "FailedToAuthenticateAtCentralSystem"
        timestamp = datetime.now(timezone.utc).isoformat()
        tech_info = "Authentication failed"

        with patch.object(security_manager, "_log_security_event", return_value=None):
            result = await security_manager.handle_security_event_notification(
                station_id, event_type, timestamp, tech_info
            )

            assert result["status"] == "Accepted"
            # The actual implementation doesn't include statusInfo for successful cases
            assert "status" in result

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_handle_security_event_notification_invalid_type(self, security_manager):
        """Test handling security event notification with invalid event type."""
        station_id = "STATION_001"
        event_type = "InvalidEventType"
        timestamp = datetime.now(timezone.utc).isoformat()
        tech_info = "Test event"

        result = await security_manager.handle_security_event_notification(
            station_id, event_type, timestamp, tech_info
        )

        assert result["status"] == "Rejected"
        assert result["statusInfo"]["reasonCode"] == "PropertyConstraintViolation"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_security_events(self, security_manager):
        """Test getting security events."""
        station_id = "STATION_001"
        mock_events = [
            {
                "event_type": "FailedToAuthenticateAtCentralSystem",
                "timestamp": datetime.now(timezone.utc),
                "tech_info": "Authentication failed",
            },
            {
                "event_type": "StartupOfTheDevice",
                "timestamp": datetime.now(timezone.utc),
                "tech_info": "Device started",
            },
        ]

        with patch.object(
            security_manager.timescale_client, "get_security_events", return_value=mock_events
        ):
            events = await security_manager.get_security_events(station_id)

            assert len(events) == 2
            assert (
                events[0].event_type == SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM
            )
            assert events[1].event_type == SecurityEventType.STARTUP_OF_THE_DEVICE

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_client_certificate(self, security_manager):
        """Test validating a client certificate."""
        station_id = "STATION_001"
        mock_cert = Mock()
        mock_cert.fingerprint.return_value = b"test_fingerprint"

        with patch.object(security_manager, "_validate_certificate_chain", return_value=True):
            is_valid = await security_manager.validate_client_certificate(mock_cert, station_id)

            assert is_valid is True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_validate_client_certificate_invalid(self, security_manager):
        """Test validating an invalid client certificate."""
        station_id = "STATION_001"
        mock_cert = Mock()
        mock_cert.fingerprint.return_value = b"test_fingerprint"

        with patch.object(security_manager, "_validate_certificate_chain", return_value=False):
            is_valid = await security_manager.validate_client_certificate(mock_cert, station_id)

            assert is_valid is False

    @pytest.mark.timeout(10)
    def test_security_event_creation(self):
        """Test SecurityEvent creation."""
        event = SecurityEvent(
            event_type=SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM,
            timestamp=datetime.now(timezone.utc),
            tech_info="Authentication failed",
            additional_info={"attempts": 3},
        )

        assert event.event_type == SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM
        assert event.tech_info == "Authentication failed"
        assert event.additional_info["attempts"] == 3
        assert event.timestamp is not None

    @pytest.mark.timeout(10)
    def test_station_auth_token_creation(self):
        """Test StationAuthToken creation."""
        token = StationAuthToken(
            station_id="STATION_001",
            token="test_token",
            token_type="Bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            created_at=datetime.now(timezone.utc),
            last_used=datetime.now(timezone.utc),
            usage_count=5,
        )

        assert token.station_id == "STATION_001"
        assert token.token == "test_token"
        assert token.token_type == "Bearer"
        assert token.usage_count == 5
        assert token.last_used is not None

    @pytest.mark.timeout(10)
    def test_security_config_creation(self):
        """Test SecurityConfig creation."""
        config = SecurityConfig(
            security_profile=SecurityProfile.PROFILE_3,
            require_mtls=True,
            require_station_auth=True,
            token_expiry_hours=48,
            max_failed_auth_attempts=3,
            lockout_duration_minutes=60,
            enable_audit_logging=True,
            require_secure_websocket=True,
        )

        assert config.security_profile == SecurityProfile.PROFILE_3
        assert config.require_mtls is True
        assert config.require_station_auth is True
        assert config.token_expiry_hours == 48
        assert config.max_failed_auth_attempts == 3
        assert config.lockout_duration_minutes == 60
        assert config.enable_audit_logging is True
        assert config.require_secure_websocket is True
        assert len(config.allowed_cipher_suites) > 0


class TestSecurityEnums:
    """Test security-related enums."""

    @pytest.mark.timeout(10)
    def test_security_profile_enum(self):
        """Test SecurityProfile enum values."""
        assert SecurityProfile.PROFILE_1.value == 1
        assert SecurityProfile.PROFILE_2.value == 2
        assert SecurityProfile.PROFILE_3.value == 3

    @pytest.mark.timeout(10)
    def test_security_event_type_enum(self):
        """Test SecurityEventType enum values."""
        assert SecurityEventType.FIRMWARE_UPDATED.value == "FirmwareUpdated"
        assert (
            SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM.value
            == "FailedToAuthenticateAtCentralSystem"
        )
        assert (
            SecurityEventType.CENTRAL_SYSTEM_FAILED_TO_AUTHENTICATE.value
            == "CentralSystemFailedToAuthenticate"
        )
        assert SecurityEventType.SETTING_SYSTEM_TIME.value == "SettingSystemTime"
        assert SecurityEventType.STARTUP_OF_THE_DEVICE.value == "StartupOfTheDevice"
        assert SecurityEventType.RESET_OR_REBOOT.value == "ResetOrReboot"
        assert SecurityEventType.SECURITY_LOG_CLEARED.value == "SecurityLogCleared"
        assert (
            SecurityEventType.RECONFIGURATION_SECURITY_PARAMETERS.value
            == "ReconfigurationSecurityParameters"
        )
        assert SecurityEventType.MEMORY_EXHAUSTION.value == "MemoryExhaustion"
        assert SecurityEventType.INVALID_MESSAGES.value == "InvalidMessages"
        assert SecurityEventType.ATTEMPTED_REPLAY_ATTACKS.value == "AttemptedReplayAttacks"
        assert SecurityEventType.TAMPER_DETECTION_ACTIVATED.value == "TamperDetectionActivated"
        assert SecurityEventType.INVALID_FIRMWARE_SIGNATURE.value == "InvalidFirmwareSignature"
        assert SecurityEventType.INVALID_CERTIFICATE.value == "InvalidCertificate"
        assert SecurityEventType.CRITICAL_SECURITY_ERROR.value == "CriticalSecurityError"

    @pytest.mark.timeout(10)
    def test_authentication_method_enum(self):
        """Test AuthenticationMethod enum values."""
        assert AuthenticationMethod.BASIC_AUTH.value == "BasicAuth"
        assert AuthenticationMethod.BEARER_TOKEN.value == "BearerToken"
        assert AuthenticationMethod.CLIENT_CERTIFICATE.value == "ClientCertificate"
        assert AuthenticationMethod.API_KEY.value == "ApiKey"
        assert AuthenticationMethod.OAUTH2.value == "OAuth2"
