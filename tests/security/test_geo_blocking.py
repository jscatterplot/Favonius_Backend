"""Comprehensive tests for geo-blocking (Article 73-3 compliance).

Tests cover:
- Blocked country rejection (RU, CN, BY)
- Allowed country pass-through
- IP allowlist override
- Fail-closed behavior when GeoIP DB is unavailable
- Private/loopback IP handling
- IPv6 address handling
- Configuration from environment variables
- FastAPI middleware integration
- Edge cases (invalid IPs, unknown countries)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.security.geo_block import (
    GeoBlockChecker,
    GeoBlockConfig,
    GeoBlockMiddleware,
    GeoBlockResult,
    check_ip_blocked,
)


# ============ Fixtures ============


@pytest.fixture
def default_config() -> GeoBlockConfig:
    """Default geo-block config with standard Article 73-3 countries."""
    return GeoBlockConfig(
        blocked_countries=["RU", "CN", "BY"],
        allowed_ips=["77.79.0.1"],
        geoip_db_path="/nonexistent/path.mmdb",
        enabled=True,
        fail_closed=True,
    )


@pytest.fixture
def disabled_config() -> GeoBlockConfig:
    """Config with geo-blocking disabled."""
    return GeoBlockConfig(
        blocked_countries=["RU", "CN", "BY"],
        enabled=False,
    )


@pytest.fixture
def fail_open_config() -> GeoBlockConfig:
    """Config with fail-open behavior."""
    return GeoBlockConfig(
        blocked_countries=["RU", "CN", "BY"],
        enabled=True,
        fail_closed=False,
        geoip_db_path="/nonexistent/path.mmdb",
    )


@dataclass
class MockGeoIPResponse:
    """Mock MaxMind GeoIP response."""

    country: MockCountry


@dataclass
class MockCountry:
    """Mock country object."""

    iso_code: Optional[str]


class MockGeoIPReader:
    """Mock MaxMind GeoIP database reader."""

    def __init__(self, country_map: dict[str, str]):
        """Initialize with IP-to-country mapping."""
        self._country_map = country_map

    def country(self, ip: str) -> MockGeoIPResponse:
        """Look up country for IP."""
        if ip in self._country_map:
            return MockGeoIPResponse(country=MockCountry(iso_code=self._country_map[ip]))
        # Simulate AddressNotFoundError
        raise type("AddressNotFoundError", (Exception,), {})()

    def close(self) -> None:
        """Close the reader."""
        pass


@pytest.fixture
def mock_reader() -> MockGeoIPReader:
    """Mock GeoIP reader with test data.

    Uses real public IP ranges (not RFC 5737 documentation ranges like
    198.51.100.x or 203.0.113.x, which Python ipaddress treats as reserved).
    """
    return MockGeoIPReader(
        {
            "8.8.8.8": "US",
            "5.6.7.8": "DE",
            "93.184.216.34": "RU",
            "114.114.114.114": "CN",
            "178.120.0.1": "BY",
            "85.206.162.1": "LT",  # Lithuanian public IP
            "77.79.0.1": "RU",  # Allowlisted Russian IP (for override test)
            "2606:4700::1": "US",  # Cloudflare IPv6 (public)
            "2a02:6b8::1": "RU",  # Yandex IPv6 (blocked country)
        }
    )


@pytest.fixture
def checker_with_mock(default_config, mock_reader) -> GeoBlockChecker:
    """GeoBlockChecker with mocked GeoIP reader."""
    checker = GeoBlockChecker.__new__(GeoBlockChecker)
    checker.config = default_config
    checker._reader = mock_reader
    checker._allowed_networks = []
    # Parse allowed IPs
    import ipaddress

    for ip_str in default_config.allowed_ips:
        try:
            checker._allowed_networks.append(ipaddress.ip_network(ip_str, strict=False))
        except ValueError:
            pass
    return checker


# ============ Tests: Blocked Countries ============


class TestBlockedCountries:
    """Test that requests from blocked countries are rejected."""

    def test_blocks_russian_ip(self, checker_with_mock: GeoBlockChecker):
        """RU is blocked per Article 73-3."""
        result = checker_with_mock.check_ip("93.184.216.34")
        assert result.blocked is True
        assert result.country_code == "RU"
        assert result.reason == "blocked_country"

    def test_blocks_chinese_ip(self, checker_with_mock: GeoBlockChecker):
        """CN is blocked per Article 73-3."""
        result = checker_with_mock.check_ip("114.114.114.114")
        assert result.blocked is True
        assert result.country_code == "CN"
        assert result.reason == "blocked_country"

    def test_blocks_belarusian_ip(self, checker_with_mock: GeoBlockChecker):
        """BY is blocked per Article 73-3."""
        result = checker_with_mock.check_ip("178.120.0.1")
        assert result.blocked is True
        assert result.country_code == "BY"
        assert result.reason == "blocked_country"


# ============ Tests: Allowed Countries ============


class TestAllowedCountries:
    """Test that requests from allowed countries pass through."""

    def test_allows_us_ip(self, checker_with_mock: GeoBlockChecker):
        """US IPs should be allowed."""
        result = checker_with_mock.check_ip("8.8.8.8")
        assert result.blocked is False
        assert result.country_code == "US"
        assert result.reason == "allowed_country"

    def test_allows_german_ip(self, checker_with_mock: GeoBlockChecker):
        """DE IPs should be allowed."""
        result = checker_with_mock.check_ip("5.6.7.8")
        assert result.blocked is False
        assert result.country_code == "DE"

    def test_allows_lithuanian_ip(self, checker_with_mock: GeoBlockChecker):
        """LT IPs should be allowed (home country)."""
        result = checker_with_mock.check_ip("85.206.162.1")
        assert result.blocked is False
        assert result.country_code == "LT"


# ============ Tests: Allowlist Override ============


class TestAllowlistOverride:
    """Test that allowlisted IPs bypass geo-blocking."""

    def test_allowlisted_ip_from_blocked_country(self, checker_with_mock: GeoBlockChecker):
        """An allowlisted IP should pass even if from a blocked country."""
        # 77.79.0.1 maps to RU but is in the allowlist
        result = checker_with_mock.check_ip("77.79.0.1")
        assert result.blocked is False
        assert result.reason == "allowlisted"

    def test_allowlist_with_cidr(self):
        """CIDR ranges in the allowlist should work."""
        config = GeoBlockConfig(
            blocked_countries=["RU"],
            allowed_ips=["77.79.0.0/24"],
            enabled=True,
            fail_closed=True,
        )
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = config
        checker._reader = MockGeoIPReader({"77.79.0.100": "RU"})
        import ipaddress

        checker._allowed_networks = [ipaddress.ip_network("77.79.0.0/24")]

        result = checker.check_ip("77.79.0.100")
        assert result.blocked is False
        assert result.reason == "allowlisted"


# ============ Tests: Fail-Closed Behavior ============


class TestFailClosed:
    """Test fail-closed behavior when GeoIP is unavailable."""

    def test_no_geoip_reader_blocks_when_fail_closed(self, default_config: GeoBlockConfig):
        """When GeoIP DB is unavailable and fail_closed=True, block."""
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = default_config
        checker._reader = None
        checker._allowed_networks = []

        with patch("src.security.geo_block.GEOIP2_AVAILABLE", True):
            result = checker.check_ip("1.2.3.4")
            assert result.blocked is True
            assert result.reason == "geoip_unavailable_fail_closed"

    def test_unknown_ip_blocks_when_fail_closed(self, checker_with_mock: GeoBlockChecker):
        """When IP is not in GeoIP DB and fail_closed=True, block."""
        # Use a public IP not in the mock reader's mapping
        result = checker_with_mock.check_ip("44.44.44.44")
        assert result.blocked is True
        assert "fail_closed" in result.reason

    def test_no_geoip_reader_allows_when_fail_open(self, fail_open_config: GeoBlockConfig):
        """When GeoIP DB is unavailable and fail_closed=False, allow."""
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = fail_open_config
        checker._reader = None
        checker._allowed_networks = []

        with patch("src.security.geo_block.GEOIP2_AVAILABLE", True):
            result = checker.check_ip("1.2.3.4")
            assert result.blocked is False
            assert result.reason == "geoip_unavailable_fail_open"

    def test_geoip2_not_installed_blocks_when_fail_closed(self, default_config: GeoBlockConfig):
        """When geoip2 package is not installed and fail_closed=True, block."""
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = default_config
        checker._reader = None
        checker._allowed_networks = []

        with patch("src.security.geo_block.GEOIP2_AVAILABLE", False):
            result = checker.check_ip("1.2.3.4")
            assert result.blocked is True
            assert result.reason == "geoip_unavailable_fail_closed"


# ============ Tests: Private/Loopback IPs ============


class TestPrivateIPs:
    """Test that private/loopback IPs are always allowed."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "::1",
            "fe80::1",
        ],
    )
    def test_private_and_loopback_always_allowed(
        self, checker_with_mock: GeoBlockChecker, ip: str
    ):
        """Private, loopback, and link-local IPs bypass geo-blocking."""
        result = checker_with_mock.check_ip(ip)
        assert result.blocked is False
        assert result.reason == "private_ip"


