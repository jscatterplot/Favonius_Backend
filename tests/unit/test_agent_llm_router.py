"""Unit tests for the two-model split router (PLAN.md §S5b, build-only spike).

Covers:
  * ``pick_model`` — phase → model, gated by ``two_model_enabled``.
  * ``resolve_org_two_model_enabled`` — per-org column precedence + fail-safe.
  * the two ``llm.py`` call sites — that ``extract_plan`` routes the "explore"
    call to Haiku when enabled (and to ``CONFIG.model`` when not), and that
    ``format_answer`` always routes the "format" call to Sonnet.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import pytest

from src.api.agent.llm_router import (
    EXPLORE_MODEL,
    FORMAT_MODEL,
    pick_model,
    resolve_org_two_model_enabled,
)

_ORG = UUID("bb000000-0000-4000-8000-0000000000bb")


# ── pick_model ───────────────────────────────────────────────────────────────


def test_pick_model_explore_enabled_routes_to_haiku() -> None:
    assert (
        pick_model("explore", two_model_enabled=True, default_model="claude-sonnet-4-6")
        == EXPLORE_MODEL
        == "claude-haiku-4-5"
    )


def test_pick_model_explore_disabled_uses_default() -> None:
    assert (
        pick_model("explore", two_model_enabled=False, default_model="claude-sonnet-4-6")
        == "claude-sonnet-4-6"
    )
    # The off-path is exactly "use the default model" — even a non-Sonnet default.
    assert (
        pick_model("explore", two_model_enabled=False, default_model="claude-opus-4-7")
        == "claude-opus-4-7"
    )


def test_pick_model_format_always_sonnet() -> None:
    # Format never downgrades, regardless of flag or default.
    for enabled in (True, False):
        assert (
            pick_model("format", two_model_enabled=enabled, default_model="claude-haiku-4-5")
            == FORMAT_MODEL
            == "claude-sonnet-4-6"
        )


def test_pick_model_unknown_phase_raises() -> None:
    with pytest.raises(ValueError):
        pick_model("planning", two_model_enabled=True, default_model="x")  # type: ignore[arg-type]


# ── resolve_org_two_model_enabled (precedence + fail-safe) ─────────────────────


class _FakeConn:
    def __init__(self, value: Any, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    async def fetchval(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._raises:
            raise RuntimeError("simulated DB failure")
        return self._value


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakePool:
    """asyncpg.Pool-shaped fake exposing ``acquire()`` → conn with ``fetchval``."""

    def __init__(self, value: Optional[Any] = None, *, raises: bool = False) -> None:
        self._conn = _FakeConn(value, raises=raises)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_resolve_true_column_enables() -> None:
    # to_jsonb(o)->>'col' returns the text form of a BOOLEAN column.
    assert await resolve_org_two_model_enabled(_FakePool("true"), _ORG) is True


@pytest.mark.asyncio
async def test_resolve_native_bool() -> None:
    assert await resolve_org_two_model_enabled(_FakePool(True), _ORG) is True
    assert await resolve_org_two_model_enabled(_FakePool(False), _ORG) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", None, "", "1", "yes", "TRUE-ish", "garbage"])
async def test_resolve_falsey_or_garbage_disabled(value: Any) -> None:
    # Only the exact text "true" (any case) enables; everything else is off.
    expected = str(value).strip().lower() == "true"
    assert await resolve_org_two_model_enabled(_FakePool(value), _ORG) is expected


@pytest.mark.asyncio
async def test_resolve_case_insensitive_true() -> None:
    assert await resolve_org_two_model_enabled(_FakePool("TRUE"), _ORG) is True
    assert await resolve_org_two_model_enabled(_FakePool(" True "), _ORG) is True


@pytest.mark.asyncio
async def test_resolve_no_pool_or_no_org_disabled() -> None:
    assert await resolve_org_two_model_enabled(None, _ORG) is False
    assert await resolve_org_two_model_enabled(_FakePool("true"), None) is False


@pytest.mark.asyncio
async def test_resolve_db_error_fails_safe_to_disabled() -> None:
    # A flag read must never break a turn: any DB error → disabled.
    assert await resolve_org_two_model_enabled(_FakePool(raises=True), _ORG) is False


# ── llm.py call-site routing ──────────────────────────────────────────────────


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, tool_input: dict[str, Any], block_id: str = "toolu_1") -> None:
        self.name = name
        self.input = tool_input
        self.id = block_id


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int = 12, output_tokens: int = 6) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _CapturingMessages:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._response


class _CapturingClient:
    def __init__(self, response: _Response) -> None:
        self.messages = _CapturingMessages(response)


_VALID_PLAN_INPUT = {
    "intent": "consumption_by_user",
    "subjects": [{"kind": "driver", "text": "John"}],
    "time_window": {"kind": "relative", "relative": "last_month"},
    "group_by": [],
    "depot_wide": False,
}


@pytest.mark.asyncio
async def test_extract_plan_routes_explore_to_haiku_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import llm

    client = _CapturingClient(_Response([_ToolUseBlock("emit_query_plan", _VALID_PLAN_INPUT)]))
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    await llm.extract_plan("How much did John charge last month?", two_model_enabled=True)

    assert client.messages.calls, "extract_plan never called the Anthropic client"
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_extract_plan_uses_default_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.agent import llm

    client = _CapturingClient(_Response([_ToolUseBlock("emit_query_plan", _VALID_PLAN_INPUT)]))
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    await llm.extract_plan("How much did John charge last month?", two_model_enabled=False)

    # Off path == current behavior: the configured default model.
    assert client.messages.calls[0]["model"] == llm.CONFIG.model


@pytest.mark.asyncio
async def test_extract_plan_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.agent import llm

    client = _CapturingClient(_Response([_ToolUseBlock("emit_query_plan", _VALID_PLAN_INPUT)]))
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    # An explicit model= (eval scripts / sweeps) beats the router even when on.
    await llm.extract_plan("x", model="claude-opus-4-7", two_model_enabled=True)

    assert client.messages.calls[0]["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_format_answer_always_routes_to_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.agent import llm
    from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow

    client = _CapturingClient(
        _Response([_TextBlock("Here is your answer.")], stop_reason="end_turn")
    )
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    plan = QueryPlan(
        intent="consumption_by_user",
        subjects=[EntityMention(kind="driver", text="John")],
        time_window=TimeWindow(kind="relative", relative="last_month"),
    )
    window = {"start": "2026-04-01T00:00:00Z", "end": "2026-05-01T00:00:00Z", "tz": "UTC"}

    for enabled in (True, False):
        client.messages.calls.clear()
        await llm.format_answer(plan, [], window, [], two_model_enabled=enabled)
        assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"
