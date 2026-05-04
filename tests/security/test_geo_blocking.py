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

import io
import os
import urllib.error
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.security.geo_block import (
    GeoBlockChecker,
    GeoBlockConfig,
    GeoBlockMiddleware,
    GeoBlockResult,
    _download_geoip_db,
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
    def test_private_and_loopback_always_allowed(self, checker_with_mock: GeoBlockChecker, ip: str):
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
        result = GeoBlockResult(
            blocked=True, reason="blocked_country", ip="1.2.3.4", country_code="RU"
        )
        assert result.blocked is True
        assert result.reason == "blocked_country"
        assert result.ip == "1.2.3.4"
        assert result.country_code == "RU"

    def test_allowed_result(self):
        """Allowed result has correct fields."""
        result = GeoBlockResult(
            blocked=False, reason="allowed_country", ip="1.2.3.4", country_code="US"
        )
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


# ============ Tests: Forwarded-IP Helpers ============


class TestForwardedIpHelpers:
    """Test the pure helper functions used to extract real client IPs."""

    def test_normalize_plain_ipv4(self):
        from src.security.forwarded_ip import normalize_forwarded_ip

        assert normalize_forwarded_ip("203.0.113.5") == "203.0.113.5"

    def test_normalize_strips_quotes(self):
        from src.security.forwarded_ip import normalize_forwarded_ip

        assert normalize_forwarded_ip('"203.0.113.5"') == "203.0.113.5"

    def test_normalize_strips_ipv4_port(self):
        from src.security.forwarded_ip import normalize_forwarded_ip

        assert normalize_forwarded_ip("203.0.113.5:54321") == "203.0.113.5"

    def test_normalize_handles_bracketed_ipv6(self):
        from src.security.forwarded_ip import normalize_forwarded_ip

        assert normalize_forwarded_ip("[2001:db8::1]:443") == "2001:db8::1"

    def test_normalize_returns_none_for_invalid(self):
        from src.security.forwarded_ip import normalize_forwarded_ip

        assert normalize_forwarded_ip("not-an-ip") is None
        assert normalize_forwarded_ip("") is None
        assert normalize_forwarded_ip("   ") is None

    def test_extract_prefers_forwarded_header(self):
        """RFC 7239 Forwarded header wins over X-Forwarded-For."""
        from src.security.geo_block import _extract_forwarded_ip

        headers = {
            "Forwarded": 'for="203.0.113.5";proto=https',
            "X-Forwarded-For": "198.51.100.7",
        }
        assert _extract_forwarded_ip(headers) == "203.0.113.5"

    def test_extract_xff_picks_leftmost_valid(self):
        """X-Forwarded-For: client, proxy1, proxy2 — leftmost is the real client."""
        from src.security.geo_block import _extract_forwarded_ip

        headers = {"X-Forwarded-For": "203.0.113.5, 100.64.0.2, 100.64.0.3"}
        assert _extract_forwarded_ip(headers) == "203.0.113.5"

    def test_extract_xff_skips_invalid_leading_entries(self):
        from src.security.geo_block import _extract_forwarded_ip

        headers = {"X-Forwarded-For": "garbage, 203.0.113.5"}
        assert _extract_forwarded_ip(headers) == "203.0.113.5"

    def test_extract_falls_back_to_xreal_ip(self):
        from src.security.geo_block import _extract_forwarded_ip

        headers = {"X-Real-IP": "203.0.113.5"}
        assert _extract_forwarded_ip(headers) == "203.0.113.5"

    def test_extract_returns_none_when_no_headers_present(self):
        from src.security.geo_block import _extract_forwarded_ip

        assert _extract_forwarded_ip({}) is None

    def test_extract_returns_none_when_headers_lack_get(self):
        from src.security.geo_block import _extract_forwarded_ip

        assert _extract_forwarded_ip(object()) is None

    def test_implicitly_trusted_includes_cgnat(self):
        """Railway / Render / Fly.io use 100.64.0.0/10 between edge and container."""
        from src.security.geo_block import _is_implicitly_trusted_proxy

        assert _is_implicitly_trusted_proxy("100.64.0.2") is True
        assert _is_implicitly_trusted_proxy("100.127.255.254") is True

    def test_implicitly_trusted_includes_private(self):
        from src.security.geo_block import _is_implicitly_trusted_proxy

        assert _is_implicitly_trusted_proxy("10.0.0.1") is True
        assert _is_implicitly_trusted_proxy("172.16.0.1") is True
        assert _is_implicitly_trusted_proxy("192.168.1.1") is True
        assert _is_implicitly_trusted_proxy("127.0.0.1") is True
        assert _is_implicitly_trusted_proxy("::1") is True
        assert _is_implicitly_trusted_proxy("fe80::1") is True

    def test_implicitly_trusted_rejects_public(self):
        """Real public-internet IPs must NOT be implicitly trusted as proxies.

        Note: RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24,
        203.0.113.0/24) are classified as is_private=True in Python's
        ipaddress module, so they cannot be used here as "public" examples.
        """
        from src.security.geo_block import _is_implicitly_trusted_proxy

        assert _is_implicitly_trusted_proxy("8.8.8.8") is False
        assert _is_implicitly_trusted_proxy("93.184.216.34") is False  # example.com
        assert _is_implicitly_trusted_proxy("18.196.90.141") is False  # AWS Frankfurt
        assert _is_implicitly_trusted_proxy("not-an-ip") is False

    def test_parse_ip_networks_skips_invalid(self):
        from src.security.geo_block import _parse_ip_networks

        nets = _parse_ip_networks("10.0.0.0/8, junk, 192.168.0.0/16, ")
        assert len(nets) == 2
        assert str(nets[0]) == "10.0.0.0/8"
        assert str(nets[1]) == "192.168.0.0/16"


# ============ Tests: Proxy Header Resolution in Middleware ============


def _make_request(peer_ip: Optional[str], headers: Optional[dict] = None) -> MagicMock:
    """Build a mock Starlette Request with a given peer IP and header dict."""
    request = MagicMock()
    if peer_ip is None:
        request.client = None
    else:
        request.client = MagicMock()
        request.client.host = peer_ip

    class _Headers:
        """Minimal stand-in supporting case-insensitive ``.get()``."""

        def __init__(self, raw: dict):
            self._raw = {k.lower(): v for k, v in raw.items()}

        def get(self, key, default=""):
            return self._raw.get(key.lower(), default)

    request.headers = _Headers(headers or {})
    request.url.path = "/admin/depots"
    request.method = "POST"
    return request


class TestProxyHeaderResolution:
    """End-to-end: middleware uses the real client IP behind a trusted proxy."""

    @pytest.mark.asyncio
    async def test_railway_cgnat_peer_uses_xff_real_ip(self):
        """Reproduces the Railway production bug: peer is 100.64.x.x, XFF holds the real client.

        The middleware MUST geo-check the real client IP from X-Forwarded-For,
        not the CGNAT peer IP (which would always miss the GeoIP DB and fail
        closed, blocking all legitimate Railway traffic).
        """
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(
            peer_ip="100.64.0.2",
            headers={"X-Forwarded-For": "18.196.90.141"},
        )

        captured: dict = {}

        def fake_check(ip: str) -> GeoBlockResult:
            captured["ip"] = ip
            return GeoBlockResult(blocked=False, reason="allowed_country", ip=ip, country_code="DE")

        async def call_next(_):
            response = MagicMock()
            response.status_code = 200
            return response

        with patch.dict(os.environ, {}, clear=False):
            for key in ("GEO_BLOCK_TRUST_PROXY_HEADERS", "GEO_BLOCK_TRUSTED_PROXY_RANGES"):
                os.environ.pop(key, None)
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", side_effect=fake_check):
            response = await middleware.dispatch(request, call_next)

        assert captured["ip"] == "18.196.90.141"
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_xff_blocked_country_still_blocks_through_proxy(self):
        """A blocked country in XFF must still produce a 403 even via a trusted proxy."""
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(
            peer_ip="100.64.0.2",
            headers={"X-Forwarded-For": "93.184.216.34"},
        )

        blocked_result = GeoBlockResult(
            blocked=True, reason="blocked_country", ip="93.184.216.34", country_code="RU"
        )

        with patch.dict(os.environ, {}, clear=False):
            for key in ("GEO_BLOCK_TRUST_PROXY_HEADERS", "GEO_BLOCK_TRUSTED_PROXY_RANGES"):
                os.environ.pop(key, None)
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", return_value=blocked_result):
            response = await middleware.dispatch(request, call_next=MagicMock())

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_untrusted_public_peer_does_not_honour_xff(self):
        """An attacker spoofing X-Forwarded-For from the public internet must be ignored."""
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(
            peer_ip="93.184.216.34",  # public, untrusted
            headers={"X-Forwarded-For": "8.8.8.8"},  # spoofed allowed-country IP
        )

        captured: dict = {}

        def fake_check(ip: str) -> GeoBlockResult:
            captured["ip"] = ip
            return GeoBlockResult(blocked=True, reason="blocked_country", ip=ip, country_code="RU")

        with patch.dict(os.environ, {}, clear=False):
            for key in ("GEO_BLOCK_TRUST_PROXY_HEADERS", "GEO_BLOCK_TRUSTED_PROXY_RANGES"):
                os.environ.pop(key, None)
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", side_effect=fake_check):
            response = await middleware.dispatch(request, call_next=MagicMock())

        # We must check the peer, not the spoofed XFF
        assert captured["ip"] == "93.184.216.34"
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_explicit_trusted_proxy_cidr_honours_xff(self):
        """An on-prem load balancer with a public IP can be opted in via env var.

        Uses a truly public peer IP (18.196.90.0/24 — AWS Frankfurt) so the
        implicit-trust path cannot fire and the explicit CIDR opt-in is what
        actually trusts the forwarded header.
        """
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(
            peer_ip="18.196.90.141",
            headers={"X-Forwarded-For": "8.8.4.4"},
        )

        captured: dict = {}

        def fake_check(ip: str) -> GeoBlockResult:
            captured["ip"] = ip
            return GeoBlockResult(blocked=False, reason="allowed_country", ip=ip, country_code="US")

        async def call_next(_):
            response = MagicMock()
            response.status_code = 200
            return response

        with patch.dict(
            os.environ,
            {"GEO_BLOCK_TRUSTED_PROXY_RANGES": "18.196.90.0/24"},
        ):
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", side_effect=fake_check):
            response = await middleware.dispatch(request, call_next)

        assert captured["ip"] == "8.8.4.4"
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_trust_proxy_headers_disabled_uses_peer_ip(self):
        """When the operator disables proxy trust, even private peers fall back to the peer IP."""
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(
            peer_ip="100.64.0.2",
            headers={"X-Forwarded-For": "18.196.90.141"},
        )

        captured: dict = {}

        def fake_check(ip: str) -> GeoBlockResult:
            captured["ip"] = ip
            return GeoBlockResult(blocked=True, reason="country_unknown_fail_closed", ip=ip)

        with patch.dict(
            os.environ,
            {"GEO_BLOCK_TRUST_PROXY_HEADERS": "false", "GEO_BLOCK_TRUSTED_PROXY_RANGES": ""},
        ):
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", side_effect=fake_check):
            response = await middleware.dispatch(request, call_next=MagicMock())

        assert captured["ip"] == "100.64.0.2"
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_trusted_peer_without_xff_falls_back_to_peer_ip(self):
        """If a trusted proxy forgets X-Forwarded-For, fall back to the peer IP (then geo-check it)."""
        from src.security.geo_block import GeoBlockMiddleware

        request = _make_request(peer_ip="100.64.0.2", headers={})

        captured: dict = {}

        def fake_check(ip: str) -> GeoBlockResult:
            captured["ip"] = ip
            return GeoBlockResult(blocked=True, reason="country_unknown_fail_closed", ip=ip)

        with patch.dict(os.environ, {}, clear=False):
            for key in ("GEO_BLOCK_TRUST_PROXY_HEADERS", "GEO_BLOCK_TRUSTED_PROXY_RANGES"):
                os.environ.pop(key, None)
            middleware = GeoBlockMiddleware(app=MagicMock())

        with patch("src.security.geo_block.check_ip_blocked", side_effect=fake_check):
            await middleware.dispatch(request, call_next=MagicMock())

        assert captured["ip"] == "100.64.0.2"


# ============ Tests: Runtime DB Download ============


def _build_mmdb_tarball(
    mmdb_bytes: bytes, *, mmdb_name: str = "GeoLite2-Country_20260101/GeoLite2-Country.mmdb"
) -> bytes:
    """Build an in-memory tar.gz archive containing a fake mmdb file."""
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name=mmdb_name)
        info.size = len(mmdb_bytes)
        tar.addfile(info, io.BytesIO(mmdb_bytes))
    return gzip.compress(raw.getvalue())


