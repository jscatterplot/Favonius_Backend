"""Provider-agnostic external data-source integration framework.

Backs the website "Data Sources" page: a user connects an external system
(first provider: Kempower ChargEye), the backend stores the credentials
Fernet-encrypted, and a durable, schedulable ingestion runtime imports the
system's inventory + history into the depot.
"""

from .base import (
    CredentialField,
    DataSourceProvider,
    IngestionContext,
    IngestionResult,
    ProviderCatalogueEntry,
)
from .errors import (
    ConnectionInUseError,
    CredentialValidationError,
    DataSourceError,
    ProviderNotFound,
)
from .registry import get_provider, iter_catalogue, register

__all__ = [
    "CredentialField",
    "DataSourceProvider",
    "IngestionContext",
    "IngestionResult",
    "ProviderCatalogueEntry",
    "ConnectionInUseError",
    "CredentialValidationError",
    "DataSourceError",
    "ProviderNotFound",
    "get_provider",
    "iter_catalogue",
    "register",
]
