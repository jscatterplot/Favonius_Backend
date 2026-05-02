"""Unit tests for the handoff SSRF guard and HMAC helpers.

Reference: audit finding H4 / src/security/handoff_validator.py
"""

from __future__ import annotations

import ipaddress
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.security.handoff_validator import (
    compute_handoff_signature,
    validate_handoff_destination,
    verify_handoff_signature,
)


def _ip(value: str) -> ipaddress._BaseAddress:
    return ipaddress.ip_address(value)


# ---------------------------------------------------------------------------
# validate_handoff_destination
# ---------------------------------------------------------------------------


class TestValidateHandoffDestination:
    PUBLIC_IP = "8.8.8.8"  # globally-routable per ipaddress.is_global / not is_private

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_public_https_target_allowed(self, resolve):
        resolve.return_value = [_ip(self.PUBLIC_IP)]
        # Should not raise.
        validate_handoff_destination("https://depot.example.com/x", "production")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_rfc1918_10_8(self, resolve):
        resolve.return_value = [_ip("10.0.0.5")]
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("https://depot.example.com", "production")
        assert exc_info.value.status_code == 400
        assert "disallowed address" in exc_info.value.detail

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_rfc1918_192_168(self, resolve):
        resolve.return_value = [_ip("192.168.1.1")]
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("https://internal.example.com", "production")
        assert exc_info.value.status_code == 400

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_loopback(self, resolve):
        resolve.return_value = [_ip("127.0.0.1")]
        with pytest.raises(HTTPException):
            validate_handoff_destination("https://depot.example.com", "production")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_link_local(self, resolve):
        # 169.254.169.254 is the AWS/GCP metadata endpoint — the canonical
        # SSRF target.
        resolve.return_value = [_ip("169.254.169.254")]
        with pytest.raises(HTTPException):
            validate_handoff_destination("https://depot.example.com", "production")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_ipv6_link_local(self, resolve):
        resolve.return_value = [_ip("fe80::1")]
        with pytest.raises(HTTPException):
            validate_handoff_destination("https://depot.example.com", "production")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_unspecified(self, resolve):
        resolve.return_value = [_ip("0.0.0.0")]
        with pytest.raises(HTTPException):
            validate_handoff_destination("https://depot.example.com", "production")

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("ftp://depot.example.com/x", "production")
        assert exc_info.value.status_code == 400
        assert "scheme" in exc_info.value.detail.lower()

    def test_rejects_http_in_production(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("http://depot.example.com/x", "production")
        assert exc_info.value.status_code == 400
        assert "HTTPS" in exc_info.value.detail

    def test_rejects_http_in_staging(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("http://depot.example.com/x", "staging")
        assert exc_info.value.status_code == 400

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_allows_http_in_development(self, resolve, monkeypatch):
        # dev still requires public IP unless override flag is set.
        monkeypatch.delenv("HANDOFF_ALLOW_PRIVATE_HOSTS", raising=False)
        resolve.return_value = [_ip(self.PUBLIC_IP)]
        validate_handoff_destination("http://depot.example.com/x", "development")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_missing_hostname(self, resolve):
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("https:///path", "production")
        assert exc_info.value.status_code == 400
        assert "hostname" in exc_info.value.detail.lower()

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_dns_resolution_failure(self, resolve):
        resolve.return_value = []
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("https://nope.invalid", "production")
        assert exc_info.value.status_code == 400
        assert "resolve" in exc_info.value.detail.lower()

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_rejects_when_any_a_record_is_private(self, resolve):
        # Multi-A-record: one public, one private — refuse so a hostile DNS
        # operator can't trick the validator with an interleaved record.
        resolve.return_value = [_ip(self.PUBLIC_IP), _ip("10.0.0.1")]
        with pytest.raises(HTTPException) as exc_info:
            validate_handoff_destination("https://depot.example.com", "production")
        assert exc_info.value.status_code == 400

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_dev_override_allows_private_hosts(self, resolve, monkeypatch):
        monkeypatch.setenv("HANDOFF_ALLOW_PRIVATE_HOSTS", "true")
        resolve.return_value = [_ip("127.0.0.1")]
        validate_handoff_destination("http://localhost:8000/x", "development")

    @patch("src.security.handoff_validator._resolve_addresses")
    def test_prod_override_is_ignored(self, resolve, monkeypatch):
        # The override flag is honoured only in development; production must
        # not be overridable from env.
        monkeypatch.setenv("HANDOFF_ALLOW_PRIVATE_HOSTS", "true")
        resolve.return_value = [_ip("10.0.0.1")]
        with pytest.raises(HTTPException):
            validate_handoff_destination("https://depot.example.com", "production")


# ---------------------------------------------------------------------------
# compute / verify signature
# ---------------------------------------------------------------------------


class TestHmacHelpers:
    KEY = b"super-secret-key"
    BODY = b'{"vehicle_id":"v1","nonce":"n1"}'

    def test_compute_returns_hex(self):
        sig = compute_handoff_signature(self.KEY, self.BODY)
        assert isinstance(sig, str)
        # SHA-256 produces 64 hex chars.
        assert len(sig) == 64
        int(sig, 16)  # parses as hex

    def test_verify_accepts_correct_signature(self):
        sig = compute_handoff_signature(self.KEY, self.BODY)
        assert verify_handoff_signature(self.KEY, self.BODY, sig) is True

    def test_verify_rejects_wrong_signature(self):
        assert verify_handoff_signature(self.KEY, self.BODY, "0" * 64) is False

    def test_verify_rejects_empty_signature(self):
        assert verify_handoff_signature(self.KEY, self.BODY, "") is False

    def test_verify_rejects_wrong_key(self):
        sig = compute_handoff_signature(self.KEY, self.BODY)
        assert verify_handoff_signature(b"different-key", self.BODY, sig) is False

    def test_verify_rejects_tampered_body(self):
        sig = compute_handoff_signature(self.KEY, self.BODY)
        assert verify_handoff_signature(self.KEY, self.BODY + b"x", sig) is False

    def test_verify_rejects_non_string_signature(self):
        assert verify_handoff_signature(self.KEY, self.BODY, None) is False  # type: ignore[arg-type]
