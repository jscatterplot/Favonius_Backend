"""Shared adaptive-thinking generation knobs for Anthropic Messages calls.

Every LLM call site in the agent — the consumption fast path's answer
formatter (:mod:`src.api.agent.llm`), the SQL Q&A loop and the workflow
runtime (:mod:`src.api.agent_workflows.runtime`) — wants the same
behaviour: use adaptive thinking + the ``effort`` parameter on models
that support them, and fall back to plain ``temperature`` on models that
don't. Two API rules make this non-trivial, and centralising them here
keeps every call site correct and identical:

1. **Adaptive thinking is mutually exclusive with custom sampling.** When
   ``thinking`` is enabled the Messages API rejects ``temperature`` /
   ``top_p`` / ``top_k``. So a call either sends thinking + effort *or*
   sends temperature — never both.
2. **Not every model supports it.** Adaptive thinking and ``effort`` are
   only available on the models in :data:`THINKING_CAPABLE_MODELS`;
   sending either to e.g. Haiku 4.5 is a 400.

Forced ``tool_choice`` is *also* incompatible with thinking, but that's a
per-call-site concern (only the consumption extractor forces a tool), so
it's enforced at that call site rather than here.
"""

from __future__ import annotations

from typing import Any

# Models that support adaptive thinking (`thinking: {type: "adaptive"}`)
# AND the effort parameter. Sending either to a model outside this set is
# a 400, so callers gate on membership. Opus 4.6 is included even though
# it isn't currently in the agent's known-good list, so the gate stays
# correct if it's added later.
THINKING_CAPABLE_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
)

# Effort levels valid on both Sonnet 4.6 and Opus 4.7. "xhigh" is
# Opus-4.7-only and is deliberately excluded so a Sonnet deployment can't
# be configured into a 400 via the effort knob.
VALID_EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "max"})

# "high" is the Messages API default and the most capable setting short of
# "max"; it keeps the agent prepared for complex/messy inputs. Lower
# values trade reasoning depth for latency and cost.
DEFAULT_EFFORT: str = "high"


def generation_kwargs(model: str, *, effort: str, temperature: float) -> dict[str, Any]:
    """Return the model-appropriate generation knobs for ``messages.create``.

    On a thinking-capable model: adaptive thinking plus the ``effort``
    parameter, and **no** ``temperature`` (custom sampling params are
    rejected when thinking is on). On any other model: just
    ``temperature`` (thinking/effort would 400).

    Spread the result into the ``messages.create`` call::

        await client.messages.create(
            model=model,
            max_tokens=...,
            system=...,
            messages=...,
            **generation_kwargs(model, effort=effort, temperature=temperature),
        )

    Args:
        model: The model ID the call will use.
        effort: Desired effort level (one of :data:`VALID_EFFORT_LEVELS`).
            Only emitted for thinking-capable models.
        temperature: Sampling temperature. Only emitted for models that
            do not support adaptive thinking.
    """
    if model in THINKING_CAPABLE_MODELS:
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    return {"temperature": temperature}
