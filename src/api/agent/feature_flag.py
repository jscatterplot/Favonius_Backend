"""Feature flags for the depot chat agent (read at import / startup only)."""

from __future__ import annotations

import os


def is_agent_search_enabled() -> bool:
    """Return True iff the agent router should be mounted.

    Default is **on** as of B6 (golden test suite passed, AT-18 green).
    Set ``AGENT_SEARCH_ENABLED=false`` to disable without redeploying code.
    """
    return os.environ.get("AGENT_SEARCH_ENABLED", "true").lower() == "true"


def is_agent_doc_fill_enabled() -> bool:
    """Return True iff the collaborative document-fill path is enabled.

    Gates both the upload/session endpoints (``/agent/documents*``) and the
    ``session_id`` route inside ``run_turn``. Default is **off** — the feature
    reuses the SQL-mode data tools, so the ``agent_views.*`` functions + the
    read-only roles must be migrated before turning it on. Set
    ``AGENT_DOC_FILL_ENABLED=true`` to enable without redeploying code.
    """
    return os.environ.get("AGENT_DOC_FILL_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
