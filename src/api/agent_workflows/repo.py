"""Persistence layer for :class:`Decision` rows.

Two implementations ship:

- :class:`AsyncpgDecisionRepo` is the production path, writing to the
  ``decisions`` table created by migration ``037_decisions.sql``. The
  table has triggers that block ``UPDATE`` and ``DELETE`` to enforce
  PRD §10.4 audit immutability — edits insert a new row with
  ``edits_decision_id`` set to the original's id.
- :class:`InMemoryDecisionRepo` is the test path. It satisfies the
  same :class:`DecisionRepo` protocol so the runtime never imports
  asyncpg in unit-test environments.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol
from uuid import UUID

from src.api.agent_workflows.schemas import Decision

logger = logging.getLogger(__name__)


class DecisionRepo(Protocol):
    """Append-only repository of :class:`Decision` rows."""

    async def write(self, decision: Decision) -> None:
        """Persist one decision. Must never overwrite an existing row."""


class InMemoryDecisionRepo:
    """In-memory repo used by tests and offline replays.

    Keeps an ordered list so tests can assert call order. Raises if a
    duplicate id is written, matching the production trigger's
    behaviour (the trigger lets the ``INSERT`` succeed but blocks the
    subsequent ``UPDATE``; here we surface duplicates as a hard error).
    """

    def __init__(self) -> None:
        self._rows: list[Decision] = []
        self._ids: set[UUID] = set()

    async def write(self, decision: Decision) -> None:
        if decision.id in self._ids:
            raise ValueError(f"Decision id {decision.id} already written; the repo is append-only.")
        self._ids.add(decision.id)
        self._rows.append(decision)

    @property
    def rows(self) -> list[Decision]:
        """Read-only view of every decision written so far."""
        return list(self._rows)


_INSERT_SQL = """
INSERT INTO decisions (
    id,
    workflow_id,
    workflow_version,
    depot_id,
    user_id,
    organization_id,
    permission_tier,
    inputs_hash,
    tool_calls,
    output,
    rule_applied,
    disposition,
    constraint_violations,
    edits_decision_id,
    model_id,
    latency_ms,
    input_tokens,
    output_tokens,
    stop_reason,
    created_at
) VALUES (
    $1::uuid, $2, $3, $4::uuid, $5::uuid, $6::uuid, $7, $8,
    $9::jsonb, $10::jsonb, $11, $12, $13::jsonb, $14::uuid,
    $15, $16, $17, $18, $19, $20
)
"""


class AsyncpgDecisionRepo:
    """Production repo backed by the ``decisions`` table.

    ``pool`` is an :class:`asyncpg.Pool`. The repo uses one connection
    per ``write`` and serialises JSONB fields with ``json.dumps`` so
    asyncpg accepts them as text.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def write(self, decision: Decision) -> None:
        payload = decision.model_dump(mode="json")
        tool_calls_json = json.dumps(payload["tool_calls"], default=str)
        output_json = json.dumps(payload["output"], default=str)
        violations_json = json.dumps(payload["constraint_violations"], default=str)
        async with self._pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                str(decision.id),
                decision.workflow_id,
                decision.workflow_version,
                str(decision.depot_id),
                str(decision.user_id),
                str(decision.organization_id) if decision.organization_id else None,
                decision.permission_tier.value,
                decision.inputs_hash,
                tool_calls_json,
                output_json,
                decision.rule_applied,
                decision.disposition,
                violations_json,
                str(decision.edits_decision_id) if decision.edits_decision_id else None,
                decision.model_id,
                decision.latency_ms,
                decision.input_tokens,
                decision.output_tokens,
                decision.stop_reason,
                decision.created_at,
            )


__all__ = ["AsyncpgDecisionRepo", "DecisionRepo", "InMemoryDecisionRepo"]
