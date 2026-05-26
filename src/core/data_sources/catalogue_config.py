"""Merge non-secret catalogue fields from the connect-form into connection config."""

from __future__ import annotations

from typing import Any

from .base import DataSourceProvider


def merge_catalogue_config(
    provider: DataSourceProvider,
    credentials: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Copy non-secret credential_fields into config when config omits them.

    The UI renders all catalogue fields as credentials; explicit config keys win.
    """
    merged = dict(config or {})
    for field in provider.catalogue_entry().credential_fields:
        if field.secret:
            continue
        value = credentials.get(field.key)
        if value is None or value == "":
            continue
        if merged.get(field.key) in (None, ""):
            merged[field.key] = value
    return merged
