"""Tests for secrets management with rotation support.

Tests cover:
- Basic secret retrieval
- Rotation secrets (current + previous)
- Missing secrets raise ValueError
- Access audit logging
- SecretsManager singleton
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.security.secrets import (
    SecretsManager,
    get_secret,
    get_secrets_manager,
)


class TestGetSecret:
    """Test basic secret retrieval."""

    def test_returns_env_value(self):
        """Returns value from environment variable."""
        with patch.dict(os.environ, {"TEST_SECRET": "secret_value"}):
            manager = SecretsManager()
            assert manager.get_secret("TEST_SECRET") == "secret_value"

    def test_returns_default(self):
        """Returns default when env var not set."""
        manager = SecretsManager()
        os.environ.pop("NONEXISTENT_SECRET", None)
        assert manager.get_secret("NONEXISTENT_SECRET", "fallback") == "fallback"

    def test_raises_without_default(self):
        """Raises ValueError when env var not set and no default."""
        manager = SecretsManager()
        os.environ.pop("MISSING_SECRET", None)
        with pytest.raises(ValueError, match="Required secret"):
            manager.get_secret("MISSING_SECRET")

    def test_logs_access(self):
        """Secret access is logged for audit trail."""
        with patch.dict(os.environ, {"AUDIT_SECRET": "value"}):
            manager = SecretsManager()
            manager.get_secret("AUDIT_SECRET")
            log = manager.get_access_log()
            assert len(log) == 1
            assert log[0]["key"] == "AUDIT_SECRET"
            assert "timestamp" in log[0]


class TestRotationSecrets:
    """Test JWT key rotation support."""

    def test_returns_current_only(self):
        """Returns single secret when no previous key set."""
        env = {"JWT_SECRET_KEY": "current_key"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            manager = SecretsManager()
            secrets = manager.get_rotation_secrets("JWT_SECRET_KEY")
            assert secrets == ["current_key"]

    def test_returns_current_and_previous(self):
        """Returns both secrets during rotation window."""
        env = {
            "JWT_SECRET_KEY": "new_key",
            "JWT_SECRET_KEY_PREVIOUS": "old_key",
        }
        with patch.dict(os.environ, env, clear=False):
            manager = SecretsManager()
            secrets = manager.get_rotation_secrets("JWT_SECRET_KEY")
            assert secrets == ["new_key", "old_key"]
            assert secrets[0] == "new_key"  # Current first

    def test_deduplicates_same_key(self):
        """Does not return duplicate if current == previous."""
        env = {
            "JWT_SECRET_KEY": "same_key",
            "JWT_SECRET_KEY_PREVIOUS": "same_key",
        }
        with patch.dict(os.environ, env, clear=False):
            manager = SecretsManager()
            secrets = manager.get_rotation_secrets("JWT_SECRET_KEY")
            assert secrets == ["same_key"]

    def test_raises_if_no_secrets(self):
        """Raises ValueError when neither current nor previous exists."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT_KEY", None)
            os.environ.pop("NONEXISTENT_KEY_PREVIOUS", None)
            manager = SecretsManager()
            with pytest.raises(ValueError, match="No secrets found"):
                manager.get_rotation_secrets("NONEXISTENT_KEY")

    def test_check_rotation_needed(self):
        """Detects when rotation is in progress."""
        env = {
            "JWT_SECRET_KEY": "new",
            "JWT_SECRET_KEY_PREVIOUS": "old",
        }
        with patch.dict(os.environ, env, clear=False):
            manager = SecretsManager()
            assert manager.check_rotation_needed("JWT_SECRET_KEY") is True

    def test_no_rotation_needed(self):
        """No rotation when previous key is absent."""
        with patch.dict(os.environ, {"JWT_SECRET_KEY": "current"}, clear=False):
            os.environ.pop("JWT_SECRET_KEY_PREVIOUS", None)
            manager = SecretsManager()
            assert manager.check_rotation_needed("JWT_SECRET_KEY") is False


class TestModuleConvenience:
    """Test module-level convenience functions."""

    def test_get_secret_function(self):
        """Module-level get_secret works."""
        with patch.dict(os.environ, {"CONV_SECRET": "value"}):
            assert get_secret("CONV_SECRET") == "value"

    def test_singleton_manager(self):
        """get_secrets_manager returns same instance."""
        m1 = get_secrets_manager()
        m2 = get_secrets_manager()
        assert m1 is m2
