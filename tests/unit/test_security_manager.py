"""
Unit tests for SecurityManager - OCPP Security Profile 3 implementation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

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
        mock_client.station_requires_basic_auth.return_value = False
        mock_client.is_basic_auth_username_allowed.return_value = False
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
        assert manager.static_auth_client == mock_timescale_client
        assert manager.config == security_config
        assert manager.token_cache == {}
        assert manager.failed_auth_attempts == {}
        assert manager.security_events == []
        assert manager.jwt_secret is not None
        assert manager.cert_validation_cache == {}

    @pytest.mark.timeout(10)
    def test_security_manager_uses_static_auth_client(self, mock_timescale_client, security_config):
        """Static OCPP auth reads can be sourced from Supabase."""
        static_auth_client = Mock()

        manager = SecurityManager(
            mock_timescale_client,
            security_config,
            static_auth_client=static_auth_client,
        )

        assert manager.timescale_client == mock_timescale_client
        assert manager.static_auth_client == static_auth_client

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
    async def test_non_provisioned_basic_auth_allows_alias_username(self, security_manager):
        """Non-provisioned stations still require canonical id or alias as Basic Auth username."""
        station_id = "legacy-station-001"
        auth_data = {"username": "operator-user", "password": "valid_password"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_station_requires_basic_auth", return_value=False),
            patch.object(
                security_manager,
                "_is_basic_auth_username_allowed",
                new_callable=AsyncMock,
                return_value=True,
            ) as username_allowed,
            patch.object(
                security_manager,
                "_validate_basic_auth",
                new_callable=AsyncMock,
                return_value=True,
            ) as validate_basic_auth,
            patch.object(security_manager, "_clear_failed_attempts", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is True
        assert error is None
        username_allowed.assert_awaited_once_with(station_id, "operator-user")
        validate_basic_auth.assert_awaited_once_with(station_id, "operator-user", "valid_password")

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_production_basic_auth_requires_username_to_match_station_id(
        self, security_manager
    ):
        """Onboarded production chargers must use the station id or a configured alias."""
        station_id = "acme-berlin-001"
        auth_data = {"username": "operator-user", "password": "valid_password"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_station_requires_basic_auth", return_value=True),
            patch.object(
                security_manager,
                "_is_basic_auth_username_allowed",
                new_callable=AsyncMock,
                return_value=False,
            ) as username_allowed,
            patch.object(
                security_manager,
                "_validate_basic_auth",
                new_callable=AsyncMock,
                return_value=True,
            ) as validate_basic_auth,
            patch.object(
                security_manager, "_record_failed_attempt", new_callable=AsyncMock
            ) as failed,
            patch.object(
                security_manager, "_log_security_event", new_callable=AsyncMock
            ) as log_event,
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is False
        assert error == "Basic Auth username must match station id or active alias"
        username_allowed.assert_awaited_once_with(station_id, "operator-user")
        validate_basic_auth.assert_not_awaited()
        failed.assert_awaited_once_with(station_id)
        log_event.assert_awaited_once()
        assert log_event.await_args.args[3]["reason"] == "basic_auth_username_mismatch"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_production_basic_auth_accepts_configured_username_alias(self, security_manager):
        """Onboarded production chargers may use an active vendor username alias."""
        station_id = "hrx-uab_hrx-vilnius-001"
        auth_data = {"username": "TACW1141622G1433", "password": "valid_password"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_station_requires_basic_auth", return_value=True),
            patch.object(
                security_manager,
                "_is_basic_auth_username_allowed",
                new_callable=AsyncMock,
                return_value=True,
            ) as username_allowed,
            patch.object(
                security_manager,
                "_validate_basic_auth",
                new_callable=AsyncMock,
                return_value=True,
            ) as validate_basic_auth,
            patch.object(security_manager, "_clear_failed_attempts", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is True
        assert error is None
        assert username_allowed.await_count == 1
        assert all(
            c == ((station_id, "TACW1141622G1433"), {}) for c in username_allowed.await_args_list
        )
        validate_basic_auth.assert_awaited_once_with(
            station_id, "TACW1141622G1433", "valid_password"
        )

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_non_required_basic_auth_rejects_unknown_username(self, security_manager):
        """Fallback basic-auth path must reject credentials with an unrecognised username."""
        station_id = "non-provisioned-001"
        auth_data = {"username": "random-hardware-serial", "password": "stolen-password"}

        with (
            patch.object(security_manager, "_is_station_locked_out", return_value=False),
            patch.object(security_manager, "_station_requires_basic_auth", return_value=False),
            patch.object(
                security_manager,
                "_is_basic_auth_username_allowed",
                new_callable=AsyncMock,
                return_value=False,
            ) as username_allowed,
            patch.object(
                security_manager,
                "_validate_basic_auth",
                new_callable=AsyncMock,
                return_value=True,
            ) as validate_basic_auth,
            patch.object(security_manager, "_authenticate_client_certificate", return_value=False),
            patch.object(security_manager, "_authenticate_bearer_token", return_value=False),
            patch.object(security_manager, "_authenticate_api_key", return_value=False),
            patch.object(security_manager, "_record_failed_attempt", return_value=None),
            patch.object(security_manager, "_log_security_event", return_value=None),
            patch.object(security_manager, "_emit_charger_auth_failure_alert", return_value=None),
        ):
            success, error = await security_manager.authenticate_station(station_id, auth_data)

        assert success is False
        username_allowed.assert_awaited_once_with(station_id, "random-hardware-serial")
        validate_basic_auth.assert_not_awaited()

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


class TestChargerAuthFailureAlertSql:
    """Regression test: alert helpers must query the local TimescaleDB
    schema (chargers/depots), not the Supabase static-data schema
    (charging_stations/sites). The legacy WS handler's pg_pool only
    sees TimescaleDB, so the wrong table names raise UndefinedTableError
    on every successful auth and spam the logs (see incident 2026-05-05)."""

    @pytest.fixture
    def security_manager(self):
        from src.websocket_handler.security_manager import SecurityConfig, SecurityManager

        tc = Mock()
        # The pool is read directly off the timescale_client.
        return SecurityManager(tc, SecurityConfig())

    def _make_pool_with_capture(self, fetchrow_return=None, fetchval_return=None):
        """Build an asyncpg-style pool whose acquire() yields a connection
        that records the SQL passed to ``fetchrow`` / ``fetchval``."""
        captured: dict[str, str] = {}

        conn = Mock()

        async def fetchrow(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return fetchrow_return

        async def fetchval(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return fetchval_return

        conn.fetchrow = fetchrow
        conn.fetchval = fetchval

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        pool = Mock()
        pool.acquire = lambda: _Acquire()
        return pool, captured

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_emit_uses_local_schema_names(self, security_manager):
        pool, captured = self._make_pool_with_capture(fetchrow_return=None)
        security_manager.timescale_client.pg_pool = pool

        await security_manager._emit_charger_auth_failure_alert(
            "hrx-uab_hrx-vilnius-001", "wrong_password"
        )

        sql = captured["sql"]
        assert "FROM chargers c" in sql
        assert "JOIN depots" in sql
        assert "c.ocpp_id = $1" in sql
        # Must not regress to Supabase-only names.
        assert "charging_stations" not in sql
        assert "FROM sites" not in sql
        assert " sites " not in sql
        assert captured["args"] == ("hrx-uab_hrx-vilnius-001",)

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_resolve_uses_local_schema_names(self, security_manager):
        pool, captured = self._make_pool_with_capture(fetchval_return=None)
        security_manager.timescale_client.pg_pool = pool

        await security_manager._resolve_charger_auth_failure_alert("hrx-uab_hrx-vilnius-001")

        sql = captured["sql"]
        assert "FROM chargers c" in sql
        assert "JOIN depots" in sql
        assert "c.ocpp_id = $1" in sql
        assert "charging_stations" not in sql
        assert "FROM sites" not in sql
        assert captured["args"] == ("hrx-uab_hrx-vilnius-001",)

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_resolve_swallows_query_failures(self, security_manager):
        """Any DB error in the alert path must stay silent — the OCPP auth
        request must not be blocked by an alert-pipeline outage."""
        conn = Mock()
        conn.fetchval = AsyncMock(side_effect=RuntimeError("DB down"))

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        pool = Mock()
        pool.acquire = lambda: _Acquire()
        security_manager.timescale_client.pg_pool = pool

        # Must not raise.
        await security_manager._resolve_charger_auth_failure_alert("station-x")

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_resolve_is_noop_without_pool(self, security_manager):
        security_manager.timescale_client.pg_pool = None
        # Must not raise even without a configured pool.
        await security_manager._resolve_charger_auth_failure_alert("station-x")
