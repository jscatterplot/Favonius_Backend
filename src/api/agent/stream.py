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
import os
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Upper bound on buffered, not-yet-sent SSE *events*. The queue is bounded so a
# runaway producer — e.g. a pathological agent loop — cannot buffer unbounded
# bytes in memory. ``emit`` is non-blocking: on overflow it sheds the oldest
# buffered event rather than awaiting a free slot, so a hung or disconnected
# consumer can never wedge the detached producer task. The cap is generous; a
# healthy turn emits well under a dozen events, so shedding only happens when the
# consumer has stopped draining. (The queue itself is sized at +1 to reserve a
# slot for the end-of-stream sentinel so ``close`` is lossless.)
_SSE_QUEUE_MAXSIZE = max(1, int(os.getenv("AGENT_SSE_QUEUE_MAXSIZE", "1000")))

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

    :meth:`emit` and :meth:`close` never block the producer. When the
    consumer keeps up (the normal case — a turn emits a handful of events
    against a 1000-deep queue) clients receive every event in order. If the
    consumer stops draining (client disconnect, cancelled response body) the
    queue fills and :meth:`emit` sheds the *oldest* buffered events to bound
    memory, so the detached producer task always reaches its
    ``finally: close()`` instead of wedging on a full ``put``. The newest
    events — including the final ``answer`` — always survive, and
    :meth:`close` is lossless: a slot is reserved for the end-of-stream
    sentinel so terminating the stream never evicts a buffered event. The
    consumer iterator also calls :meth:`close` from its own ``finally`` so a
    cancelled stream flips ``_closed`` and any further :meth:`emit` calls
    become cheap no-ops.
    """

    def __init__(self) -> None:
        # Hold up to ``_SSE_QUEUE_MAXSIZE`` buffered *events* plus one reserved
        # slot for the sentinel, so ``close`` can always terminate the stream
        # without dropping a pending event.
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(
            maxsize=_SSE_QUEUE_MAXSIZE + 1
        )
        self._closed: bool = False
        self._dropped: int = 0

    async def emit(self, name: str, data: Any) -> None:
        """Encode ``(name, data)`` and enqueue it for the consumer.

        Non-blocking. Events are capped at ``_SSE_QUEUE_MAXSIZE``; on overflow
        the oldest buffered event is shed rather than blocking the producer — a
        hung or disconnected consumer must never wedge the detached turn task.
        If :meth:`close` has already run this is a no-op so a producer that
        races the consumer's disconnect can still complete cleanly.
        """
        if self._closed:
            logger.debug("SSEEventStream.emit called after close; dropping event=%s", name)
            return
        chunk = format_sse_event(name, data)
        # Cap *events* at _SSE_QUEUE_MAXSIZE; the reserved +1 slot is for the
        # sentinel only. Shedding only happens when the consumer is not draining.
        if self._queue.qsize() >= _SSE_QUEUE_MAXSIZE:
            try:
                self._queue.get_nowait()  # drop the oldest buffered event
                self._dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - consumer drained concurrently
                pass
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:  # pragma: no cover - reserved slot keeps headroom
            pass

    def close(self) -> None:
        """Signal end-of-stream. Idempotent, non-blocking, and lossless."""
        if self._closed:
            return
        self._closed = True
        # The reserved slot guarantees the sentinel fits without evicting a
        # buffered event, so a terminating stream never drops a pending answer.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - reserved slot should always fit
            pass
        if self._dropped:
            logger.warning(
                "SSEEventStream shed %d event(s) on a stalled/disconnected consumer",
                self._dropped,
            )

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            # A client disconnect cancels this iterator. Flip ``_closed`` so the
            # detached producer's subsequent emits short-circuit to no-ops
            # instead of encoding events nobody will read.
            self.close()
