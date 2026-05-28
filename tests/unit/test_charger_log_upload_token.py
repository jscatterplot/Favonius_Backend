"""Unit tests for the charger-log upload HMAC token helper.

The token is the only authentication on the public upload endpoint, so
every shape / signature / expiry path is exercised here. The endpoint
maps every failure to HTTP 401 to keep the timing side channel narrow.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest

from src.adapters.chargers.upload_token import (
    UPLOAD_PATH,
    UploadTokenError,
    build_upload_url,
    derive_upload_base_url,
    get_max_upload_bytes,
    get_token_ttl_seconds,
    mint_token,
    verify_token,
)


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    """Provide a deterministic HMAC key + base URL for every test."""
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", "unit-test-key-1234567890")
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_BASE_URL", "https://api.example.test/internal/charger_logs/upload")


class TestMintAndVerify:
    def test_round_trip(self):
        import_id = uuid4()
        token = mint_token(import_id)
        decoded = verify_token(token)
        assert decoded.import_id == import_id
        assert decoded.raw == token

    def test_sha256_matches_canonical_hash(self):
        import_id = uuid4()
        token = mint_token(import_id)
        decoded = verify_token(token)
        import hashlib
        assert decoded.sha256_hex == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_rejects_empty_token(self):
        with pytest.raises(UploadTokenError):
            verify_token("")

    def test_rejects_malformed_token(self):
        with pytest.raises(UploadTokenError):
            verify_token("not.a.token")  # uuid parse will fail
        with pytest.raises(UploadTokenError):
            verify_token("not-three-segments")
        with pytest.raises(UploadTokenError):
            verify_token("a.b")

    def test_rejects_bad_signature(self):
        import_id = uuid4()
        token = mint_token(import_id)
        # Mangle the signature byte
        parts = token.split(".")
        parts[2] = "0" * len(parts[2])
        bad = ".".join(parts)
        with pytest.raises(UploadTokenError):
            verify_token(bad)

    def test_rejects_token_minted_with_different_key(self, monkeypatch):
        import_id = uuid4()
        token = mint_token(import_id)
        # Re-key the env and verify must reject
        monkeypatch.setenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", "different-key-9999999999")
        with pytest.raises(UploadTokenError):
            verify_token(token)

    def test_rejects_expired_token(self):
        import_id = uuid4()
        # Mint with an explicit -1s TTL so it's expired the moment we verify.
        token = mint_token(import_id, ttl_s=1, now=time.time() - 3600)
        with pytest.raises(UploadTokenError):
            verify_token(token)

    def test_default_ttl_within_reasonable_range(self, monkeypatch):
        monkeypatch.delenv("CHARGER_LOG_UPLOAD_TOKEN_TTL_S", raising=False)
        assert 60 <= get_token_ttl_seconds() <= 24 * 3600

    def test_ttl_floors_at_60_seconds(self, monkeypatch):
        monkeypatch.setenv("CHARGER_LOG_UPLOAD_TOKEN_TTL_S", "5")
        assert get_token_ttl_seconds() == 60

    def test_max_bytes_default_is_50_mib(self, monkeypatch):
        monkeypatch.delenv("CHARGER_LOG_UPLOAD_MAX_BYTES", raising=False)
        assert get_max_upload_bytes() == 50 * 1024 * 1024


class TestBuildUploadUrl:
    def test_includes_token_and_import_id(self):
        import_id = uuid4()
        url = build_upload_url(import_id)
        assert url.startswith("https://api.example.test/internal/charger_logs/upload?")
        assert f"import_id={import_id}" in url
        assert "token=" in url

    def test_raises_when_base_url_unset(self, monkeypatch):
        monkeypatch.delenv("CHARGER_LOG_UPLOAD_BASE_URL", raising=False)
        with pytest.raises(RuntimeError):
            build_upload_url(uuid4())

    def test_strips_trailing_slash_from_base(self):
        # A trailing slash makes the charger POST to /upload/ which FastAPI
        # 307-redirects; embedded charger clients often drop the body across
        # the redirect, so the upload silently never lands. The query must
        # attach directly to the slash-less route.
        import_id = uuid4()
        url = build_upload_url(
            import_id,
            base_url="https://api.example.test/internal/charger_logs/upload/",
            token="tok",
        )
        assert url.startswith("https://api.example.test/internal/charger_logs/upload?")
        assert "/upload/?" not in url


class TestDeriveUploadBaseUrl:
    def test_derives_from_host_and_forwarded_proto(self):
        url = derive_upload_base_url(
            host="favoniusbackend-production.up.railway.app",
            forwarded_proto="https",
            fallback_scheme="http",
        )
        assert url == (
            "https://favoniusbackend-production.up.railway.app"
            "/internal/charger_logs/upload"
        )
        assert url.endswith(UPLOAD_PATH)

    def test_forwarded_proto_uses_leftmost_value(self):
        # X-Forwarded-Proto can be a comma-separated chain; the public
        # scheme is the leftmost (closest to the client).
        url = derive_upload_base_url(
            host="api.example.test",
            forwarded_proto="https, http",
            fallback_scheme="http",
        )
        assert url.startswith("https://")

    def test_falls_back_to_request_scheme_without_forwarded_proto(self):
        # Direct local uvicorn: no edge proxy, no X-Forwarded-Proto.
        url = derive_upload_base_url(
            host="localhost:8000", forwarded_proto=None, fallback_scheme="http"
        )
        assert url == "http://localhost:8000/internal/charger_logs/upload"

    def test_returns_none_without_host(self):
        assert derive_upload_base_url(host=None) is None
        assert derive_upload_base_url(host="") is None
        assert derive_upload_base_url(host="   ") is None

    def test_strips_whitespace_around_host(self):
        url = derive_upload_base_url(
            host="  api.example.test  ", forwarded_proto="https"
        )
        assert url == "https://api.example.test/internal/charger_logs/upload"

    def test_derived_url_is_usable_as_build_upload_url_base(self):
        # The derived value is the same shape build_upload_url expects as
        # an explicit base_url, so it round-trips into a usable upload URL.
        base = derive_upload_base_url(host="api.example.test", forwarded_proto="https")
        url = build_upload_url(uuid4(), base_url=base)
        assert url.startswith("https://api.example.test/internal/charger_logs/upload?")
        assert "token=" in url


class TestSigningKeyRequired:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", raising=False)
        with pytest.raises(RuntimeError):
            mint_token(uuid4())

    def test_verify_also_raises_without_key(self, monkeypatch):
        # Mint with the key, then drop it before verify.
        import_id = uuid4()
        token = mint_token(import_id)
        monkeypatch.delenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", raising=False)
        with pytest.raises(RuntimeError):
            verify_token(token)
