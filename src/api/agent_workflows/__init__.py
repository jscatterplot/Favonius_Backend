"""HTTP surface for depot-agent workflows (PRD §6.1, §7.1, §7.3).

Mounted under ``/agent-workflows`` in :mod:`src.api.main` behind the
``DEPOT_AGENT_ENABLED`` feature flag. When the flag is off the router
is never registered and every path under ``/agent-workflows`` returns
404 — the same shape Sprint 1 of the agent rollout used.

The router itself depends on:

- :mod:`src.core.workflows.orchestrator` for the synchronous workflow
  execution (used by ``POST /today/...``).
- :mod:`src.core.workflows.tools` (``DatabaseToolBundle``) for the
  read-only graph queries the workflow consumes.
- :mod:`src.security.auth` for JWT verification and depot access.

The scheduler in :mod:`src.core.workflow_scheduler` shares the same
orchestrator path so the row a scheduled run persists is
indistinguishable in shape from a manually triggered one.
"""

from .feature_flag import is_depot_agent_enabled

__all__ = ["is_depot_agent_enabled"]
