"""Pre-LLM router between the consumption_by_user fast path and SQL mode.

The depot chat agent has two execution paths:

1. ``consumption_by_user`` — deterministic intent compiler in
   ``src/api/agent/intents/consumption_by_user.py``. Cheap, fast, gated
   by the 50-entry golden suite.
2. ``sql_general`` — general SQL agent built on top of agent_views.*
   table-functions. Broader, slower, more expensive.

The planner is rule-first: a short regex sweep classifies obvious
consumption questions to the fast path; everything else is routed to
``sql_general`` provided SQL mode is enabled for the caller's org.

Keeping this rule-based avoids a Haiku round-trip on every turn while
still capturing the canonical questions the fast path was built for.
The classifier is intentionally precision-first: a borderline case
falls through to ``sql_general`` rather than wrongly hitting the fast
path (false positives there mean refusal).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Literal, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


Route = Literal["consumption_by_user", "sql_general", "refuse"]


@dataclass(frozen=True)
class PlannerDecision:
    route: Route
    reason: str


# Phrases that strongly suggest the consumption fast path. All
# lowercased; matched against a lowercased user message.
_CONSUMPTION_TRIGGERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhow much (did|has) .+ (charged?|consumed?|used)\b"),
    re.compile(r"\bconsumption (of|for|by) \S+"),
    re.compile(r"\benergy (used|consumed) by \S+"),
    re.compile(r"\bhow many kwh did \S+ "),
)

# Anti-patterns that pull a borderline message OUT of the fast path
# back to sql_general (e.g. "which charger consumed the most" is a
# ranking question, not a per-user consumption question).
_CONSUMPTION_ANTIPATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwhich (depot|charger|vehicle|driver)\b"),
    re.compile(r"\bcompare\b"),
    re.compile(r"\bunderutil"),
    re.compile(r"\bfault"),
    re.compile(r"\bschedule"),
    re.compile(r"\bopt(imization|imisation) (run|trigger)"),
)


def is_sql_mode_enabled() -> bool:
    """``AGENT_SQL_MODE_ENABLED`` env-var check (default False)."""
    return os.environ.get("AGENT_SQL_MODE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_org_in_sql_allowlist(organization_id: Optional[UUID]) -> bool:
    """Check the per-org allowlist for SQL mode.

    Empty/unset allowlist means SQL mode is open to every org (paired
    with ``AGENT_SQL_MODE_ENABLED=true`` this is full rollout).
    """
    raw = os.environ.get("AGENT_SQL_ORG_ALLOWLIST", "").strip()
    if not raw:
        return True
    if organization_id is None:
        return False
    allowed = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    return str(organization_id).lower() in allowed


def classify(
    message: str,
    *,
    organization_id: Optional[UUID],
) -> PlannerDecision:
    """Pick a route for one user message.

    Args:
        message: Raw user-typed message.
        organization_id: The caller's organization. Used to gate SQL
            mode via the per-org allowlist.

    Returns:
        A :class:`PlannerDecision` whose ``route`` is one of
        ``consumption_by_user`` / ``sql_general`` / ``refuse``.

        ``refuse`` is returned when SQL mode is disabled (globally or
        for this org) AND the message does not look like a consumption
        question — the caller surfaces a polite refusal.
    """
    text = (message or "").strip().lower()
    if not text:
        return PlannerDecision(route="refuse", reason="empty_message")

    consumption_match = any(p.search(text) for p in _CONSUMPTION_TRIGGERS)
    anti_match = any(p.search(text) for p in _CONSUMPTION_ANTIPATTERNS)

    if consumption_match and not anti_match:
        return PlannerDecision(
            route="consumption_by_user",
            reason="matched_consumption_trigger",
        )

    if is_sql_mode_enabled() and is_org_in_sql_allowlist(organization_id):
        return PlannerDecision(route="sql_general", reason="sql_mode_route")

    # SQL mode off → only consumption_by_user is reachable.
    if consumption_match:
        # Anti-pattern matched but no SQL mode — best effort fall back.
        return PlannerDecision(
            route="consumption_by_user",
            reason="consumption_fallback_no_sql_mode",
        )

    return PlannerDecision(route="refuse", reason="sql_mode_disabled")
