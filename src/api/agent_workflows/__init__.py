"""Workflow agent runtime — one agent, many workflows, scoped tools.

This package implements the depot-agent workflow runtime (PRD §4.3 / §4.4):

- :class:`~src.api.agent_workflows.runtime.WorkflowAgent` runs one
  turn of a workflow against the Anthropic Messages API with tool-use.
- :class:`~src.api.agent_workflows.tools.ToolRegistry` is the single
  source of truth for tool callables and JSON schemas.
- :class:`~src.api.agent_workflows.constraints.HardConstraintGuard`
  enforces the substrate hard constraints (PRD §10.3) on every tool
  dispatch and on the final structured output.
- :class:`~src.api.agent_workflows.schemas.Workflow` and
  :class:`~src.api.agent_workflows.schemas.Decision` are the
  workflow-and-audit dataclasses (PRD §5.2, §5.3). They carry the
  Sprint 1 contract forward inside this package; if Sprint 1 ships a
  separate canonical location, re-export from there.

The runtime is intentionally HTTP-less for v1. It is exercised via
unit tests with fake tools and a fake Anthropic client.
"""

from src.api.agent_workflows.constraints import (
    ConstraintViolation,
    DepotConstraints,
    HardConstraintGuard,
)
from src.api.agent_workflows.repo import (
    DecisionRepo,
    InMemoryDecisionRepo,
)
from src.api.agent_workflows.runtime import (
    EMIT_DECISION_TOOL_NAME,
    ToolNotAllowedError,
    WorkflowAgent,
    WorkflowRuntimeError,
)
from src.api.agent_workflows.schemas import (
    Decision,
    PermissionTier,
    ToolCall,
    Workflow,
)
from src.api.agent_workflows.tools import (
    ToolDefinition,
    ToolNotRegisteredError,
    ToolRegistry,
)

__all__ = [
    "ConstraintViolation",
    "Decision",
    "DecisionRepo",
    "DepotConstraints",
    "EMIT_DECISION_TOOL_NAME",
    "HardConstraintGuard",
    "InMemoryDecisionRepo",
    "PermissionTier",
    "ToolCall",
    "ToolDefinition",
    "ToolNotAllowedError",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "Workflow",
    "WorkflowAgent",
    "WorkflowRuntimeError",
]
