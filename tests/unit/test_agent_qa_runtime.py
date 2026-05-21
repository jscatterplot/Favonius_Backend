"""Unit tests for the Q&A extension of the workflow runtime.

Exercises ``run_qa_turn`` in isolation with a fake Anthropic client + a
hand-rolled ToolRegistry. Verifies:

- happy path: explorer call → SQL call → terminator
- max-iterations bail-out
- ToolNotAllowedError when the LLM picks a name outside allowed_tools
- on_step callback fires for every dispatched tool call
"""

from __future__ import annotations

from typing import Any

import pytest

from src.api.agent_workflows.runtime import (
    EMIT_FINAL_ANSWER_TOOL,
    QAResult,
    ToolNotAllowedError,
    run_qa_turn,
)
from src.api.agent_workflows.tools import ToolRegistry


# ── Fake Anthropic SDK ──────────────────────────────────────────────────


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


# ── Tool registry helpers ───────────────────────────────────────────────


def _registry() -> tuple[ToolRegistry, list[tuple[str, dict]]]:
    """Build a registry of two tools + the terminator. Returns the
    registry plus a mutable call log the tests can assert on."""
    reg = ToolRegistry()
    log: list[tuple[str, dict]] = []

    async def _list_tables(**_):
        log.append(("list_tables", {}))
        return [{"name": "agent_views.sessions"}]

    async def _run_select(*, sql: str, **_):
        log.append(("run_select_ts", {"sql": sql}))
        return {"rows": [{"depot_id": "d1", "n": 42}], "row_count": 1}

    async def _terminator(*, text: str, row_evidence: int = 0, **_):
        log.append((EMIT_FINAL_ANSWER_TOOL, {"text": text, "row_evidence": row_evidence}))
        return {"text": text, "row_evidence": row_evidence}

    reg.register(
        "list_tables",
        description="list",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_list_tables,
    )
    reg.register(
        "run_select_ts",
        description="select",
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
        fn=_run_select,
    )
    reg.register(
        EMIT_FINAL_ANSWER_TOOL,
        description="terminator",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "row_evidence": {"type": "integer", "default": 0},
            },
            "required": ["text"],
        },
        fn=_terminator,
    )
    return reg, log


def _allowed_tools() -> list[str]:
    return ["list_tables", "run_select_ts", EMIT_FINAL_ANSWER_TOOL]


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_explorer_then_select_then_terminator():
    reg, log = _registry()
    script = [
        _FakeResponse([_tool_use("list_tables", "b1", {})]),
        _FakeResponse([
            _tool_use("run_select_ts", "b2", {"sql": "SELECT 1"})
        ]),
        _FakeResponse([
            _tool_use(
                EMIT_FINAL_ANSWER_TOOL,
                "b3",
                {"text": "There were 42 sessions.", "row_evidence": 1},
            )
        ]),
    ]
    client = _FakeClient(script)

    step_log: list[str] = []

    async def _on_step(tc):
        step_log.append(tc.name)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="user q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
        on_step=_on_step,
    )

    assert isinstance(result, QAResult)
    assert result.status == "success"
    assert result.text == "There were 42 sessions."
    assert result.row_evidence == 1
    assert [tc.name for tc in result.tool_calls] == [
        "list_tables",
        "run_select_ts",
        EMIT_FINAL_ANSWER_TOOL,
    ]
    assert step_log == ["list_tables", "run_select_ts", EMIT_FINAL_ANSWER_TOOL]
    assert [name for name, _ in log] == [
        "list_tables",
        "run_select_ts",
        EMIT_FINAL_ANSWER_TOOL,
    ]


@pytest.mark.asyncio
async def test_max_iterations_without_terminator():
    reg, _ = _registry()
    # Always call list_tables, never terminate.
    looping_script = [
        _FakeResponse([_tool_use("list_tables", f"b{i}", {})])
        for i in range(10)
    ]
    client = _FakeClient(looping_script)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="user q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
        max_iterations=3,
    )

    assert result.status == "max_iterations"
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_max_iterations_zero_clamped_to_one():
    """``max_iterations=0`` is clamped to 1 (matching WorkflowAgent.__init__).

    Without the clamp the loop would never execute and the returned
    ``QAResult.iterations`` would be 0 — nonsensical: the caller asked
    for "zero turns" but the runtime still returned a result. Clamping
    to 1 means the model gets at least one turn even when the caller
    passes a degenerate value, and ``iterations`` reflects what the
    runtime actually did.
    """
    reg, _ = _registry()
    script = [_FakeResponse([_tool_use("list_tables", "b1", {})])]
    client = _FakeClient(script)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="user q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
        max_iterations=0,
    )

    assert result.iterations == 1
    assert result.status == "max_iterations"


@pytest.mark.asyncio
async def test_tool_not_allowed_raises():
    reg, _ = _registry()
    # Add a "secret" tool to the registry but NOT to allowed_tools.
    async def _secret(**_):
        return {"ok": True}
    reg.register(
        "secret_admin_tool",
        description="x",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_secret,
    )

    script = [_FakeResponse([_tool_use("secret_admin_tool", "b1", {})])]
    client = _FakeClient(script)

    with pytest.raises(ToolNotAllowedError):
        await run_qa_turn(
            anthropic_client=client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            user_message="q",
            tool_registry=reg,
            allowed_tools=_allowed_tools(),
        )


