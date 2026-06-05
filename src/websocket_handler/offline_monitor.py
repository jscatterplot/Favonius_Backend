"""Charger offline-duration monitor.

Produces ``notification_alerts`` when a charger has been disconnected for too
long, escalating from a warning (default 3 h) to critical (default 24 h). Both
tiers auto-resolve the moment the charger reconnects.

Design notes
------------
* **DB-derived, restart-safe.** "Offline since" comes from ``connector_status``:
  the WS close hook (``timescale_client.mark_connectors_unavailable``) appends an
  ``(Unavailable, 'ConnectionLost')`` row on every drop, and the reconnect path
  clears it. So the offline window is reconstructed from the database and
  survives a WS-handler redeploy — important, because an outage can outlast the
  process that first observed it.
* **Reuses the alerts pipeline.** This module only *produces* / *resolves*
  ``notification_alerts`` via ``src.notifications.alerts``; delivery, dedup,
  recipient gating and re-notification are handled by the existing
  ``AlertDispatcher``. No new tables.
* **Pure core, thin shell.** ``plan_sweep`` is a pure function (no I/O) so the
  threshold/escalation/resolution logic is unit-tested without a database;
  ``OfflineChargerMonitor.run_once`` is the thin DB glue around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from src.notifications.alerts import (
    Alert,
    list_active_by_types,
    resolve_alert,
    upsert_alert,
)
from src.notifications.severity import Severity

logger = logging.getLogger(__name__)

# Alert types — free-form strings (notification_alerts.alert_type is VARCHAR(64)).
# Recipients with alert_types=ARRAY['*'] match these automatically; a recipient
# scoped to explicit types must include them.
ALERT_TYPE_3H = "charger_offline_3h"
ALERT_TYPE_24H = "charger_offline_24h"
OFFLINE_ALERT_TYPES: tuple[str, str] = (ALERT_TYPE_3H, ALERT_TYPE_24H)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfflineCharger:
    """A charger whose latest ``connector_status`` row is a stale disconnect."""

    station_id: str
    offline_since: datetime
    offline_seconds: float
    organization_id: Optional[UUID]
    depot_id: Optional[UUID]


@dataclass(frozen=True)
class PlannedUpsert:
    organization_id: UUID
    depot_id: Optional[UUID]
    alert_type: str
    severity: Severity
    title: str
    detail: dict[str, Any]
    dedup_key: str


@dataclass(frozen=True)
class PlannedResolve:
    organization_id: UUID
    dedup_key: str


@dataclass
class SweepPlan:
    upserts: list[PlannedUpsert] = field(default_factory=list)
    resolves: list[PlannedResolve] = field(default_factory=list)
    skipped_no_tenant: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure planning core
# ---------------------------------------------------------------------------


def _hour_label(seconds: int) -> int:
    """Whole-hours label for a threshold (3 / 24 by default)."""
    return max(1, round(seconds / 3600))


def _build_upsert(
    charger: OfflineCharger,
    *,
    threshold_seconds: int,
    severity: Severity,
    alert_type: str,
) -> PlannedUpsert:
    hours = charger.offline_seconds / 3600.0
    tier = _hour_label(threshold_seconds)
    detail: dict[str, Any] = {
        "description": (
            f"Charger {charger.station_id} has been offline for {hours:.1f} h "
            f"(no connection to the platform since "
            f"{charger.offline_since:%Y-%m-%d %H:%M} UTC)."
        ),
        "suggestedAction": (
            "Check the charger's power and network/Ethernet link at the depot. "
            "It has stopped connecting; if a shared switch or circuit serves "
            "multiple chargers, check those too."
        ),
        "context": {"kind": "charger", "id": charger.station_id, "label": charger.station_id},
        "station_id": charger.station_id,
        "offline_since": charger.offline_since.isoformat(),
        "offline_hours": round(hours, 1),
        "threshold_hours": tier,
    }
    # organization_id is guaranteed non-None by the caller (None ones are skipped).
    assert charger.organization_id is not None
    return PlannedUpsert(
        organization_id=charger.organization_id,
        depot_id=charger.depot_id,
        alert_type=alert_type,
        severity=severity,
        title=f"Charger {charger.station_id} offline {tier}h+",
        detail=detail,
        dedup_key=f"{alert_type}:{charger.station_id}",
    )


def _station_of(alert: Alert) -> Optional[str]:
    """Recover the station id an offline alert refers to.

    Prefers ``detail['station_id']``; falls back to the dedup_key suffix
    (``charger_offline_3h:<station_id>``) for robustness.
    """
    detail = getattr(alert, "detail", None)
    if isinstance(detail, dict):
        station = detail.get("station_id")
        if station:
            return str(station)
    dedup = getattr(alert, "dedup_key", "") or ""
    return dedup.split(":", 1)[1] if ":" in dedup else None


def plan_sweep(
    offline: Sequence[OfflineCharger],
    active: Sequence[Alert],
    *,
    warn_after_s: int,
    escalate_after_s: int,
) -> SweepPlan:
    """Decide which offline alerts to raise / escalate / resolve.

    Pure function — no I/O. Given the chargers currently offline (with how long)
    and the alerts currently active, returns the upserts and resolves to apply.

    * ``offline_seconds >= warn_after_s``  → ``charger_offline_3h`` (warning).
    * ``offline_seconds >= escalate_after_s`` → ``charger_offline_24h`` (critical).
      A charger past the 24 h line is also past 3 h, so it carries both rows.
    * An active offline alert whose charger is no longer in the matching breach
      set (reconnected, or dropped back under the 24 h line) is resolved.
    * Chargers with no resolvable tenant (organization_id) are skipped — we
      cannot scope an alert to an org. Mirrors the migration-022 trigger, which
      bails when ``organization_id IS NULL``.
    """
    plan = SweepPlan()
    breach_3h: set[str] = set()
    breach_24h: set[str] = set()

    for charger in offline:
        if charger.organization_id is None:
            plan.skipped_no_tenant.append(charger.station_id)
            continue
        if charger.offline_seconds >= escalate_after_s:
            breach_24h.add(charger.station_id)
            plan.upserts.append(
                _build_upsert(
                    charger,
                    threshold_seconds=escalate_after_s,
                    severity=Severity.CRITICAL,
                    alert_type=ALERT_TYPE_24H,
                )
            )
        if charger.offline_seconds >= warn_after_s:
            breach_3h.add(charger.station_id)
            plan.upserts.append(
                _build_upsert(
                    charger,
                    threshold_seconds=warn_after_s,
                    severity=Severity.WARNING,
                    alert_type=ALERT_TYPE_3H,
                )
            )

    for alert in active:
        station = _station_of(alert)
        if alert.alert_type == ALERT_TYPE_3H and station not in breach_3h:
            plan.resolves.append(PlannedResolve(alert.organization_id, alert.dedup_key))
        elif alert.alert_type == ALERT_TYPE_24H and station not in breach_24h:
            plan.resolves.append(PlannedResolve(alert.organization_id, alert.dedup_key))

    return plan


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------


async def fetch_offline_chargers(
    conn: Any, *, min_offline_seconds: float
) -> list[OfflineCharger]:
    """Return chargers whose latest ``connector_status`` row is a stale disconnect.

    A charger is "offline" when its most-recent row (across connectors) is the
    close hook's ``(Unavailable, 'ConnectionLost')`` marker. ``offline_seconds``
    is computed with the DB clock (single source of truth, no app/DB skew).

    Tenant context is read from that row (populated by
    ``mark_connectors_unavailable`` since the org-stamp fix); for rows written
    before that fix it falls back to the most recent tenant-stamped row for the
    same station. Stations that never carried tenant context come back with
    ``organization_id = None`` and are skipped by ``plan_sweep``.
    """
    rows = await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (station_id)
                   station_id, status, error_code, timestamp,
                   organization_id, depot_id
              FROM connector_status
             ORDER BY station_id, timestamp DESC
        ),
        tenant AS (
            SELECT DISTINCT ON (station_id)
                   station_id, organization_id, depot_id
              FROM connector_status
             WHERE organization_id IS NOT NULL
             ORDER BY station_id, timestamp DESC
        )
        SELECT l.station_id,
               l.timestamp AS offline_since,
               EXTRACT(EPOCH FROM (NOW() - l.timestamp))::double precision
                   AS offline_seconds,
               COALESCE(l.organization_id, t.organization_id) AS organization_id,
               COALESCE(l.depot_id, t.depot_id)               AS depot_id
          FROM latest l
          LEFT JOIN tenant t USING (station_id)
         WHERE l.status = 'Unavailable'
           AND l.error_code = 'ConnectionLost'
           AND l.timestamp <= NOW() - ($1::double precision * INTERVAL '1 second')
         ORDER BY l.station_id
        """,
        float(min_offline_seconds),
    )
    return [
        OfflineCharger(
            station_id=r["station_id"],
            offline_since=r["offline_since"],
            offline_seconds=float(r["offline_seconds"]),
            organization_id=r["organization_id"],
            depot_id=r["depot_id"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Monitor (thin DB shell around plan_sweep)
# ---------------------------------------------------------------------------


class OfflineChargerMonitor:
    """Runs one offline-charger sweep per call to ``run_once``.

    The periodic loop lives in the WS handler app (``_offline_monitor_loop``),
    mirroring the orphan-recovery sweep; this class is the unit of work.
    """

    def __init__(
        self,
        *,
        pool: Any,
        warn_after_s: int,
        escalate_after_s: int,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._pool = pool
        self._warn_after_s = warn_after_s
        self._escalate_after_s = escalate_after_s
        self._logger = logger or globals()["logger"]

    async def run_once(self) -> SweepPlan:
        """Fetch offline chargers, plan, and apply the upserts/resolves.

        Returns the ``SweepPlan`` (useful for tests and logging). Producers do
        not pg_notify — the dispatcher's poll backstop picks the rows up within
        one poll interval (same path as the controller's ``missing_input``).
        """
        # Use the smaller threshold as the SQL floor so a single query feeds
        # both tiers (warn < escalate in any sane config).
        floor = float(min(self._warn_after_s, self._escalate_after_s))
        async with self._pool.acquire() as conn:
            offline = await fetch_offline_chargers(conn, min_offline_seconds=floor)
            active = await list_active_by_types(conn, OFFLINE_ALERT_TYPES)
            plan = plan_sweep(
                offline,
                active,
                warn_after_s=self._warn_after_s,
                escalate_after_s=self._escalate_after_s,
            )
            for up in plan.upserts:
                await upsert_alert(
                    conn,
                    organization_id=up.organization_id,
                    depot_id=up.depot_id,
                    alert_type=up.alert_type,
                    severity=up.severity,
                    title=up.title,
                    detail=up.detail,
                    dedup_key=up.dedup_key,
                )
            for rs in plan.resolves:
                await resolve_alert(
                    conn,
                    organization_id=rs.organization_id,
                    dedup_key=rs.dedup_key,
                )

        if plan.upserts or plan.resolves or plan.skipped_no_tenant:
            self._logger.info(
                "offline_charger_monitor: %d alert(s) raised/escalated, "
                "%d resolved, %d skipped (no tenant)",
                len(plan.upserts),
                len(plan.resolves),
                len(plan.skipped_no_tenant),
            )
        return plan


__all__ = [
    "ALERT_TYPE_3H",
    "ALERT_TYPE_24H",
    "OFFLINE_ALERT_TYPES",
    "OfflineCharger",
    "PlannedUpsert",
    "PlannedResolve",
    "SweepPlan",
    "plan_sweep",
    "fetch_offline_chargers",
    "OfflineChargerMonitor",
]
