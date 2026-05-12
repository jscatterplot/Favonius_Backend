"""Depot Agent workflows — sprint 1 foundations.

This package holds the substrate for the Depot Agent V1 build defined in
``docs/PRD_Depot_Agent.md`` (workflows as first-class objects, per-depot
permission tiers, immutable decision log). Sprint 1 ships schema + types +
repository only — no runtime, no tools, no API endpoints.

Layout::

    feature_flag.py    DEPOT_AGENT_ENABLED env-var gate
    models.py          Pydantic v2 types: Workflow, PermissionTier,
                       GraduationRule, Decision, ToolCall
    repository.py      Async DB helpers backed by the TimescaleDB pool

The agent runtime, tool definitions, and API endpoints arrive in later
sprints. Do not import this package from request paths until the feature
flag flips on and the runtime exists.
"""

from src.api.agent_workflows.feature_flag import is_depot_agent_enabled
from src.api.agent_workflows.models import (
    Decision,
    Disposition,
    GraduationRule,
    PermissionTier,
    ToolCall,
    Workflow,
)

__all__ = [
    "Decision",
    "Disposition",
    "GraduationRule",
    "PermissionTier",
    "ToolCall",
    "Workflow",
    "is_depot_agent_enabled",
]
