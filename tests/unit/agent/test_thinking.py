"""Unit tests for :mod:`src.api.agent.thinking`.

The helper centralises two Anthropic Messages API rules that every agent
LLM call site depends on: adaptive thinking + effort is mutually
exclusive with custom sampling params, and both are model-gated. These
tests pin that behaviour so a regression here can't silently 400 every
call site at once.
"""

from __future__ import annotations

import pytest

from src.api.agent.thinking import (
    DEFAULT_EFFORT,
    THINKING_CAPABLE_MODELS,
    VALID_EFFORT_LEVELS,
    generation_kwargs,
)


class TestGenerationKwargs:
    @pytest.mark.parametrize("model", sorted(THINKING_CAPABLE_MODELS))
    def test_capable_model_uses_adaptive_thinking_and_effort(self, model: str):
        kwargs = generation_kwargs(model, effort="high", temperature=0.3)
        assert kwargs == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
        # Thinking forbids custom sampling params — temperature must NOT
        # be emitted for a thinking-capable model.
        assert "temperature" not in kwargs

    def test_effort_value_is_passed_through(self):
        kwargs = generation_kwargs("claude-sonnet-4-6", effort="low", temperature=0.0)
        assert kwargs["output_config"] == {"effort": "low"}

    def test_non_capable_model_falls_back_to_temperature(self):
        kwargs = generation_kwargs("claude-haiku-4-5", effort="high", temperature=0.3)
        assert kwargs == {"temperature": 0.3}
        assert "thinking" not in kwargs
        assert "output_config" not in kwargs

    def test_unknown_model_falls_back_to_temperature(self):
        # An unrecognised model is treated as non-capable (fail safe: a
        # temperature-only call works everywhere; a thinking call 400s on
        # models that don't support it).
        kwargs = generation_kwargs("some-future-model", effort="high", temperature=0.7)
        assert kwargs == {"temperature": 0.7}


class TestConstants:
    def test_default_effort_is_a_valid_level(self):
        assert DEFAULT_EFFORT in VALID_EFFORT_LEVELS

    def test_xhigh_excluded_from_valid_levels(self):
        # "xhigh" is Opus-4.7-only; excluding it keeps a Sonnet deployment
        # from being configured into a 400 via the effort knob.
        assert "xhigh" not in VALID_EFFORT_LEVELS
        assert VALID_EFFORT_LEVELS == {"low", "medium", "high", "max"}
