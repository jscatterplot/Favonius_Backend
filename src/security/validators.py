"""Input validation for API endpoints.

See PRD_v2.md Section 10.4 for security requirements.
"""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import HTTPException, status

# Validation patterns
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)


def validate_uuid(value: str, field_name: str) -> UUID:
    """Validate UUID v4 format.

    Args:
        value: String to validate
        field_name: Name for error messages

    Raises:
        HTTPException: If invalid UUID format
    """
    if not UUID_PATTERN.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid UUID format for {field_name}",
        )
    return UUID(value)


def validate_soc(value: float, field_name: str) -> float:
    """Validate SoC is in range [0.0, 1.0].

    Args:
        value: SoC value to validate
        field_name: Name for error messages

    Raises:
        HTTPException: If out of bounds
    """
    if not (0.0 <= value <= 1.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be between 0.0 and 1.0, got {value}",
        )
    return value


def validate_power(value: float, field_name: str, max_site_power: float) -> float:
    """Validate power value is non-negative and within site limits.

    Args:
        value: Power value in kW
        field_name: Name for error messages
        max_site_power: Maximum allowed power (kW)

    Raises:
        HTTPException: If invalid
    """
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be non-negative, got {value}",
        )
    if value > max_site_power:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} exceeds max site power ({max_site_power} kW)",
        )
    return value


def sanitize_sql_identifier(value: str) -> str:
    """Sanitize identifier for SQL queries (defense in depth).

    Only allows alphanumeric and underscore characters.
    Primary protection is parameterized queries.
    """
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid identifier format"
        )
    return value
