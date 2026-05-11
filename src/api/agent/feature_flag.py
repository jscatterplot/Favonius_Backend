"""Feature flags for the depot chat agent (read at import / startup only)."""

from __future__ import annotations

import os


def is_agent_search_enabled() -> bool:
    """Return True iff the agent router should be mounted.

    Default is **on** as of B6 (golden test suite passed, AT-18 green).
    Set ``AGENT_SEARCH_ENABLED=false`` to disable without redeploying code.
    """
    return os.environ.get("AGENT_SEARCH_ENABLED", "true").lower() == "true"
