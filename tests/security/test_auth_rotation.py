"""Tests for JWT authentication with key rotation and JWKS support.

Tests cover:
- HS256 token verification with current key
- HS256 token verification with previous key during rotation
- Expired tokens rejected regardless of key
- Invalid tokens rejected
- Missing JWT_SECRET_KEY: verify_token returns 401 for HS256 tokens;
  _get_jwt_secrets still raises 500 for operational paths that require HS256
- get_user_role extracts Favonius role from metadata
- ES256 token verified via mocked PyJWKClient
- ES256 token rejected when JWKS URL is not configured
- Disallowed alg values rejected before key lookup
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

from src.security.auth import (
    _get_jwt_secrets,
    get_user_id,
    get_user_role,
    is_demo_user,
    is_platform_admin,
    verify_depot_access,
    verify_token,
)


class TestGetJwtSecrets:
    """Test _get_jwt_secrets helper."""

    def test_returns_current_secret(self):
        """Returns current JWT secret."""
        env = {"JWT_SECRET_KEY": "test_secret"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            secrets = _get_jwt_secrets()
            assert secrets == ["test_secret"]

    def test_returns_both_during_rotation(self):
        """Returns current and previous during rotation."""
        env = {
            "JWT_SECRET_KEY": "new_secret",
            "JWT_SECRET_KEY_PREVIOUS": "old_secret",
        }
        with patch.dict(os.environ, env, clear=False):
            secrets = _get_jwt_secrets()
            assert "new_secret" in secrets
            assert "old_secret" in secrets

    def test_raises_when_no_secret(self):
        """Raises HTTPException when no JWT secret configured."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JWT_SECRET_KEY", None)
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            with pytest.raises(HTTPException) as exc_info:
                _get_jwt_secrets()
            assert exc_info.value.status_code == 500


class TestVerifyToken:
    """Test token verification with rotation."""

    @pytest.fixture
    def current_secret(self):
        return "current_jwt_secret_key_for_testing"

    @pytest.fixture
    def previous_secret(self):
        return "previous_jwt_secret_key_for_testing"

    def _make_token(self, secret: str, **extra_claims) -> str:
        """Create a test JWT token."""
        payload = {
            "sub": "user-uuid-123",
            "email": "test@favonius.energy",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            **extra_claims,
        }
        return jwt.encode(payload, secret, algorithm="HS256")

    @pytest.mark.asyncio
    async def test_verify_with_current_key(self, current_secret):
        """Token signed with current key is verified."""
        from unittest.mock import MagicMock

        token = self._make_token(current_secret)
        creds = MagicMock()
        creds.credentials = token

        env = {"JWT_SECRET_KEY": current_secret}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            payload = await verify_token(creds)
            assert payload["sub"] == "user-uuid-123"

    @pytest.mark.asyncio
    async def test_verify_with_previous_key(self, current_secret, previous_secret):
        """Token signed with previous key is verified during rotation."""
        from unittest.mock import MagicMock

        token = self._make_token(previous_secret)
        creds = MagicMock()
        creds.credentials = token

        env = {
            "JWT_SECRET_KEY": current_secret,
            "JWT_SECRET_KEY_PREVIOUS": previous_secret,
        }
        with patch.dict(os.environ, env, clear=False):
            payload = await verify_token(creds)
            assert payload["sub"] == "user-uuid-123"

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, current_secret):
        """Expired token is rejected regardless of key."""
        from unittest.mock import MagicMock

        payload = {
            "sub": "user-uuid-123",
            "aud": "authenticated",
            "exp": int(time.time()) - 3600,  # Expired 1 hour ago
        }
        token = jwt.encode(payload, current_secret, algorithm="HS256")
        creds = MagicMock()
        creds.credentials = token

        env = {"JWT_SECRET_KEY": current_secret}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(creds)
            assert exc_info.value.status_code == 401
            assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, current_secret):
        """Token signed with unknown key is rejected."""
        from unittest.mock import MagicMock

        token = self._make_token("completely_wrong_key")
        creds = MagicMock()
        creds.credentials = token

        env = {"JWT_SECRET_KEY": current_secret}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(creds)
            assert exc_info.value.status_code == 401


