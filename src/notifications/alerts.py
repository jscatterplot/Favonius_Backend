"""notification_alerts repository.

Module-level async functions (matching src/db/queries.py and src/adapters/
vdv463/repository.py style) for the queries the dispatcher and API need.
All queries are parameterized; never interpolate user input.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from .severity import Severity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    """A row from notification_alerts as returned by the dispatcher / API."""

    id: UUID
    organization_id: UUID
    depot_id: Optional[UUID]
    alert_type: str
    severity: Severity
    title: str
    detail: dict[str, Any]
    dedup_key: str
    status: str
    first_occurrence_at: datetime
    last_occurrence_at: datetime
    occurrence_count: int
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[UUID]
    acknowledged_by_email: Optional[str]
    resolved_at: Optional[datetime]
    last_notified_at: Optional[datetime]
    last_notified_count: int

    @classmethod
    def from_record(cls, row: Any) -> "Alert":
        return cls(
            id=row["id"],
            organization_id=row["organization_id"],
            depot_id=row["depot_id"],
            alert_type=row["alert_type"],
            severity=Severity.from_str(row["severity"]),
            title=row["title"],
            detail=_coerce_jsonb(row["detail"]),
            dedup_key=row["dedup_key"],
            status=row["status"],
            first_occurrence_at=row["first_occurrence_at"],
            last_occurrence_at=row["last_occurrence_at"],
            occurrence_count=int(row["occurrence_count"]),
            acknowledged_at=row["acknowledged_at"],
            acknowledged_by=row["acknowledged_by"],
            acknowledged_by_email=row.get("acknowledged_by_email"),
            resolved_at=row["resolved_at"],
            last_notified_at=row["last_notified_at"],
            last_notified_count=row["last_notified_count"],
        )


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning("non-JSON detail payload on notification_alerts row")
            return {}
    return {}


# ---------------------------------------------------------------------------
# Dispatcher queries
# ---------------------------------------------------------------------------


_ALERT_COLUMNS = (
    "id, organization_id, depot_id, alert_type, severity, title, detail, "
    "dedup_key, status, first_occurrence_at, last_occurrence_at, "
    "occurrence_count, acknowledged_at, acknowledged_by, acknowledged_by_email, "
    "resolved_at, last_notified_at, last_notified_count"
)


_CLAIM_QUERY = f"""
    SELECT {_ALERT_COLUMNS}
      FROM notification_alerts
     WHERE status = 'active'
       AND (last_notified_at IS NULL
            OR last_notified_at < (NOW() - ($1::int * INTERVAL '1 second')))
     ORDER BY severity_level DESC, last_occurrence_at DESC
     LIMIT $2
"""


async def claim_pending_alerts(
    conn: Any, *, resend_interval_s: int, limit: int
) -> list[Alert]:
    """Return alerts that need notification.

    "Claim" is a misnomer here — per decision 4.1 we rely on a single-worker
    dispatcher with an in-memory dedup set rather than a DB-side row lock.
    The dispatcher must filter the result against its `_currently_sending`
    set before processing.
    """
    rows = await conn.fetch(_CLAIM_QUERY, resend_interval_s, limit)
    return [Alert.from_record(r) for r in rows]


async def mark_notified(conn: Any, alert_id: UUID) -> int:
    """Bump last_notified_at/last_notified_count, return new count.

    The returned count is the idempotency anchor for notification_deliveries
    (UNIQUE (alert_id, recipient_id, notified_count)).
    """
    new_count = await conn.fetchval(
        """
        UPDATE notification_alerts
           SET last_notified_at = NOW(),
               last_notified_count = last_notified_count + 1,
               updated_at = NOW()
         WHERE id = $1
         RETURNING last_notified_count
        """,
        alert_id,
    )
    if new_count is None:
        raise ValueError(f"notification_alerts row {alert_id} not found")
    return int(new_count)


# ---------------------------------------------------------------------------
# API queries
# ---------------------------------------------------------------------------


async def list_for_depot(
    conn: Any,
    depot_id: UUID,
    *,
    statuses: Sequence[str] = ("active", "acknowledged"),
    limit: int = 200,
) -> list[Alert]:
    """List alerts for a depot. Default excludes resolved.

    Powers the notification_alerts half of the /depots/{id}/alerts UNION
    (decision 4.3). Order matches the indexed sort: severity_level DESC,
    last_occurrence_at DESC.
    """
    rows = await conn.fetch(
        f"""
        SELECT {_ALERT_COLUMNS}
          FROM notification_alerts
         WHERE depot_id = $1
           AND status = ANY($2::text[])
         ORDER BY severity_level DESC, last_occurrence_at DESC
         LIMIT $3
        """,
        depot_id,
        list(statuses),
        limit,
    )
    return [Alert.from_record(r) for r in rows]


async def get_by_id(conn: Any, alert_id: UUID) -> Optional[Alert]:
    row = await conn.fetchrow(
        f"""
        SELECT {_ALERT_COLUMNS}
          FROM notification_alerts
         WHERE id = $1
        """,
        alert_id,
    )
    return Alert.from_record(row) if row else None


async def acknowledge(
    conn: Any, alert_id: UUID, *, user_id: UUID
) -> Optional[Alert]:
    """Mark an active alert as acknowledged. Returns the updated row, or None
    if the alert doesn't exist or is already resolved."""
    row = await conn.fetchrow(
        f"""
        UPDATE notification_alerts
           SET status = 'acknowledged',
               acknowledged_at = NOW(),
               acknowledged_by = $2,
               updated_at = NOW()
         WHERE id = $1
           AND status = 'active'
         RETURNING {_ALERT_COLUMNS}
        """,
        alert_id,
        user_id,
    )
    return Alert.from_record(row) if row else None