# ============ Tests: IPv6 ============


class TestIPv6:
    """Test IPv6 address handling."""

    def test_ipv6_allowed_country(self, checker_with_mock: GeoBlockChecker):
        """IPv6 address from allowed country passes."""
        result = checker_with_mock.check_ip("2606:4700::1")
        assert result.blocked is False
        assert result.country_code == "US"

    def test_ipv6_blocked_country(self, checker_with_mock: GeoBlockChecker):
        """IPv6 address from blocked country is rejected."""
        result = checker_with_mock.check_ip("2a02:6b8::1")
        assert result.blocked is True
        assert result.country_code == "RU"

    def test_ipv6_loopback(self, checker_with_mock: GeoBlockChecker):
        """IPv6 loopback is always allowed."""
        result = checker_with_mock.check_ip("::1")
        assert result.blocked is False
        assert result.reason == "private_ip"


# ============ Tests: Disabled ============


class TestDisabled:
    """Test behavior when geo-blocking is disabled."""

    def test_disabled_allows_all(self, disabled_config: GeoBlockConfig):
        """When disabled, all IPs pass through."""
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = disabled_config
        checker._reader = None
        checker._allowed_networks = []

        result = checker.check_ip("93.184.216.34")  # Would normally be blocked (RU)
        assert result.blocked is False
        assert result.reason == "geo_blocking_disabled"