@pytest.mark.asyncio
async def test_tool_not_allowed_preserves_partial_trace():
    """Codex P2: when a disallowed tool aborts the loop mid-response,
    successful prior tool calls in the SAME response (or earlier turns)
    must be reachable on the exception so the caller can mirror them to
    the admin audit feed. Without this, policy-violation turns lose
    audit evidence for the calls that DID execute.
    """
    reg, _ = _registry()
    async def _secret(**_):
        return {"ok": True}
    reg.register(
        "secret_admin_tool",
        description="x",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_secret,
    )

    # Response 1 dispatches an allowed run_select_ts successfully.
    # Response 2 calls a disallowed secret tool → aborts the loop.
    script = [
        _FakeResponse([_tool_use("run_select_ts", "b1", {"sql": "SELECT 1"})]),
        _FakeResponse([_tool_use("secret_admin_tool", "b2", {})]),
    ]
    client = _FakeClient(script)

    with pytest.raises(ToolNotAllowedError) as ei:
        await run_qa_turn(
            anthropic_client=client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            user_message="q",
            tool_registry=reg,
            allowed_tools=_allowed_tools(),
        )

    # The successful run_select_ts call must still be on the exception
    # so the controller can write it to the admin audit feed.
    partial = list(getattr(ei.value, "tool_calls", []))
    assert len(partial) == 1
    assert partial[0].name == "run_select_ts"
    assert partial[0].ok is True


@pytest.mark.asyncio
async def test_no_terminator_when_model_returns_text_only():
    reg, _ = _registry()
    script = [_FakeResponse(
        [_FakeBlock("text", text="here is my answer without using emit_final_answer")]
    )]
    client = _FakeClient(script)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
    )
    assert result.status == "no_terminator"
    assert "here is my answer" in result.text


@pytest.mark.asyncio
async def test_terminator_must_be_in_allowed_tools():
    reg, _ = _registry()
    client = _FakeClient([])  # never called

    with pytest.raises(Exception):  # WorkflowRuntimeError
        await run_qa_turn(
            anthropic_client=client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            user_message="q",
            tool_registry=reg,
            allowed_tools=["list_tables"],  # no terminator
        )


@pytest.mark.asyncio
async def test_error_envelope_return_marks_tool_call_as_failure():
    """A tool that returns ``{"error": ...}`` (without raising) must be
    surfaced to the runtime as ``ok=False`` so audit aggregation and
    tool_result(is_error=True) downstream both match what actually
    happened. See PR #216 review thread."""
    reg = ToolRegistry()

    async def _envelope_failer(*, sql: str, **_):
        return {"error": "rejected by validator", "error_kind": "table_not_allowed"}

    async def _terminator(*, text: str, row_evidence: int = 0, **_):
        return {"text": text, "row_evidence": row_evidence}

    reg.register(
        "run_select_ts",
        description="select",
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
        fn=_envelope_failer,
    )
    reg.register(
        EMIT_FINAL_ANSWER_TOOL,
        description="terminator",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=_terminator,
    )

    script = [
        _FakeResponse([_tool_use("run_select_ts", "b1", {"sql": "SELECT * FROM public.x"})]),
        _FakeResponse([
            _tool_use(
                EMIT_FINAL_ANSWER_TOOL, "b2",
                {"text": "I cannot answer because the validator rejected my SQL."}
            )
        ]),
    ]
    client = _FakeClient(script)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=["run_select_ts", EMIT_FINAL_ANSWER_TOOL],
    )

    failed = next(tc for tc in result.tool_calls if tc.name == "run_select_ts")
    assert failed.ok is False
    assert "table_not_allowed" in (failed.error or "")
    assert result.status == "success"


@pytest.mark.asyncio
async def test_tool_dispatch_error_is_returned_to_model():
    """A tool that raises mid-loop must produce a tool_result(error=True)
    and not abort the whole turn."""
    reg = ToolRegistry()

    async def _broken_tool(**_):
        raise RuntimeError("simulated DB outage")

    async def _terminator(*, text: str, row_evidence: int = 0, **_):
        return {"text": text, "row_evidence": row_evidence}

    reg.register(
        "list_tables",
        description="",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_broken_tool,
    )
    reg.register(
        EMIT_FINAL_ANSWER_TOOL,
        description="",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=_terminator,
    )

    script = [
        _FakeResponse([_tool_use("list_tables", "b1", {})]),
        _FakeResponse([
            _tool_use(
                EMIT_FINAL_ANSWER_TOOL, "b2",
                {"text": "tool failed, here is what I can say"}
            )
        ]),
    ]
    client = _FakeClient(script)

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=["list_tables", EMIT_FINAL_ANSWER_TOOL],
    )

    assert result.status == "success"
    # The broken tool's tool_call must be in the trace with ok=False.
    broken = next(tc for tc in result.tool_calls if tc.name == "list_tables")
    assert broken.ok is False
    assert "simulated DB outage" in (broken.error or "")
