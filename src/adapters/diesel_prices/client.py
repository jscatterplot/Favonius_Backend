"""Diesel wholesale-price REST client (source-agnostic).

Subclass of :class:`~src.adapters.rest_client.BaseRestClient`. The concrete
source is selected by ``DIESEL_PRICE_SOURCE``:

- ``eu_oil_bulletin`` (default) — the European Commission's Weekly Oil
  Bulletin open data. Free, no key; ``_fetch_token`` returns ``""`` (the base
  client then sends an empty bearer, which the open endpoint ignores).
- ``fuel_prices_eu`` — fuel-prices.eu JSON REST API (same underlying bulletin,
  cleaner envelope). Needs ``DIESEL_PRICE_API_KEY``.
- ``tankerkonig`` — Tankerkönig (Germany only, live station prices). Needs a
  free ``DIESEL_PRICE_API_KEY``.

The endpoint paths, pagination envelope, and field names are PLACEHOLDERS
confirmed by ``scripts/probe_diesel_prices.py`` against the chosen source —
correcting them is a localized edit to the constants here + the ``_*_KEYS`` in
``mapping.py``. Field translation lives in ``mapping.py``, never here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx

from ..rest_client import BaseRestClient, RestClientError

logger = logging.getLogger(__name__)

# Known source keys → (default base URL, latest-prices path). VERIFY VIA PROBE.
_SOURCE_DEFAULTS: dict[str, tuple[str, str]] = {
    "eu_oil_bulletin": ("https://energy.ec.europa.eu/api", "/oil-bulletin/prices/latest"),
    "fuel_prices_eu": ("https://www.fuel-prices.eu/api", "/v1/prices/latest"),
    "tankerkonig": ("https://creativecommons.tankerkoenig.de", "/json/prices.php"),
}

DEFAULT_SOURCE = "eu_oil_bulletin"


class DieselPriceClientError(RestClientError):
    """Raised on unrecoverable diesel-price API errors."""


def configured_source() -> str:
    """Return the configured ``DIESEL_PRICE_SOURCE`` (falling back to default)."""
    return (os.getenv("DIESEL_PRICE_SOURCE") or DEFAULT_SOURCE).strip().lower()


class DieselPriceClient(BaseRestClient):
    """Async client for a wholesale diesel-price source."""

    def __init__(
        self,
        *,
        source: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Resolve source + credentials + base URL from args/env."""
        self.source = (source or configured_source()).strip().lower()
        default_base, default_path = _SOURCE_DEFAULTS.get(
            self.source, _SOURCE_DEFAULTS[DEFAULT_SOURCE]
        )
        self._prices_path = default_path
        self._api_key = api_key or os.getenv("DIESEL_PRICE_API_KEY")
        resolved_base_url = base_url or os.getenv("DIESEL_PRICE_API_BASE_URL") or default_base
        super().__init__(
            base_url=resolved_base_url,
            provider_name=f"DieselPrice[{self.source}]",
            error_cls=DieselPriceClientError,
            client=client,
            timeout_s=timeout_s,
        )

    async def _fetch_token(self) -> str:
        """Return the bearer token.

        The EU Oil Bulletin open endpoint needs no auth, so an empty string is
        returned (the base client sends ``Authorization: Bearer `` which the
        open API ignores). Keyed sources return the configured API key directly
        — none of the supported diesel sources use a token-exchange handshake.
        """
        return self._api_key or ""

    def iter_prices(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate the latest per-country wholesale diesel price records.

        One paginated call per poll cycle — the diesel feed's single API round,
        mirroring ``NavirecClient.iter_vehicles``.
        """
        return self._iter_paginated(self._prices_path)