# ---------------------------------------------------------------------------
# Producer-side UPSERT
# ---------------------------------------------------------------------------


_UPSERT_QUERY = f"""
    INSERT INTO notification_alerts (
        organization_id, depot_id, alert_type, severity, title, detail, dedup_key
    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
    ON CONFLICT (organization_id, dedup_key) WHERE status != 'resolved'
    DO UPDATE SET
        last_occurrence_at = NOW(),
        occurrence_count   = notification_alerts.occurrence_count + 1,
        severity           = EXCLUDED.severity,
        title              = EXCLUDED.title,
        detail             = EXCLUDED.detail,
        updated_at         = NOW()
    RETURNING {_ALERT_COLUMNS}
"""


async def upsert_alert(
    conn: Any,
    *,
    organization_id: UUID,
    depot_id: Optional[UUID],
    alert_type: str,
    severity: Severity,
    title: str,
    detail: dict[str, Any],
    dedup_key: str,
) -> Alert:
    """Insert a new alert or bump an existing active row.

    Mirrors the ``fn_alerts_on_connector_status`` trigger logic for app-code
    producers (charger_auth_failure, degraded_optimization, missing_input,
    stale_telemetry). Increments ``occurrence_count`` on dedup conflict; the
    partial unique index keeps one active row per ``(org, dedup_key)``.

    Callers should run this inside an existing transaction if they want the
    insert to roll back atomically with their own writes.
    """
    row = await conn.fetchrow(
        _UPSERT_QUERY,
        organization_id,
        depot_id,
        alert_type,
        severity.value,
        title,
        json.dumps(detail) if detail else "{}",
        dedup_key,
    )
    if row is None:
        raise RuntimeError(
            f"upsert_alert returned no row for dedup_key={dedup_key!r}"
        )
    return Alert.from_record(row)


async def resolve_alert(
    conn: Any,
    *,
    organization_id: UUID,
    dedup_key: str,
) -> Optional[Alert]:
    """Mark the active alert with the given dedup_key as resolved.

    Returns the resolved row, or None if no active alert matched (already
    resolved, or never existed). Producers call this when the underlying
    condition clears (e.g. successful charger auth after a string of
    failures).
    """
    row = await conn.fetchrow(
        f"""
        UPDATE notification_alerts
           SET status = 'resolved',
               resolved_at = NOW(),
               updated_at = NOW()
         WHERE organization_id = $1
           AND dedup_key = $2
           AND status != 'resolved'
         RETURNING {_ALERT_COLUMNS}
        """,
        organization_id,
        dedup_key,
    )
    return Alert.from_record(row) if row else None


# ---------------------------------------------------------------------------
# Deliveries ledger
# ---------------------------------------------------------------------------


async def record_delivery(
    conn: Any,
    *,
    alert_id: UUID,
    recipient_id: UUID,
    notified_count: int,
    provider_message_id: Optional[str],
    status: str = "sent",
    status_detail: Optional[dict] = None,
    channel: str = "email",
) -> bool:
    """Insert a notification_deliveries row idempotently.

    Returns True if a new row was inserted, False if the (alert_id,
    recipient_id, notified_count) tuple already existed (silently dedup'd).
    """
    result = await conn.execute(
        """
        INSERT INTO notification_deliveries (
            alert_id, recipient_id, notified_count, channel,
            provider_message_id, status, status_detail
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (alert_id, recipient_id, notified_count) DO NOTHING
        """,
        alert_id,
        recipient_id,
        notified_count,
        channel,
        provider_message_id,
        status,
        json.dumps(status_detail) if status_detail else None,
    )
    return result.endswith(" 1")


