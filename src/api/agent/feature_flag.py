"""Feature flags for the depot chat agent (read at import / startup only)."""

from __future__ import annotations

import os


def is_agent_search_enabled() -> bool:
    """Return True iff the agent router should be mounted.

    Default is **on** as of B6 (golden test suite passed, AT-18 green).
    Set ``AGENT_SEARCH_ENABLED=false`` to disable without redeploying code.
    """
    return os.environ.get("AGENT_SEARCH_ENABLED", "true").lower() == "true"


def is_automation_suggestions_enabled() -> bool:
    """Return True iff post-turn automation-suggestion detection runs.

    Default **off** — this is a new proactive behaviour that writes
    ``schedule_suggestion`` rows to ``agent_actions`` (surfaced in the today
    view) and can auto-create a report schedule on approval, so it ships dark and
    is opted into per environment. Mirrors the ``AGENT_SQL_MODE_ENABLED`` /
    ``DEPOT_AGENT_ENABLED`` default-off convention.
    """
    return os.environ.get("AGENT_AUTOMATION_SUGGESTIONS_ENABLED", "false").lower() == "true"