# ============ Tests: Configuration ============


class TestConfiguration:
    """Test configuration loading from environment variables."""

    def test_from_env_defaults(self):
        """Default env config blocks RU, CN, BY."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove any existing overrides
            for key in ["GEO_BLOCK_ENABLED", "GEO_BLOCK_COUNTRIES", "GEO_BLOCK_ALLOWLIST"]:
                os.environ.pop(key, None)
            config = GeoBlockConfig.from_env()
            assert config.enabled is True
            assert "RU" in config.blocked_countries
            assert "CN" in config.blocked_countries
            assert "BY" in config.blocked_countries
            assert config.fail_closed is True

    def test_from_env_custom_countries(self):
        """Custom country list from environment."""
        with patch.dict(os.environ, {"GEO_BLOCK_COUNTRIES": "RU,KP,IR"}):
            config = GeoBlockConfig.from_env()
            assert config.blocked_countries == ["RU", "KP", "IR"]

    def test_from_env_with_allowlist(self):
        """Allowlist from environment."""
        with patch.dict(os.environ, {"GEO_BLOCK_ALLOWLIST": "1.2.3.4,5.6.7.8"}):
            config = GeoBlockConfig.from_env()
            assert "1.2.3.4" in config.allowed_ips
            assert "5.6.7.8" in config.allowed_ips

    def test_from_env_disabled(self):
        """Disabled via environment."""
        with patch.dict(os.environ, {"GEO_BLOCK_ENABLED": "false"}):
            config = GeoBlockConfig.from_env()
            assert config.enabled is False

    def test_from_env_fail_open(self):
        """Fail-open via environment."""
        with patch.dict(os.environ, {"GEO_BLOCK_FAIL_CLOSED": "false"}):
            config = GeoBlockConfig.from_env()
            assert config.fail_closed is False


# ============ Tests: GeoBlockResult ============


class TestGeoBlockResult:
    """Test the result dataclass."""

    def test_blocked_result(self):
        """Blocked result has correct fields."""
        result = GeoBlockResult(blocked=True, reason="blocked_country", ip="1.2.3.4", country_code="RU")
        assert result.blocked is True
        assert result.reason == "blocked_country"
        assert result.ip == "1.2.3.4"
        assert result.country_code == "RU"

    def test_allowed_result(self):
        """Allowed result has correct fields."""
        result = GeoBlockResult(blocked=False, reason="allowed_country", ip="1.2.3.4", country_code="US")
        assert result.blocked is False
        assert result.country_code == "US"

    def test_default_optional_fields(self):
        """Optional fields default to None."""
        result = GeoBlockResult(blocked=False, reason="test")
        assert result.ip is None
        assert result.country_code is None


# ============ Tests: Edge Cases ============


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_ip_string(self, checker_with_mock: GeoBlockChecker):
        """Empty IP string doesn't crash."""
        result = checker_with_mock.check_ip("")
        # Empty string is not a valid IP — should be handled gracefully
        # _is_private_or_loopback returns False for invalid IPs
        # Then GeoIP lookup will fail, triggering fail-closed
        assert isinstance(result, GeoBlockResult)

    def test_invalid_ip_format(self, checker_with_mock: GeoBlockChecker):
        """Invalid IP format is handled gracefully."""
        result = checker_with_mock.check_ip("not-an-ip")
        assert isinstance(result, GeoBlockResult)

    def test_geoip_reader_error(self, default_config: GeoBlockConfig):
        """GeoIP reader throwing unexpected error triggers fail-closed."""

        class ErrorReader:
            def country(self, ip):
                raise RuntimeError("Database corrupted")

            def close(self):
                pass

        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = default_config
        checker._reader = ErrorReader()
        checker._allowed_networks = []

        with patch("src.security.geo_block.GEOIP2_AVAILABLE", True):
            result = checker.check_ip("1.2.3.4")
            assert result.blocked is True
            assert result.reason == "geoip_error_fail_closed"

    def test_close_reader(self, checker_with_mock: GeoBlockChecker):
        """Closing the checker closes the reader."""
        checker_with_mock.close()
        assert checker_with_mock._reader is None

    def test_close_without_reader(self, default_config: GeoBlockConfig):
        """Closing when reader is None doesn't crash."""
        checker = GeoBlockChecker.__new__(GeoBlockChecker)
        checker.config = default_config
        checker._reader = None
        checker._allowed_networks = []
        checker.close()  # Should not raise


