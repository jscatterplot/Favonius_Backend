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


<<<<<<< claude/gracious-brahmagupta-3MvPy
# Kempower IAM endpoint: exchange a permanent refresh token for a short-lived
# access JWT.  This is a platform-level URL, separate from the main API base.
_REFRESH_TOKEN_URL = "https://kempower.io/api/auth/refreshAccessToken"

# Retry policy. ENTSO-E uses the same shape (see ``src/adapters/entsoe/``).
_MAX_RETRIES = 4
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)
=======
class KempowerClientError(RestClientError):
    """Raised on unrecoverable Kempower API errors."""
>>>>>>> main


class KempowerClient(BaseRestClient):
    """Async client for the Kempower ChargEye REST API."""

    DEFAULT_BASE_URL = "https://api.chargeye.com"

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
<<<<<<< claude/gracious-brahmagupta-3MvPy
        self._refresh_token = refresh_token or os.getenv("KEMPOWER_REFRESH_TOKEN")
=======
        """Validate credentials, resolve the base URL, and init the shared base client."""
>>>>>>> main
        self._username = username or os.getenv("KEMPOWER_USERNAME")
        self._password = password or os.getenv("KEMPOWER_PASSWORD")
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

<<<<<<< claude/gracious-brahmagupta-3MvPy
    async def _ensure_token(self, *, force: bool = False) -> str:
        """Return a non-expired JWT, refreshing if needed."""
        now = time.monotonic()
        if (
            not force
            and self._token
            and (now - self._token_acquired_at)
            < (_TOKEN_EXPIRY_S - _TOKEN_REFRESH_BUFFER_S)
        ):
            return self._token

        token = (
            await self._acquire_token_from_refresh()
            if self._refresh_token
            else await self._acquire_token_from_password()
        )
        self._token = token
        self._token_acquired_at = now
        logger.debug("Kempower auth refreshed (token len=%d)", len(token))
        return token

    async def _acquire_token_from_refresh(self) -> str:
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
        return token

    async def _acquire_token_from_password(self) -> str:
        """Exchange username + password for a short-lived access JWT."""
=======
    async def _fetch_token(self) -> str:
        """Exchange username/password for a ChargEye JWT."""
        url = f"{self._base_url}/auth/login"
>>>>>>> main
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
<<<<<<< claude/gracious-brahmagupta-3MvPy
            raise KempowerClientError(
                "Kempower auth response missing 'accessToken'/'token' field"
            )
        return token

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        _allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """Issue one authenticated request with retry / refresh handling."""
        url = f"{self._base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            token = await self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = await self._client.request(method, url, params=params, headers=headers)
            except httpx.HTTPError as exc:  # connect / read / timeout
                last_exc = exc
                logger.warning(
                    "Kempower %s %s transport error (attempt %d/%d): %s",
                    method, path, attempt + 1, _MAX_RETRIES, exc,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401 and _allow_refresh:
                logger.info("Kempower 401 — forcing token refresh")
                await self._ensure_token(force=True)
                _allow_refresh = False  # one refresh per call
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", _RETRY_BACKOFF_S[attempt]))
                logger.warning(
                    "Kempower 429 — backing off %.1fs (attempt %d/%d)",
                    retry_after, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600:
                logger.warning(
                    "Kempower %s %s 5xx (attempt %d/%d): HTTP %d",
                    method, path, attempt + 1, _MAX_RETRIES, resp.status_code,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
                continue

            # 4xx other than 401/429 — surface immediately, retry won't help.
            raise KempowerClientError(
                f"Kempower {method} {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )

        raise KempowerClientError(
            f"Kempower {method} {path} exhausted {_MAX_RETRIES} retries"
        ) from last_exc

    async def _iter_paginated(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate items across pages.

        Kempower's pagination shape is documented as
        ``{"items": [...], "nextPage": "<cursor>"}``. We pass the cursor
        back as ``page`` until it's absent / empty.
        """
        cursor: Optional[str] = None
        params = dict(params or {})
        params.setdefault("pageSize", page_size)
        while True:
            request_params = dict(params)
            if cursor:
                request_params["page"] = cursor
            body = await self._request("GET", path, params=request_params)
            items = body.get("items") or []
            for item in items:
                yield item
            cursor = body.get("nextPage")
            if not cursor:
                break
=======
            raise KempowerClientError("Kempower auth response missing 'accessToken'/'token' field")
        return str(token)
>>>>>>> main

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
