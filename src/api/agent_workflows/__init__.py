"""Workflow agent runtime — the substrate the depot agent calls into.

Sprint 2 introduced the runtime (this package). Sprint 3 (this branch)
introduces the evaluation harness in :mod:`src.api.agent_workflows.eval`
ahead of the workflows themselves (per PRD principle 6: "The evaluation
harness ships before the workflow").

Real workflow implementations live alongside this package and register
themselves into :data:`WORKFLOW_REGISTRY` at import time. The harness
discovers them through the registry; tests register fakes the same way.
"""

from src.api.agent_workflows.runtime import (
    Decision,
    Workflow,
    WorkflowAgent,
    WorkflowContext,
    WorkflowNotRegisteredError,
    register_workflow,
    workflow_registry,
)

__all__ = [
    "Decision",
    "Workflow",
    "WorkflowAgent",
    "WorkflowContext",
    "WorkflowNotRegisteredError",
    "register_workflow",
    "workflow_registry",
]
