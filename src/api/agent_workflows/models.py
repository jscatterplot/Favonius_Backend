"""Pydantic v2 types for the Depot Agent workflow substrate.

The shapes here mirror the dataclasses sketched in
``docs/PRD_Depot_Agent.md`` §5.2 (Workflow object) and §5.3 (Decision /
audit record). They are pure data transfer objects — no DB access, no
business logic — so the same module can be safely imported by the
repository, future tool layer, future API, and tests.

Type rules:

* UUIDs use :class:`uuid.UUID` so callers cannot pass arbitrary strings.
* Timestamps use timezone-aware :class:`datetime.datetime`.
* :class:`PermissionTier` and :class:`Disposition` are
  :class:`enum.StrEnum` subclasses, so they round-trip cleanly through
  JSON and match the SQL ``CHECK`` constraints in migration 037
  one-for-one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PermissionTier(StrEnum):
    """The four trust tiers an agent action can run under (PRD §9.1).

    Tier graduation is per workflow per depot (PRD §9.3) — every
    ``(workflow_id, depot_id)`` pair carries its own tier.
    """

    INFORM = "inform"
    DRAFT_AND_WAIT = "draft_and_wait"
    ACT_AND_NOTIFY = "act_and_notify"
    AUTONOMOUS = "autonomous"


class Disposition(StrEnum):
    """How a human disposed of a decision (PRD §5.3).

    ``auto_executed`` is reserved for ``act_and_notify`` and
    ``autonomous`` tiers, where the agent has already acted by the time
    the row is written.
    """

    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    AUTO_EXECUTED = "auto_executed"


class GraduationRule(BaseModel):
    """Per-(workflow, depot) rule that controls tier graduation.

    See PRD §5.2 and §9.3. ``next_tier`` is the tier this row's workflow
    promotes to when the rule fires; it may be ``None`` if the workflow
    is already at the ceiling for its depot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_decisions: int = Field(..., ge=0)
    max_override_rate: float = Field(..., ge=0.0, le=1.0)
    max_edit_rate: float = Field(..., ge=0.0, le=1.0)
    requires_human_signoff: bool = True
    next_tier: Optional[PermissionTier] = None


class Workflow(BaseModel):
    """A named, versioned workflow definition (PRD §5.2).

    The runtime resolves ``allowed_tools`` against a registry to
    decide which tool calls the LLM may emit during this workflow's
    turn. ``parameters`` carries customer-tunable knobs (Tier-1
    openness, PRD §7.4); the registry validates them against the
    workflow's schema before any agent invocation.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    description: str = ""
    prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ToolCall(BaseModel):
    """One tool invocation in a decision's audit trail.

    Captured verbatim from the agent so an auditor can replay the
    inputs/outputs without re-running the LLM. ``ok`` distinguishes
    successful tool returns from raised errors; ``error`` carries the
    error message when ``ok`` is false.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    ok: bool = True
    error: Optional[str] = None


class Decision(BaseModel):
    """An immutable record of one agent decision (PRD §5.3, §10.4).

    Stored in the ``decisions`` hypertable. UPDATE/DELETE are blocked
    by a row-level trigger; edits write a new row with
    ``parent_decision_id`` pointing at the original and
    ``diff_if_edited`` carrying the delta.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    workflow_id: UUID
    depot_id: UUID
    organization_id: UUID
    timestamp: datetime
    inputs_hash: str = Field(..., min_length=1)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    rule_applied: Optional[str] = None
    disposition: Disposition
    human_user_id: Optional[UUID] = None
    diff_if_edited: Optional[dict[str, Any]] = None
    parent_decision_id: Optional[UUID] = None
