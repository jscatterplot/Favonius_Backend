"""Unit tests for :mod:`src.api.agent.llm`.

These tests do not call the live Anthropic API. The ``AsyncAnthropic``
client and its ``messages.create`` method are patched, which means we
can both inspect the request arguments (model selection, system-prompt
caching, tool definition) and feed back canned structured responses
to drive the round-trip through Pydantic.

The model-selection assertions are the load-bearing tests for this
sprint — switching the agent's model is supposed to be a redeploy of
``AGENT_LLM_MODEL``, not a code change. If those tests regress, the
abstraction is broken.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import src.api.agent.llm as llm_module
from src.api.agent.llm import (
    KNOWN_GOOD_MODELS,
    LLMConfig,
    LLMExtractionError,
    extract_plan,
    format_answer,
)
from src.api.agent.plan import QueryPlan


def _replace_config(monkeypatch: pytest.MonkeyPatch, **fields) -> None:
    """Swap ``llm_module.CONFIG`` for a fresh frozen instance.

    LLMConfig is frozen by design (so request-path code can't mutate
    generation knobs mid-flight), which means tests that need to vary
    the model can't ``setattr`` on the existing instance. Build a fresh
    one with the requested overrides and bind it onto the module via
    ``monkeypatch.setattr`` so it's automatically restored after the
    test.
    """
    base = llm_module.CONFIG
    new_config = LLMConfig(
        model=fields.get("model", base.model),
        extract_max_tokens=fields.get("extract_max_tokens", base.extract_max_tokens),
        format_max_tokens=fields.get("format_max_tokens", base.format_max_tokens),
        temperature=fields.get("temperature", base.temperature),
        effort=fields.get("effort", base.effort),
        request_timeout_s=fields.get("request_timeout_s", base.request_timeout_s),
    )
    monkeypatch.setattr(llm_module, "CONFIG", new_config)


# ── Fixtures and helpers ──────────────────────────────────────────────────


def _make_tool_use_block(input_data: dict[str, Any]) -> SimpleNamespace:
    """Build a fake content block that mimics ``ToolUseBlock``."""
    return SimpleNamespace(
        type="tool_use",
        name="emit_query_plan",
        id="toolu_test_1",
        input=input_data,
    )


def _make_text_block(text: str) -> SimpleNamespace:
    """Build a fake content block that mimics ``TextBlock``."""
    return SimpleNamespace(type="text", text=text)


def _make_response(content_blocks: list[SimpleNamespace]) -> SimpleNamespace:
    """Build a fake :class:`anthropic.types.Message`."""
    return SimpleNamespace(
        content=content_blocks,
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )


VALID_PLAN_INPUT: dict[str, Any] = {
    "intent": "consumption_by_user",
    "subjects": [{"kind": "driver", "text": "John"}],
    "time_window": {"kind": "relative", "relative": "last_month"},
    "group_by": [],
}


@pytest.fixture
def patch_anthropic_client(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_get_client`` with a function returning a MagicMock.

    Returns the mock ``messages.create`` so tests can:

    - Set its ``return_value`` to a canned response (single attempt).
    - Set its ``side_effect`` to a list of responses (retry path).
    - Inspect ``call_args`` / ``call_args_list`` to assert the model
      argument and system-prompt shape.

    Patching ``_get_client`` (rather than ``anthropic.AsyncAnthropic``)
    keeps the test isolated from the secrets-manager construction path
    and avoids the ``importlib.reload``-vs-monkeypatch interaction that
    bites cross-class test ordering.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")

    create_mock = AsyncMock()
    fake_client_instance = MagicMock()
    fake_client_instance.messages.create = create_mock

    monkeypatch.setattr(llm_module, "_get_client", lambda: fake_client_instance)
    llm_module._reset_client_for_tests()

    yield create_mock

    llm_module._reset_client_for_tests()


# ── LLMConfig ──────────────────────────────────────────────────────────────


class TestLLMConfig:
    def test_default_model_when_env_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AGENT_LLM_MODEL", raising=False)
        config = LLMConfig.from_env()
        assert config.model == "claude-sonnet-4-6"

    def test_reads_model_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENT_LLM_MODEL", "claude-haiku-4-5")
        config = LLMConfig.from_env()
        assert config.model == "claude-haiku-4-5"

    def test_rejects_unknown_model_at_load(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENT_LLM_MODEL", "gpt-4-turbo")
        # ValidationError is a subclass of ValueError per Pydantic's hierarchy,
        # so either type works here. We check ValueError to match the spec
        # ("rejects unknown values with a ValueError at startup").
        with pytest.raises((ValueError, ValidationError)) as excinfo:
            LLMConfig.from_env()
        message = str(excinfo.value)
        assert "gpt-4-turbo" in message
        # Should also point the operator at the right file/list to fix it.
        assert "KNOWN_GOOD_MODELS" in message

    def test_reads_token_budgets_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENT_LLM_EXTRACT_MAX_TOKENS", "200")
        monkeypatch.setenv("AGENT_LLM_FORMAT_MAX_TOKENS", "1500")
        monkeypatch.setenv("AGENT_LLM_TEMPERATURE", "0.5")
        monkeypatch.setenv("AGENT_LLM_TIMEOUT_S", "60")
        config = LLMConfig.from_env()
        assert config.extract_max_tokens == 200
        assert config.format_max_tokens == 1500
        assert config.temperature == 0.5
        assert config.request_timeout_s == 60

    def test_default_effort_is_high(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AGENT_LLM_EFFORT", raising=False)
        assert LLMConfig.from_env().effort == "high"

    def test_reads_effort_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENT_LLM_EFFORT", "low")
        assert LLMConfig.from_env().effort == "low"

    def test_rejects_invalid_effort_at_load(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AGENT_LLM_EFFORT", "turbo")
        with pytest.raises((ValueError, ValidationError)) as excinfo:
            LLMConfig.from_env()
        assert "turbo" in str(excinfo.value)

    def test_config_is_frozen(self):
        config = LLMConfig(model="claude-haiku-4-5")
        with pytest.raises(ValidationError):
            config.model = "claude-opus-4-7"  # type: ignore[misc]

    def test_each_known_model_passes_validation(self):
        for model in KNOWN_GOOD_MODELS:
            assert LLMConfig(model=model).model == model

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            LLMConfig(model="claude-sonnet-4-6", unknown_param="x")  # type: ignore[call-arg]


# Module-level startup behavior (logs active model on import; rejects
# bad env vars at load) is verified in tests/unit/agent/test_llm_import.py.
# Those tests use importlib.reload which would otherwise stale the
# extract_plan / format_answer references imported at the top of this
# file, so they're isolated in their own module.


# ── Model selection in extract_plan ────────────────────────────────────────


class TestExtractPlanModelSelection:
    """The whole point of LLMConfig: model is configurable per env.

    These tests swap the frozen ``CONFIG`` instance via ``_replace_config``
    rather than reloading the module — keeps the imported
    ``extract_plan`` / ``format_answer`` references valid and avoids
    cross-class test pollution.
    """

    @pytest.mark.asyncio
    async def test_uses_default_sonnet_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch, patch_anthropic_client: AsyncMock
    ):
        # The default LLMConfig() has model="claude-sonnet-4-6"; that's
        # exactly what we want to verify the request uses, so no override.
        _replace_config(monkeypatch, model="claude-sonnet-4-6")

        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        result = await extract_plan("how much did John charge last month")

        assert isinstance(result, QueryPlan)
        assert patch_anthropic_client.call_args.kwargs["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_uses_haiku_when_env_set_to_haiku(
        self, monkeypatch: pytest.MonkeyPatch, patch_anthropic_client: AsyncMock
    ):
        _replace_config(monkeypatch, model="claude-haiku-4-5")

        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        result = await extract_plan("how much did John charge last month")

        assert isinstance(result, QueryPlan)
        assert patch_anthropic_client.call_args.kwargs["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_per_call_model_override_beats_env_default(
        self, monkeypatch: pytest.MonkeyPatch, patch_anthropic_client: AsyncMock
    ):
        # Set env-default to haiku, then call with an explicit opus override.
        _replace_config(monkeypatch, model="claude-haiku-4-5")

        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        result = await extract_plan(
            "how much did John charge last month",
            model="claude-opus-4-7",
        )

        assert isinstance(result, QueryPlan)
        # The call must use the overridden value, not the env-default haiku.
        assert patch_anthropic_client.call_args.kwargs["model"] == "claude-opus-4-7"

    def test_from_env_picks_up_haiku_env_var(self, monkeypatch: pytest.MonkeyPatch):
        # Concrete env-var-to-config plumbing; covers the spec's "reads
        # AGENT_LLM_MODEL from env" assertion without using importlib.
        monkeypatch.setenv("AGENT_LLM_MODEL", "claude-haiku-4-5")
        config = LLMConfig.from_env()
        assert config.model == "claude-haiku-4-5"


# ── extract_plan happy path & retry ────────────────────────────────────────


class TestExtractPlanRoundTrip:
    @pytest.mark.asyncio
    async def test_canned_structured_output_round_trips(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )

        plan = await extract_plan("how much did John charge last month")

        assert plan.intent == "consumption_by_user"
        assert plan.subjects[0].kind == "driver"
        assert plan.subjects[0].text == "John"
        assert plan.time_window.kind == "relative"
        assert plan.time_window.relative == "last_month"
        assert plan.group_by == []

    @pytest.mark.asyncio
    async def test_retry_after_validation_failure_succeeds(self, patch_anthropic_client: AsyncMock):
        # First response: malformed (unknown intent), second: valid.
        bad_input = {**VALID_PLAN_INPUT, "intent": "schedule_charge"}
        patch_anthropic_client.side_effect = [
            _make_response([_make_tool_use_block(bad_input)]),
            _make_response([_make_tool_use_block(VALID_PLAN_INPUT)]),
        ]

        plan = await extract_plan("how much did John charge last month")

        assert plan.intent == "consumption_by_user"
        assert patch_anthropic_client.call_count == 2

    @pytest.mark.asyncio
    async def test_two_validation_failures_raise_typed_error(
        self, patch_anthropic_client: AsyncMock
    ):
        # Both responses: malformed.
        bad_input = {**VALID_PLAN_INPUT, "intent": "schedule_charge"}
        patch_anthropic_client.side_effect = [
            _make_response([_make_tool_use_block(bad_input)]),
            _make_response([_make_tool_use_block(bad_input)]),
        ]

        with pytest.raises(LLMExtractionError):
            await extract_plan("how much did John charge last month")

        assert patch_anthropic_client.call_count == 2

    @pytest.mark.asyncio
    async def test_no_tool_call_then_recovery_on_retry(self, patch_anthropic_client: AsyncMock):
        # First response: no tool call (just text). Second: a valid tool call.
        patch_anthropic_client.side_effect = [
            _make_response([_make_text_block("I'm not going to call that tool.")]),
            _make_response([_make_tool_use_block(VALID_PLAN_INPUT)]),
        ]

        plan = await extract_plan("how much did John charge last month")

        assert plan.intent == "consumption_by_user"
        assert patch_anthropic_client.call_count == 2

    @pytest.mark.asyncio
    async def test_no_tool_call_twice_raises(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.side_effect = [
            _make_response([_make_text_block("Sorry, I can't do that.")]),
            _make_response([_make_text_block("Still not doing it.")]),
        ]

        with pytest.raises(LLMExtractionError):
            await extract_plan("how much did John charge last month")


# ── Prompt-caching shape ───────────────────────────────────────────────────


class TestPromptCaching:
    @pytest.mark.asyncio
    async def test_extract_plan_marks_system_prompt_for_caching(
        self, patch_anthropic_client: AsyncMock
    ):
        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        await extract_plan("how much did John charge last month")

        kwargs = patch_anthropic_client.call_args.kwargs
        system = kwargs["system"]
        # The system prompt is sent as a list of typed blocks; the last
        # (or only) block should carry an ephemeral cache_control marker.
        assert isinstance(system, list)
        assert system, "system list must not be empty"
        last_block = system[-1]
        assert last_block.get("type") == "text"
        assert last_block.get("cache_control") == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_format_answer_marks_system_prompt_for_caching(
        self, patch_anthropic_client: AsyncMock
    ):
        patch_anthropic_client.return_value = _make_response(
            [_make_text_block("John charged 1,243.0 kWh in May 2026.")]
        )

        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        await format_answer(plan, [], {"start_utc": "x", "end_utc": "y"}, [])

        kwargs = patch_anthropic_client.call_args.kwargs
        system = kwargs["system"]
        last_block = system[-1]
        assert last_block.get("cache_control") == {"type": "ephemeral"}


# ── Tool definition shape ──────────────────────────────────────────────────


class TestToolDefinition:
    @pytest.mark.asyncio
    async def test_extract_plan_passes_query_plan_tool(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        await extract_plan("how much did John charge last month")

        kwargs = patch_anthropic_client.call_args.kwargs
        tools = kwargs["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "emit_query_plan"
        # Forces the model to use the tool — we never want a free-text answer.
        assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_query_plan"}

    @pytest.mark.asyncio
    async def test_extract_plan_uses_extract_token_budget(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response(
            [_make_tool_use_block(VALID_PLAN_INPUT)]
        )
        await extract_plan("how much did John charge last month")

        kwargs = patch_anthropic_client.call_args.kwargs
        # Default extract_max_tokens; if a future test bumps the env var,
        # this assertion must move to a parametrized check.
        assert kwargs["max_tokens"] == llm_module.CONFIG.extract_max_tokens
        assert kwargs["temperature"] == llm_module.CONFIG.temperature


# ── format_answer ──────────────────────────────────────────────────────────


class TestFormatAnswer:
    @pytest.mark.asyncio
    async def test_returns_text_from_response(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response(
            [_make_text_block("John charged 1,243.0 kWh in May 2026.")]
        )

        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        text = await format_answer(
            plan,
            [{"display": "John Smith (Vilnius)", "primary_id": "uuid"}],
            {"start_utc": "2026-04-01T00:00:00Z", "end_utc": "2026-05-01T00:00:00Z"},
            [{"day_local": "2026-04-15", "energy_kwh": "12.4"}],
        )
        assert text == "John charged 1,243.0 kWh in May 2026."

    @pytest.mark.asyncio
    async def test_uses_format_token_budget(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response([_make_text_block("ok")])

        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        await format_answer(plan, [], {}, [])

        kwargs = patch_anthropic_client.call_args.kwargs
        assert kwargs["max_tokens"] == llm_module.CONFIG.format_max_tokens

    @pytest.mark.asyncio
    async def test_thinking_capable_model_uses_adaptive_thinking_and_effort(
        self, monkeypatch: pytest.MonkeyPatch, patch_anthropic_client: AsyncMock
    ):
        # Sonnet 4.6 supports adaptive thinking + effort. The format step
        # must send both and OMIT temperature — thinking is incompatible
        # with custom sampling params.
        _replace_config(monkeypatch, model="claude-sonnet-4-6", effort="high")
        patch_anthropic_client.return_value = _make_response([_make_text_block("ok")])

        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        await format_answer(plan, [], {}, [])

        kwargs = patch_anthropic_client.call_args.kwargs
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}
        assert "temperature" not in kwargs

    @pytest.mark.asyncio
    async def test_non_thinking_model_falls_back_to_temperature(
        self, monkeypatch: pytest.MonkeyPatch, patch_anthropic_client: AsyncMock
    ):
        # Haiku 4.5 supports neither adaptive thinking nor effort; sending
        # either is a 400, so the format step must fall back to temperature
        # and omit thinking/output_config. The two-model router pins the
        # format phase to Sonnet regardless of CONFIG.model, so a non-thinking
        # model now reaches format_answer only via the explicit per-call
        # override (which still wins over the router).
        _replace_config(monkeypatch, model="claude-haiku-4-5")
        patch_anthropic_client.return_value = _make_response([_make_text_block("ok")])

        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        await format_answer(plan, [], {}, [], model="claude-haiku-4-5")

        kwargs = patch_anthropic_client.call_args.kwargs
        assert kwargs["temperature"] == llm_module.FORMAT_STEP_TEMPERATURE
        assert "thinking" not in kwargs
        assert "output_config" not in kwargs

    @pytest.mark.asyncio
    async def test_per_call_model_override_used(self, patch_anthropic_client: AsyncMock):
        patch_anthropic_client.return_value = _make_response([_make_text_block("ok")])
        plan = QueryPlan.model_validate(VALID_PLAN_INPUT)
        await format_answer(plan, [], {}, [], model="claude-opus-4-7")
        assert patch_anthropic_client.call_args.kwargs["model"] == "claude-opus-4-7"


# ── Client construction (rotation-aware secrets) ───────────────────────────


class TestClientConstruction:
    def test_get_client_pulls_api_key_from_secrets_manager(self, monkeypatch: pytest.MonkeyPatch):
        # Simulate a key rotation window: current + previous both set.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-current")
        monkeypatch.setenv("ANTHROPIC_API_KEY_PREVIOUS", "sk-previous")
        llm_module._reset_client_for_tests()

        with patch("src.api.agent.llm.anthropic.AsyncAnthropic") as ctor:
            ctor.return_value = MagicMock()
            llm_module._get_client()
            ctor.assert_called_once()
            # The client signs *outbound* requests with the current key —
            # the previous key only matters when verifying inbound.
            assert ctor.call_args.kwargs["api_key"] == "sk-current"
            assert ctor.call_args.kwargs["timeout"] == float(llm_module.CONFIG.request_timeout_s)

        llm_module._reset_client_for_tests()

    def test_get_client_is_a_singleton(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        llm_module._reset_client_for_tests()

        with patch("src.api.agent.llm.anthropic.AsyncAnthropic") as ctor:
            ctor.return_value = MagicMock()
            a = llm_module._get_client()
            b = llm_module._get_client()
            assert a is b
            ctor.assert_called_once()

        llm_module._reset_client_for_tests()
