"""Database-layer exceptions.

Application code should raise these (instead of bare ``Exception`` or letting
``asyncpg.PostgresError`` propagate with raw error text) so the global
exception handlers can map them to stable, sanitized API responses.

The ``code`` attribute is one of :class:`src.api.error_codes.ErrorCode` and
drives both the public ``error_code`` field on the response and the HTTP
status code.
"""

from __future__ import annotations

from ..api.error_codes import ErrorCode


class DatabaseError(Exception):
    """Raised when a database operation fails.

    The original exception (if any) is preserved via ``__cause__`` (use
    ``raise DatabaseError(...) from exc``) so handlers can log full context
    without leaking the raw error string in the API response.
    """

    code: ErrorCode = ErrorCode.DATABASE_ERROR

    def __init__(self, message: str = "Database operation failed") -> None:
        super().__init__(message)
        self.message = message


class IdempotencyKeyReusedError(DatabaseError):
    """Raised when an idempotency key collides with a different prior request."""

    code = ErrorCode.IDEMPOTENCY_KEY_REUSED

    def __init__(self, message: str = "Idempotency key has already been used") -> None:
        super().__init__(message)


class StaticDbUnavailableError(DatabaseError):
    """Raised when the Supabase (static-data) pool is absent or unreachable.

    Distinct from ``DatabaseError`` so API clients can distinguish a transient
    Supabase connectivity blip (STATIC_DB_UNAVAILABLE, HTTP 503) from a
    TimescaleDB write failure (DATABASE_ERROR).
    """

    code = ErrorCode.STATIC_DB_UNAVAILABLE

    def __init__(self, message: str = "Depot access check temporarily unavailable") -> None:
        super().__init__(message)
