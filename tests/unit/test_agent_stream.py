"""Unit tests for ``src.api.agent.stream.SSEEventStream``.

The producer (``emit``/``close``) runs as a detached ``asyncio.create_task``
task while the consumer iterates the stream inside a ``StreamingResponse``.
These tests pin the contract that matters for memory safety: the bounded queue
caps memory, yet ``emit``/``close`` never block the producer — so a stalled or
disconnected consumer cannot wedge the turn task (regression guard for the
bounded-queue change).
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.agent.stream import _SSE_QUEUE_MAXSIZE, SSEEventStream, format_sse_event


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emit_then_close_delivers_all_events_in_order():
    """The normal path: a handful of events, consumer keeps up → completeness."""
    stream = SSEEventStream()
    for i in range(5):
        await stream.emit("step", {"i": i})
    stream.close()

    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 5
    assert chunks[0] == format_sse_event("step", {"i": 0})
    assert chunks[4] == format_sse_event("step", {"i": 4})
    assert stream._dropped == 0  # nothing shed under the cap


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emit_never_blocks_when_consumer_stalled():
    """A stalled/disconnected consumer must not wedge the producer: ``emit``
    stays non-blocking and memory stays bounded even past the cap."""
    stream = SSEEventStream()
    overflow = _SSE_QUEUE_MAXSIZE + 50

    # No consumer draining. Each emit must return promptly (wait_for would raise
    # TimeoutError if emit blocked on a full put — the regression we guard).
    for i in range(overflow):
        await asyncio.wait_for(stream.emit("step", {"i": i}), timeout=1.0)

    # Memory bound holds: the queue never exceeds the configured cap.
    assert stream._queue.qsize() <= _SSE_QUEUE_MAXSIZE
    assert stream._dropped >= overflow - _SSE_QUEUE_MAXSIZE

    # close() is non-blocking too and always terminates the iterator.
    await asyncio.wait_for(_close_async(stream), timeout=1.0)

    chunks = [chunk async for chunk in stream]
    assert len(chunks) <= _SSE_QUEUE_MAXSIZE
    # The newest event survives; the very oldest was shed (drop-oldest).
    assert any(b'"i": %d' % (overflow - 1) in c for c in chunks)
    assert not any(c == format_sse_event("step", {"i": 0}) for c in chunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_is_lossless_when_queue_full():
    """close() reserves a sentinel slot, so terminating a full queue delivers
    every buffered event (including the final one) — it never evicts to fit the
    sentinel."""
    stream = SSEEventStream()
    # Fill exactly to the event cap with no consumer draining.
    for i in range(_SSE_QUEUE_MAXSIZE):
        await stream.emit("step", {"i": i})
    assert stream._dropped == 0  # nothing shed while at/under the cap

    stream.close()
    assert stream._dropped == 0  # close did not drop anything to fit the sentinel

    chunks = [chunk async for chunk in stream]
    assert len(chunks) == _SSE_QUEUE_MAXSIZE
    # First and last buffered events both survive termination.
    assert chunks[0] == format_sse_event("step", {"i": 0})
    assert chunks[-1] == format_sse_event("step", {"i": _SSE_QUEUE_MAXSIZE - 1})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_is_idempotent_and_terminates():
    stream = SSEEventStream()
    await stream.emit("step", {"x": 1})
    stream.close()
    stream.close()  # second close must no-op, not double-enqueue a sentinel

    chunks = [chunk async for chunk in stream]
    assert chunks == [format_sse_event("step", {"x": 1})]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emit_after_close_is_noop():
    stream = SSEEventStream()
    stream.close()
    await stream.emit("step", {"x": 1})  # no-op, must not raise

    chunks = [chunk async for chunk in stream]
    assert chunks == []


async def _close_async(stream: SSEEventStream) -> None:
    """Call the synchronous ``close`` from an awaitable so ``wait_for`` can bound it."""
    stream.close()
