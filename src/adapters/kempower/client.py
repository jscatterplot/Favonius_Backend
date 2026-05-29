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

# Kempower IAM endpoint: exchange a permanent refresh token for a short-lived
# access JWT.  This is a platform-level URL, separate from the main API base.
_REFRESH_TOKEN_URL = "https://kempower.io/api/auth/refreshAccessToken"


class KempowerClientError(RestClientError):
    """Raised on unrecoverable Kempower API errors."""


class KempowerClient(BaseRestClient):
    """Async client for the Kempower ChargEye REST API."""

    # ChargEye's REST API shares the kempower.io host with the auth endpoint
    # above (``…/api/auth/refreshAccessToken``); resource paths hang off ``/api``.
    # (The old ``api.chargeye.com`` default does not resolve in public DNS.)
    DEFAULT_BASE_URL = "https://kempower.io/api"

    def __init__(
        self,
        *,
        refresh_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Validate credentials, resolve the base URL, and init the shared base client."""
        # Env-var fallbacks only apply when the caller passed no credentials at
        # all (CLI / default-deployment usage). Explicit constructor args always
        # win completely — mixing e.g. username+password from args with a
        # KEMPOWER_REFRESH_TOKEN from the environment would silently use the
        # wrong auth path.
        if refresh_token is None and username is None and password is None:
            self._refresh_token = os.getenv("KEMPOWER_REFRESH_TOKEN") or None
            self._username = os.getenv("KEMPOWER_USERNAME")
            self._password = os.getenv("KEMPOWER_PASSWORD")
        else:
            self._refresh_token = refresh_token
            self._username = username
            self._password = password
        if not self._refresh_token and not (self._username and self._password):
            raise KempowerClientError(
                "Kempower credentials missing. Provide a refresh_token, or both "
                "username and password (or set KEMPOWER_REFRESH_TOKEN / "
                "KEMPOWER_USERNAME + KEMPOWER_PASSWORD)."
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
        """Obtain a short-lived access JWT using whichever credential was provided.

        Refresh token path: GET the Kempower IAM endpoint with the permanent
        refresh token as Bearer — no username/password needed.
        Password path: POST /auth/login with username + password.
        """
        if self._refresh_token:
            return await self._fetch_token_from_refresh()
        return await self._fetch_token_from_password()

    async def _fetch_token_from_refresh(self) -> str:
        """Exchange the permanent refresh token for a short-lived access JWT."""
        resp = await self._client.get(
            _REFRESH_TOKEN_URL,
            headers={"Authorization": f"Bearer {self._refresh_token}"},
        )
        if resp.status_code != 200:
            raise KempowerClientError(
                f"Kempower token refresh failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        token = body.get("accessToken") or body.get("token")
        if not token:
            raise KempowerClientError(
                "Kempower token refresh response missing 'accessToken'/'token' field"
            )
        return str(token)

    async def _fetch_token_from_password(self) -> str:
        """Exchange username + password for a short-lived access JWT."""
        resp = await self._client.post(
            f"{self._base_url}/auth/login",
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

    # ChargEye's list endpoints, per the official OpenAPI specs at
    # docs.kempower.io: stations + vehicles are unversioned bare resources and
    # take ``locationUid`` (the system-internal location id; ``locationId`` is
    # a *customer-supplied* reference field, NOT the right query param name).
    # Transactions are scoped under their charging station and paginate with
    # DynamoDB-style ``exclusiveStartKey`` / ``lastEvaluatedKey`` instead of a
    # generic ``{items, nextPage}`` envelope, so we drive iteration directly
    # rather than going through BaseRestClient._iter_paginated.
    async def iter_charging_stations(self, location_id: str) -> AsyncIterator[dict[str, Any]]:
        """Iterate ChargingStation objects scoped to a Location."""
        body = await self._request("GET", "/stations", params={"locationUid": location_id})
        for item in body.get("stations") or []:
            yield item

    async def iter_vehicles(self, location_id: str) -> AsyncIterator[dict[str, Any]]:
        """Iterate Vehicle objects scoped to a Location."""
        body = await self._request("GET", "/vehicles", params={"locationUid": location_id})
        for item in body.get("vehicles") or []:
            yield item

    async def iter_transactions(
        self,
        *,
        station_id: str,
        start_iso: str,
        end_iso: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate Transaction objects for a station within a time window."""
        path = f"/chargingStations/{station_id}/transactions"
        params: dict[str, Any] = {"startDate": start_iso, "endDate": end_iso}
        while True:
            body = await self._request("GET", path, params=params)
            for item in body.get("transactions") or []:
                yield item
            cursor = body.get("lastEvaluatedKey")
            if not cursor:
                break
            params["exclusiveStartKey"] = cursor