class TestUserHelpers:
    """Test user extraction helpers."""

    def test_get_user_id(self):
        """Extracts user UUID from token."""
        assert get_user_id({"sub": "uuid-123"}) == "uuid-123"

    def test_get_user_id_missing(self):
        """Raises when sub claim missing."""
        with pytest.raises(HTTPException):
            get_user_id({})

    def test_get_user_role_favonius(self):
        """Extracts Favonius-specific role from app_metadata."""
        token = {"app_metadata": {"favonius_role": "customer_operator"}}
        assert get_user_role(token) == "customer_operator"

    def test_get_user_role_default(self):
        """Falls back to Supabase role claim."""
        token = {"role": "authenticated"}
        assert get_user_role(token) == "authenticated"

    def test_is_demo_user(self):
        """Detects demo users."""
        assert is_demo_user({"user_metadata": {"is_demo": True}}) is True
        assert is_demo_user({"user_metadata": {"is_demo": False}}) is False
        assert is_demo_user({}) is False


class TestEmailBasedAdminPromotion:
    """Auto-promotion to ``favonius_admin`` based on the JWT email domain.

    Anyone who can authenticate with a verified ``@favoniusenergy.com`` address
    must resolve to ``favonius_admin`` regardless of ``app_metadata.favonius_role``,
    so a stale Supabase metadata value cannot demote a Favonius employee.
    """

    _confirmed_at = {"email_confirmed_at": "2024-01-01T00:00:00Z"}

    @pytest.fixture(autouse=True)
    def _clear_env_override(self):
        """Default to the hardcoded domain unless a test sets the override."""
        env = {k: v for k, v in os.environ.items() if k != "FAVONIUS_ADMIN_EMAIL_DOMAINS"}
        with patch.dict(os.environ, env, clear=True):
            yield

    def test_promotes_matching_email(self):
        token = {"email": "alice@favoniusenergy.com", **self._confirmed_at}
        assert get_user_role(token) == "favonius_admin"

    def test_promotes_when_email_case_varies(self):
        token = {"email": "Alice@FavoniusEnergy.COM", **self._confirmed_at}
        assert get_user_role(token) == "favonius_admin"

    def test_promotion_overrides_explicit_lower_role(self):
        """A Favonius email beats any prior ``app_metadata.favonius_role``."""
        token = {
            "email": "alice@favoniusenergy.com",
            "app_metadata": {"favonius_role": "customer_operator"},
            **self._confirmed_at,
        }
        assert get_user_role(token) == "favonius_admin"

    def test_does_not_promote_other_domain(self):
        token = {
            "email": "alice@example.com",
            "app_metadata": {"favonius_role": "customer_operator"},
        }
        assert get_user_role(token) == "customer_operator"

    def test_does_not_promote_subdomain(self):
        """Subdomains and prefix collisions must NOT match."""
        token = {"email": "attacker@evil.favoniusenergy.com"}
        assert get_user_role(token) == "authenticated"

    def test_does_not_promote_suffix_lookalike(self):
        """``…favoniusenergy.com.attacker.com`` ends in the brand string but is not it."""
        token = {"email": "attacker@favoniusenergy.com.attacker.com"}
        assert get_user_role(token) == "authenticated"

    def test_does_not_promote_lookalike_brand(self):
        """``favonius-energy.com`` and ``favonius.energy`` are distinct domains."""
        for email in ("alice@favonius-energy.com", "alice@favonius.energy"):
            assert get_user_role({"email": email}) == "authenticated"

    def test_no_email_claim_falls_back(self):
        token = {"app_metadata": {"favonius_role": "customer_admin"}}
        assert get_user_role(token) == "customer_admin"

    def test_malformed_email_falls_back(self):
        token = {"email": "no-at-sign", "role": "authenticated"}
        assert get_user_role(token) == "authenticated"

    def test_empty_email_falls_back(self):
        token = {"email": "", "role": "authenticated"}
        assert get_user_role(token) == "authenticated"

    def test_non_string_email_falls_back(self):
        """Defensive: never crash if a malformed JWT carries a non-string email."""
        token = {"email": 12345, "role": "authenticated"}
        assert get_user_role(token) == "authenticated"

    def test_unconfirmed_email_blocks_promotion(self):
        """No confirmation signals must defeat promotion."""
        token = {"email": "alice@favoniusenergy.com", "user_metadata": {}}
        assert get_user_role(token) == "authenticated"

    def test_promotes_without_email_confirmed_at_when_app_metadata_email_provider(
        self,
    ):
        """Real Supabase access tokens omit ``email_confirmed_at``; ``app_metadata`` suffices."""
        token = {
            "email": "alice@favoniusenergy.com",
            "app_metadata": {"provider": "email", "providers": ["email"]},
        }
        assert get_user_role(token) == "favonius_admin"

    def test_phone_only_provider_without_email_confirmed_at_does_not_promote(self):
        """Phone-only ``app_metadata`` must not promote (``confirmed_at`` pattern)."""
        token = {
            "email": "alice@favoniusenergy.com",
            "app_metadata": {"provider": "phone", "providers": ["phone"]},
        }
        assert get_user_role(token) == "authenticated"

    def test_user_metadata_email_verified_true_does_not_bypass_missing_confirmation(self):
        """``user_metadata`` is user-editable and must not imply a confirmed email."""
        token = {
            "email": "alice@favoniusenergy.com",
            "user_metadata": {"email_verified": True},
        }
        assert get_user_role(token) == "authenticated"

    def test_confirmed_email_allows_promotion(self):
        token = {"email": "alice@favoniusenergy.com", **self._confirmed_at}
        assert get_user_role(token) == "favonius_admin"

    def test_confirmed_at_without_email_confirmed_at_does_not_promote(self):
        """``confirmed_at`` can reflect phone-only confirmation; it must not promote."""
        token = {
            "email": "alice@favoniusenergy.com",
            "confirmed_at": "2024-01-01T00:00:00Z",
        }
        assert get_user_role(token) == "authenticated"

    def test_env_var_overrides_default_domain(self):
        with patch.dict(
            os.environ, {"FAVONIUS_ADMIN_EMAIL_DOMAINS": "favonius.energy"}, clear=False
        ):
            assert (
                get_user_role({"email": "ops@favonius.energy", **self._confirmed_at})
                == "favonius_admin"
            )
            # Default domain no longer counts when the override is set.
            assert (
                get_user_role(
                    {
                        "email": "alice@favoniusenergy.com",
                        "role": "authenticated",
                        **self._confirmed_at,
                    }
                )
                == "authenticated"
            )

    def test_env_var_supports_multiple_domains(self):
        with patch.dict(
            os.environ,
            {"FAVONIUS_ADMIN_EMAIL_DOMAINS": "favoniusenergy.com, favonius.energy"},
            clear=False,
        ):
            assert (
                get_user_role({"email": "ops@favonius.energy", **self._confirmed_at})
                == "favonius_admin"
            )
            assert (
                get_user_role({"email": "alice@favoniusenergy.com", **self._confirmed_at})
                == "favonius_admin"
            )
            assert (
                get_user_role({"email": "stranger@example.com", "role": "authenticated"})
                == "authenticated"
            )

    def test_blank_env_var_falls_back_to_default(self):
        """Whitespace-only override must not silently disable promotion."""
        with patch.dict(os.environ, {"FAVONIUS_ADMIN_EMAIL_DOMAINS": "  ,  "}, clear=False):
            assert (
                get_user_role({"email": "alice@favoniusenergy.com", **self._confirmed_at})
                == "favonius_admin"
            )

    def test_is_platform_admin_honours_email_promotion(self):
        """The downstream admin gate must also see the promoted role."""
        token = {"email": "alice@favoniusenergy.com", **self._confirmed_at}
        assert is_platform_admin(token) is True

    @pytest.mark.asyncio
    async def test_verify_depot_access_bypasses_tenant_check_for_favonius_email(self):
        """``verify_depot_access`` must bypass the DB lookup for Favonius staff.

        Passes a sentinel ``object()`` as the static pool — if we got far enough
        to call ``pool.acquire()`` the test would explode, so a clean return is
        proof that the platform-admin shortcut fired before any DB access.
        """
        token = {"email": "ops@favoniusenergy.com", **self._confirmed_at}
        sentinel_pool = object()
        await verify_depot_access("00000000-0000-4000-8000-000000000000", token, pool=sentinel_pool)


