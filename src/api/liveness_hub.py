"""LISTEN/NOTIFY-driven fan-out hub for charger liveness events.

Runs in the API service. Holds one asyncpg connection LISTENing on the
``charger_liveness`` Postgres channel; the WS handler does
``pg_notify('charger_liveness', '{...}')`` on every received OCPP frame
(rate-limited to one per 10 s per station). When a notification arrives,
the hub fans it out to every per-organisation subscriber queue. Each
SSE endpoint subscribes a queue on connect and drains it until the
client disconnects.

Why pg_notify and not Supabase Realtime / Redis: NOTIFY is already the
inter-service signaling primitive in this codebase (migrations 014 and
022) and reuses the same Postgres infrastructure as everything else.
**No row is written** anywhere — the producer calls
``SELECT pg_notify(...)`` directly, and this hub only LISTENs +
discards.

Multi-replica behavior: every API replica runs its own LivenessHub
instance with its own LISTEN connection. Postgres broadcasts each
notification to every listener, so each replica receives every event
and forwards to its own SSE subscribers. No coordination needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


# Channel name — must match ``LIVENESS_NOTIFY_CHANNEL`` in
# src/websocket_handler/liveness_notifier.py.
LIVENESS_CHANNEL = "charger_liveness"


# Per-subscriber queue size. The producer rate-limits to ~10 s per
# station, so a depot with N chargers produces at most N events per 10 s.
# 50 entries comfortably absorbs a burst on subscriber connect or after
# a brief network blip; if a subscriber stays slow longer than that,
# we drop oldest events (the latest one wins for "last interaction").
_SUBSCRIBER_QUEUE_MAXSIZE = 50


class LivenessHub:
    """LISTEN/NOTIFY → per-org fan-out for SSE subscribers."""

    def __init__(self, ts_pool: Any) -> None:
        """Initialise the hub.

        Args:
            ts_pool: asyncpg pool against the TimescaleDB instance — same
                pool the WS handler publishes to. The hub holds one
                dedicated connection from this pool for the lifetime of
                the application; subscribers don't touch the pool
                directly.
        """
        self._pool = ts_pool
        # organization_id (str) -> set of subscriber queues.
        self._subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._listener_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._running = False

    async def start(self) -> None:
        """Spawn the LISTEN background task. Idempotent."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._listener_task = asyncio.create_task(self._listen_loop(), name="liveness_hub_listen")
        logger.info("LivenessHub started")

    async def stop(self) -> None:
        """Cancel the LISTEN task and clear subscriber state."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener_task
            self._listener_task = None
        # Wake any subscribers still waiting so their handlers can exit.
        for queues in self._subscribers.values():
            for q in queues:
                try:
                    q.put_nowait(None)  # sentinel for "stream closing"
                except asyncio.QueueFull:
                    pass
        self._subscribers.clear()
        logger.info("LivenessHub stopped")

    def subscribe(self, organization_id: str) -> asyncio.Queue:
        """Register a new subscriber queue for ``organization_id``.

        The caller drains the returned queue and is responsible for
        passing it back to :meth:`unsubscribe` when the SSE stream ends.
        Queue is bounded — when full, the oldest event is dropped (the
        most recent "last interaction" is what matters for the UI; an
        in-flight stale entry being dropped is the right trade-off).
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers[organization_id].add(q)
        return q

    def unsubscribe(self, organization_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue. Idempotent."""
        bucket = self._subscribers.get(organization_id)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            self._subscribers.pop(organization_id, None)

    async def _listen_loop(self) -> None:
        """Hold an asyncpg connection LISTENing on the liveness channel.

        Reconnects with exponential backoff on connection drops; pg_notify
        is fire-and-forget so we never replay missed events — the next
        OCPP frame from any charger naturally regenerates a fresh
        liveness signal.
        """
        backoff = 1.0
        max_backoff = 30.0
        while not self._stop_event.is_set():
            conn = None
            terminated_event = asyncio.Event()

            def _on_connection_terminated(_conn: Any) -> None:
                logger.warning(
                    "LivenessHub LISTEN connection terminated; reconnecting channel '%s'",
                    LIVENESS_CHANNEL,
                )
                terminated_event.set()

            try:
                conn = await self._pool.acquire()
                await conn.add_listener(LIVENESS_CHANNEL, self._on_notify)
                conn.add_termination_listener(_on_connection_terminated)
                logger.info("LivenessHub LISTENing on channel '%s'", LIVENESS_CHANNEL)
                backoff = 1.0  # successful connect resets backoff
                # Hold the listener open until stop is requested or Postgres drops.
                stop_wait = asyncio.create_task(self._stop_event.wait())
                terminated_wait = asyncio.create_task(terminated_event.wait())
                done, pending = await asyncio.wait(
                    {stop_wait, terminated_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if terminated_wait in done and not self._stop_event.is_set():
                    raise ConnectionError("LISTEN connection terminated")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "LivenessHub listener error: %s — retrying in %.1fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.remove_termination_listener(_on_connection_terminated)
                    with contextlib.suppress(Exception):
                        await conn.remove_listener(LIVENESS_CHANNEL, self._on_notify)
                    with contextlib.suppress(Exception):
                        await self._pool.release(conn)

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        """asyncpg listener callback — synchronous, must not block.

        Parse the JSON payload, find the per-org subscriber bucket, and
        push to each queue. Slow subscribers get the oldest event
        evicted (we drop one entry from a full queue and re-put). Any
        parse / dispatch error is logged WARN and dropped.
        """
        try:
            event = json.loads(payload)
        except (TypeError, ValueError) as exc:
            logger.warning("liveness_hub_invalid_payload error=%s", exc)
            return

        org_id = event.get("organization_id")
        if not org_id:
            return

        bucket = self._subscribers.get(str(org_id))
        if not bucket:
            return

        # Strip organization_id before fanning out — subscribers are
        # already scoped to their org and the front-end doesn't need it.
        outbound = {
            "station_id": event.get("station_id"),
            "last_interaction_at": event.get("last_interaction_at"),
        }
        for q in bucket:
            try:
                q.put_nowait(outbound)
            except asyncio.QueueFull:
                # Drop the oldest entry to make room — "last interaction"
                # is monotonic, the newer event always supersedes.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(outbound)
