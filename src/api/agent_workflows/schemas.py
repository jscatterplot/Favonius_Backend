"""Workflow + Decision schemas for the depot-agent workflow runtime.

Mirrors the dataclasses sketched in ``docs/PRD_Depot_Agent.md``
§5.2 (workflow) and §5.3 (decision/audit record). Pydantic is used
over plain dataclasses so the same models round-trip through JSONB
columns, FastAPI bodies, and snapshot tests without an adapter layer.

Field-level notes:

- :class:`Workflow` is frozen at construction: the prompt and the
  tool allow-list are the workflow's identity, and the runtime caches
  the Anthropic system-prompt block against them. Mutating them in
  place would produce a stale cache key without any test failure.
- :class:`Decision` is the source of truth for what an agent did.
  Edits are not in-place mutations — per PRD §10.4 they are *new*
  rows that reference the original via ``edits_decision_id``. The
  schema therefore exposes the field but the database trigger
  installed by migration 037 blocks ``UPDATE``/``DELETE`` outright.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class PermissionTier(str, Enum):
    """The four trust tiers from PRD §5.2."""

    INFORM = "inform"
    DRAFT_AND_WAIT = "draft_and_wait"
    ACT_AND_NOTIFY = "act_and_notify"
    AUTONOMOUS = "autonomous"


class GraduationRule(BaseModel):
    """When a workflow may advance to its next permission tier (§5.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_decisions: int = Field(ge=0)
    max_override_rate: float = Field(ge=0.0, le=1.0)
    max_edit_rate: float = Field(ge=0.0, le=1.0)
    requires_human_signoff: bool
    next_tier: PermissionTier


class Workflow(BaseModel):
    """A named, evaluable agent recipe (PRD §4.4, §5.2).

    ``allowed_tools`` is the read-only contract between the workflow
    author and the runtime: the model only ever sees this subset of
    the :class:`~src.api.agent_workflows.tools.ToolRegistry`. Tuple-typed
    so it cannot be mutated post-hoc.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    prompt: str = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    parameters: dict[str, Any] = Field(default_factory=dict)
    permission_tier: PermissionTier
    graduation_rule: GraduationRule
    eval_set_id: Optional[str] = None


class ToolCallRecord(BaseModel):
    """One observed tool call within a turn.

    ``output`` is whatever the tool returned, JSON-serialised by the
    caller before persistence. ``duration_ms`` is captured for
    operability; it is excluded from :attr:`Decision.inputs_hash` so
    the hash stays stable across retries of identical tool sequences.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    input: dict[str, Any]
    output: Any = None
    duration_ms: int = Field(ge=0, default=0)
    error: Optional[str] = None


_DISPOSITIONS = ("pending", "approved", "edited", "rejected", "auto_executed")


class Decision(BaseModel):
    """Append-only audit record for one workflow turn (PRD §5.3, §10.4).

    The runtime always writes ``disposition='pending'`` for V1 since the
    only tiers that ship are ``inform`` and ``draft_and_wait`` — see
    PRD §10.2 ("All outbound emails, vendor tickets, and customer
    messages are drafted in V1"). The other disposition values exist on
    the schema so the human-review surface can record approvals/edits
    later by inserting *new* decision rows that link back via
    ``edits_decision_id``.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    workflow_id: str
    workflow_version: str
    depot_id: UUID
    user_id: UUID
    organization_id: Optional[UUID] = None
    permission_tier: PermissionTier
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    inputs_hash: str = Field(min_length=64, max_length=64)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    rule_applied: Optional[str] = None

    disposition: Literal["pending", "approved", "edited", "rejected", "auto_executed"] = "pending"
    constraint_violations: list[str] = Field(default_factory=list)
    edits_decision_id: Optional[UUID] = None

    model_id: str
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)
    stop_reason: Optional[str] = None


__all__ = [
    "Decision",
    "GraduationRule",
    "PermissionTier",
    "ToolCallRecord",
    "Workflow",
]
