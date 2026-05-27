"""Tests for the SQL-mode ``get_page_context`` tool.

Exercises the REAL registry (``build_sql_agent_tool_registry``) and the REAL
``run_qa_turn`` loop with a deterministic fake Anthropic client. The
get_page_context handler and the terminator never touch the pools, so
MagicMock pools are sufficient — no DB required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from src.api.agent.sql_tools import SQL_AGENT_TOOL_NAMES, build_sql_agent_tool_registry
from src.api.agent_workflows.runtime import EMIT_FINAL_ANSWER_TOOL, run_qa_turn

# ── Minimal fake Anthropic SDK (mirrors tests/unit/test_agent_qa_runtime.py) ──


class _FakeBlock:
    def __init__(self, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


class _FakeMessages:
    def __init__(self, script: list[_FakeResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake messages out of script")
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script: list[_FakeResponse]) -> None:
        self.messages = _FakeMessages(script)


def _tool_use(name: str, block_id: str, payload: dict[str, Any]) -> _FakeBlock:
    return _FakeBlock("tool_use", id=block_id, name=name, input=payload)


def _registry(page_context: dict[str, Any] | None):
    auth = MagicMock()
    auth.visible_depot_ids = [UUID("11111111-1111-4111-8111-111111111111")]
    return build_sql_agent_tool_registry(MagicMock(), MagicMock(), auth, page_context=page_context)


# ── Registration + handler ────────────────────────────────────────────────


def test_get_page_context_is_in_allowed_tools() -> None:
    assert "get_page_context" in SQL_AGENT_TOOL_NAMES


@pytest.mark.asyncio
async def test_handler_returns_parked_payload() -> None:
    payload = {"available": True, "note": "n", "view": {"page": "reports"}}
    reg = _registry(payload)
    result = await reg.dispatch("get_page_context", {})
    assert result == payload


@pytest.mark.asyncio
async def test_handler_returns_unavailable_when_no_context() -> None:
    reg = _registry(None)
    result = await reg.dispatch("get_page_context", {})
    assert result == {"available": False}


# ── End-to-end through the real run_qa_turn loop ───────────────────────────


@pytest.mark.asyncio
async def test_loop_calls_get_page_context_then_terminates() -> None:
    payload = {
        "available": True,
        "note": "informational",
        "view": {"page": "charger_detail"},
        "focus": {"type": "charger", "id": "cp-7"},
    }
    reg = _registry(payload)
    script = [
        _FakeResponse([_tool_use("get_page_context", "b1", {})]),
        _FakeResponse(
            [
                _tool_use(
                    EMIT_FINAL_ANSWER_TOOL,
                    "b2",
                    {"text": "Charger cp-7 is faulted.", "row_evidence": 0},
                )
            ]
        ),
    ]
    client = _FakeClient(script)

    seen: list[Any] = []

    async def _on_step(tc: Any) -> None:
        seen.append(tc)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="why is this one down?",
        tool_registry=reg,
        allowed_tools=SQL_AGENT_TOOL_NAMES,
        on_step=_on_step,
    )

    assert result.status == "success"
    assert result.text == "Charger cp-7 is faulted."
    names = [tc.name for tc in result.tool_calls]
    assert names == ["get_page_context", EMIT_FINAL_ANSWER_TOOL]
    # The dispatched tool returned the parked payload verbatim.
    ctx_call = next(tc for tc in result.tool_calls if tc.name == "get_page_context")
    assert ctx_call.ok is True
    assert ctx_call.result == payload
    assert [tc.name for tc in seen] == ["get_page_context", EMIT_FINAL_ANSWER_TOOL]
