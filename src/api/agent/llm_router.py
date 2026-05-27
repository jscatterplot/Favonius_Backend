"""Two-model split router for the depot chat agent (PLAN.md §S5b).

Build-only spike. The consumption fast path makes two LLM calls per turn
(``src/api/agent/llm.py``):

* the **explore** phase — :func:`~src.api.agent.llm.extract_plan`'s
  forced-tool extraction call. It is cheap, structured, and uses no adaptive
  thinking, so it is a good fit for the smaller, cheaper ``claude-haiku-4-5``
  when an org opts in.
* the **format** phase — :func:`~src.api.agent.llm.format_answer`'s
  user-facing reply. When the split is on it uses ``claude-sonnet-4-6`` (we
  never downgrade the prose a human reads to the cheap model).

The split is **per phase, gated on the org flag**: when the flag is OFF both
phases return ``default_model`` — i.e. the existing single-model behavior is
preserved exactly (an operator's ``AGENT_LLM_MODEL`` still drives both calls).
When the flag is ON, explore routes to Haiku and format to Sonnet.

The phase is supplied by the call site (each call site in ``llm.py`` passes a
literal ``"explore"`` / ``"format"``). :func:`pick_model` never inspects
message history to guess the phase — that approach was rejected as fragile.

Per-org gating mirrors the S4 budget knob (``src/api/agent/budget.py``): a
first-class ``organizations.agent_two_model_enabled`` boolean column
(``migrations/supabase/046``), resolved fail-open. There is intentionally **no
env var** — like the token budget, this is a per-company commercial attribute,
not a deploy-time switch.

This module is intentionally free of any ``src.api.agent.llm`` import (and thus
of the Anthropic SDK): the controller resolves routing on every consumption
turn, including turns that inject a fake/custom ``LLMClient``, and those paths
must stay llm-free. :data:`DEFAULT_MODEL` / :func:`configured_default_model`
own the single-model default so neither the controller nor the router needs to
import the real client module to know it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# The single-model default. Owned here (the llm-free routing module) and reused
# by ``llm.LLMConfig`` so the default lives in exactly one place; the controller
# can resolve it for routing/audit without importing the Anthropic-backed llm
# module on injected-client paths.
DEFAULT_MODEL = "claude-sonnet-4-6"

# The two models the split routes between when enabled. Both are in
# ``src.api.agent.llm.KNOWN_GOOD_MODELS`` (the set CONFIG.model is validated
# against at startup), so neither needs a separate allow-list entry.
EXPLORE_MODEL = "claude-haiku-4-5"
FORMAT_MODEL = "claude-sonnet-4-6"

Phase = Literal["explore", "format"]


def configured_default_model() -> str:
    """Return the single-model default — ``AGENT_LLM_MODEL`` or :data:`DEFAULT_MODEL`.

    Mirrors ``llm.LLMConfig.from_env``'s read of ``AGENT_LLM_MODEL`` but without
    importing the llm module, so the controller can resolve the routing default
    on injected-client (fake/eval) turns without dragging in the Anthropic SDK
    or tripping ``LLMConfig`` validation. ``llm.CONFIG.model`` remains the
    validated source of truth for the *actual* outbound call in ``llm.py``;
    this is only the value used for routing/audit at the call site.
    """
    return os.environ.get("AGENT_LLM_MODEL", DEFAULT_MODEL)


def pick_model(phase: Phase, *, two_model_enabled: bool, default_model: str) -> str:
    """Return the model id to use for ``phase``.

    * Split **off** (``two_model_enabled`` falsy) → ``default_model`` for **both**
      phases. This is the existing single-model behavior, preserved exactly —
      a non-default ``AGENT_LLM_MODEL`` still drives the format call too.
    * Split **on**, ``"explore"`` → :data:`EXPLORE_MODEL` (the cheap model).
    * Split **on**, ``"format"`` → :data:`FORMAT_MODEL` (never the cheap model;
      the user-facing reply does not downgrade).

    Pure: no I/O, no module state, no message-history introspection. ``phase``
    comes from the call site.

    Args:
        phase: ``"explore"`` or ``"format"`` — determined by the caller.
        two_model_enabled: Whether the calling org has the split turned on
            (resolved upstream via :func:`resolve_org_two_model_enabled`).
        default_model: The single-model model id used for both phases when the
            split is off — normally :func:`configured_default_model`.

    Returns:
        The Anthropic model id for this phase.

    Raises:
        ValueError: ``phase`` is neither ``"explore"`` nor ``"format"``.
    """
    if phase not in ("explore", "format"):
        raise ValueError(f"unknown phase {phase!r}; expected 'explore' or 'format'")
    if not two_model_enabled:
        return default_model
    return EXPLORE_MODEL if phase == "explore" else FORMAT_MODEL


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
    "DEFAULT_MODEL",
    "EXPLORE_MODEL",
    "FORMAT_MODEL",
    "Phase",
    "configured_default_model",
    "pick_model",
    "resolve_org_two_model_enabled",
]
