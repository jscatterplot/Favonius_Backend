"""Kempower ChargEye REST API client.

A thin :class:`~src.adapters.rest_client.BaseRestClient` subclass that handles
JWT bearer auth (cached, refreshed on 401 or T-5min expiry), pagination, and
the small amount of retry / back-off needed against the public ChargEye API.

Public surface is the iterators / fetchers the CLI consumes:

- :meth:`iter_charging_stations`
- :meth:`iter_vehicles`
- :meth:`iter_transactions`
- :meth:`get_location`
- :meth:`get_power_group`

Field names follow the ChargEye reference (``stationId``, ``maxPowerKw``,
``netBatterySizeKwh``, …). Translation to Favonius shapes happens in
``mapping.py``, not here. The shared auth / retry / pagination plumbing lives
in :mod:`src.adapters.rest_client`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx

from ..rest_client import BaseRestClient, RestClientError

logger = logging.getLogger(__name__)


class KempowerClientError(RestClientError):
    """Raised on unrecoverable Kempower API errors."""


class KempowerClient(BaseRestClient):
    """Async client for the Kempower ChargEye REST API."""

    DEFAULT_BASE_URL = "https://api.chargeye.com"

    def __init__(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Validate credentials, resolve the base URL, and init the shared base client."""
        self._username = username or os.getenv("KEMPOWER_USERNAME")
        self._password = password or os.getenv("KEMPOWER_PASSWORD")
        if not self._username or not self._password:
            raise KempowerClientError(
                "Kempower credentials missing. Set KEMPOWER_USERNAME and "
                "KEMPOWER_PASSWORD, or pass them directly to KempowerClient."
            )
        resolved_base_url = base_url or os.getenv("KEMPOWER_API_BASE_URL") or self.DEFAULT_BASE_URL
        super().__init__(
            base_url=resolved_base_url,
            provider_name="Kempower",
            error_cls=KempowerClientError,
            client=client,
            timeout_s=timeout_s,
        )

    # ------------------------------------------------------------------
    # Auth — provider hook
    # ------------------------------------------------------------------

    async def _fetch_token(self) -> str:
        """Exchange username/password for a ChargEye JWT."""
        url = f"{self._base_url}/auth/login"
        resp = await self._client.post(
            url,
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise KempowerClientError(
                f"Kempower auth failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        token = body.get("accessToken") or body.get("token")
        if not token:
            raise KempowerClientError("Kempower auth response missing 'accessToken'/'token' field")
        return str(token)

    # ------------------------------------------------------------------
    # Public surface — only what the CLI consumes
    # ------------------------------------------------------------------

    async def get_location(self, location_id: str) -> dict[str, Any]:
        """Fetch one Location object by Kempower id."""
        return await self._request("GET", f"/locations/{location_id}")

    async def get_power_group(self, group_id: str) -> dict[str, Any]:
        """Fetch one Power Group (load-balancing) object by Kempower id."""
        return await self._request("GET", f"/power-groups/{group_id}")

    def iter_charging_stations(self, location_id: str) -> AsyncIterator[dict[str, Any]]:
        """Iterate ChargingStation objects scoped to a Location."""
        return self._iter_paginated(
            "/charging-stations",
            params={"locationId": location_id},
        )

    def iter_vehicles(self, location_id: str) -> AsyncIterator[dict[str, Any]]:
        """Iterate Vehicle objects scoped to a Location."""
        return self._iter_paginated(
            "/vehicles",
            params={"locationId": location_id},
        )

    def iter_transactions(
        self,
        *,
        station_id: str,
        start_iso: str,
        end_iso: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate Transaction objects for a station within a time window."""
        return self._iter_paginated(
            "/transactions",
            params={
                "stationId": station_id,
                "from": start_iso,
                "to": end_iso,
            },
        )
