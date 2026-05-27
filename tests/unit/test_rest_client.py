"""Unit tests for the shared BaseRestClient (extracted from KempowerClient).

`respx`-mocked endpoints via a minimal concrete subclass. Covers token caching,
refresh on 401, pagination (including a subclass envelope override), 429 / 5xx
retry, the no-retry 4xx path, and retry exhaustion — the behaviour both the
Kempower and Navirec clients now inherit.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.adapters.rest_client import BaseRestClient, RestClientError

_BASE = "https://api.example.test"


@pytest.fixture(autouse=True)
def _patch_sleep(monkeypatch):
    import src.adapters.rest_client as rest_client_module

    monkeypatch.setattr(rest_client_module.asyncio, "sleep", AsyncMock())


class _FakeError(RestClientError):
    pass


class _FakeClient(BaseRestClient):
    def __init__(self, *, client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(
            base_url=_BASE,
            provider_name="Fake",
            error_cls=_FakeError,
            client=client,
        )

    async def _fetch_token(self) -> str:
        resp = await self._client.post(f"{self._base_url}/auth")
        if resp.status_code != 200:
            raise _FakeError(f"auth failed HTTP {resp.status_code}")
        token = resp.json().get("accessToken")
        if not token:
            raise _FakeError("auth response missing accessToken")
        return token

    async def get_thing(self, thing_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/things/{thing_id}")

    def iter_things(self) -> AsyncIterator[dict[str, Any]]:
        return self._iter_paginated("/things")


class _CursorClient(_FakeClient):
    """Subclass that overrides the pagination envelope (different field names)."""

    _PAGE_PARAM = "cursor"

    def _extract_items(self, body):
        return body.get("data") or []

    def _extract_next_cursor(self, body):
        return body.get("next")


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


def _route_auth(mock, token="tok-1", status_code=200):
    return mock.post(f"{_BASE}/auth").mock(
        return_value=httpx.Response(status_code, json={"accessToken": token})
    )


@pytest.mark.asyncio
async def test_token_cached_across_calls(client):
    with respx.mock(assert_all_called=False) as mock:
        auth = _route_auth(mock)
        mock.get(f"{_BASE}/things/1").mock(return_value=httpx.Response(200, json={"id": "1"}))
        await client.get_thing("1")
        await client.get_thing("1")
        assert auth.call_count == 1  # cached
    await client.aclose()


@pytest.mark.asyncio
async def test_refresh_on_401(client):
    with respx.mock(assert_all_called=False) as mock:
        auth = mock.post(f"{_BASE}/auth").mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "old"}),
                httpx.Response(200, json={"accessToken": "new"}),
            ]
        )
        mock.get(f"{_BASE}/things/1").mock(
            side_effect=[
                httpx.Response(401, text="expired"),
                httpx.Response(200, json={"id": "1"}),
            ]
        )
        result = await client.get_thing("1")
        assert result["id"] == "1"
        assert auth.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_missing_token_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_BASE}/auth").mock(return_value=httpx.Response(200, json={"nope": 1}))
        with pytest.raises(_FakeError, match="missing accessToken"):
            await client.get_thing("1")
    await client.aclose()


@pytest.mark.asyncio
async def test_pagination_default_envelope(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        mock.get(f"{_BASE}/things").mock(
            side_effect=[
                httpx.Response(200, json={"items": [{"n": 1}, {"n": 2}], "nextPage": "c"}),
                httpx.Response(200, json={"items": [{"n": 3}], "nextPage": None}),
            ]
        )
        seen = [x async for x in client.iter_things()]
        assert [x["n"] for x in seen] == [1, 2, 3]
    await client.aclose()


@pytest.mark.asyncio
async def test_pagination_overridden_envelope():
    c = _CursorClient()
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        route = mock.get(f"{_BASE}/things").mock(
            side_effect=[
                httpx.Response(200, json={"data": [{"n": 1}], "next": "page-2"}),
                httpx.Response(200, json={"data": [{"n": 2}], "next": None}),
            ]
        )
        seen = [x async for x in c.iter_things()]
        assert [x["n"] for x in seen] == [1, 2]
        # Second request carried the overridden ?cursor= param.
        assert dict(route.calls[1].request.url.params).get("cursor") == "page-2"
    await c.aclose()


@pytest.mark.asyncio
async def test_429_then_success(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        route = mock.get(f"{_BASE}/things/x").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": "x"}),
            ]
        )
        result = await client.get_thing("x")
        assert result["id"] == "x"
        assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_429_http_date_retry_after_then_success(client):
    # Retry-After may be an HTTP-date, not just seconds — must not crash.
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        route = mock.get(f"{_BASE}/things/x").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}),
                httpx.Response(200, json={"id": "x"}),
            ]
        )
        result = await client.get_thing("x")
        assert result["id"] == "x"
        assert route.call_count == 2
    await client.aclose()


def test_parse_retry_after_variants():
    from src.adapters.rest_client import _MAX_RETRY_AFTER_S, _parse_retry_after

    assert _parse_retry_after(None, 7.0) == 7.0
    assert _parse_retry_after("3", 7.0) == 3.0
    assert _parse_retry_after("garbage", 7.0) == 7.0  # malformed → default
    assert _parse_retry_after("Wed, 21 Oct 1999 07:28:00 GMT", 7.0) == 0.0  # past date → 0
    # Absurd delay-seconds and far-future dates are capped so sleep stays bounded.
    assert _parse_retry_after("100000", 7.0) == _MAX_RETRY_AFTER_S
    assert _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT", 7.0) == _MAX_RETRY_AFTER_S


@pytest.mark.asyncio
async def test_5xx_retry(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        route = mock.get(f"{_BASE}/things/x").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"id": "x"})]
        )
        result = await client.get_thing("x")
        assert result["id"] == "x"
        assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_204_no_content_returns_empty(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        mock.get(f"{_BASE}/things/x").mock(return_value=httpx.Response(204))
        result = await client.get_thing("x")
        assert result == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_2xx_with_body_is_success(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        mock.get(f"{_BASE}/things/x").mock(return_value=httpx.Response(201, json={"id": "x"}))
        result = await client.get_thing("x")
        assert result["id"] == "x"
    await client.aclose()


@pytest.mark.asyncio
async def test_4xx_no_retry_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        route = mock.get(f"{_BASE}/things/x").mock(return_value=httpx.Response(404, text="nope"))
        with pytest.raises(_FakeError, match="HTTP 404"):
            await client.get_thing("x")
        assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_exhaustion_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        mock.get(f"{_BASE}/things/x").mock(return_value=httpx.Response(503))
        with pytest.raises(_FakeError, match="exhausted"):
            await client.get_thing("x")
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_retries_then_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_auth(mock)
        mock.get(f"{_BASE}/things/x").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(_FakeError, match="exhausted"):
            await client.get_thing("x")
    await client.aclose()


@pytest.mark.asyncio
async def test_externally_owned_client_not_closed():
    external = httpx.AsyncClient()
    c = _FakeClient(client=external)
    await c.aclose()
    assert not external.is_closed
    await external.aclose()
