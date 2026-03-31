"""Integration tests for WebSocket Handler security.

Tests verify that:
- SecurityManager authentication chain works correctly
- Geo-blocking module integrates with server
- Lockout mechanism works after failed attempts

Note: The websocket_handler has heavy transitive dependencies (pandas,
sqlalchemy, uvloop, etc.). We mock the heavy modules before importing
security_manager to isolate it for testing.
"""

from __future__ import annotations

import importlib
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest
from unittest.mock import AsyncMock

# ── Pre-mock heavy transitive deps that security_manager imports transitively ──
# security_manager → .monitoring (structlog), .timescale_client (asyncpg, pandas, sqlalchemy)
_MOCK_TARGETS = {}

# Create mock for timescale_client module
_mock_timescale_mod = MagicMock()
_mock_timescale_mod.TimescaleClient = MagicMock

# Create mock for monitoring module with a real get_logger
_mock_monitoring_mod = MagicMock()
_mock_monitoring_mod.get_logger = lambda name=__name__: logging.getLogger(name)

# Patch before any websocket_handler imports
for mod_name in [
    "structlog",
    "pandas",
    "sqlalchemy",
    "sqlalchemy.text",
    "uvloop",
]:
    if mod_name not in sys.modules:
        _MOCK_TARGETS[mod_name] = sys.modules.get(mod_name)
        sys.modules[mod_name] = MagicMock()

# Patch the websocket_handler submodules themselves if not yet loaded
_ws_prefix = "src.websocket_handler"
if f"{_ws_prefix}.monitoring" not in sys.modules:
    sys.modules[f"{_ws_prefix}.monitoring"] = _mock_monitoring_mod
if f"{_ws_prefix}.timescale_client" not in sys.modules:
    sys.modules[f"{_ws_prefix}.timescale_client"] = _mock_timescale_mod

from src.websocket_handler.security_manager import (  # noqa: E402
    SecurityConfig,
    SecurityManager,
)


# ============ Tests: SecurityManager Authentication ============


class TestSecurityManagerAuth:
    """Test SecurityManager authentication chain."""

    @pytest.fixture
    def mock_timescale(self):
        """Mock TimescaleDB client with async methods.

        SecurityManager calls various async methods on the timescale client
        (store_security_event, store_auth_token, etc). Using AsyncMock as the
        base ensures all attribute accesses return awaitable mocks.
        """
        client = AsyncMock()
        client.pg_pool = MagicMock()
        return client

    @pytest.fixture
    def security_manager(self, mock_timescale) -> SecurityManager:
        """SecurityManager with auth required."""
        config = SecurityConfig(
            require_station_auth=True,
            require_mtls=False,
        )
        return SecurityManager(mock_timescale, config)

    @pytest.mark.asyncio
    async def test_authenticate_station_no_credentials(self, security_manager):
        """Station with no credentials fails authentication."""
        auth_ok, error = await security_manager.authenticate_station(
            station_id="station_001",
            auth_data={},
        )
        assert auth_ok is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_authenticate_station_with_bearer_token(self, security_manager):
        """Station with valid bearer token authenticates."""
        token_obj = await security_manager.generate_station_token("station_001")
        auth_ok, error = await security_manager.authenticate_station(
            station_id="station_001",
            auth_data={"bearer_token": token_obj.token},
        )
        assert auth_ok is True
        assert error is None

    @pytest.mark.asyncio
    async def test_authenticate_station_with_invalid_token(self, security_manager):
        """Station with invalid bearer token fails."""
        auth_ok, error = await security_manager.authenticate_station(
            station_id="station_001",
            auth_data={"bearer_token": "invalid_token_here"},
        )
        assert auth_ok is False

    @pytest.mark.asyncio
    async def test_lockout_after_max_failures(self, security_manager):
        """Station is locked out after max failed authentication attempts."""
        for i in range(security_manager.config.max_failed_auth_attempts):
            auth_ok, _ = await security_manager.authenticate_station(
                station_id="station_bad",
                auth_data={"bearer_token": "wrong"},
            )
            assert auth_ok is False

        # Next attempt should be locked out
        auth_ok, error = await security_manager.authenticate_station(
            station_id="station_bad",
            auth_data={"bearer_token": "wrong"},
        )
        assert auth_ok is False
        assert "locked out" in error.lower()

    @pytest.mark.asyncio
    async def test_security_events_stored_to_db(self, security_manager, mock_timescale):
        """Authentication attempts store security events to the database."""
        await security_manager.authenticate_station(
            station_id="station_test",
            auth_data={},
        )
        # Security events are stored via timescale_client.store_security_event
        mock_timescale.store_security_event.assert_called()
        call_args = mock_timescale.store_security_event.call_args[0][0]
        assert call_args["station_id"] == "station_test"
        assert "event_type" in call_args

    @pytest.mark.asyncio
    async def test_successful_auth_clears_failed_attempts(self, security_manager):
        """Successful auth clears the failed attempt counter."""
        for _ in range(3):
            await security_manager.authenticate_station(
                station_id="station_recover",
                auth_data={"bearer_token": "wrong"},
            )

        token_obj = await security_manager.generate_station_token("station_recover")
        auth_ok, _ = await security_manager.authenticate_station(
            station_id="station_recover",
            auth_data={"bearer_token": token_obj.token},
        )
        assert auth_ok is True
        assert len(security_manager.failed_auth_attempts.get("station_recover", [])) == 0


# ============ Tests: Server-Level Security Wiring ============


class TestServerSecurityWiring:
    """Test that security controls are wired into the WebSocket server."""

    def test_security_manager_import(self):
        """SecurityManager can be imported."""
        assert SecurityManager is not None
        assert SecurityConfig is not None

    def test_geo_block_import(self):
        """Geo-blocking module can be imported."""
        from src.security.geo_block import check_ip_blocked

        assert check_ip_blocked is not None

    def test_geo_block_checker_fail_closed(self):
        """Geo-block checker blocks unknown IPs when fail-closed."""
        from src.security.geo_block import GeoBlockChecker, GeoBlockConfig

        config = GeoBlockConfig(
            blocked_countries=["RU", "CN", "BY"],
            enabled=True,
            fail_closed=True,
        )
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = config
        checker._reader = None
        checker._allowed_networks = []

        with patch("src.security.geo_block.GEOIP2_AVAILABLE", True):
            result = checker.check_ip("93.184.216.34")
            assert result.blocked is True

    def test_security_config_defaults(self):
        """SecurityConfig has correct defaults for OCPP security."""
        config = SecurityConfig()
        assert config.require_station_auth is True
        assert config.max_failed_auth_attempts == 5
        assert config.lockout_duration_minutes == 30
