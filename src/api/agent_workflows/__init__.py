"""Depot Agent workflows — package surface.

Sprint 1 (``models.py``, ``repository.py``, ``feature_flag.py``) shipped
the substrate: schema + Pydantic types + a thin async repo on top of
TimescaleDB. Sprint 2 (this PR) adds the *runtime* on top of that
substrate:

* :mod:`~src.api.agent_workflows.runtime` — :class:`WorkflowAgent`, one
  turn of one workflow against the Anthropic Messages API with
  allow-list-enforced tool use.
* :mod:`~src.api.agent_workflows.tools` — :class:`ToolRegistry`, the
  single registry of tool name → JSON schema + async callable.
* :mod:`~src.api.agent_workflows.constraints` — the hard-constraint
  guard the runtime runs at both tool dispatch and final-emit time
  (PRD §10.3).
* :mod:`~src.api.agent_workflows.repo` — :class:`DecisionRepo` Protocol
  + adapters around the canonical
  :func:`~src.api.agent_workflows.repository.insert_decision` writer.

The agent itself never writes ``auto_executed`` (PRD §9.2): every
:class:`Decision` produced by the runtime arrives at the repo with
``disposition=Disposition.PENDING``. Humans or a later promotion
pathway advance the disposition.
"""

from src.api.agent_workflows.constraints import (
    ConstraintViolation,
    DepotConstraints,
    HardConstraintGuard,
)
from src.api.agent_workflows.feature_flag import is_depot_agent_enabled
from src.api.agent_workflows.models import (
    Decision,
    Disposition,
    GraduationRule,
    PermissionTier,
    ToolCall,
    Workflow,
)
from src.api.agent_workflows.repo import (
    AsyncpgDecisionRepo,
    DecisionRepo,
    InMemoryDecisionRepo,
)
from src.api.agent_workflows.runtime import (
    EMIT_DECISION_TOOL_NAME,
    ToolNotAllowedError,
    WorkflowAgent,
    WorkflowRuntimeError,
)
from src.api.agent_workflows.tools import (
    ToolDefinition,
    ToolNotRegisteredError,
    ToolRegistry,
)

__all__ = [
    "AsyncpgDecisionRepo",
    "ConstraintViolation",
    "Decision",
    "DecisionRepo",
    "DepotConstraints",
    "Disposition",
    "EMIT_DECISION_TOOL_NAME",
    "GraduationRule",
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
    "is_depot_agent_enabled",
]
