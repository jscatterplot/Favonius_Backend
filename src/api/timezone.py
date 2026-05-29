"""Shared timezone helpers for API modules."""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo


def safe_zone(tz_name: Optional[str]) -> ZoneInfo:
    """Resolve an IANA tz name to ``ZoneInfo``, falling back to UTC.

    A missing or invalid depot timezone degrades to UTC rather than
    raising — a small wall-clock skew is preferable to a 500.
    """
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - any bad zone name → UTC
            return ZoneInfo("UTC")
    return ZoneInfo("UTC")
