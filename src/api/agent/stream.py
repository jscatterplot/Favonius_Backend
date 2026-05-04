"""SSE (Server-Sent Events) helpers for the depot chat agent.

The agent's streaming endpoint emits one ``step`` event per orchestrator
phase (``extract_plan``, ``resolve_entities``, ``compile``, ``execute``)
and a final ``answer`` event with the formatted reply. This module owns
two tiny pieces of plumbing:

- :func:`format_sse_event` — encodes a ``(name, data)`` pair as the wire
  bytes the SSE protocol mandates: ``event: <name>\\ndata: <json>\\n\\n``.
- :class:`SSEEventStream` — an async-generator wrapper that lets the
  orchestrator ``await stream.emit(...)`` from each step and yields the
  encoded bytes to the FastAPI ``StreamingResponse``.

The router returns a ``StreamingResponse`` with ``Cache-Control: no-cache``
plus ``X-Accel-Buffering: no`` so nginx/Railway proxies do not buffer the
event chunks. See ``docs/plans/agent_search_architecture_v0.md`` §6.2.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Headers a caller passes to ``StreamingResponse`` for SSE:
# - ``Cache-Control: no-cache``  → tell every cache the body is volatile.
# - ``X-Accel-Buffering: no``    → nginx-specific; disables proxy buffering
#   so events reach the client as they are emitted (Railway uses nginx).
# - ``Connection: keep-alive``   → required by SSE; HTTP/1.1 already defaults
#   to it, but being explicit keeps load balancers from closing the socket.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def format_sse_event(name: str, data: Any) -> bytes:
    """Encode an SSE event as wire bytes.

    Returns ``event: <name>\\ndata: <json>\\n\\n`` as a UTF-8 byte string.
    The trailing blank line is required by the SSE spec to terminate the
    event; clients (browsers, ``curl -N``) buffer until they see it.

    Args:
        name: The SSE event name (e.g. ``step``, ``answer``, ``error``).
            Must not contain newlines — embedded ``\\n`` would split a
            single event into two on the wire.
        data: A JSON-serializable payload. ``json.dumps`` runs with
            ``default=str`` so :class:`datetime`, :class:`UUID`, and
            other repr-friendly types pass through.

    Returns:
        UTF-8 encoded bytes ready to be yielded into a ``StreamingResponse``.

    Raises:
        ValueError: If ``name`` contains a newline.
    """
    if "\n" in name or "\r" in name:
        raise ValueError(f"SSE event name must not contain newlines: {name!r}")
    payload = json.dumps(data, default=str, sort_keys=True)
    return f"event: {name}\ndata: {payload}\n\n".encode("utf-8")


class SSEEventStream:
    """Async generator wrapper for SSE step + answer events.

    Usage from an orchestrator:

    .. code-block:: python

        async def run_turn_streaming() -> AsyncIterator[bytes]:
            stream = SSEEventStream()

            async def driver():
                try:
                    await stream.emit("step", {"name": "extract_plan", ...})
                    ...
                    await stream.emit("answer", {"text": "..."})
                finally:
                    stream.close()

            asyncio.create_task(driver())
            async for chunk in stream:
                yield chunk

    The producer side calls :meth:`emit` once per event and :meth:`close`
    when the turn ends (success or failure). The consumer side iterates
    the instance to receive encoded byte chunks. Order is preserved
    because the underlying queue is FIFO.

    Closing while events are still buffered drains them before the
    iterator stops — clients receive every event the producer sent up to
    the close call, even if the producer also raises.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._closed: bool = False

    async def emit(self, name: str, data: Any) -> None:
        """Encode ``(name, data)`` and enqueue it for the consumer.

        If :meth:`close` has already run, this is a no-op so a producer
        that races the consumer's disconnect can still complete cleanly.
        """
        if self._closed:
            logger.debug("SSEEventStream.emit called after close; dropping event=%s", name)
            return
        chunk = format_sse_event(name, data)
        await self._queue.put(chunk)

    def close(self) -> None:
        """Signal end-of-stream. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Sentinel ``None`` tells the consumer iterator to stop.
        self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk
