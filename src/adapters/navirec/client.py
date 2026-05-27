"""Navirec telematics REST API client.

Subclass of :class:`~src.adapters.rest_client.BaseRestClient`; reuses the
shared cached-bearer-auth + retry + pagination plumbing. Two auth modes are
supported so the integration works whether the account uses a static API key
or a username/password token exchange:

- ``NAVIREC_API_KEY`` — used directly as the bearer token (no exchange).
- ``NAVIREC_USERNAME`` / ``NAVIREC_PASSWORD`` — exchanged for a token.

The endpoint paths, pagination envelope, and response field names are
confirmed by ``scripts/probe_navirec_api.py`` against the live account. The
constants below carry the documented best-guess shapes; correcting them after
the probe is a localized edit. Field-name → Favonius translation lives in
``mapping.py``, never here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx

from ..rest_client import BaseRestClient, RestClientError

logger = logging.getLogger(__name__)

# VERIFY VIA PROBE — endpoint paths and auth route.
_AUTH_PATH = "/auth/login"
_VEHICLES_PATH = "/vehicles"
_HISTORY_PATH = "/vehicles/{vehicle_id}/history"


class NavirecClientError(RestClientError):
    """Raised on unrecoverable Navirec API errors."""


class NavirecClient(BaseRestClient):
    """Async client for the Navirec telematics REST API."""

    DEFAULT_BASE_URL = "https://api.navirec.com"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 30.0,
    ) -> None:
        """Resolve credentials + base URL from args/env and init the shared base client."""
        self._api_key = api_key or os.getenv("NAVIREC_API_KEY")
        self._username = username or os.getenv("NAVIREC_USERNAME")
        self._password = password or os.getenv("NAVIREC_PASSWORD")
        if not self._api_key and not (self._username and self._password):
            raise NavirecClientError(
                "Navirec credentials missing. Set NAVIREC_API_KEY, or "
                "NAVIREC_USERNAME and NAVIREC_PASSWORD."
            )
        resolved_base_url = base_url or os.getenv("NAVIREC_API_BASE_URL") or self.DEFAULT_BASE_URL
        super().__init__(
            base_url=resolved_base_url,
            provider_name="Navirec",
            error_cls=NavirecClientError,
            client=client,
            timeout_s=timeout_s,
        )

    # ------------------------------------------------------------------
    # Auth — provider hook
    # ------------------------------------------------------------------

    async def _fetch_token(self) -> str:
        """Return a bearer token: the static API key, or a fresh exchange."""
        if self._api_key:
            return self._api_key

        url = f"{self._base_url}{_AUTH_PATH}"
        resp = await self._client.post(
            url,
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise NavirecClientError(
                f"Navirec auth failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        token = body.get("accessToken") or body.get("token")
        if not token:
            raise NavirecClientError("Navirec auth response missing 'accessToken'/'token' field")
        return str(token)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def iter_vehicles(self) -> AsyncIterator[dict[str, Any]]:
        """Iterate account-wide vehicle objects (each carrying latest SoC/position).

        One paginated call per poll cycle — the live feed's single API round.
        """
        return self._iter_paginated(_VEHICLES_PATH)

    def iter_vehicle_history(
        self,
        *,
        vehicle_id: str,
        start_iso: str,
        end_iso: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate historical readings for one vehicle within a window (backfill)."""
        return self._iter_paginated(
            _HISTORY_PATH.format(vehicle_id=vehicle_id),
            params={"from": start_iso, "to": end_iso},
        )
