"""Provider registry — the single lookup for ``provider_key`` → provider.

Adding a new data source is a drop-in: implement :class:`DataSourceProvider`
and call :func:`register` (or add it to the bootstrap block below).
"""

from __future__ import annotations

from .base import DataSourceProvider, ProviderCatalogueEntry
from .errors import ProviderNotFound
from .kempower_provider import KempowerProvider

_PROVIDERS: dict[str, DataSourceProvider] = {}


def register(provider: DataSourceProvider) -> None:
    """Register (or replace) a provider keyed by its ``provider_key``."""
    _PROVIDERS[provider.provider_key] = provider


def get_provider(provider_key: str) -> DataSourceProvider:
    """Return the provider for ``provider_key``.

    Raises:
        ProviderNotFound: If no provider is registered under that key.
    """
    try:
        return _PROVIDERS[provider_key]
    except KeyError as exc:
        raise ProviderNotFound(f"unknown data source provider: {provider_key!r}") from exc


def iter_catalogue() -> list[ProviderCatalogueEntry]:
    """Return catalogue entries for every registered provider (stable order)."""
    return [_PROVIDERS[key].catalogue_entry() for key in sorted(_PROVIDERS)]


# Bootstrap the built-in providers.
register(KempowerProvider())