class _FakeUrlopenResponse:
    """Context-managed stand-in for urllib.request.urlopen()'s return value."""

    def __init__(self, body: bytes, *, status: int = 200, reason: str = "OK"):
        self._body = io.BytesIO(body) if isinstance(body, bytes) else body
        self.status = status
        self.reason = reason
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._body.close()
        return False

    def read(self, *args, **kwargs):
        return self._body.read(*args, **kwargs)


class TestRuntimeDownload:
    """Tests for the runtime fallback download path in geo_block.py.

    MaxMind's 2024 policy change requires HTTP Basic Auth (account_id +
    license_key) against the new `/geoip/databases/.../download` endpoint;
    these tests pin both the credential plumbing and the new URL.
    """

    def test_returns_false_when_license_key_empty(self, tmp_path):
        """No license key is a no-op — returns False, no network call."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        with patch("src.security.geo_block.urllib.request.urlopen") as mock_open:
            assert _download_geoip_db(db_path, license_key="", account_id="acct") is False
            mock_open.assert_not_called()
        assert not os.path.exists(db_path)

    def test_returns_false_when_account_id_empty(self, tmp_path):
        """No account_id is a no-op — MaxMind requires both since 2024."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        with patch("src.security.geo_block.urllib.request.urlopen") as mock_open:
            assert _download_geoip_db(db_path, license_key="key", account_id="") is False
            mock_open.assert_not_called()
        assert not os.path.exists(db_path)

    def test_returns_true_when_db_already_exists(self, tmp_path):
        """Idempotent: existing non-empty DB short-circuits download."""
        db_path = tmp_path / "GeoLite2-Country.mmdb"
        db_path.write_bytes(b"existing-db-bytes")
        with patch("src.security.geo_block.urllib.request.urlopen") as mock_open:
            assert _download_geoip_db(str(db_path), license_key="key", account_id="acct") is True
            mock_open.assert_not_called()

    def test_successful_download_writes_mmdb(self, tmp_path):
        """Happy path — tarball is fetched, mmdb extracted to db_path."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"FAKE_MMDB_BYTES")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            assert _download_geoip_db(db_path, license_key="real-key", account_id="acct") is True

        assert os.path.exists(db_path)
        with open(db_path, "rb") as f:
            assert f.read() == b"FAKE_MMDB_BYTES"

    def test_uses_new_endpoint_with_basic_auth_header(self, tmp_path):
        """Request hits the 2024 endpoint and carries the Basic Auth header."""
        import base64

        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"OK")
        captured: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            captured["full_url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            _download_geoip_db(db_path, license_key="my-key", account_id="123456")

        assert (
            captured["full_url"]
            == "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"
        )
        # The legacy `?license_key=` query-param URL must NOT be used.
        assert "license_key=" not in captured["full_url"]
        assert "/app/geoip_download" not in captured["full_url"]
        expected_token = base64.b64encode(b"123456:my-key").decode("ascii")
        assert captured["authorization"] == f"Basic {expected_token}"

    def test_authorization_is_unredirected_so_r2_redirect_does_not_see_it(self, tmp_path):
        """Authorization must NOT propagate through urllib's redirect handler.

        MaxMind's `/geoip/databases/.../download` endpoint 302-redirects to a
        Cloudflare R2 presigned URL whose own auth lives in the query string
        (X-Amz-Signature, etc.). urllib propagates `req.headers` verbatim on
        redirect, so a Basic-Auth header in `req.headers` would leak into the
        R2 request and trigger a 400. The fix is `add_unredirected_header()`,
        which keeps Authorization out of the redirected request.
        """
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"OK")
        captured: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            # `headers` holds the redirected-through headers; `unredirected_hdrs`
            # holds the per-request-only headers that urllib drops on 30x.
            captured["redirected_headers"] = dict(request.headers)
            captured["unredirected_headers"] = dict(request.unredirected_hdrs)
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            _download_geoip_db(db_path, license_key="my-key", account_id="123456")

        # Authorization MUST be in the unredirected bucket, never in the
        # redirected bucket — case-insensitive because urllib title-cases.
        redirected_lower = {k.lower() for k in captured["redirected_headers"]}
        unredirected_lower = {k.lower() for k in captured["unredirected_headers"]}
        assert "authorization" not in redirected_lower
        assert "authorization" in unredirected_lower

    def test_sets_user_agent_to_avoid_cloudflare_waf(self, tmp_path):
        """A non-default User-Agent is sent so Cloudflare's WAF in front of
        MaxMind doesn't 403 us as `Python-urllib/3.x`."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"OK")
        captured: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            captured["user_agent"] = request.get_header("User-agent")
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            _download_geoip_db(db_path, license_key="my-key", account_id="123456")

        assert captured["user_agent"] is not None
        assert "Python-urllib" not in captured["user_agent"]
        # A recognisable identifier, not just the empty string.
        assert "favonius" in captured["user_agent"].lower()

    def test_logs_http_status_code_on_failure(self, tmp_path, caplog):
        """An HTTPError surfaces the status code in the warning message,
        so operators can tell 401 (bad creds) from 403 (WAF) from 404."""
        import logging

        db_path = str(tmp_path / "GeoLite2-Country.mmdb")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            raise urllib.error.HTTPError(
                "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download",
                401,
                "Unauthorized",
                {},
                None,
            )

        with caplog.at_level(logging.WARNING, logger="src.security.geo_block"):
            with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
                result = _download_geoip_db(
                    db_path,
                    license_key="bad-key",
                    account_id="acct",
                    retries=0,
                    backoff_s=0.0,
                    sleep=lambda _: None,
                )

        assert result is False
        # The exact status code is in the log so an operator can act on it.
        assert any("401" in rec.message for rec in caplog.records)
        assert any("Unauthorized" in rec.message for rec in caplog.records)

    def test_credentials_with_special_chars_basic_auth_encodes_correctly(self, tmp_path):
        """Account ID and license key with URL-significant chars survive Basic Auth.

        Basic Auth uses base64 of the raw `account:key`, so URL-significant
        characters like `&`, `=`, and `:` should NOT need percent-encoding —
        they round-trip cleanly via base64.
        """
        import base64

        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"OK")
        captured: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            captured["authorization"] = request.get_header("Authorization")
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            _download_geoip_db(db_path, license_key="abc&def=xyz", account_id="acct:1")

        # Verify the auth token decodes back to the raw `account:key` pair.
        assert captured["authorization"].startswith("Basic ")
        token = captured["authorization"].removeprefix("Basic ")
        assert base64.b64decode(token).decode("utf-8") == "acct:1:abc&def=xyz"

    def test_retries_on_transient_http_error_then_succeeds(self, tmp_path):
        """First attempt raises, second succeeds — DB written, sleep called once."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"OK")
        attempts = {"n": 0}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.URLError("connection refused")
            return _FakeUrlopenResponse(archive)

        sleep_calls: list[float] = []
        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            assert (
                _download_geoip_db(
                    db_path,
                    license_key="key",
                    account_id="acct",
                    retries=2,
                    backoff_s=0.01,
                    sleep=sleep_calls.append,
                )
                is True
            )

        assert attempts["n"] == 2
        assert sleep_calls == [0.01]
        assert os.path.exists(db_path)

    def test_returns_false_when_response_not_gzip(self, tmp_path):
        """Rate-limit page (non-gzip body) → all attempts fail, no DB written."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            return _FakeUrlopenResponse(b"<html>rate limited</html>")

        sleep_calls: list[float] = []
        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _download_geoip_db(
                db_path,
                license_key="key",
                account_id="acct",
                retries=2,
                backoff_s=0.01,
                sleep=sleep_calls.append,
            )

        assert result is False
        assert not os.path.exists(db_path)
        # 2 retries means 3 total attempts → 2 sleeps between them
        assert len(sleep_calls) == 2

    def test_returns_false_when_archive_missing_mmdb_member(self, tmp_path):
        """Tarball without GeoLite2-Country.mmdb → all attempts fail."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        archive = _build_mmdb_tarball(b"x", mmdb_name="something_else.txt")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            return _FakeUrlopenResponse(archive)

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            assert (
                _download_geoip_db(
                    db_path,
                    license_key="key",
                    account_id="acct",
                    retries=1,
                    backoff_s=0.0,
                    sleep=lambda _: None,
                )
                is False
            )
        assert not os.path.exists(db_path)

    def test_all_retries_exhausted_returns_false(self, tmp_path):
        """Persistent network failure — no DB, returns False, retries exhausted."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            raise urllib.error.URLError("network unreachable")

        sleep_calls: list[float] = []
        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _download_geoip_db(
                db_path,
                license_key="key",
                account_id="acct",
                retries=3,
                backoff_s=0.5,
                sleep=sleep_calls.append,
            )

        assert result is False
        assert not os.path.exists(db_path)
        # Linear backoff: 0.5, 1.0, 1.5 between 4 total attempts
        assert sleep_calls == [0.5, 1.0, 1.5]

    def test_partial_download_does_not_replace_existing_db(self, tmp_path):
        """Atomic replace: a failed extraction must not clobber an existing DB."""
        db_path = tmp_path / "GeoLite2-Country.mmdb"
        # Simulate a *different* DB at the path (size > 0 → short-circuits download).
        db_path.write_bytes(b"original-mmdb")

        def fake_urlopen(request, timeout):  # noqa: ARG001
            raise urllib.error.URLError("would corrupt existing DB if called")

        with patch("src.security.geo_block.urllib.request.urlopen", side_effect=fake_urlopen):
            assert _download_geoip_db(str(db_path), license_key="key", account_id="acct") is True

        assert db_path.read_bytes() == b"original-mmdb"


