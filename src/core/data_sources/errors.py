"""Exception types for the data-source integration framework."""

from __future__ import annotations


class DataSourceError(RuntimeError):
    """Base class for data-source framework errors."""


class ProviderNotFound(DataSourceError):
    """Raised when a ``provider_key`` has no registered provider."""


class CredentialValidationError(DataSourceError):
    """Raised when a provider rejects the supplied credentials/config.

    The endpoint maps this to HTTP 422 so the user can correct the form.
    """


class ConnectionInUseError(DataSourceError):
    """Raised when a sync is enqueued while one is already pending/running.

    The endpoint maps this to HTTP 409. Enforced by the
    ``uq_dsij_one_active_per_conn`` partial unique index.
    """
