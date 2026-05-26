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
from functools import lru_cache
from typing import Any, Literal, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


Route = Literal["consumption_by_user", "sql_general", "refuse"]


@dataclass(frozen=True)
class PlannerDecision:
    route: Route
    reason: str


# Phrases that strongly suggest the consumption fast path. All
# lowercased; matched against a lowercased user message. The fast
# path's compiler (`compile_consumption_by_user`) resolves DRIVER, RFID
# CARD, and VEHICLE/FLEET subjects, plus depot-wide totals — so the
# first trigger accepts an optional measurand noun ("power", "energy",
# "kwh") and the past-tense "was/were consumed" phrasing that operators
# actually type ("how much power was consumed last month"). Borderline
# shapes that the compiler still can't service (rankings, comparisons)
# are pulled back to sql_general by the anti-patterns below.
_CONSUMPTION_TRIGGERS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bhow much(?:\s+(?:power|energy|electricity|kwh|charge|charging))?"
        r"\s+(?:did|has|have|was|were)\b.+?"
        r"\b(?:charg(?:e|ed|ing)|consum(?:e|ed|ing|ption)|use[ds]?)\b"
    ),
    re.compile(r"\bconsumption (of|for|by) \S+"),
    re.compile(r"\benergy (used|consumed) by \S+"),
    re.compile(r"\bhow many kwh did \S+ "),
)

# Anti-patterns that pull a borderline message OUT of the fast path
# back to sql_general (e.g. "which charger consumed the most" is a
# ranking question, not a per-user consumption question). Note there is
# deliberately no bare `vehicle(s)` anti-pattern: vehicle and fleet
# consumption are now first-class on the fast path. Rankings over
# vehicles ("which vehicle used the most") are still caught by the
# `which …` pattern and routed to SQL mode.
_CONSUMPTION_ANTIPATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwhich (depots?|chargers?|vehicles?|drivers?)\b"),
    re.compile(r"\bcompare\b"),
    re.compile(r"\bunderutil"),
    re.compile(r"\bfault"),
    re.compile(r"\bschedule"),
    re.compile(r"\bopt(imization|imisation) (run|trigger)"),
)


@lru_cache(maxsize=1)
def is_sql_mode_enabled() -> bool:
    """``AGENT_SQL_MODE_ENABLED`` env-var check (default False).

    Parsed once per process; tests must call ``cache_clear()`` after
    monkeypatching the environment.
    """
    return os.environ.get("AGENT_SQL_MODE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def fetch_org_sql_enabled(static_pool: Any, organization_id: Optional[UUID]) -> bool:
    """Query ``organizations.agent_sql_mode_enabled`` for this org.

    Returns ``True`` (default-on) when the org row is not found yet
    (tenant mirror may not have run). Returns ``False`` only when the
    column is explicitly ``FALSE``, or when ``organization_id`` is
    ``None`` (no org context to check).
    """
    if organization_id is None:
        return False
    row = await static_pool.fetchrow(
        "SELECT agent_sql_mode_enabled FROM organizations WHERE id = $1",
        organization_id,
    )
    if row is None:
        return True  # org not mirrored yet — honour the default-on policy
    return bool(row["agent_sql_mode_enabled"])


def classify(
    message: str,
    *,
    sql_mode_allowed: bool,
) -> PlannerDecision:
    """Pick a route for one user message.

    Args:
        message: Raw user-typed message.
        sql_mode_allowed: Pre-resolved boolean combining the global
            ``AGENT_SQL_MODE_ENABLED`` flag and the per-org DB check
            (``organizations.agent_sql_mode_enabled``). Callers should
            derive this via :func:`fetch_org_sql_enabled`.

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

    if is_sql_mode_enabled() and sql_mode_allowed:
        return PlannerDecision(route="sql_general", reason="sql_mode_route")

    # SQL mode off → fall back to the consumption fast path. The intent
    # compiler has its own extraction step that returns a refusal if it
    # cannot recognise the question — keeping the legacy behaviour from
    # before SQL mode existed. Without this fallback, ~half of the 50
    # golden consumption prompts (any that don't match the narrow
    # _CONSUMPTION_TRIGGERS regexes) regress to `refuse` during the
    # default-off rollout period, e.g. "What did Lukas Jankauskas charge
    # last month?", "Show energy for Smith this week", etc.
    return PlannerDecision(
        route="consumption_by_user",
        reason="consumption_fallback_no_sql_mode",
    )
