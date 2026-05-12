"""Depot-agent workflow runtime (sprint 2).

This package owns the agent loop that executes a single :class:`Workflow`
against an allow-listed tool set and writes one immutable
:class:`Decision` row per turn. The runtime is wired up by callers
that already have a verified JWT-derived :class:`AuthContext`; no HTTP
endpoint is exposed yet.

See :mod:`src.api.agent_workflows.runtime` for the entry point
(:class:`WorkflowAgent`). The substrate-level architecture is in
``docs/PRD_Depot_Agent.md`` §4.3–§4.6 and §10.3–§10.5.
"""

from src.api.agent_workflows.constraints import (
    DepotConstraints,
    HardConstraintGuard,
)
from src.api.agent_workflows.repo import (
    AsyncpgDecisionRepo,
    DecisionRepo,
    InMemoryDecisionRepo,
)
from src.api.agent_workflows.runtime import (
    SUBMIT_DECISION_TOOL_NAME,
    DecisionLoopError,
    WorkflowAgent,
)
from src.api.agent_workflows.schemas import (
    Decision,
    GraduationRule,
    PermissionTier,
    ToolCallRecord,
    Workflow,
)
from src.api.agent_workflows.tools import (
    Tool,
    ToolNotAllowedError,
    ToolNotRegisteredError,
    ToolRegistry,
)

__all__ = [
    "AsyncpgDecisionRepo",
    "Decision",
    "DecisionLoopError",
    "DecisionRepo",
    "DepotConstraints",
    "GraduationRule",
    "HardConstraintGuard",
    "InMemoryDecisionRepo",
    "PermissionTier",
    "SUBMIT_DECISION_TOOL_NAME",
    "Tool",
    "ToolCallRecord",
    "ToolNotAllowedError",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "Workflow",
    "WorkflowAgent",
]
