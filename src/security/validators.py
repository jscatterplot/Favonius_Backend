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


def validate_uuid(value: str, field_name: str = "id") -> str:
    """Validate UUID format and return the canonical string.

    Accepts any UUID variant (not just v4) and enforces the canonical
    hyphenated lowercase form to prevent encoding inconsistencies.

    Args:
        value: String to validate
        field_name: Name for error messages

    Raises:
        HTTPException: If invalid UUID format
    """
    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("Non-canonical UUID format")
        return value
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: must be valid UUID format, got: {value}",
        )


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


def validate_depot_id(depot_id: str) -> str:
    """Validate depot_id is a valid UUID.

    Args:
        depot_id: Depot identifier to validate

    Returns:
        Validated depot_id string

    Raises:
        HTTPException 400: If depot_id is not a valid UUID
    """
    return validate_uuid(depot_id, "depot_id")


def validate_vehicle_id(vehicle_id: str) -> str:
    """Validate vehicle_id is a valid UUID.

    Args:
        vehicle_id: Vehicle identifier to validate

    Returns:
        Validated vehicle_id string

    Raises:
        HTTPException 400: If vehicle_id is not a valid UUID
    """
    return validate_uuid(vehicle_id, "vehicle_id")


def validate_horizon_hours(horizon_hours: int) -> int:
    """Validate horizon_hours is in the range [1, 48].

    Args:
        horizon_hours: Optimization horizon in hours

    Returns:
        Validated horizon_hours

    Raises:
        HTTPException 400: If out of range
    """
    if not (1 <= horizon_hours <= 48):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"horizon_hours must be between 1 and 48, got: {horizon_hours}",
        )
    return horizon_hours


def validate_timestamp_range(start: str, end: str, field_prefix: str = "") -> None:
    """Validate that start is before end (ISO 8601 strings).

    Args:
        start: Start timestamp (ISO 8601)
        end: End timestamp (ISO 8601)
        field_prefix: Prefix for field names in error messages

    Raises:
        HTTPException 422: If start >= end
    """
    from datetime import datetime

    try:
        t_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid timestamp format: {e}",
        )
    if t_start >= t_end:
        prefix = f"{field_prefix}_" if field_prefix else ""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}start must be before {prefix}end",
        )


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
