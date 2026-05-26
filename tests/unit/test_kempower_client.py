"""Unit tests for the Kempower ChargEye HTTP client.

`respx`-mocked endpoints. Covers JWT refresh on 401, pagination across
multiple pages, 429 back-off (with monkeypatched ``asyncio.sleep`` so
tests stay fast), 5xx retry, and the exhaustion path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.adapters.kempower import KempowerClient, KempowerClientError

_BASE = "https://api.chargeye.example"


@pytest.fixture(autouse=True)
def _patch_sleep(monkeypatch):
    """Replace ``asyncio.sleep`` in the shared base client module so retry
    waits don't actually block the test event loop. (The retry loop moved
    from kempower.client into src.adapters.rest_client.)"""
    import src.adapters.rest_client as rest_client_module

    monkeypatch.setattr(rest_client_module.asyncio, "sleep", AsyncMock())


@pytest.fixture
def client() -> KempowerClient:
    return KempowerClient(
        username="u",
        password="p",
        base_url=_BASE,
    )


def _route_login(mock, token: str = "token-1", status_code: int = 200):
    return mock.post(f"{_BASE}/auth/login").mock(
        return_value=httpx.Response(status_code, json={"accessToken": token})
    )


@pytest.mark.asyncio
async def test_auth_caches_token(client):
    with respx.mock(assert_all_called=False) as mock:
        login = _route_login(mock)
        mock.get(f"{_BASE}/locations/loc-1").mock(
            return_value=httpx.Response(200, json={"id": "loc-1", "name": "Vilnius"})
        )
        a = await client.get_location("loc-1")
        b = await client.get_location("loc-1")
        assert a["name"] == "Vilnius"
        assert b["name"] == "Vilnius"
        # Token endpoint hit exactly once across the two calls.
        assert login.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_refreshes_on_401(client):
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(f"{_BASE}/auth/login").mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "old"}),
                httpx.Response(200, json={"accessToken": "new"}),
            ]
        )
        mock.get(f"{_BASE}/locations/loc-1").mock(
            side_effect=[
                httpx.Response(401, text="expired"),
                httpx.Response(200, json={"id": "loc-1", "name": "X"}),
            ]
        )
        result = await client.get_location("loc-1")
        assert result["name"] == "X"
        assert login.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_response_missing_token_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_BASE}/auth/login").mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )
        with pytest.raises(KempowerClientError, match="accessToken"):
            await client.get_location("loc-1")
    await client.aclose()


@pytest.mark.asyncio
async def test_pagination_across_two_pages(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        page1 = {
            "items": [{"stationId": "s1"}, {"stationId": "s2"}],
            "nextPage": "cursor-a",
        }
        page2 = {
            "items": [{"stationId": "s3"}],
            "nextPage": None,
        }
        mock.get(f"{_BASE}/charging-stations").mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
            ]
        )
        seen: list[dict] = []
        async for item in client.iter_charging_stations("loc-1"):
            seen.append(item)
        assert [x["stationId"] for x in seen] == ["s1", "s2", "s3"]
    await client.aclose()


@pytest.mark.asyncio
async def test_429_backoff_then_succeeds(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        route = mock.get(f"{_BASE}/locations/x").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": "x"}),
            ]
        )
        result = await client.get_location("x")
        assert result["id"] == "x"
        assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retry(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        route = mock.get(f"{_BASE}/locations/x").mock(
            side_effect=[
                httpx.Response(503, text="bad gateway"),
                httpx.Response(200, json={"id": "x"}),
            ]
        )
        result = await client.get_location("x")
        assert result["id"] == "x"
        assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_400_does_not_retry(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        route = mock.get(f"{_BASE}/locations/x").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        with pytest.raises(KempowerClientError, match="HTTP 400"):
            await client.get_location("x")
        assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_exhaustion_raises(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        mock.get(f"{_BASE}/locations/x").mock(
            return_value=httpx.Response(503, text="down")
        )
        with pytest.raises(KempowerClientError, match="exhausted"):
            await client.get_location("x")
    await client.aclose()


@pytest.mark.asyncio
async def test_iter_transactions_passes_window_params(client):
    with respx.mock(assert_all_called=False) as mock:
        _route_login(mock)
        route = mock.get(f"{_BASE}/transactions").mock(
            return_value=httpx.Response(
                200, json={"items": [{"txId": "t1"}], "nextPage": None}
            )
        )
        seen = []
        async for tx in client.iter_transactions(
            station_id="s-1",
            start_iso="2025-01-01T00:00:00Z",
            end_iso="2025-01-02T00:00:00Z",
        ):
            seen.append(tx)
        assert seen == [{"txId": "t1"}]
        assert route.call_count == 1
        params = dict(route.calls.last.request.url.params)
        assert params["stationId"] == "s-1"
        assert params["from"] == "2025-01-01T00:00:00Z"
        assert params["to"] == "2025-01-02T00:00:00Z"
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_credentials_raises():
    with pytest.raises(KempowerClientError, match="credentials missing"):
        KempowerClient(username=None, password=None, base_url=_BASE)


@pytest.mark.asyncio
async def test_async_context_manager_closes_owned_client():
    async with KempowerClient(username="u", password="p", base_url=_BASE) as c:
        assert c is not None
    # Calling aclose again is a no-op; just verify no exception raised.
    await c.aclose()


@pytest.mark.asyncio
async def test_externally_owned_client_not_closed():
    external: Any = httpx.AsyncClient()
    c = KempowerClient(username="u", password="p", base_url=_BASE, client=external)
    await c.aclose()
    assert not external.is_closed
    await external.aclose()
