"""Secrets management helpers.

Per PRD Section 10.3, use environment variables or Vault (future).
"""

from __future__ import annotations

import os
from typing import Optional


def get_secret(key: str, default: Optional[str] = None) -> str:
    """Get secret from environment variable.

    In production, this should integrate with HashiCorp Vault or similar.

    Args:
        key: Environment variable name
        default: Default value if not found (raises ValueError if None)

    Returns:
        Secret value from environment

    Raises:
        ValueError: If secret not found and no default provided
    """
    value = os.getenv(key, default)
    if value is None:
        raise ValueError(f"Required secret {key} not found in environment")
    return value


# Usage:
# db_password = get_secret('DB_PASSWORD')
# jwt_secret = get_secret('JWT_SECRET_KEY')
