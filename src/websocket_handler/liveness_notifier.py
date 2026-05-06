"""Rate-limited pg_notify of OCPP-frame liveness signals.

Used by the legacy WS handler to push "this charger just sent us
something" notifications to API replicas via Postgres NOTIFY/LISTEN.
The API replicas hold open SSE streams for the frontend; one notify
fan-outs to all subscribed clients for the relevant organisation.

Why pg_notify and not Supabase Realtime / Redis: NOTIFY is already the
inter-service signaling primitive in this codebase (see migrations 014
and 022) and reuses the existing TimescaleDB pool. **No row is written**
— this module calls ``SELECT pg_notify(...)`` directly with no trigger
and no enclosing transaction, so the only persistent cost is a tiny
WAL record for replica forwarding. Tracked entirely in-memory on the
WS handler side (``connection_manager.last_heartbeats``) and on the
frontend side (per-station Map keyed by ocpp_id).

Rate limit is per-station, in-process. With one WS handler replica
this is exact; with multiple replicas each handles a disjoint subset
of stations (a charger only attaches to one replica at a time), so
each station still gets one notify per ``interval_s`` seconds.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Channel name for pg_notify. API-side LivenessHub LISTENs on this.
LIVENESS_NOTIFY_CHANNEL = "charger_liveness"


class LivenessNotifier:
    """Per-station rate-limited pg_notify of liveness signals."""

    def __init__(
        self,
        timescale_client: Any,
        *,
        interval_s: float = 10.0,
        enabled: bool = True,
    ) -> None:
        """Initialise the notifier.

        Args:
            timescale_client: Async TimescaleDB client (must expose
                ``pg_pool`` for connection acquisition).
            interval_s: Minimum seconds between notifies for the same
                station. ``10`` matches the agreed cadence — any tighter
                wastes Postgres bandwidth on idle chargers, any looser
                degrades the frontend's "last interaction" responsiveness.
            enabled: Kill switch. When ``False`` the rate-limit dict
                still updates so toggling on doesn't trigger a burst,
                but ``pg_notify`` is skipped.
        """
        self._timescale = timescale_client
        self._interval_s = float(interval_s)
        self._enabled = bool(enabled)
        # station_id -> last successful notify time (monotonic seconds).
        self._last_notified: Dict[str, float] = {}

    async def maybe_notify(self, station_id: str, organization_id: Optional[str]) -> None:
        """Publish a liveness event for ``station_id`` if rate-limit allows.

        ``organization_id`` is the resolved tenant context (set on
        ``OCPP16Session`` during ``_on_boot``). When it's ``None``
        (charger not onboarded yet, Supabase lookup failed, etc.) we
        skip the notify — the API can't fan it out without a tenant
        scope anyway. Any DB / serialization error is logged WARN and
        swallowed; this path must never break OCPP frame handling.
        """
        if not organization_id:
            return

        now = time.monotonic()
        last = self._last_notified.get(station_id)
        if last is not None and (now - last) < self._interval_s:
            return

        # Update the rate-limit clock BEFORE the await — if the notify
        # call hangs, we don't queue a stampede of duplicate attempts
        # behind it.
        self._last_notified[station_id] = now

        if not self._enabled:
            return

        try:
            payload = json.dumps(
                {
                    "station_id": station_id,
                    "organization_id": str(organization_id),
                    "last_interaction_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:  # defensive — payload values are simple
            logger.warning(
                "liveness_notify_serialize_failed station=%s error=%s",
                station_id,
                exc,
            )
            return

        pool = getattr(self._timescale, "pg_pool", None)
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_notify($1, $2)", LIVENESS_NOTIFY_CHANNEL, payload)
        except Exception as exc:
            logger.warning(
                "liveness_notify_failed station=%s error=%s",
                station_id,
                exc,
            )