class TestCheckerWiresRuntimeDownload:
    """Tests that GeoBlockChecker.__init__ invokes the runtime download fallback."""

    def test_checker_attempts_runtime_download_when_db_missing(self, tmp_path, monkeypatch):
        """If DB missing and both creds set, download is attempted at init."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        monkeypatch.setenv("MAXMIND_LICENSE_KEY", "test-key")
        monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "123456")
        config = GeoBlockConfig(
            blocked_countries=["RU"],
            geoip_db_path=db_path,
            enabled=True,
            fail_closed=True,
        )

        called: dict = {}

        def fake_download(path, license_key, **kwargs):
            called["path"] = path
            called["license_key"] = license_key
            called["account_id"] = kwargs.get("account_id")
            return False  # fail — DB still missing, fall through to existing error path

        with patch("src.security.geo_block._download_geoip_db", side_effect=fake_download):
            checker = GeoBlockChecker(config)

        assert called == {"path": db_path, "license_key": "test-key", "account_id": "123456"}
        # Download failed → reader stays None → fail-closed at request time
        assert checker._reader is None

    def test_checker_skips_runtime_download_when_no_license_key(self, tmp_path, monkeypatch):
        """No env license key → no download attempted, even if DB missing."""
        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        monkeypatch.delenv("MAXMIND_LICENSE_KEY", raising=False)
        monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "123456")
        config = GeoBlockConfig(
            blocked_countries=["RU"],
            geoip_db_path=db_path,
            enabled=True,
            fail_closed=True,
        )

        with patch("src.security.geo_block._download_geoip_db") as mock_dl:
            GeoBlockChecker(config)
            mock_dl.assert_not_called()

    def test_checker_skips_runtime_download_when_no_account_id(self, tmp_path, monkeypatch, caplog):
        """License key set but no account ID → download skipped + WARNING logged."""
        import logging

        db_path = str(tmp_path / "GeoLite2-Country.mmdb")
        monkeypatch.setenv("MAXMIND_LICENSE_KEY", "test-key")
        monkeypatch.delenv("MAXMIND_ACCOUNT_ID", raising=False)
        config = GeoBlockConfig(
            blocked_countries=["RU"],
            geoip_db_path=db_path,
            enabled=True,
            fail_closed=True,
        )

        with caplog.at_level(logging.WARNING, logger="src.security.geo_block"):
            with patch("src.security.geo_block._download_geoip_db") as mock_dl:
                GeoBlockChecker(config)
                mock_dl.assert_not_called()

        assert any("MAXMIND_ACCOUNT_ID" in rec.message for rec in caplog.records)

    def test_checker_skips_runtime_download_when_db_present(self, tmp_path, monkeypatch):
        """Existing DB at path → no download attempted."""
        db_path = tmp_path / "GeoLite2-Country.mmdb"
        # We need the file to merely exist for the os.path.exists() check before
        # geoip2 tries to open it. geoip2 will fail to parse but that's fine —
        # the assertion is on _download_geoip_db not being called.
        db_path.write_bytes(b"")
        monkeypatch.setenv("MAXMIND_LICENSE_KEY", "test-key")
        monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "123456")
        config = GeoBlockConfig(
            blocked_countries=["RU"],
            geoip_db_path=str(db_path),
            enabled=True,
            fail_closed=True,
        )

        with patch("src.security.geo_block._download_geoip_db") as mock_dl:
            GeoBlockChecker(config)
            mock_dl.assert_not_called()
