"""Per-turn audit writers for the depot chat agent.

The agent leaves two trails:

- ``agent_runs`` is a domain-specific run record (mirrors
  ``optimization_runs``): one row per turn with a JSONB step log,
  outcome status, and duration. Indexed for "show this user's last 50
  chats" UX. See ``migrations/025_agent_runs.sql``.
- ``audit_log`` is the existing admin audit feed. Every executed query
  also writes one ``action='agent.query'`` row here so agent reads are
  visible alongside the cross-org reads and credential rotations
  admins already monitor. See ``src/security/admin_audit.py``.

The writers in this module are intentionally tiny — every helper
turns into a single SQL statement so the per-turn cost is bounded and
each side effect is independently mockable in tests.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from src.api.agent.auth_context import AuthContext
from src.security.admin_audit import AdminAuditRow, write_admin_audit_row


async def agent_runs_open(ts_pool: Any, auth: AuthContext, message: str) -> UUID:
    """Open a new ``agent_runs`` row and return its ``run_id``.

    The placeholder ``status='running'`` matches the migration's CHECK
    constraint and signals that the row is mid-turn. The final status
    (``success`` / ``disambiguation`` / ``not_found`` / ``error``) is
    written by :func:`agent_runs_close`.

    Args:
        ts_pool: asyncpg pool for the time-series database where
            ``agent_runs`` lives.
        auth: The caller's :class:`AuthContext`. ``organization_id``
            is stored as-is (``NULL`` for favonius_admin).
        message: The raw user message that opened the turn.

    Returns:
        The newly inserted ``run_id``.
    """
    async with ts_pool.acquire() as conn:
        run_id = await conn.fetchval(
            """
            INSERT INTO agent_runs (
                user_id,
                organization_id,
                depot_id,
                user_message,
                status
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'running')
            RETURNING run_id
            """,
            str(auth.user_id),
            str(auth.organization_id) if auth.organization_id else None,
            None,  # depot_id may span multiple; populated by the close step if known
            message,
        )
    return UUID(str(run_id))


async def agent_runs_step(
    ts_pool: Any,
    run_id: UUID,
    name: str,
    payload: Any,
) -> None:
    """Append one step to a run's ``steps_json`` array.

    Each step is stored as ``{"name": ..., "payload": ...}``. The
    JSONB ``||`` operator concatenates arrays, so wrapping the entry
    in a one-element list extends the existing trace rather than
    replacing it.

    Args:
        ts_pool: asyncpg pool for the time-series database.
        run_id: The ``run_id`` returned by :func:`agent_runs_open`.
        name: Step name (e.g. ``extract_plan``, ``resolve_entities``,
            ``compile``, ``execute``).
        payload: Arbitrary JSON-serializable payload. Datetimes and
            UUIDs are coerced via ``json.dumps(default=str)``.
    """
    step_blob = json.dumps([{"name": name, "payload": payload}], default=str)
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET steps_json = steps_json || $1::jsonb
            WHERE run_id = $2::uuid
            """,
            step_blob,
            str(run_id),
        )


async def agent_runs_close(
    ts_pool: Any,
    run_id: UUID,
    status: str,
    reply: Any,
) -> None:
    """Stamp final status, duration, and intent onto a run.

    ``duration_ms`` is computed from the row's ``created_at`` so it
    reflects the wall-clock time of the turn end-to-end and is captured
    once (idempotent re-calls don't reset it). ``final_intent`` is
    pulled from ``reply.intent`` when present (dict or attribute) so
    callers can pass either an :class:`AgentReply` instance or a plain
    dict produced by the formatter.

    Args:
        ts_pool: asyncpg pool for the time-series database.
        run_id: The ``run_id`` returned by :func:`agent_runs_open`.
        status: One of ``success``, ``disambiguation``, ``not_found``,
            ``error``. Other values violate the migration's CHECK
            constraint.
        reply: The end-of-turn reply object (or dict). Inspected for
            an ``intent`` field; ignored otherwise.
    """
    final_intent = _extract_intent(reply)
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET status       = $1,
                final_intent = COALESCE($2, final_intent),
                duration_ms  = COALESCE(
                    duration_ms,
                    (EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000)::int
                )
            WHERE run_id = $3::uuid
            """,
            status,
            final_intent,
            str(run_id),
        )


async def write_agent_query_audit(
    ts_pool: Any,
    auth: AuthContext,
    run_id: UUID,
    intent: str,
    row_count: int,
    depot_id: Optional[UUID] = None,
) -> None:
    """Mirror an executed agent query into ``audit_log``.

    Thin wrapper around :func:`write_admin_audit_row` that fills in
    the agent-specific fields. The ``target_id`` is the agent's
    ``run_id`` (stringified) so a reviewer can pivot from the
    ``audit_log`` row back to the full step trace in ``agent_runs``.

    Args:
        ts_pool: asyncpg pool used by the underlying
            :func:`write_admin_audit_row` (passed through unchanged).
        auth: The caller's :class:`AuthContext`. ``user_id`` and
            ``role`` populate the actor fields; ``organization_id``
            populates the org column.
        run_id: The agent's per-turn run identifier. Stored as
            ``target_id`` so the audit row links back to ``agent_runs``.
        intent: The compiled intent name (e.g. ``consumption_by_user``).
        row_count: Number of rows the query returned.
        depot_id: Optional depot UUID when the query targets a single
            depot. ``None`` when the query may span multiple depots.
    """
    row = AdminAuditRow(
        action="agent.query",
        actor_user_id=str(auth.user_id),
        actor_role=auth.role,
        organization_id=str(auth.organization_id) if auth.organization_id else None,
        depot_id=str(depot_id) if depot_id else None,
        target_type="charging_sessions",
        target_id=str(run_id),
        metadata={"intent": intent, "row_count": row_count},
    )
    await write_admin_audit_row(ts_pool, row)


def _extract_intent(reply: Any) -> Optional[str]:
    """Return ``reply.intent`` if present (dict or attribute access)."""
    if reply is None:
        return None
    if isinstance(reply, dict):
        value = reply.get("intent")
    else:
        value = getattr(reply, "intent", None)
    return str(value) if value is not None else None
