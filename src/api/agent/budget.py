"""Per-org monthly token-budget for the SQL-mode chat agent (PLAN.md §S4).

This module owns the depot chat agent's cost-protection budget. It lands in two
steps:

* **Config resolution (S4-A, this file today)** —
  :func:`resolve_org_token_budget` returns the effective monthly token ceiling
  for an organization.
* **Enforcement (S4-B, next)** — ``check_and_reserve`` / ``record_actual`` will
  maintain an in-process counter against that ceiling and refuse a turn *before*
  it calls Anthropic when the org is over budget.

Budget precedence (highest wins):

1. **Per-org override** — ``organizations.metadata->>'agent_token_budget_monthly'``
   when present AND parseable as a positive integer. Stored in the Supabase
   static DB (migration ``supabase/044``).
2. **Env default** — ``AGENT_SQL_TOKEN_BUDGET_PER_ORG_MONTHLY``
   (default :data:`DEFAULT_TOKEN_BUDGET_MONTHLY` = 10,000,000).

The resolver is **fail-open**: any DB error, a missing ``metadata`` column
(during the ``supabase/044`` rollout window), or an absent / blank / zero /
non-numeric override all fall back to the env default. Token accounting is
cost-protection, not billing, so reading the budget must never break a turn.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# Env knob + hard-coded fallback. 10M tokens/org/month is generous headroom for
# in-app operator chat while still capping a runaway loop's blast radius.
TOKEN_BUDGET_ENV_VAR = "AGENT_SQL_TOKEN_BUDGET_PER_ORG_MONTHLY"
DEFAULT_TOKEN_BUDGET_MONTHLY = 10_000_000


def _parse_positive_int(raw: Any) -> Optional[int]:
    """Return ``raw`` as a positive ``int``, or ``None`` if it isn't one.

    Accepts the text asyncpg hands back from a JSONB ``->>`` extraction
    (e.g. ``"5000000"``) as well as an already-typed int. Whitespace is
    stripped; zero, negatives, blanks, and non-numeric values yield ``None``
    so every "bad override" path collapses to a single fall-back-to-default
    decision in the caller.
    """
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@lru_cache(maxsize=1)
def env_default_budget() -> int:
    """Resolve the env-default monthly token budget (cached per process).

    Reads :data:`TOKEN_BUDGET_ENV_VAR`; falls back to
    :data:`DEFAULT_TOKEN_BUDGET_MONTHLY` when unset or not a positive integer.

    Cached with :func:`functools.lru_cache`; tests that monkeypatch the env var
    must call ``env_default_budget.cache_clear()`` (mirrors the env-cache
    pattern in ``src/api/agent/planner.py``).
    """
    parsed = _parse_positive_int(os.environ.get(TOKEN_BUDGET_ENV_VAR))
    return parsed if parsed is not None else DEFAULT_TOKEN_BUDGET_MONTHLY


async def resolve_org_token_budget(
    static_pool: Any,
    organization_id: Optional[UUID],
) -> int:
    """Return the effective monthly token ceiling for ``organization_id``.

    Precedence: a parseable positive ``organizations.metadata
    ->>'agent_token_budget_monthly'`` override beats the env default
    (:func:`env_default_budget`). See the module docstring.

    Fail-open by contract — returns the env default when there is no org, no
    pool, the override is missing/garbage, or the read raises. The override is
    read via ``to_jsonb(o)->'metadata'->>...`` rather than ``metadata->>...``
    so a database where ``supabase/044`` has not yet applied returns NULL (→
    env default) instead of raising ``UndefinedColumn`` every turn (mirrors the
    resilient ``to_jsonb(row)->>'col'`` reads in
    ``src/core/state/assembler.py``).

    Args:
        static_pool: asyncpg pool for the Supabase static schema (where
            ``organizations`` lives). ``None`` short-circuits to the env
            default.
        organization_id: The caller's org. ``None`` (e.g. ``favonius_admin``)
            short-circuits to the env default — no per-org override applies.

    Returns:
        The monthly token budget as a positive ``int``.
    """
    default = env_default_budget()
    if static_pool is None or organization_id is None:
        return default

    try:
        async with static_pool.acquire() as conn:
            raw = await conn.fetchval(
                """
                SELECT to_jsonb(o) -> 'metadata' ->> 'agent_token_budget_monthly'
                FROM organizations o
                WHERE o.id = $1::uuid
                """,
                str(organization_id),
            )
    except Exception:  # noqa: BLE001 — fail open: budget read must never break a turn
        logger.warning(
            "Failed to read per-org token budget for org %s; using env default %d",
            organization_id,
            default,
            exc_info=True,
        )
        return default

    override = _parse_positive_int(raw)
    return override if override is not None else default
