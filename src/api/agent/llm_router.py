"""Two-model split router for the depot chat agent (PLAN.md §S5b).

Build-only spike. The consumption fast path makes two LLM calls per turn
(``src/api/agent/llm.py``):

* the **explore** phase — :func:`~src.api.agent.llm.extract_plan`'s
  forced-tool extraction call. It is cheap, structured, and uses no adaptive
  thinking, so it is a good fit for the smaller, cheaper ``claude-haiku-4-5``
  when an org opts in.
* the **format** phase — :func:`~src.api.agent.llm.format_answer`'s
  user-facing reply. It always stays on ``claude-sonnet-4-6``: we never
  downgrade the prose a human reads.

The phase is supplied by the call site (each call site in ``llm.py`` passes a
literal ``"explore"`` / ``"format"``). :func:`pick_model` never inspects
message history to guess the phase — that approach was rejected as fragile.

Per-org gating mirrors the S4 budget knob (``src/api/agent/budget.py``): a
first-class ``organizations.agent_two_model_enabled`` boolean column
(``migrations/supabase/046``), resolved fail-open. There is intentionally **no
env var** — like the token budget, this is a per-company commercial attribute,
not a deploy-time switch.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# The two models the split routes between. Both are already in
# ``src.api.agent.llm.KNOWN_GOOD_MODELS`` (the set CONFIG.model is validated
# against at startup), so neither needs a separate allow-list entry.
EXPLORE_MODEL = "claude-haiku-4-5"
FORMAT_MODEL = "claude-sonnet-4-6"

Phase = Literal["explore", "format"]


def pick_model(phase: Phase, *, two_model_enabled: bool, default_model: str) -> str:
    """Return the model id to use for ``phase``.

    * ``"format"`` → :data:`FORMAT_MODEL` **always** — the user-facing reply
      never downgrades, independent of the flag or ``default_model``.
    * ``"explore"`` → :data:`EXPLORE_MODEL` when ``two_model_enabled`` is set,
      otherwise ``default_model`` (the current single-model behavior).

    Pure: no I/O, no module state, no message-history introspection. ``phase``
    comes from the call site.

    Args:
        phase: ``"explore"`` or ``"format"`` — determined by the caller.
        two_model_enabled: Whether the calling org has the split turned on
            (resolved upstream via :func:`resolve_org_two_model_enabled`).
        default_model: The model to fall back to for the explore phase when
            the split is off — normally ``llm.CONFIG.model``.

    Returns:
        The Anthropic model id for this phase.

    Raises:
        ValueError: ``phase`` is neither ``"explore"`` nor ``"format"``.
    """
    if phase == "format":
        return FORMAT_MODEL
    if phase == "explore":
        return EXPLORE_MODEL if two_model_enabled else default_model
    raise ValueError(f"unknown phase {phase!r}; expected 'explore' or 'format'")


def _parse_flag(raw: Any) -> bool:
    """Coerce the column read to ``bool``.

    Accepts a native ``bool`` as well as the text form asyncpg returns from a
    ``to_jsonb(o)->>'col'`` extraction of a ``BOOLEAN`` column (``"true"`` /
    ``"false"``). Anything else — ``None`` (NULL / column absent), blank, or
    garbage — is ``False``, the spike's safe default (the split stays off).
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() == "true"


async def resolve_org_two_model_enabled(static_pool: Any, organization_id: Optional[UUID]) -> bool:
    """Return whether the two-model split is enabled for ``organization_id``.

    Precedence mirrors :func:`src.api.agent.budget.resolve_org_token_budget`:
    the per-org ``organizations.agent_two_model_enabled`` column, defaulting
    off. The column is read via ``to_jsonb(o)->>'agent_two_model_enabled'``
    (not selected directly) so a database where ``supabase/046`` has not yet
    applied returns NULL → ``False`` instead of raising ``UndefinedColumn``
    every turn — matching the resilient JSONB reads in ``budget.py`` and
    ``src/core/state/assembler.py``.

    Fail-safe to ``False`` (the conservative default): no pool, no org, an
    unset/garbage value, or any DB error all leave the split off rather than
    break a turn. A flag read must never fail a request.

    Args:
        static_pool: asyncpg pool for the Supabase static schema (where
            ``organizations`` lives). ``None`` short-circuits to ``False``.
        organization_id: The caller's org. ``None`` (e.g. ``favonius_admin``)
            short-circuits to ``False`` — no per-org value applies.

    Returns:
        ``True`` only when the org explicitly has the column set to true.
    """
    if static_pool is None or organization_id is None:
        return False

    try:
        async with static_pool.acquire() as conn:
            raw = await conn.fetchval(
                """
                SELECT to_jsonb(o) ->> 'agent_two_model_enabled'
                FROM organizations o
                WHERE o.id = $1::uuid
                """,
                str(organization_id),
            )
    except Exception:  # noqa: BLE001 — fail safe: a flag read must never break a turn
        logger.warning(
            "Failed to read agent_two_model_enabled for org %s; defaulting to disabled",
            organization_id,
            exc_info=True,
        )
        return False

    return _parse_flag(raw)


__all__ = [
    "EXPLORE_MODEL",
    "FORMAT_MODEL",
    "Phase",
    "pick_model",
    "resolve_org_two_model_enabled",
]
