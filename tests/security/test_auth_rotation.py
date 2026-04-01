"""Tests for JWT authentication with key rotation support.

Tests cover:
- Token verification with current key
- Token verification with previous key during rotation
- Expired tokens rejected regardless of key
- Invalid tokens rejected
- Missing JWT_SECRET_KEY raises 500
- get_user_role extracts Favonius role from metadata
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
        """Extracts Favonius-specific role from user_metadata."""
        token = {"user_metadata": {"favonius_role": "operator"}}
        assert get_user_role(token) == "operator"

    def test_get_user_role_default(self):
        """Falls back to Supabase role claim."""
        token = {"role": "authenticated"}
        assert get_user_role(token) == "authenticated"

    def test_is_demo_user(self):
        """Detects demo users."""
        assert is_demo_user({"user_metadata": {"is_demo": True}}) is True
        assert is_demo_user({"user_metadata": {"is_demo": False}}) is False
        assert is_demo_user({}) is False
