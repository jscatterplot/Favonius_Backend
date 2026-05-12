"""Feature flag for the Depot Agent (read at import / startup only).

Mirrors the ``AGENT_SEARCH_ENABLED`` flag in
``src/api/agent/feature_flag.py``. Default is **off** — sprint 1 only
lands schema + types + repo; nothing wires the agent into request paths
yet, so flipping this on without the later sprints does not enable any
behaviour. The flag exists now so deployment infra and tests can target
the eventual runtime without a follow-up code change.
"""

from __future__ import annotations

import os


def is_depot_agent_enabled() -> bool:
    """Return True iff the Depot Agent should be mounted at startup.

    Reads ``DEPOT_AGENT_ENABLED`` from the environment. Any value other
    than the case-insensitive string ``"true"`` is treated as disabled.
    """
    return os.environ.get("DEPOT_AGENT_ENABLED", "false").strip().lower() == "true"
