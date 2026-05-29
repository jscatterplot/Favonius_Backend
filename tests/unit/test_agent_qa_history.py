"""Guard test for the additive ``history`` param on run_qa_turn.

The collaborative document-fill path passes prior conversation turns via
``history=``. This test pins two invariants so the golden-gated runtime change
stays safe:

1. ``history=None`` (every existing caller) → the message list is exactly the
   single fresh user turn, identical to before.
2. ``history=[...]`` → those turns are prepended, in order, before the new
   user message.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.api.agent_workflows.runtime import EMIT_FINAL_ANSWER_TOOL, run_qa_turn
from src.api.agent_workflows.tools import ToolRegistry


class _FakeBlock:
    def __init__(self, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.stop_reason = "tool_use"
        self.usage = None


class _FakeMessages:
    def __init__(self, script: list[_FakeResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        # Snapshot ``messages`` at call time — the loop mutates the same list
        # afterwards (appending the assistant turn), exactly as the real SDK
        # serializes the list at call time. Without the copy we'd inspect the
        # post-loop state, not what was sent.
        snap = dict(kwargs)
        if "messages" in snap:
            snap["messages"] = [dict(m) for m in snap["messages"]]
        self.calls.append(snap)
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script: list[_FakeResponse]) -> None:
        self.messages = _FakeMessages(script)


def _terminating_registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def _terminator(*, text: str = "", row_evidence: int = 0, **_: Any) -> dict:
        return {"text": text, "row_evidence": row_evidence}

    reg.register(
        EMIT_FINAL_ANSWER_TOOL,
        description="terminator",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        fn=_terminator,
    )
    return reg


def _term_script() -> list[_FakeResponse]:
    return [
        _FakeResponse(
            [_FakeBlock("tool_use", id="b1", name=EMIT_FINAL_ANSWER_TOOL, input={"text": "ok"})]
        )
    ]


@pytest.mark.asyncio
async def test_history_none_is_single_user_turn():
    client = _FakeClient(_term_script())
    await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="new question",
        tool_registry=_terminating_registry(),
        allowed_tools=[EMIT_FINAL_ANSWER_TOOL],
        temperature=0.0,
    )
    sent = client.messages.calls[0]["messages"]
    assert sent == [{"role": "user", "content": "new question"}]


@pytest.mark.asyncio
async def test_history_is_prepended_in_order():
    client = _FakeClient(_term_script())
    history = [
        {"role": "user", "content": "first q"},
        {"role": "assistant", "content": "first a"},
    ]
    await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="second q",
        tool_registry=_terminating_registry(),
        allowed_tools=[EMIT_FINAL_ANSWER_TOOL],
        temperature=0.0,
        history=history,
    )
    sent = client.messages.calls[0]["messages"]
    assert sent == [
        {"role": "user", "content": "first q"},
        {"role": "assistant", "content": "first a"},
        {"role": "user", "content": "second q"},
    ]


@pytest.mark.asyncio
async def test_history_entries_are_copied_not_mutated():
    # The loop appends the assistant turn + tool results to its own list; the
    # caller's history dicts must not be mutated.
    client = _FakeClient(_term_script())
    history = [{"role": "user", "content": "q"}]
    original = [dict(m) for m in history]
    await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q2",
        tool_registry=_terminating_registry(),
        allowed_tools=[EMIT_FINAL_ANSWER_TOOL],
        temperature=0.0,
        history=history,
    )
    assert history == original
