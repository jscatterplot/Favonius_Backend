"""Async repository for the Depot Agent workflow tables.

Backed by the TimescaleDB pool initialised in ``src/api/main.py`` (the
``ts`` field of :class:`src.db.pools.DatabasePools`). Production callers
pass that pool in; tests pass a mock that quacks like ``asyncpg.Pool``.

All queries are parametrised — no string interpolation of caller input.
The repository deliberately exposes a thin surface: workflow lookup by
name, per-(workflow, depot) tier read/upsert, decision insert, and
windowed decision listing. The agent runtime and CLI tools call into
these and nothing else.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic_core import to_jsonable_python

from src.api.agent_workflows.models import (
    Decision,
    Disposition,
    GraduationRule,
    PermissionTier,
    ToolCall,
    Workflow,
)

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────


class WorkflowNotFoundError(LookupError):
    """Raised when :func:`get_workflow` finds no row for the given name."""


# ── workflows ─────────────────────────────────────────────────────────────


_WORKFLOW_COLUMNS = (
    "id, name, version, description, prompt, allowed_tools, parameters, " "created_at, updated_at"
)


async def get_workflow(pool: Any, name: str) -> Workflow:
    """Return the workflow with the given unique ``name``.

    Raises:
        WorkflowNotFoundError: when no row matches.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE name = $1",
            name,
        )
    if row is None:
        raise WorkflowNotFoundError(f"workflow not found: {name!r}")
    return _workflow_from_row(row)


async def upsert_workflow(
    pool: Any,
    *,
    name: str,
    version: str,
    description: str,
    prompt: str,
    allowed_tools: list[str],
    parameters: dict[str, Any],
) -> Workflow:
    """Upsert a workflow row by unique ``name``.

    Used by the per-workflow startup registration paths (sprint 5 +
    later workflows). The row is fully rewritten on every call so the
    DB always tracks the code's prompt + tool allow-list + parameter
    defaults — drift is impossible.

    Returns the resulting :class:`Workflow` (insert or update — the
    returned row reflects whichever happened).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO workflows
                (name, version, description, prompt, allowed_tools, parameters,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW(), NOW())
            ON CONFLICT (name) DO UPDATE SET
                version       = EXCLUDED.version,
                description   = EXCLUDED.description,
                prompt        = EXCLUDED.prompt,
                allowed_tools = EXCLUDED.allowed_tools,
                parameters    = EXCLUDED.parameters,
                updated_at    = NOW()
            RETURNING {_WORKFLOW_COLUMNS}
            """,
            name,
            version,
            description,
            prompt,
            list(allowed_tools),
            json.dumps(parameters),
        )
    return _workflow_from_row(row)


# ── workflow_tiers ────────────────────────────────────────────────────────


async def get_tier(
    pool: Any,
    workflow_id: UUID,
    depot_id: UUID,
) -> Optional[tuple[PermissionTier, GraduationRule]]:
    """Return ``(tier, rule)`` for ``(workflow_id, depot_id)`` or ``None``.

    ``None`` means the workflow has never been configured for this depot
    yet — callers should fall back to the workflow's launch default
    (PRD §9.2: ``inform`` or ``draft_and_wait``).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                tier,
                min_decisions,
                max_override_rate,
                max_edit_rate,
                requires_human_signoff,
                next_tier
            FROM workflow_tiers
            WHERE workflow_id = $1 AND depot_id = $2
            """,
            workflow_id,
            depot_id,
        )
    if row is None:
        return None
    rule = GraduationRule(
        min_decisions=row["min_decisions"],
        max_override_rate=float(row["max_override_rate"]),
        max_edit_rate=float(row["max_edit_rate"]),
        requires_human_signoff=row["requires_human_signoff"],
        next_tier=PermissionTier(row["next_tier"]) if row["next_tier"] else None,
    )
    return PermissionTier(row["tier"]), rule


async def set_tier(
    pool: Any,
    workflow_id: UUID,
    depot_id: UUID,
    tier: PermissionTier,
    rule: GraduationRule,
) -> None:
    """Upsert the tier + graduation rule for ``(workflow_id, depot_id)``.

    Used by the eventual graduation / demotion endpoints (PRD §9.3,
    §9.4). Writes ``updated_at = NOW()`` so the audit log surface can
    show when a tier last changed.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workflow_tiers (
                workflow_id,
                depot_id,
                tier,
                min_decisions,
                max_override_rate,
                max_edit_rate,
                requires_human_signoff,
                next_tier,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (workflow_id, depot_id) DO UPDATE
            SET tier                   = EXCLUDED.tier,
                min_decisions          = EXCLUDED.min_decisions,
                max_override_rate      = EXCLUDED.max_override_rate,
                max_edit_rate          = EXCLUDED.max_edit_rate,
                requires_human_signoff = EXCLUDED.requires_human_signoff,
                next_tier              = EXCLUDED.next_tier,
                updated_at             = NOW()
            """,
            workflow_id,
            depot_id,
            tier.value,
            rule.min_decisions,
            rule.max_override_rate,
            rule.max_edit_rate,
            rule.requires_human_signoff,
            rule.next_tier.value if rule.next_tier else None,
        )


