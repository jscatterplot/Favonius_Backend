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
    acknowledged_at: Optional[datetime]
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
            acknowledged_at=row["acknowledged_at"],
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


_CLAIM_QUERY = """
    SELECT id, organization_id, depot_id, alert_type, severity, title, detail,
           dedup_key, status, first_occurrence_at, last_occurrence_at,
           acknowledged_at, last_notified_at, last_notified_count
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
        """
        SELECT id, organization_id, depot_id, alert_type, severity, title, detail,
               dedup_key, status, first_occurrence_at, last_occurrence_at,
               acknowledged_at, last_notified_at, last_notified_count
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
        """
        SELECT id, organization_id, depot_id, alert_type, severity, title, detail,
               dedup_key, status, first_occurrence_at, last_occurrence_at,
               acknowledged_at, last_notified_at, last_notified_count
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
        """
        UPDATE notification_alerts
           SET status = 'acknowledged',
               acknowledged_at = NOW(),
               acknowledged_by = $2,
               updated_at = NOW()
         WHERE id = $1
           AND status = 'active'
         RETURNING id, organization_id, depot_id, alert_type, severity, title, detail,
                   dedup_key, status, first_occurrence_at, last_occurrence_at,
                  acknowledged_at, last_notified_at, last_notified_count
        """,
        alert_id,
        user_id,
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


__all__ = [
    "Alert",
    "claim_pending_alerts",
    "mark_notified",
    "list_for_depot",
    "get_by_id",
    "acknowledge",
    "record_delivery",
    "update_delivery_status",
]