# ============ Tests: FastAPI Middleware ============


class TestGeoBlockMiddleware:
    """Test the FastAPI middleware integration."""

    @pytest.mark.asyncio
    async def test_middleware_blocks_request(self):
        """Middleware returns 403 for blocked IPs."""
        mock_request = MagicMock()
        mock_request.client.host = "93.184.216.34"
        mock_request.url.path = "/optimize"
        mock_request.method = "POST"

        blocked_result = GeoBlockResult(
            blocked=True, reason="blocked_country", ip="93.184.216.34", country_code="RU"
        )

        middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", return_value=blocked_result):
            response = await middleware.dispatch(mock_request, call_next=MagicMock())
            assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_middleware_allows_request(self):
        """Middleware passes through for allowed IPs."""
        mock_request = MagicMock()
        mock_request.client.host = "1.2.3.4"

        allowed_result = GeoBlockResult(
            blocked=False, reason="allowed_country", ip="1.2.3.4", country_code="US"
        )

        async def mock_call_next(request):
            response = MagicMock()
            response.status_code = 200
            return response

        middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", return_value=allowed_result):
            response = await middleware.dispatch(mock_request, call_next=mock_call_next)
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_handles_no_client(self):
        """Middleware handles missing client info gracefully."""
        mock_request = MagicMock()
        mock_request.client = None

        async def mock_call_next(request):
            response = MagicMock()
            response.status_code = 200
            return response

        middleware = GeoBlockMiddleware(app=MagicMock())
        response = await middleware.dispatch(mock_request, call_next=mock_call_next)
        assert response.status_code == 200