# ── decisions ─────────────────────────────────────────────────────────────


async def insert_decision(pool: Any, decision: Decision) -> None:
    """Insert one row into the append-only ``decisions`` hypertable.

    The DB-level trigger blocks UPDATE/DELETE, so edits to a prior
    decision must arrive as a new :class:`Decision` whose
    ``parent_decision_id`` references the original.
    """
    tool_calls_json = json.dumps([_tool_call_to_dict(tc) for tc in decision.tool_calls])
    output_json = json.dumps(to_jsonable_python(decision.output))
    diff_json = (
        json.dumps(to_jsonable_python(decision.diff_if_edited))
        if decision.diff_if_edited is not None
        else None
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO decisions (
                id,
                workflow_id,
                depot_id,
                organization_id,
                timestamp,
                inputs_hash,
                tool_calls,
                output,
                rule_applied,
                disposition,
                human_user_id,
                diff_if_edited,
                parent_decision_id
            )
            VALUES (
                $1, $2, $3, $4, $5, $6,
                $7::jsonb, $8::jsonb, $9, $10, $11, $12::jsonb, $13
            )
            """,
            decision.id,
            decision.workflow_id,
            decision.depot_id,
            decision.organization_id,
            decision.timestamp,
            decision.inputs_hash,
            tool_calls_json,
            output_json,
            decision.rule_applied,
            decision.disposition.value,
            decision.human_user_id,
            diff_json,
            decision.parent_decision_id,
        )


async def list_decisions(
    pool: Any,
    workflow_id: UUID,
    depot_id: UUID,
    since: datetime,
    limit: int = 100,
) -> list[Decision]:
    """List decisions for ``(workflow_id, depot_id)`` since ``since``.

    Ordered by timestamp descending — the natural shape for "what has
    the agent done lately on this depot's workflow?" reads. ``limit``
    is clamped to ``[1, 1000]`` to protect the API; callers that need
    a fuller dump should page on ``timestamp``.
    """
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                workflow_id,
                depot_id,
                organization_id,
                timestamp,
                inputs_hash,
                tool_calls,
                output,
                rule_applied,
                disposition,
                human_user_id,
                diff_if_edited,
                parent_decision_id
            FROM decisions
            WHERE workflow_id = $1 AND depot_id = $2 AND timestamp >= $3
            ORDER BY timestamp DESC
            LIMIT $4
            """,
            workflow_id,
            depot_id,
            since,
            limit,
        )
    return [_decision_from_row(r) for r in rows]


# ── Row → model helpers ───────────────────────────────────────────────────


def _workflow_from_row(row: Any) -> Workflow:
    return Workflow(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        description=row["description"] or "",
        prompt=row["prompt"],
        allowed_tools=list(row["allowed_tools"] or []),
        parameters=_loads(row["parameters"], default={}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _decision_from_row(row: Any) -> Decision:
    raw_tool_calls = _loads(row["tool_calls"], default=[])
    diff_raw = row["diff_if_edited"]
    diff = _loads(diff_raw, default=None) if diff_raw is not None else None
    return Decision(
        id=row["id"],
        workflow_id=row["workflow_id"],
        depot_id=row["depot_id"],
        organization_id=row["organization_id"],
        timestamp=row["timestamp"],
        inputs_hash=row["inputs_hash"],
        tool_calls=[ToolCall(**tc) for tc in raw_tool_calls],
        output=_loads(row["output"], default={}),
        rule_applied=row["rule_applied"],
        disposition=Disposition(row["disposition"]),
        human_user_id=row["human_user_id"],
        diff_if_edited=diff,
        parent_decision_id=row["parent_decision_id"],
    )


def _tool_call_to_dict(tc: ToolCall) -> dict[str, Any]:
    # ``mode="json"`` coerces nested UUIDs/datetimes in ``arguments``/``result``
    # to JSON-safe primitives for ``json.dumps`` (e.g. in ``insert_decision``).
    # ``mode="python"`` would keep native types and can raise ``TypeError`` on dump.
    return tc.model_dump(mode="json")


def _loads(value: Any, *, default: Any) -> Any:
    """Decode a JSONB column whether asyncpg gave us a str or a parsed value.

    With the default ``jsonb`` codec asyncpg returns Python objects, but
    some test doubles and older codec configurations return the raw JSON
    text. Handle both transparently.
    """
    if value is None:
        return default
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value
