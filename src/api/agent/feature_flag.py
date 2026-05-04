"""Feature flags for the depot chat agent (read at import / startup only)."""

from __future__ import annotations

import os


def is_agent_search_enabled() -> bool:
    """Return True iff the agent router should be mounted.

    Flipping the env var requires a redeploy. Default is **off** for v0 —
    flipped to default-on in B6 after the golden test suite passes per
    architecture doc §11 step 8.
    """
    return os.environ.get("AGENT_SEARCH_ENABLED", "false").lower() == "true"
