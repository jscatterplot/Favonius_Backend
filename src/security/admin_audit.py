"""Admin audit log writer for cross-org reads and credential rotations.

This module is distinct from ``security/audit_log.py`` (which writes to the
``security_audit_log`` TimescaleDB hypertable for NKSC / NIS2 logs).

``audit_log`` stores application-level admin actions:
    - admin.read              — cross-org read by favonius_admin
    - charger.credentials.rotated
    - (future admin actions: depot.updated, etc.)

Every row is keyed by actor_user_id, actor_role, organization_id, depot_id,
action, target_type, target_id, metadata (JSONB).

Two write modes:
    - best-effort (default): failures are logged and never raised — used for
      audit rows that complement the endpoint response but are not required
      for correctness.
    - strict (``strict=True``): failures are raised so the caller can fail
      the request closed — used for cross-org admin enumeration where the
      audit row is part of the security guarantee (negative-result reads
      MUST leave a trail).
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


class AdminAuditWriteError(RuntimeError):
    """Raised by write_admin_audit_row when strict=True and the insert fails.

    Callers in strict mode should translate this to an HTTP 503 with
    ``error_code=AUDIT_LOG_UNAVAILABLE`` so the endpoint fails closed instead
    of returning data without a trail.
    """


async def write_admin_audit_row(pool: Any, row: AdminAuditRow, *, strict: bool = False) -> None:
    """Insert a single audit_log row.

    Args:
        pool: asyncpg pool. ``audit_log`` lives in TimescaleDB (mig 021), so
            production callers pass ``db_pools.ts``. The legacy parameter
            name is preserved for backward compatibility with existing tests.
        row: AdminAuditRow to insert.
        strict: When True, propagate insert failures as
            ``AdminAuditWriteError`` so the caller can fail the request
            closed. Default False preserves best-effort semantics.
    """
    if pool is None:
        logger.warning(
            "Admin audit: no DB pool — dropping action=%s target=%s/%s",
            row.action,
            row.target_type,
            row.target_id,
        )
        if strict:
            raise AdminAuditWriteError(f"Admin audit pool unavailable for action={row.action}")
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
    except Exception as exc:
        logger.warning(
            "Admin audit insert failed action=%s actor=%s target=%s/%s strict=%s",
            row.action,
            row.actor_user_id,
            row.target_type,
            row.target_id,
            strict,
            exc_info=True,
        )
        if strict:
            raise AdminAuditWriteError(
                f"Admin audit insert failed for action={row.action}"
            ) from exc
