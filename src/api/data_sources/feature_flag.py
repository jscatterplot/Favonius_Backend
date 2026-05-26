"""Feature flag + readiness gate for the Data Sources feature."""

from __future__ import annotations

import os

from src.security.credential_cipher import is_configured


def is_data_sources_enabled() -> bool:
    """Return True iff the Data Sources router + scheduler should be active.

    Default on. Set ``DATA_SOURCES_ENABLED=false`` to disable.
    """
    return os.environ.get("DATA_SOURCES_ENABLED", "true").lower() == "true"


def is_data_sources_ready() -> bool:
    """Enabled AND a credential-encryption key is configured (fail-closed)."""
    return is_data_sources_enabled() and is_configured()
