"""Stable error codes and their sanitized response messages.

Centralizes the mapping of internal exceptions to stable, public-safe error
codes and human-readable strings. Handlers MUST use the message text from
``ERROR_MESSAGES`` rather than ``str(exc)`` so that DB internals (SQL,
table/column names, query fragments), filesystem paths, or stack details
never leak through the API.

Usage::

    from .error_codes import ErrorCode, ERROR_MESSAGES, http_status_for

    code = ErrorCode.DATABASE_ERROR
    payload = {
        "error_code": code,
        "detail": ERROR_MESSAGES[code],
        "request_id": request_id,
    }
"""

from __future__ import annotations

from enum import Enum

from fastapi import status


class ErrorCode(str, Enum):
    """Public, stable error codes returned to API clients.

    These values are part of the public contract — clients may key off them
    for programmatic error handling. Renaming or removing a value is a
    breaking change.
    """

    INTERNAL_ERROR = "INTERNAL_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    DEPOT_NOT_FOUND = "DEPOT_NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    UNAUTHORIZED = "UNAUTHORIZED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    OPTIMIZATION_ERROR = "OPTIMIZATION_ERROR"
    OPTIMIZER_INFEASIBLE = "OPTIMIZER_INFEASIBLE"
    OPTIMIZER_TIMEOUT = "OPTIMIZER_TIMEOUT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    BAD_REQUEST = "BAD_REQUEST"
    CONFLICT = "CONFLICT"
    UNPROCESSABLE_ENTITY = "UNPROCESSABLE_ENTITY"
    DUPLICATE_ID_TAG = "DUPLICATE_ID_TAG"
    DUPLICATE_VIN = "DUPLICATE_VIN"
    DUPLICATE_EXTERNAL_ID = "DUPLICATE_EXTERNAL_ID"
    DUPLICATE_SESSION = "DUPLICATE_SESSION"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_STATUS = "INVALID_STATUS"
    INVALID_ENERGY = "INVALID_ENERGY"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    STATIC_DB_UNAVAILABLE = "STATIC_DB_UNAVAILABLE"


ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INTERNAL_ERROR: "An internal server error occurred",
    ErrorCode.DATABASE_ERROR: "A database error occurred",
    ErrorCode.VALIDATION_ERROR: "Request validation failed",
    ErrorCode.INVALID_INPUT: "Invalid input",
    ErrorCode.RATE_LIMIT_EXCEEDED: "Rate limit exceeded",
    ErrorCode.NOT_FOUND: "Resource not found",
    ErrorCode.DEPOT_NOT_FOUND: "Depot not found",
    ErrorCode.FORBIDDEN: "Access denied",
    ErrorCode.UNAUTHORIZED: "Authentication required",
    ErrorCode.IDEMPOTENCY_KEY_REUSED: "Idempotency key has already been used",
    ErrorCode.OPTIMIZATION_ERROR: "Optimization failed",
    ErrorCode.OPTIMIZER_INFEASIBLE: "Optimization model is infeasible",
    ErrorCode.OPTIMIZER_TIMEOUT: "Optimization solver exceeded time limit",
    ErrorCode.SERVICE_UNAVAILABLE: "Service temporarily unavailable",
    ErrorCode.BAD_REQUEST: "Bad request",
    ErrorCode.CONFLICT: "Resource conflict",
    ErrorCode.UNPROCESSABLE_ENTITY: "Unprocessable entity",
    ErrorCode.DUPLICATE_ID_TAG: "idTag is already registered",
    ErrorCode.DUPLICATE_VIN: "VIN is already registered",
    ErrorCode.DUPLICATE_EXTERNAL_ID: "External identifier is already registered",
    ErrorCode.DUPLICATE_SESSION: "Charging session has already been imported",
    ErrorCode.INVALID_TIMESTAMP: "Invalid timestamp format",
    ErrorCode.INVALID_STATUS: "Invalid charge status value",
    ErrorCode.INVALID_ENERGY: "Invalid energy value",
    ErrorCode.MISSING_REQUIRED_FIELD: "Required field is missing",
    ErrorCode.STATIC_DB_UNAVAILABLE: "Depot access check temporarily unavailable",
}


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.DATABASE_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.VALIDATION_ERROR: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_INPUT: status.HTTP_400_BAD_REQUEST,
    ErrorCode.RATE_LIMIT_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.DEPOT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: status.HTTP_409_CONFLICT,
    ErrorCode.OPTIMIZATION_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.OPTIMIZER_INFEASIBLE: 422,
    ErrorCode.OPTIMIZER_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    ErrorCode.SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.BAD_REQUEST: status.HTTP_400_BAD_REQUEST,
    ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.UNPROCESSABLE_ENTITY: 422,
    ErrorCode.DUPLICATE_ID_TAG: status.HTTP_409_CONFLICT,
    ErrorCode.DUPLICATE_VIN: status.HTTP_409_CONFLICT,
    ErrorCode.DUPLICATE_EXTERNAL_ID: status.HTTP_409_CONFLICT,
    ErrorCode.DUPLICATE_SESSION: status.HTTP_409_CONFLICT,
    ErrorCode.INVALID_TIMESTAMP: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_STATUS: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_ENERGY: status.HTTP_400_BAD_REQUEST,
    ErrorCode.MISSING_REQUIRED_FIELD: status.HTTP_400_BAD_REQUEST,
    ErrorCode.STATIC_DB_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def http_status_for(code: ErrorCode) -> int:
    """Return the default HTTP status code for a given error code."""
    return _HTTP_STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)


def safe_message_for(code: ErrorCode) -> str:
    """Return the sanitized public-safe message for a given error code."""
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR])
