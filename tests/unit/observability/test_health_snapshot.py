"""Unit tests for the health-snapshot helpers in src.api.main.

Tiger Cloud = the TimescaleDB (``ts``) pool; WebSocket resolves via the
in-process server, else an HTTP probe, else "unknown".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.api.main as main

pytestmark = pytest.mark.asyncio


def _ok_ts_pool():
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool


async def test_tiger_cloud_healthy():
    with patch.object(main, "db_pools", _ok_ts_pool()):
        assert await main.check_tiger_cloud_health() == "healthy"


async def test_tiger_cloud_unavailable_on_error():
    pool = MagicMock()
    pool.ts = pool
    pool.acquire.side_effect = RuntimeError("db down")
    with patch.object(main, "db_pools", pool):
        assert await main.check_tiger_cloud_health() == "unavailable"


async def test_tiger_cloud_unavailable_without_pools():
    with patch.object(main, "db_pools", None):
        assert await main.check_tiger_cloud_health() == "unavailable"


async def test_websocket_in_process_running_with_chargers():
    server = MagicMock()
    server._running = True
    server.charge_points = {"cp1": object()}
    with patch.object(main, "ocpp_server", server):
        assert await main.check_websocket_health() == "healthy"


async def test_websocket_unknown_when_disabled_and_no_probe(monkeypatch):
    monkeypatch.delenv("WEBSOCKET_HEALTH_PROBE_URL", raising=False)
    with patch.object(main, "ocpp_server", None):
        assert await main.check_websocket_health() == "unknown"


async def test_websocket_probe_healthy(monkeypatch):
    monkeypatch.setenv("WEBSOCKET_HEALTH_PROBE_URL", "http://ws:8081/health")

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    with patch.object(main, "ocpp_server", None), patch("httpx.AsyncClient", _Client):
        assert await main.check_websocket_health() == "healthy"


async def test_websocket_probe_non_200_is_degraded(monkeypatch):
    monkeypatch.setenv("WEBSOCKET_HEALTH_PROBE_URL", "http://ws:8081/health")

    class _Resp:
        status_code = 503

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    with patch.object(main, "ocpp_server", None), patch("httpx.AsyncClient", _Client):
        assert await main.check_websocket_health() == "degraded"


async def test_websocket_probe_timeout_is_unavailable(monkeypatch):
    monkeypatch.setenv("WEBSOCKET_HEALTH_PROBE_URL", "http://ws:8081/health")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise TimeoutError("probe timed out")

    with patch.object(main, "ocpp_server", None), patch("httpx.AsyncClient", _Client):
        assert await main.check_websocket_health() == "unavailable"


async def test_build_health_snapshot_shape(monkeypatch):
    monkeypatch.delenv("WEBSOCKET_HEALTH_PROBE_URL", raising=False)
    with patch.object(main, "db_pools", _ok_ts_pool()), patch.object(main, "ocpp_server", None):
        snap = await main.build_health_snapshot()
    assert snap["tiger_cloud"] == "healthy"
    assert snap["websocket"] == "unknown"
    assert snap["websocket_source"] == "unknown"
    assert "captured_at" in snap
