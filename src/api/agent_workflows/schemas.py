"""Workflow + decision schemas (PRD §5.2 and §5.3).

These dataclasses are the persistent representation of:

- A :class:`Workflow` — a named, versioned object the agent runs. The
  same workflow definition is loaded across depots; per-depot tier and
  parameter overrides live elsewhere (per `(workflow, depot)` rows in
  the deployed schema) and are projected onto the
  :class:`Workflow.permission_tier` and :class:`Workflow.parameters`
  fields at load time.
- A :class:`Decision` — the immutable audit record produced by one turn
  of the agent. One row per turn. Edits and approvals reference the
  original by id (PRD §10.4).

Sprint 1 was scoped to land these schemas plus the migrations; until
Sprint 1 lands canonically, they live here so the Sprint 2 runtime can
compile and be tested in isolation. The intent is to keep this module
*structural*: no I/O, no validation that requires a database round-trip.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PermissionTier(str, Enum):
    """Per-(workflow, depot) trust tier (PRD §9.1).

    The string values are the names used in the persistent representation
    and on the wire. Inheriting from :class:`str` makes them JSON-
    serialisable without a custom encoder.
    """

    INFORM = "inform"
    DRAFT_AND_WAIT = "draft_and_wait"
    ACT_AND_NOTIFY = "act_and_notify"
    AUTONOMOUS = "autonomous"


# Disposition values for the audit row. ``pending`` is the only value the
# runtime itself writes — humans (or the act-and-notify pathway, in a
# later sprint) advance to ``approved`` / ``edited`` / ``rejected`` /
# ``auto_executed``.
Disposition = Literal["pending", "approved", "edited", "rejected", "auto_executed"]


class ToolCall(BaseModel):
    """One tool invocation inside a workflow turn.

    The runtime captures the input as observed (already JSON-coerced by
    the Anthropic SDK), the output it produced (either the tool's return
    value coerced to ``dict`` or an ``{"error": ...}`` envelope when the
    dispatch was blocked or raised), and the wall-clock duration in
    milliseconds.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = False


class Workflow(BaseModel):
    """A named workflow the agent can run (PRD §5.2).

    ``allowed_tools`` is the per-workflow allow-list of tool names the
    runtime will let the LLM call. The runtime adds the reserved
    ``emit_decision`` terminator separately; it is not (and must not
    be) listed here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: str
    description: str
    prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    permission_tier: PermissionTier = PermissionTier.DRAFT_AND_WAIT


class Decision(BaseModel):
    """One immutable audit record produced by a workflow turn (PRD §5.3).

    Append-only. Edits are recorded as new records that reference the
    original ``id`` (PRD §10.4).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    workflow_id: str
    depot_id: UUID
    timestamp: datetime
    inputs_hash: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    rule_applied: Optional[str] = None
    disposition: Disposition = "pending"
    human_user_id: Optional[UUID] = None
    diff_if_edited: Optional[str] = None
