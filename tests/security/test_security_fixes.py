"""Regression tests for security audit fixes (2026-04-10).

Each test validates that a specific vulnerability (C1-C5, H1-H12, M1-M10, L1-L3)
is properly mitigated. Tests are independent of a running database.

Reference: docs/SECURITY_AUDIT.md
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# C2: IDOR — verify_depot_access
# ---------------------------------------------------------------------------


class TestVerifyDepotAccess:
    """Test the depot ownership check (C2 fix)."""

    @pytest.fixture
    def _import_verify(self):
        """Import verify_depot_access, handling import errors gracefully."""
        from src.security.auth import verify_depot_access

        return verify_depot_access

    @staticmethod
    def _mock_pool(fetchval_result: bool):
        pool = MagicMock()
        conn = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=ctx)
        conn.fetchval = AsyncMock(return_value=fetchval_result)
        return pool

    @pytest.mark.asyncio
    async def test_access_allowed_when_depot_in_organization(self, _import_verify):
        """Customer role allowed when static DB confirms depot organization."""
        verify = _import_verify
        user = {
            "sub": "user-123",
            "app_metadata": {
                "favonius_role": "customer_operator",
                "organization_id": "00000000-0000-4000-8000-0000000000aa",
            },
        }
        pool = self._mock_pool(True)
        await verify("550e8400-e29b-41d4-a716-446655440001", user, pool=pool)

    @pytest.mark.asyncio
    async def test_access_denied_wrong_organization(self, _import_verify):
        """Depot not in caller organization → 403."""
        from fastapi import HTTPException

        verify = _import_verify
        user = {
            "sub": "user-123",
            "app_metadata": {
                "favonius_role": "customer_operator",
                "organization_id": "00000000-0000-4000-8000-0000000000bb",
            },
        }
        pool = self._mock_pool(False)
        with pytest.raises(HTTPException) as exc_info:
            await verify("550e8400-e29b-41d4-a716-446655440001", user, pool=pool)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_favonius_admin_bypasses_org_check(self, _import_verify):
        """Platform admin accesses any depot without DB lookup."""
        verify = _import_verify
        user = {"sub": "admin-1", "app_metadata": {"favonius_role": "favonius_admin"}}
        await verify("any-depot-id", user, pool=None)

    @pytest.mark.asyncio
    async def test_user_metadata_depot_ids_ignored(self, _import_verify):
        """user_metadata must not grant depot access."""
        from fastapi import HTTPException

        verify = _import_verify
        user = {
            "sub": "user-456",
            "user_metadata": {"depot_ids": ["depot-aaa"], "favonius_role": "admin"},
            "app_metadata": {"favonius_role": "customer_operator", "organization_id": "00000000-0000-4000-8000-0000000000cc"},
        }
        pool = self._mock_pool(False)
        with pytest.raises(HTTPException) as exc_info:
            await verify("depot-aaa", user, pool=pool)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_org_id_customer_denied(self, _import_verify):
        """Customer role without organization_id in app_metadata → 403."""
        from fastapi import HTTPException

        verify = _import_verify
        user = {"sub": "user-456", "app_metadata": {"favonius_role": "customer_operator"}}
        pool = self._mock_pool(True)
        with pytest.raises(HTTPException) as exc_info:
            await verify("depot-aaa", user, pool=pool)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# H1: JWT algorithm allowlist
# ---------------------------------------------------------------------------


class TestJWTAlgorithmAllowlist:
    """Test JWT algorithm enforcement (H1 fix)."""

    def test_allowed_algorithms_hardcoded_tuple(self):
        """Allowlist is a fixed tuple: legacy HS256 plus Supabase JWT Signing Key algs."""
        from src.security.auth import (
            _ALLOWED_ALGORITHMS,
            _ASYMMETRIC_ALGORITHMS,
            _HS_ALGORITHMS,
        )

        assert _ALLOWED_ALGORITHMS == _HS_ALGORITHMS + _ASYMMETRIC_ALGORITHMS
        assert _HS_ALGORITHMS == ("HS256",)
        assert _ASYMMETRIC_ALGORITHMS == ("ES256", "RS256", "EdDSA")

    def test_disallowed_algorithms_not_in_allowlist(self):
        """Unsafe or env-driven algorithms (e.g. none) must never verify."""
        from src.security.auth import _ALLOWED_ALGORITHMS

        for bad in ("none", "HS384", "HS512"):
            assert bad not in _ALLOWED_ALGORITHMS

    def test_error_message_redacted(self):
        """verify_token should return 'Invalid token' without leaking details."""
        # We test the error message format by checking the auth module
        # The actual JWT decode test requires a real token, so we verify the code path
        import inspect

        from src.security.auth import verify_token

        source = inspect.getsource(verify_token)
        # Should NOT contain f-string with last_error in the detail
        assert 'detail=f"Invalid token: {last_error}"' not in source
        assert 'detail="Invalid token"' in source


# ---------------------------------------------------------------------------
# C4: Path traversal — firmware and log file writes
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """Test path traversal mitigation (C4 fix)."""

    def test_firmware_filename_uses_uuid_not_station_id(self):
        """Firmware filename should use server-generated UUID, not station_id."""
        import inspect

        from src.websocket_handler.diagnostics_firmware import FirmwareManager

        source = inspect.getsource(FirmwareManager._download_firmware)
        # Should NOT interpolate station_id into filename
        assert 'f"{firmware_request.station_id}_' not in source
        # Should use uuid-based safe_id
        assert "safe_id" in source or "uuid" in source.lower()

    def test_log_filename_uses_uuid_not_station_id(self):
        """Log filename should use server-generated UUID, not station_id."""
        import inspect

        from src.websocket_handler.diagnostics_firmware import DiagnosticsManager

        source = inspect.getsource(DiagnosticsManager._collect_logs)
        # Should NOT interpolate station_id into filename
        assert 'f"{station_id}_' not in source
        # Should use uuid-based safe_id
        assert "safe_id" in source or "uuid" in source.lower()

    def test_path_confinement_check_present(self):
        """Both firmware and log writes should have commonpath confinement."""
        import inspect

        from src.websocket_handler.diagnostics_firmware import (
            DiagnosticsManager,
            FirmwareManager,
        )

        fw_source = inspect.getsource(FirmwareManager._download_firmware)
        log_source = inspect.getsource(DiagnosticsManager._collect_logs)
        assert "commonpath" in fw_source
        assert "commonpath" in log_source


# ---------------------------------------------------------------------------
# C5: SSRF — firmware URL validation
# ---------------------------------------------------------------------------


class TestSSRFFirmwareURL:
    """Test SSRF mitigation on firmware download URL (C5 fix)."""

    @pytest.fixture
    def firmware_manager(self):
        """Create a FirmwareManager instance for testing."""
        from src.websocket_handler.diagnostics_firmware import FirmwareManager

        mock_client = MagicMock()
        mgr = FirmwareManager(mock_client)
        return mgr

    def test_rejects_http_scheme(self, firmware_manager):
        """HTTP URLs should be rejected (HTTPS only)."""
        assert firmware_manager._validate_firmware_url("http://example.com/fw.bin") is False

    def test_rejects_file_scheme(self, firmware_manager):
        """file:// URLs should be rejected."""
        assert firmware_manager._validate_firmware_url("file:///etc/passwd") is False

    def test_rejects_empty_url(self, firmware_manager):
        """Empty/invalid URLs should be rejected."""
        assert firmware_manager._validate_firmware_url("") is False

    @patch("socket.getaddrinfo")
    def test_rejects_loopback_ip(self, mock_dns, firmware_manager):
        """URLs resolving to 127.0.0.1 should be rejected (SSRF to localhost)."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        assert firmware_manager._validate_firmware_url("https://evil.com/fw.bin") is False

    @patch("socket.getaddrinfo")
    def test_rejects_private_ip(self, mock_dns, firmware_manager):
        """URLs resolving to RFC1918 ranges should be rejected."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ]
        assert firmware_manager._validate_firmware_url("https://internal.corp/fw.bin") is False

    @patch("socket.getaddrinfo")
    def test_rejects_link_local_ip(self, mock_dns, firmware_manager):
        """URLs resolving to 169.254.x.x (cloud metadata) should be rejected."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        ]
        assert firmware_manager._validate_firmware_url("https://metadata.google/fw") is False

    @patch("socket.getaddrinfo")
    def test_allows_valid_public_https(self, mock_dns, firmware_manager):
        """Valid HTTPS URL resolving to public IP should pass."""
        mock_dns.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        assert firmware_manager._validate_firmware_url("https://cdn.example.com/fw.bin") is True


# ---------------------------------------------------------------------------
# M5: MeterValues range validation
# ---------------------------------------------------------------------------


class TestMeterValuesValidation:
    """Test OCPP MeterValues range checks (M5 fix)."""

    def test_validation_code_checks_soc_range(self):
        """SoC validation should reject values outside 0-100."""
        import inspect

        from src.websocket_handler.message_handler import MessageHandler

        source = inspect.getsource(MessageHandler)
        # Should contain range check for SoC
        assert "soc_percent < 0" in source or "soc_percent > 100" in source

    def test_validation_code_checks_nan_inf(self):
        """MeterValues should reject NaN and Infinity."""
        import inspect

        from src.websocket_handler.message_handler import MessageHandler

        source = inspect.getsource(MessageHandler)
        assert "isfinite" in source or "math.isfinite" in source


# ---------------------------------------------------------------------------
# H12: Request body size limit
# ---------------------------------------------------------------------------


class TestBodySizeLimit:
    """Test request body size middleware (H12 fix)."""

    def test_middleware_class_exists(self):
        """MaxBodySizeMiddleware should be defined in api main."""
        from src.api.main import MaxBodySizeMiddleware

        assert MaxBodySizeMiddleware is not None

    def test_max_body_size_default_is_1mb(self):
        """Default max body size should be 1 MB."""
        from src.api.main import _MAX_BODY_SIZE

        assert _MAX_BODY_SIZE == 1 * 1024 * 1024


# ---------------------------------------------------------------------------
# M3: CORS — no wildcard with credentials
# ---------------------------------------------------------------------------


class TestCORSConfiguration:
    """Test CORS configuration safety (M3 fix)."""

    def test_wildcard_disables_credentials(self):
        """When CORS origins include *, credentials should be False."""
        import inspect

        from src.api import main

        source = inspect.getsource(main)
        # The fix sets allow_credentials=not _cors_is_wildcard
        assert "allow_credentials=not _cors_is_wildcard" in source


# ---------------------------------------------------------------------------
# H7 / M11: Internal API token hardening
# ---------------------------------------------------------------------------


class TestInternalTokenHardening:
    """Test /internal/ocpp-event auth fixes (H7, M11)."""

    def test_timing_safe_comparison_used(self):
        """Token comparison should use secrets.compare_digest."""
        import inspect

        from src.api import main

        source = inspect.getsource(main.receive_ocpp_event)
        assert "compare_digest" in source

    def test_production_requires_token(self):
        """Production should fail startup without INTERNAL_API_TOKEN."""
        import inspect

        from src.api import main

        source = inspect.getsource(main)
        # Check for the production guard
        assert "INTERNAL_API_TOKEN must be set in production" in source


# ---------------------------------------------------------------------------
# H4: Handoff signing
# ---------------------------------------------------------------------------


class TestHandoffSigning:
    """Test inter-depot handoff HMAC signing (H4 fix)."""

    def test_handoff_includes_nonce_and_timestamp(self):
        """Handoff payload should include nonce and timestamp for replay prevention."""
        import inspect

        from src.api import main

        source = inspect.getsource(main.send_handoff)
        assert '"nonce"' in source
        assert '"timestamp"' in source

    def test_handoff_blocks_http_in_production(self):
        """HTTP handoff URLs should be blocked in production."""
        import inspect

        from src.api import main

        source = inspect.getsource(main.send_handoff)
        assert "http://" in source  # Check for the HTTP detection
        assert "Inter-depot handoff requires HTTPS" in source

    def test_handoff_hmac_signature(self):
        """Handoff should sign payload with HMAC-SHA256 when key is set."""
        import inspect

        from src.api import main

        source = inspect.getsource(main.send_handoff)
        assert "hmac" in source.lower()
        assert "sha256" in source.lower()


# ---------------------------------------------------------------------------
# H5: Rate limiter — user_id based keying
# ---------------------------------------------------------------------------


class TestRateLimiterKeying:
    """Test rate limiter uses authenticated user_id (H5 fix)."""

    def test_rate_limit_key_uses_jwt_sub(self):
        """Rate limiter should extract user_id from JWT sub claim."""
        import inspect

        from src.api.main import RateLimitMiddleware

        source = inspect.getsource(RateLimitMiddleware.dispatch)
        # Should use JWT-based client_id
        assert "user:" in source  # f"user:{payload.get('sub', ...)}"
        # Should NOT trust X-API-Key blindly
        assert "api_key:" not in source


# ---------------------------------------------------------------------------
# M7: Zip bomb protection
# ---------------------------------------------------------------------------


class TestZipBombProtection:
    """Test zip bomb mitigation on CAISO price feed (M7 fix)."""

    def test_compressed_size_check(self):
        """Should reject compressed content exceeding 10 MB."""
        import inspect

        from src.websocket_handler.price_feeder import PriceFeederService

        source = inspect.getsource(PriceFeederService._parse_zip_response)
        assert "_MAX_COMPRESSED_SIZE" in source
        assert "10 * 1024 * 1024" in source

    def test_decompressed_size_check(self):
        """Should reject decompressed content exceeding 100 MB."""
        import inspect

        from src.websocket_handler.price_feeder import PriceFeederService

        source = inspect.getsource(PriceFeederService._parse_zip_response)
        assert "_MAX_DECOMPRESSED_SIZE" in source
        assert "100 * 1024 * 1024" in source


# ---------------------------------------------------------------------------
# M8: Per-IP WebSocket connection limit
# ---------------------------------------------------------------------------


class TestPerIPWebSocketLimit:
    """Test per-IP WebSocket connection limit (M8 fix)."""

    def test_ip_connection_tracking_exists(self):
        """Server should track per-IP connection counts."""
        from src.websocket_handler.server import OCPPWebSocketServer

        assert hasattr(OCPPWebSocketServer, "__init__")
        import inspect

        source = inspect.getsource(OCPPWebSocketServer.__init__)
        assert "_ip_connection_count" in source
        assert "_max_connections_per_ip" in source

    def test_connection_limit_in_handler(self):
        """Connection handler should enforce per-IP limit."""
        import inspect

        from src.websocket_handler.server import OCPPWebSocketServer

        source = inspect.getsource(OCPPWebSocketServer._handle_connection)
        assert "_ip_connection_count" in source
        assert "Too many connections from this IP" in source


# ---------------------------------------------------------------------------
# C3: OCPP auth mandatory in production
# ---------------------------------------------------------------------------


class TestOCPPAuthMandatory:
    """Test OCPP auth is required in production (C3 fix)."""

    def test_production_guard_exists(self):
        """Server should raise if OCPP auth disabled in production."""
        import inspect

        from src.websocket_handler.server import OCPPWebSocketServer

        source = inspect.getsource(OCPPWebSocketServer._initialize_managers)
        assert "OCPP_REQUIRE_AUTH must be" in source
        assert "RuntimeError" in source


# ---------------------------------------------------------------------------
# H6: VDV 463 authentication
# ---------------------------------------------------------------------------


class TestVDV463Authentication:
    """Test VDV 463 WebSocket authentication (H6 fix)."""

    def test_vdv463_auth_check_in_handler(self):
        """VDV 463 path should invoke SecurityManager authenticate_station."""
        import inspect

        from src.websocket_handler.server import OCPPWebSocketServer

        source = inspect.getsource(OCPPWebSocketServer._handle_connection)
        # After the VDV 463 routing block, auth should be checked
        # Find the vdv463 section and verify auth is called
        vdv_section_idx = source.find('protocol == "vdv463"')
        assert vdv_section_idx != -1
        vdv_section = source[vdv_section_idx : vdv_section_idx + 2000]
        assert "authenticate_station" in vdv_section


# ---------------------------------------------------------------------------
# H3: XXE protection
# ---------------------------------------------------------------------------


class TestXXEProtection:
    """Test XML parsing uses defusedxml (H3 fix)."""

    def test_price_feeder_uses_defusedxml(self):
        """price_feeder.py should import defusedxml, not stdlib xml."""
        import inspect

        from src.websocket_handler import price_feeder

        source = inspect.getsource(price_feeder)
        assert "defusedxml" in source
        assert "xml.etree.ElementTree" not in source

    def test_entsoe_adapter_uses_defusedxml(self):
        """entsoe/prices.py should import defusedxml, not stdlib xml."""
        import inspect

        from src.adapters.entsoe import prices

        source = inspect.getsource(prices)
        assert "defusedxml" in source
        assert "xml.etree.ElementTree" not in source


# ---------------------------------------------------------------------------
# L1: Log injection prevention
# ---------------------------------------------------------------------------


class TestLogInjection:
    """Test log injection mitigation (L1 fix)."""

    def test_control_chars_stripped_in_log_write(self):
        """Log writes should strip control characters."""
        import inspect

        from src.websocket_handler.diagnostics_firmware import DiagnosticsManager

        source = inspect.getsource(DiagnosticsManager._collect_logs)
        # Should contain regex substitution for control chars
        assert "re.sub" in source
        assert "\\x00" in source or "0x00" in source


# ---------------------------------------------------------------------------
# M1: Database info redaction
# ---------------------------------------------------------------------------


class TestDatabaseInfoRedaction:
    """Test database credentials redaction in logs (M1 fix)."""

    def test_describe_database_target_masks_sensitive_fields(self):
        """describe_database_target should not reveal user, host, or db name."""
        from src.api.main import describe_database_target

        result = describe_database_target(
            "postgresql://admin:secret@prod-db.example.com:5432/mydb"
        )
        assert "admin" not in result
        assert "secret" not in result
        assert "prod-db" not in result
        assert "mydb" not in result
        # Should show port and masked host suffix
        assert "5432" in result
        assert "*****" in result


# ---------------------------------------------------------------------------
# H11: Solver concurrency limit
# ---------------------------------------------------------------------------


class TestSolverConcurrencyLimit:
    """Test MILP solver concurrency limit (H11 fix)."""

    def test_semaphore_exists(self):
        """An asyncio.Semaphore should limit concurrent solves."""
        from src.api.main import _optimize_semaphore

        assert isinstance(_optimize_semaphore, asyncio.Semaphore)

    def test_max_concurrent_solves_default(self):
        """Default max concurrent solves should be 2."""
        from src.api.main import _MAX_CONCURRENT_SOLVES

        assert _MAX_CONCURRENT_SOLVES == 2


# ---------------------------------------------------------------------------
# M6: VDV 463 hard validation in production
# ---------------------------------------------------------------------------


class TestVDV463ProductionValidation:
    """Test VDV 463 defaults to hard validation in production (M6 fix)."""

    def test_production_forces_hard_validation(self):
        """parse_message should override SOFT to HARD in production."""
        import inspect

        from src.adapters.vdv463.messages import parse_message

        source = inspect.getsource(parse_message)
        assert "production" in source.lower()
        assert "HARD" in source
