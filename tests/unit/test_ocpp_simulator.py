import asyncio

import pytest

from scripts.ocpp_simulator import SimulatedChargePoint, _normalize_ocpp_server_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", "ws://localhost:9000/ocpp"),
        ("localhost:1234", "ws://localhost:1234/ocpp"),
        ("example.com", "wss://example.com/ocpp"),
        ("wss://host/ocpp", "wss://host/ocpp"),
    ],
)
def test_normalize_ocpp_server_url(raw, expected):
    assert _normalize_ocpp_server_url(raw) == expected


@pytest.mark.asyncio
async def test_call_with_timeout_success():
    class Dummy:
        async def call(self, request):
            await asyncio.sleep(0.01)
            return {"ok": True, "request": request}

    result = await SimulatedChargePoint._call_with_timeout(Dummy(), "req", timeout_s=0.5)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_call_with_timeout_raises_timeout():
    class Dummy:
        async def call(self, request):
            await asyncio.sleep(0.2)
            return request

    with pytest.raises(asyncio.TimeoutError):
        await SimulatedChargePoint._call_with_timeout(Dummy(), "req", timeout_s=0.01)
