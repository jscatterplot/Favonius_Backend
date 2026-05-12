"""Depot-agent workflows.

Phase 1 ships exactly one workflow: the daily readiness check
(PRD §6.1). Each workflow lives in its own module and exposes:

- A ``run_*`` async function that takes the assembled inputs and returns
  a Decision dataclass containing the structured §6.1 payload plus the
  tool-call trace and an ``inputs_hash`` for replay.
- The list of "tools" the workflow consumed (the small set of
  ``get_*`` / ``find_*`` helpers from
  :mod:`src.core.workflows.tools`).

The router in :mod:`src.api.agent_workflows.router` persists each
:class:`Decision` to ``workflow_decisions`` (migration 037) before
returning it. The :class:`WorkflowScheduler` in
:mod:`src.core.workflow_scheduler` invokes the same ``run_*`` function
ahead of the earliest scheduled departure.
"""

from .readiness import (
    ReadinessDecision,
    ReadinessException,
    ReadinessOutput,
    ToolCall,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    compute_inputs_hash,
    run_daily_readiness_check,
)

__all__ = [
    "ReadinessDecision",
    "ReadinessException",
    "ReadinessOutput",
    "ToolCall",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "compute_inputs_hash",
    "run_daily_readiness_check",
]
