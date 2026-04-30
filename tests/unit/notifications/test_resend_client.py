"""Unit tests for src.notifications.resend_client.

Uses httpx.MockTransport so no real HTTP is performed. Asserts:
  - request shape (URL, auth header, payload)
  - 2xx success extracts provider_message_id
  - 4xx fails without retry
  - 5xx retries, then fails
  - transport errors retry, then fail
"""

from __future__ import annotations

import json
from typing import Callable
from unittest.mock import patch

import httpx
import pytest

from src.notifications.email_client import EmailMessage
from src.notifications.resend_client import ResendEmailClient


def _make_message(**overrides) -> EmailMessage:
    defaults = dict(
        to="ops@example.com",
        subject="[Favonius] critical: Charger faulted",
        html="<p>fault</p>",
        text="fault",
        from_address="alerts@favonius.energy",
        headers={"X-Alert-Id": "abc"},
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


def _client_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs,
) -> ResendEmailClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    kwargs.setdefault("initial_backoff_s", 0.001)
    return ResendEmailClient(
        api_key="rk_test_123",
        default_from="alerts@favonius.energy",
        http_client=http_client,
        **kwargs,
    )


class TestConstructor:
    def test_empty_api_key_raises(self):
        with pytest.raises(ValueError, match="API key"):
            ResendEmailClient(api_key="", default_from="x@y.com")

    def test_empty_from_raises(self):
        with pytest.raises(ValueError, match="default_from"):
            ResendEmailClient(api_key="rk_x", default_from="")


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_posts_to_resend_emails(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "msg_abc123"})

        client = _client_with_handler(handler)
        await client.send(_make_message())

        assert len(captured) == 1
        req = captured[0]
        assert str(req.url) == "https://api.resend.com/emails"
        assert req.method == "POST"
        assert req.headers["Authorization"] == "Bearer rk_test_123"
        assert req.headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_payload_includes_required_fields(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "ok"})

        client = _client_with_handler(handler)
        await client.send(_make_message())

        assert captured["from"] == "alerts@favonius.energy"
        assert captured["to"] == ["ops@example.com"]
        assert captured["subject"].startswith("[Favonius]")
        assert "<p>" in captured["html"]
        assert captured["text"] == "fault"
        assert captured["headers"] == {"X-Alert-Id": "abc"}

    @pytest.mark.asyncio
    async def test_omits_headers_when_empty(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "ok"})

        client = _client_with_handler(handler)
        await client.send(_make_message(headers={}))
        assert "headers" not in captured


class TestSuccessPath:
    @pytest.mark.asyncio
    async def test_2xx_returns_sent_with_message_id(self):
        def handler(_request):
            return httpx.Response(200, json={"id": "msg_123"})

        client = _client_with_handler(handler)
        result = await client.send(_make_message())

        assert result.ok
        assert result.provider_message_id == "msg_123"
        assert result.detail == {"status_code": 200}

    @pytest.mark.asyncio
    async def test_202_also_treated_as_success(self):
        def handler(_request):
            return httpx.Response(202, json={"id": "msg_async"})

        client = _client_with_handler(handler)
        result = await client.send(_make_message())
        assert result.ok
        assert result.provider_message_id == "msg_async"

    @pytest.mark.asyncio
    async def test_2xx_without_id_treated_as_failed(self):
        """Defensive: if Resend changes its response shape we surface it
        rather than silently dropping the alert."""

        def handler(_request):
            return httpx.Response(200, json={"unexpected": True})

        client = _client_with_handler(handler)
        result = await client.send(_make_message())
        assert not result.ok
        assert result.detail and result.detail["error"] == "no_id"


class TestClientErrors:
    @pytest.mark.asyncio
    async def test_4xx_does_not_retry(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(422, json={"message": "validation"})

        client = _client_with_handler(handler, max_retries=3)
        result = await client.send(_make_message())

        assert calls == 1, "4xx must not trigger retries"
        assert not result.ok
        assert result.detail and result.detail["error"] == "client_error"
        assert result.detail["status_code"] == 422

    @pytest.mark.asyncio
    async def test_401_returns_failed(self):
        def handler(_request):
            return httpx.Response(401, json={"message": "invalid api key"})

        client = _client_with_handler(handler, max_retries=3)
        result = await client.send(_make_message())
        assert not result.ok
        assert result.detail["status_code"] == 401


class TestServerErrorRetry:
    @pytest.mark.asyncio
    async def test_5xx_retries_until_max(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "unavailable"})

        client = _client_with_handler(handler, max_retries=3)
        result = await client.send(_make_message())

        assert calls == 4, "1 initial + 3 retries"
        assert not result.ok
        assert result.detail["error"] == "server_error"

    @pytest.mark.asyncio
    async def test_5xx_then_success_returns_sent(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(502, json={})
            return httpx.Response(200, json={"id": "msg_after_retry"})

        client = _client_with_handler(handler, max_retries=3)
        result = await client.send(_make_message())

        assert calls == 3
        assert result.ok
        assert result.provider_message_id == "msg_after_retry"


class TestTransportError:
    @pytest.mark.asyncio
    async def test_transport_error_retries_then_fails(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("network down")

        client = _client_with_handler(handler, max_retries=2)
        result = await client.send(_make_message())

        assert calls == 3, "1 initial + 2 retries"
        assert not result.ok
        assert result.detail["error"] == "transport"

    @pytest.mark.asyncio
    async def test_transport_recovers_after_retry(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("transient")
            return httpx.Response(200, json={"id": "ok-after-net-recovery"})

        client = _client_with_handler(handler, max_retries=2)
        result = await client.send(_make_message())

        assert calls == 2
        assert result.ok


class TestBackoff:
    @pytest.mark.asyncio
    async def test_backoff_is_exponential(self):
        delays: list[float] = []

        async def fake_sleep(d):
            delays.append(d)

        def handler(_request):
            return httpx.Response(503, json={})

        client = _client_with_handler(
            handler, max_retries=3, initial_backoff_s=0.5
        )
        with patch("asyncio.sleep", new=fake_sleep):
            await client.send(_make_message())

        assert delays == [0.5, 1.0, 2.0]