class TestVerifyTokenJWKS:
    """ES256 / asymmetric verification path (Supabase JWT Signing Keys)."""

    @staticmethod
    def _make_es256_keypair():
        """Generate a P-256 keypair and the matching PyJWK signing key."""
        from cryptography.hazmat.primitives.asymmetric import ec

        from src.security import auth as auth_mod

        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key()

        class _FakeSigningKey:
            def __init__(self, key):
                self.key = key

        signing_key = _FakeSigningKey(public_key)
        # Reset cached client so each test starts fresh
        auth_mod._reset_jwks_client_for_tests()
        return private_key, signing_key

    @pytest.mark.asyncio
    async def test_es256_verified_via_jwks(self):
        """ES256 token is decoded using the JWKS public key."""
        from unittest.mock import MagicMock, patch

        from src.security import auth as auth_mod

        private_key, signing_key = self._make_es256_keypair()
        payload = {
            "sub": "user-uuid-es256",
            "email": "es256@favonius.energy",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": "k1"})
        creds = MagicMock()
        creds.credentials = token

        env = {"SUPABASE_URL": "https://example.supabase.co"}
        fake_client = MagicMock()
        fake_client.get_signing_key_from_jwt.return_value = signing_key

        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(auth_mod, "PyJWKClient", return_value=fake_client),
        ):
            os.environ.pop("JWT_SECRET_KEY", None)
            auth_mod._reset_jwks_client_for_tests()
            decoded = await auth_mod.verify_token(creds)

        assert decoded["sub"] == "user-uuid-es256"
        fake_client.get_signing_key_from_jwt.assert_called_once_with(token)

    @pytest.mark.asyncio
    async def test_hs256_rejected_when_symmetric_secret_missing(self):
        """HS256 token with no JWT_SECRET_KEY returns 401 (asymmetric-only deploy)."""
        from unittest.mock import MagicMock

        from src.security import auth as auth_mod

        payload = {
            "sub": "user-uuid",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, "any_secret", algorithm="HS256")
        creds = MagicMock()
        creds.credentials = token

        env = {"SUPABASE_URL": "https://example.supabase.co"}
        with patch.dict(os.environ, env, clear=False):
            for var in ("JWT_SECRET_KEY", "JWT_SECRET_KEY_PREVIOUS"):
                os.environ.pop(var, None)
            auth_mod._reset_jwks_client_for_tests()
            with pytest.raises(HTTPException) as exc_info:
                await auth_mod.verify_token(creds)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_es256_rejected_when_jwks_url_missing(self):
        """ES256 token without SUPABASE_URL is rejected as 401, not 500."""
        from unittest.mock import MagicMock

        from src.security import auth as auth_mod

        private_key, _ = self._make_es256_keypair()
        payload = {
            "sub": "user-uuid",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, private_key, algorithm="ES256")
        creds = MagicMock()
        creds.credentials = token

        with patch.dict(os.environ, {}, clear=False):
            for var in ("SUPABASE_URL", "SUPABASE_JWKS_URL", "JWT_SECRET_KEY"):
                os.environ.pop(var, None)
            auth_mod._reset_jwks_client_for_tests()
            with pytest.raises(HTTPException) as exc_info:
                await auth_mod.verify_token(creds)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_disallowed_alg_rejected(self):
        """Tokens with an algorithm outside the allowlist are rejected."""
        from unittest.mock import MagicMock

        from src.security import auth as auth_mod

        # Craft a header-only token with alg=none — never decode-able.
        token = jwt.encode(
            {"sub": "x", "aud": "authenticated"},
            key="",
            algorithm="none",
        )
        creds = MagicMock()
        creds.credentials = token

        auth_mod._reset_jwks_client_for_tests()
        with pytest.raises(HTTPException) as exc_info:
            await auth_mod.verify_token(creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_jwks_lookup_failure_returns_401(self):
        """A JWKS network/lookup failure is surfaced as 401, not 500."""
        from unittest.mock import MagicMock, patch

        from jwt import PyJWKClientError

        from src.security import auth as auth_mod

        private_key, _ = self._make_es256_keypair()
        token = jwt.encode(
            {"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 3600},
            private_key,
            algorithm="ES256",
        )
        creds = MagicMock()
        creds.credentials = token

        fake_client = MagicMock()
        fake_client.get_signing_key_from_jwt.side_effect = PyJWKClientError("boom")

        env = {"SUPABASE_URL": "https://example.supabase.co"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(auth_mod, "PyJWKClient", return_value=fake_client),
        ):
            auth_mod._reset_jwks_client_for_tests()
            with pytest.raises(HTTPException) as exc_info:
                await auth_mod.verify_token(creds)
            assert exc_info.value.status_code == 401

    def test_derive_jwks_url_from_supabase_url(self):
        """SUPABASE_URL is normalised into the standard JWKS path."""
        from src.security import auth as auth_mod

        with patch.dict(
            os.environ,
            {"SUPABASE_URL": "https://abc.supabase.co/"},
            clear=False,
        ):
            os.environ.pop("SUPABASE_JWKS_URL", None)
            assert (
                auth_mod._derive_jwks_url()
                == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"
            )

    def test_derive_jwks_url_explicit_override(self):
        """SUPABASE_JWKS_URL overrides the derived path."""
        from src.security import auth as auth_mod

        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://abc.supabase.co",
                "SUPABASE_JWKS_URL": "https://custom.example/jwks.json",
            },
            clear=False,
        ):
            assert auth_mod._derive_jwks_url() == "https://custom.example/jwks.json"
