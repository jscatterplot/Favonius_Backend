"""Secrets management with rotation support.

Per PRD Section 10.3 and NIS2 Article 21, secrets must support
rotation without downtime. This module provides:

- Environment-based secret retrieval (current)
- Multiple active secrets during rotation windows
- Secret access audit logging
- Vault integration readiness (interface for future HashiCorp Vault)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SecretMetadata:
    """Metadata about a secret for audit and rotation tracking.

    Attributes:
        key: The environment variable or secret name.
        source: Where the secret came from (env, vault, file).
        loaded_at: When the secret was loaded.
        expires_at: When the secret should be rotated (None = no expiry).
        version: Version identifier for rotation tracking.
    """

    key: str
    source: str = "env"
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    version: str = "1"


class SecretsManager:
    """Manages secrets with rotation support.

    Supports multiple active values for the same key during rotation
    windows (e.g. two JWT signing keys valid simultaneously).

    Usage:
        manager = SecretsManager()
        jwt_secrets = manager.get_rotation_secrets("JWT_SECRET_KEY")
        # Returns list of valid secrets (current + previous during rotation)
    """

    def __init__(self) -> None:
        self._metadata: dict[str, SecretMetadata] = {}
        self._access_log: list[dict] = []

    def get_secret(self, key: str, default: Optional[str] = None) -> str:
        """Get secret from environment variable.

        Args:
            key: Environment variable name.
            default: Default value if not found (raises ValueError if None).

        Returns:
            Secret value from environment.

        Raises:
            ValueError: If secret not found and no default provided.
        """
        value = os.getenv(key, default)
        if value is None:
            raise ValueError(f"Required secret {key} not found in environment")

        # Track metadata
        if key not in self._metadata:
            self._metadata[key] = SecretMetadata(key=key, source="env")

        # Audit log access
        self._access_log.append(
            {
                "key": key,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "env",
            }
        )

        return value

    def get_rotation_secrets(self, key: str) -> list[str]:
        """Get all valid secrets for a key during rotation.

        Checks for:
        1. {KEY} — current secret
        2. {KEY}_PREVIOUS — previous secret (valid during rotation window)

        During a rotation, both secrets are valid for verification
        but only the current one is used for signing.

        Args:
            key: Base environment variable name.

        Returns:
            List of valid secret values (current first, then previous).
        """
        secrets = []

        current = os.getenv(key)
        if current:
            secrets.append(current)

        previous = os.getenv(f"{key}_PREVIOUS")
        if previous and previous != current:
            secrets.append(previous)

        if not secrets:
            raise ValueError(
                f"No secrets found for {key}. "
                f"Set {key} (and optionally {key}_PREVIOUS for rotation)."
            )

        return secrets

    def check_rotation_needed(self, key: str) -> bool:
        """Check if a secret has a pending rotation (previous key is set)."""
        return os.getenv(f"{key}_PREVIOUS") is not None

    def get_access_log(self) -> list[dict]:
        """Get the secret access audit log."""
        return self._access_log.copy()


# ── Module-level convenience functions ────────────────────────────────────

_manager: Optional[SecretsManager] = None


def get_secrets_manager() -> SecretsManager:
    """Get or create the module-level SecretsManager."""
    global _manager
    if _manager is None:
        _manager = SecretsManager()
    return _manager


def get_secret(key: str, default: Optional[str] = None) -> str:
    """Get secret from environment variable.

    Args:
        key: Environment variable name.
        default: Default value if not found (raises ValueError if None).

    Returns:
        Secret value from environment.

    Raises:
        ValueError: If secret not found and no default provided.
    """
    return get_secrets_manager().get_secret(key, default)
