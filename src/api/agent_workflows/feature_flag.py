"""Feature flag for the depot-agent workflows HTTP surface.

When ``DEPOT_AGENT_ENABLED`` is anything other than the literal string
``true`` (case-insensitive), the router is not mounted and every
``/agent-workflows/*`` path returns 404. This is the rollout gate the
PRD §15 release plan requires: production stays off until the scenario
suite + a one-week shadow run pass.

Default is **off** — Phase 1 ships behind the flag and is flipped to
``true`` in staging only after this PR merges.
"""

from __future__ import annotations

import os


def is_depot_agent_enabled() -> bool:
    """Return True iff the agent-workflows router should be mounted."""
    return os.environ.get("DEPOT_AGENT_ENABLED", "false").lower() == "true"
