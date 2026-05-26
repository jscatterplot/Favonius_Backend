"""Shared async REST client base for third-party telematics / charging APIs.

Extracted from the original ``KempowerClient`` so the Navirec telematics
adapter can reuse the same hardened plumbing: cached bearer-token auth
(refreshed on 401 or near-expiry), cursor pagination, and retry / back-off
for 429 and 5xx responses.

Subclasses implement the provider-specific bits via template-method hooks:

- :meth:`_fetch_token` — perform the auth exchange, return a bearer token.
- :meth:`_extract_items` / :meth:`_extract_next_cursor` — adapt the provider's
  pagination envelope (defaults match the ``{"items": [...], "nextPage": ...}``
  shape ChargEye uses).

Everything else — the retry loop, token caching, HTTP client ownership — is
shared. Field-name translation to Favonius shapes happens in each provider's
``mapping.py``, never here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


def _parse_retry_after(value: Optional[str], default: float) -> float:
    """Parse a 429 ``Retry-After`` header into seconds.

    HTTP permits either delay-seconds or an HTTP-date; fall back to ``default``
    when the header is absent or unparseable so a rate-limit response stays
    recoverable (retry with backoff) instead of crashing the request.
    """
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class RestClientError(RuntimeError):
    """Raised on unrecoverable REST API errors.

    Recoverable failures (429, 5xx, transport errors) are retried in-process;
    this is only raised when retries are exhausted, auth fails, or the response
    shape is malformed. Provider clients subclass this so callers can catch a
    provider-specific type while sharing the base behaviour.
    """


# Retry policy. ENTSO-E and Kempower share this shape.
_MAX_RETRIES = 4
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)

# Default bearer-token lifetime assumptions (ChargEye documents 8h). Subclasses
# pass their own via the constructor when the provider differs.
_DEFAULT_TOKEN_EXPIRY_S = 8 * 60 * 60
_DEFAULT_TOKEN_REFRESH_BUFFER_S = 5 * 60


class BaseRestClient:
    """Async base for authenticated, paginated, retrying REST clients."""

    # Pagination envelope knobs — override in a subclass if the provider differs.
    _PAGE_PARAM = "page"
    _PAGE_SIZE_PARAM = "pageSize"

    def __init__(
        self,
        *,
        base_url: str,
        provider_name: str,
        error_cls: type[RestClientError] = RestClientError,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 30.0,
        token_expiry_s: float = _DEFAULT_TOKEN_EXPIRY_S,
        token_refresh_buffer_s: float = _DEFAULT_TOKEN_REFRESH_BUFFER_S,
    ) -> None:
        """Store config and lazily create an owned httpx client when none is passed."""
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name
        self._error_cls = error_cls
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._token: Optional[str] = None
        self._token_acquired_at: float = 0.0
        self._token_expiry_s = token_expiry_s
        self._token_refresh_buffer_s = token_refresh_buffer_s

    async def __aenter__(self) -> "BaseRestClient":
        """Enter the async context, returning self."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Close the owned HTTP client on context exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client (only when this instance owns it)."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Auth — subclass hook
    # ------------------------------------------------------------------

    async def _fetch_token(self) -> str:
        """Perform the provider auth exchange and return a bearer token.

        Subclasses MUST override. Should raise ``self._error_cls`` on failure.
        """
        raise NotImplementedError

    async def _ensure_token(self, *, force: bool = False) -> str:
        """Return a non-expired bearer token, refreshing if needed."""
        now = time.monotonic()
        if (
            not force
            and self._token
            and (now - self._token_acquired_at)
            < (self._token_expiry_s - self._token_refresh_buffer_s)
        ):
            return self._token

        token = await self._fetch_token()
        self._token = token
        self._token_acquired_at = now
        logger.debug("%s auth refreshed (token len=%d)", self._provider_name, len(token))
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
                    "%s %s %s transport error (attempt %d/%d): %s",
                    self._provider_name,
                    method,
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
                continue

            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return data

            if resp.status_code == 401 and _allow_refresh:
                logger.info("%s 401 — forcing token refresh", self._provider_name)
                await self._ensure_token(force=True)
                _allow_refresh = False  # one refresh per call
                continue

            if resp.status_code == 429:
                retry_after = _parse_retry_after(
                    resp.headers.get("Retry-After"), _RETRY_BACKOFF_S[attempt]
                )
                logger.warning(
                    "%s 429 — backing off %.1fs (attempt %d/%d)",
                    self._provider_name,
                    retry_after,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600:
                logger.warning(
                    "%s %s %s 5xx (attempt %d/%d): HTTP %d",
                    self._provider_name,
                    method,
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                    resp.status_code,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
                continue

            # 4xx other than 401/429 — surface immediately, retry won't help.
            raise self._error_cls(
                f"{self._provider_name} {method} {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )

        raise self._error_cls(
            f"{self._provider_name} {method} {path} exhausted {_MAX_RETRIES} retries"
        ) from last_exc

    # ------------------------------------------------------------------
    # Pagination — subclass hooks for the envelope shape
    # ------------------------------------------------------------------

    def _extract_items(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the page's items out of the response envelope."""
        return body.get("items") or []

    def _extract_next_cursor(self, body: dict[str, Any]) -> Optional[str]:
        """Pull the next-page cursor out of the response envelope (None = end)."""
        return body.get("nextPage")

    async def _iter_paginated(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate items across pages, following the next-page cursor."""
        cursor: Optional[str] = None
        params = dict(params or {})
        params.setdefault(self._PAGE_SIZE_PARAM, page_size)
        while True:
            request_params = dict(params)
            if cursor:
                request_params[self._PAGE_PARAM] = cursor
            body = await self._request("GET", path, params=request_params)
            for item in self._extract_items(body):
                yield item
            cursor = self._extract_next_cursor(body)
            if not cursor:
                break
