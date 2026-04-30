"""Severity enum mirroring the migration 022 DB generated column.

The DB stores severity as text ('info' | 'warning' | 'critical') with a
generated SMALLINT severity_level (1 | 2 | 3) used for indexing and
threshold comparisons. This module is the canonical Python side; the
mapping must stay in lock-step with migrations/022_alerts_pipeline.sql.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """Alert severity. Values match the DB CHECK constraint."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def level(self) -> int:
        """Integer level matching the DB severity_level generated column."""
        return _SEVERITY_TO_LEVEL[self]

    @classmethod
    def from_str(cls, value: str) -> "Severity":
        """Parse a string severity, case-insensitive.

        Raises ValueError for unknown values to surface bugs early rather than
        silently downgrading to a default.
        """
        if not isinstance(value, str):
            raise TypeError(f"severity must be str, got {type(value).__name__}")
        return cls(value.lower())

    @classmethod
    def from_level(cls, level: int) -> "Severity":
        """Inverse of `.level`. Raises ValueError for unknown levels."""
        try:
            return _LEVEL_TO_SEVERITY[level]
        except KeyError as exc:
            raise ValueError(f"unknown severity level: {level}") from exc

    def meets_threshold(self, threshold: "Severity") -> bool:
        """True if this severity is at or above the given threshold.

        Used for recipient gating: a recipient with min_severity=warning
        receives alerts where severity.meets_threshold(warning) is True.
        """
        return self.level >= threshold.level


_SEVERITY_TO_LEVEL: dict[Severity, int] = {
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.CRITICAL: 3,
}

_LEVEL_TO_SEVERITY: dict[int, Severity] = {v: k for k, v in _SEVERITY_TO_LEVEL.items()}


__all__ = ["Severity"]
