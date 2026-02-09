"""Adapter to use Starlette/FastAPI WebSocket as OCPP connection (same port as REST).

Reference: Railway plan Option A - mount OCPP on same port as FastAPI.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class StarletteOCPPAdapter:
    """Wraps a Starlette WebSocket so it can be used as connection for FleetChargePoint.

    ocpp ChargePoint expects connection with async recv() and send(msg).
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def recv(self) -> str:
        msg = await self._ws.receive()
        if msg.get("type") == "websocket.disconnect":
            raise ConnectionError("WebSocket disconnected")
        text = msg.get("text")
        if text is not None:
            return text
        data = msg.get("bytes")
        if data is not None:
            return data.decode("utf-8")
        raise ValueError("Unexpected WebSocket message: no text or bytes")

    async def send(self, message: str) -> None:
        await self._ws.send_text(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code)