async def update_delivery_status(
    conn: Any,
    *,
    provider_message_id: str,
    status: str,
    status_detail: Optional[dict] = None,
) -> bool:
    """Update a delivery row's status (used by Resend webhook).

    Returns True if a row matched, False if no delivery exists with this
    provider_message_id (e.g. webhook arrived before the INSERT committed —
    rare but possible; caller should retry).
    """
    result = await conn.execute(
        """
        UPDATE notification_deliveries
           SET status = $2,
               status_detail = $3::jsonb,
               provider_updated_at = NOW()
         WHERE provider_message_id = $1
        """,
        provider_message_id,
        status,
        json.dumps(status_detail) if status_detail else None,
    )
    return result.endswith(" 0") is False


async def list_for_org(
    conn: Any,
    org_id: UUID,
    *,
    status_filter: Optional[str] = None,
    severity_filter: Optional[str] = None,
    depot_id_filter: Optional[UUID] = None,
    alert_type_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Alert], int]:
    """Paginated org-scoped alert list for GET /alerts.

    Returns (items, total_count). Sorted by last_occurrence_at DESC.
    """
    conditions: list[str] = ["organization_id = $1"]
    params: list[Any] = [org_id]
    idx = 2

    if status_filter is not None:
        conditions.append(f"status = ${idx}")
        params.append(status_filter)
        idx += 1

    if severity_filter is not None:
        conditions.append(f"severity = ${idx}")
        params.append(severity_filter)
        idx += 1

    if depot_id_filter is not None:
        conditions.append(f"depot_id = ${idx}")
        params.append(depot_id_filter)
        idx += 1

    if alert_type_filter is not None:
        conditions.append(f"alert_type = ${idx}")
        params.append(alert_type_filter)
        idx += 1

    where = " AND ".join(conditions)

    total: int = int(
        await conn.fetchval(
            f"SELECT COUNT(*) FROM notification_alerts WHERE {where}",
            *params,
        )
        or 0
    )

    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT {_ALERT_COLUMNS}
          FROM notification_alerts
         WHERE {where}
         ORDER BY last_occurrence_at DESC
         LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params,
        page_size,
        offset,
    )
    return [Alert.from_record(r) for r in rows], total


async def acknowledge_for_org(
    conn: Any,
    alert_id: UUID,
    *,
    org_id: UUID,
    user_id: UUID,
    user_email: Optional[str] = None,
) -> Optional[Alert]:
    """Acknowledge an active alert scoped to an org.

    Returns the updated row, or None when the alert is not found, doesn't
    belong to the org, or is not in 'active' state.
    """
    row = await conn.fetchrow(
        f"""
        UPDATE notification_alerts
           SET status = 'acknowledged',
               acknowledged_at = NOW(),
               acknowledged_by = $3,
               acknowledged_by_email = $4,
               updated_at = NOW()
         WHERE id = $1
           AND organization_id = $2
           AND status = 'active'
         RETURNING {_ALERT_COLUMNS}
        """,
        alert_id,
        org_id,
        user_id,
        user_email,
    )
    return Alert.from_record(row) if row else None


async def resolve_by_id(
    conn: Any,
    alert_id: UUID,
    *,
    org_id: UUID,
    user_id: Optional[UUID] = None,
    user_email: Optional[str] = None,
) -> Optional[Alert]:
    """Resolve an active or acknowledged alert by id within an org.

    Preserves existing acknowledged_by/email via COALESCE.
    Returns the updated row, or None when not found or already resolved.
    """
    row = await conn.fetchrow(
        f"""
        UPDATE notification_alerts
           SET status = 'resolved',
               resolved_at = NOW(),
               acknowledged_by = COALESCE(acknowledged_by, $3),
               acknowledged_by_email = COALESCE(acknowledged_by_email, $4),
               updated_at = NOW()
         WHERE id = $1
           AND organization_id = $2
           AND status != 'resolved'
         RETURNING {_ALERT_COLUMNS}
        """,
        alert_id,
        org_id,
        user_id,
        user_email,
    )
    return Alert.from_record(row) if row else None


__all__ = [
    "Alert",
    "claim_pending_alerts",
    "mark_notified",
    "list_for_depot",
    "list_for_org",
    "get_by_id",
    "acknowledge",
    "acknowledge_for_org",
    "resolve_by_id",
    "upsert_alert",
    "resolve_alert",
    "record_delivery",
    "update_delivery_status",
]
