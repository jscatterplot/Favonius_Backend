"""notification_recipients repository.

CRUD + filtered lookup. The lookup query (list_for_alert) is hot-path —
called once per alert per dispatcher tick — so it relies on the
(organization_id, active, min_severity_level) INCLUDE (email, alert_types)
index from migration 022.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from .severity import Severity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipient:
    """A row from notification_recipients."""

    id: UUID
    organization_id: UUID
    email: str
    display_name: Optional[str]
    alert_types: list[str]
    min_severity: Severity
    active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, row: Any) -> "Recipient":
        return cls(
            id=row["id"],
            organization_id=row["organization_id"],
            email=row["email"],
            display_name=row["display_name"],
            alert_types=list(row["alert_types"]),
            min_severity=Severity.from_str(row["min_severity"]),
            active=row["active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Dispatcher-side lookup
# ---------------------------------------------------------------------------


async def list_for_alert(
    conn: Any,
    *,
    organization_id: UUID,
    alert_type: str,
    severity: Severity,
) -> list[Recipient]:
    """Return active recipients matching an alert.

    Filters in SQL:
      - active = TRUE
      - severity meets recipient's min_severity (severity.level >= min_severity_level)
      - alert_types contains '*' OR contains the specific alert_type

    The ARRAY containment uses the && operator so a single index on
    (organization_id, active, min_severity_level) covers the predicate.
    """
    rows = await conn.fetch(
        """
        SELECT id, organization_id, email, display_name, alert_types,
               min_severity, active, created_at, updated_at
          FROM notification_recipients
         WHERE organization_id = $1
           AND active = TRUE
           AND min_severity_level <= $2
           AND alert_types && ARRAY[$3, '*']::TEXT[]
         ORDER BY email
        """,
        organization_id,
        severity.level,
        alert_type,
    )
    return [Recipient.from_record(r) for r in rows]


# ---------------------------------------------------------------------------
# CRUD (admin endpoints)
# ---------------------------------------------------------------------------


async def list_for_org(
    conn: Any, organization_id: UUID, *, include_inactive: bool = False
) -> list[Recipient]:
    if include_inactive:
        rows = await conn.fetch(
            """
            SELECT id, organization_id, email, display_name, alert_types,
                   min_severity, active, created_at, updated_at
              FROM notification_recipients
             WHERE organization_id = $1
             ORDER BY email
            """,
            organization_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, organization_id, email, display_name, alert_types,
                   min_severity, active, created_at, updated_at
              FROM notification_recipients
             WHERE organization_id = $1
               AND active = TRUE
             ORDER BY email
            """,
            organization_id,
        )
    return [Recipient.from_record(r) for r in rows]


async def get_by_id(
    conn: Any, recipient_id: UUID, *, organization_id: Optional[UUID] = None
) -> Optional[Recipient]:
    """Fetch by id; if organization_id is given, scope the read to that org
    (defense-in-depth on top of API authz)."""
    if organization_id is None:
        row = await conn.fetchrow(
            """
            SELECT id, organization_id, email, display_name, alert_types,
                   min_severity, active, created_at, updated_at
              FROM notification_recipients
             WHERE id = $1
            """,
            recipient_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, organization_id, email, display_name, alert_types,
                   min_severity, active, created_at, updated_at
              FROM notification_recipients
             WHERE id = $1
               AND organization_id = $2
            """,
            recipient_id,
            organization_id,
        )
    return Recipient.from_record(row) if row else None


async def create(
    conn: Any,
    *,
    organization_id: UUID,
    email: str,
    display_name: Optional[str] = None,
    alert_types: Sequence[str] = ("*",),
    min_severity: Severity = Severity.WARNING,
) -> Recipient:
    """Insert a recipient. Raises asyncpg.UniqueViolationError if (org, email)
    already exists; callers should translate to a 409 at the API layer."""
    row = await conn.fetchrow(
        """
        INSERT INTO notification_recipients (
            organization_id, email, display_name, alert_types, min_severity
        ) VALUES ($1, $2, $3, $4::TEXT[], $5)
        RETURNING id, organization_id, email, display_name, alert_types,
                  min_severity, active, created_at, updated_at
        """,
        organization_id,
        email,
        display_name,
        list(alert_types),
        min_severity.value,
    )
    return Recipient.from_record(row)


async def update(
    conn: Any,
    recipient_id: UUID,
    *,
    organization_id: UUID,
    display_name: Optional[str] = None,
    alert_types: Optional[Sequence[str]] = None,
    min_severity: Optional[Severity] = None,
    active: Optional[bool] = None,
) -> Optional[Recipient]:
    """Patch a recipient. Only provided fields are updated. Scoped to org for
    defense-in-depth."""
    row = await conn.fetchrow(
        """
        UPDATE notification_recipients
           SET display_name  = COALESCE($3, display_name),
               alert_types   = COALESCE($4::TEXT[], alert_types),
               min_severity  = COALESCE($5, min_severity),
               active        = COALESCE($6, active),
               updated_at    = NOW()
         WHERE id = $1
           AND organization_id = $2
         RETURNING id, organization_id, email, display_name, alert_types,
                   min_severity, active, created_at, updated_at
        """,
        recipient_id,
        organization_id,
        display_name,
        list(alert_types) if alert_types is not None else None,
        min_severity.value if min_severity is not None else None,
        active,
    )
    return Recipient.from_record(row) if row else None


async def delete(
    conn: Any, recipient_id: UUID, *, organization_id: UUID
) -> bool:
    """Hard delete (cascades to notification_deliveries). Returns True if a
    row was deleted."""
    result = await conn.execute(
        """
        DELETE FROM notification_recipients
         WHERE id = $1
           AND organization_id = $2
        """,
        recipient_id,
        organization_id,
    )
    return result.endswith(" 1")


__all__ = [
    "Recipient",
    "list_for_alert",
    "list_for_org",
    "get_by_id",
    "create",
    "update",
    "delete",
]
