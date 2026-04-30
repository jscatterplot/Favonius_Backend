"""Admin audit log writer for cross-org reads and credential rotations.

This module is distinct from ``security/audit_log.py`` (which writes to the
``security_audit_log`` TimescaleDB hypertable for NKSC / NIS2 logs).

``audit_log`` stores application-level admin actions:
    - admin.read              — cross-org read by favonius_admin
    - charger.credentials.rotated
    - (future admin actions: depot.updated, etc.)

Every row is keyed by actor_user_id, actor_role, organization_id, depot_id,
action, target_type, target_id, metadata (JSONB).

Writes are best-effort: failures are logged and never raised back into the
request path. The audit row is recorded *after* the endpoint succeeds via a
FastAPI dependency (see ``record_admin_action`` below).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class AdminAuditRow:
    """One row in the ``audit_log`` table.

    Attributes:
        action: Dotted action identifier (e.g. ``admin.read``).
        actor_user_id: Supabase user UUID making the request.
        actor_role: Favonius role from JWT (e.g. ``favonius_admin``).
        organization_id: Caller's org_id (None for favonius_admin).
        depot_id: Optional depot UUID this action targets.
        target_type: e.g. ``organization``, ``depot``, ``charger``.
        target_id: Stringified identifier of the target.
        metadata: Free-form JSON details (never include plaintext credentials).
        occurred_at: Defaults to UTC now.
    """

    action: str
    actor_user_id: Optional[str] = None
    actor_role: Optional[str] = None
    organization_id: Optional[str] = None
    depot_id: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.occurred_at is None:
            self.occurred_at = datetime.now(timezone.utc)


async def write_admin_audit_row(pool: Any, row: AdminAuditRow) -> None:
    """Insert a single audit_log row. Errors are logged, never raised.

    Args:
        pool: asyncpg pool (typically static Supabase pool).
        row: AdminAuditRow to insert.
    """
    if pool is None:
        logger.warning(
            "Admin audit: no DB pool — dropping action=%s target=%s/%s",
            row.action,
            row.target_type,
            row.target_id,
        )
        return

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (
                    occurred_at,
                    actor_user_id,
                    actor_role,
                    organization_id,
                    depot_id,
                    action,
                    target_type,
                    target_id,
                    metadata
                )
                VALUES ($1, $2::uuid, $3, $4::uuid, $5::uuid, $6, $7, $8, $9::jsonb)
                """,
                row.occurred_at,
                row.actor_user_id,
                row.actor_role,
                row.organization_id,
                row.depot_id,
                row.action,
                row.target_type,
                row.target_id,
                json.dumps(row.metadata or {}, default=str),
            )
    except Exception:
        logger.warning(
            "Admin audit insert failed action=%s actor=%s target=%s/%s",
            row.action,
            row.actor_user_id,
            row.target_type,
            row.target_id,
            exc_info=True,
        )
