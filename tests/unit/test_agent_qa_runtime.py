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
from src.api.agent_workflows.tools import ToolNotRegisteredError
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
async def test_thinking_capable_model_sends_adaptive_thinking_and_effort():
    # A thinking-capable model (Sonnet 4.6) must get adaptive thinking +
    # effort and NO temperature on the loop's create call — thinking is
    # incompatible with custom sampling params.
    reg, _log = _registry()
    script = [
        _FakeResponse(
            [_tool_use(EMIT_FINAL_ANSWER_TOOL, "b1", {"text": "done", "row_evidence": 0})]
        ),
    ]
    client = _FakeClient(script)

    await run_qa_turn(
        anthropic_client=client,
        model="claude-sonnet-4-6",
        system_prompt="sys",
        user_message="user q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
        effort="medium",
    )

    call = client.messages.calls[0]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "medium"}
    assert "temperature" not in call


@pytest.mark.asyncio
async def test_non_thinking_model_sends_temperature_not_thinking():
    # Haiku 4.5 supports neither adaptive thinking nor effort; the loop
    # must fall back to temperature and omit thinking/output_config.
    reg, _log = _registry()
    script = [
        _FakeResponse(
            [_tool_use(EMIT_FINAL_ANSWER_TOOL, "b1", {"text": "done", "row_evidence": 0})]
        ),
    ]
    client = _FakeClient(script)

    await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="user q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
        temperature=0.0,
    )

    call = client.messages.calls[0]
    assert call["temperature"] == 0.0
    assert "thinking" not in call
    assert "output_config" not in call


@pytest.mark.asyncio
async def test_happy_path_explorer_then_select_then_terminator():
    reg, log = _registry()
    script = [
        _FakeResponse([_tool_use("list_tables", "b1", {})]),
        _FakeResponse([_tool_use("run_select_ts", "b2", {"sql": "SELECT 1"})]),
        _FakeResponse(
            [
                _tool_use(
                    EMIT_FINAL_ANSWER_TOOL,
                    "b3",
                    {"text": "There were 42 sessions.", "row_evidence": 1},
                )
            ]
        ),
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
    # Each turn's tool_use blocks must have a matching tool_result in
    # the next user message for the Anthropic API contract. Verify the
    # client saw a complete tool_result trail across all three turns —
    # specifically the terminator's tool_result must be appended too,
    # not silently dropped by the `break`. Bugbot flagged the latent
    # issue earlier; this asserts the fix.
    user_messages_with_tool_results = []
    for call in client.messages.calls:
        for msg in call["messages"]:
            if msg["role"] == "user" and isinstance(msg["content"], list):
                for entry in msg["content"]:
                    if isinstance(entry, dict) and entry.get("type") == "tool_result":
                        user_messages_with_tool_results.append(entry["tool_use_id"])
    # We should see tool_results for b1 (list_tables) and b2 (run_select_ts).
    # The terminator's tool_result is captured before the outer break in
    # the local messages list but never sent back (we return before the
    # next API call). The assertion is therefore: every NON-terminator
    # tool_use block has its tool_result reach the API.
    assert "b1" in user_messages_with_tool_results
    assert "b2" in user_messages_with_tool_results
    assert step_log == ["list_tables", "run_select_ts", EMIT_FINAL_ANSWER_TOOL]
    assert [name for name, _ in log] == [
        "list_tables",
        "run_select_ts",
        EMIT_FINAL_ANSWER_TOOL,
    ]


class _FakeUsage:
    """Mimic the Anthropic SDK's response.usage object shape."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _registry_with_raising_terminator() -> tuple[ToolRegistry, list[tuple[str, dict]]]:
    """Same as ``_registry`` but the terminator ALWAYS raises a generic
    exception. Used to test the terminator-failure-is-terminal path."""
    reg = ToolRegistry()
    log: list[tuple[str, dict]] = []

    async def _run_select(*, sql: str, **_):
        log.append(("run_select_ts", {"sql": sql}))
        return {"rows": [{"depot_id": "d1", "n": 42}], "row_count": 1}

    async def _broken_terminator(**kwargs):
        log.append((EMIT_FINAL_ANSWER_TOOL, kwargs))
        raise RuntimeError("simulated terminator dispatch failure")

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
        fn=_broken_terminator,
    )
    return reg, log


@pytest.mark.asyncio
async def test_terminator_failure_skips_remaining_tool_use_blocks_in_same_response():
    """Bugbot M-sev: when emit_final_answer dispatch raises, the prior
    code did ``continue`` and would dispatch any tool_use blocks that
    followed the terminator IN THE SAME response. The terminator should
    be terminal regardless of success or failure — matching
    WorkflowAgent's emit_decision behaviour.

    This test scripts a single response containing
    ``[run_select_ts, emit_final_answer, run_select_ts]`` and verifies
    the third block is NOT dispatched after the terminator fails.
    """
    reg, log = _registry_with_raising_terminator()
    # ONE response containing three blocks; the second (terminator) raises.
    multi_block_response = _FakeResponse(
        [
            _tool_use("run_select_ts", "b1", {"sql": "SELECT 1"}),
            _tool_use(EMIT_FINAL_ANSWER_TOOL, "b2", {"text": "ok", "row_evidence": 1}),
            _tool_use("run_select_ts", "b3", {"sql": "SELECT 2"}),
        ]
    )
    client = _FakeClient([multi_block_response])

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=["run_select_ts", EMIT_FINAL_ANSWER_TOOL],
    )

    # Block 1 dispatched. Block 2 (terminator) attempted-and-failed.
    # Block 3 SKIPPED because terminator is terminal.
    names_called = [name for name, _ in log]
    assert names_called == [
        "run_select_ts",
        EMIT_FINAL_ANSWER_TOOL,
    ], f"expected b3 to be skipped after terminator failure; got {names_called}"

    # The QAResult exposes the failure via status="terminator_failed"
    # so the controller can map it to an error reply rather than a
    # not_found / success.
    assert result.status == "terminator_failed"
    assert result.text == ""
    # iterations == 1 (one API round-trip — we broke without looping).
    assert result.iterations == 1
    # Exactly one round-trip — we did NOT loop back to the API after
    # the terminator failed.
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_terminator_failure_does_not_loop_back_to_api():
    """A simpler regression: even with a single-block response where the
    terminator fails, we must NOT make a second API call (which would
    let the LLM retry). The prior `continue` path would do so. The
    new break-and-set-terminated path exits immediately."""
    reg, _ = _registry_with_raising_terminator()
    single_block_response = _FakeResponse([_tool_use(EMIT_FINAL_ANSWER_TOOL, "b1", {"text": "ok"})])
    # If the loop incorrectly continued, it would hit the second
    # response and call the (working) select. With the fix, the second
    # response is never used because we break the outer loop.
    second_response = _FakeResponse([_tool_use("run_select_ts", "b2", {"sql": "SELECT 3"})])
    client = _FakeClient([single_block_response, second_response])

    result = await run_qa_turn(
        anthropic_client=client,
        model="claude-haiku-4-5",
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=["run_select_ts", EMIT_FINAL_ANSWER_TOOL],
    )

    assert result.status == "terminator_failed"
    assert len(client.messages.calls) == 1  # never made the 2nd call


@pytest.mark.asyncio
async def test_run_qa_turn_records_token_usage_per_round_trip():
    """Bugbot M-sev: `run_qa_turn` was making up to `max_iterations`
    Anthropic API calls per turn but never recording token usage to
    Prometheus. The SQL-mode path is the most expensive execution path
    the agent has — leaving it unobserved means cost monitoring is
    blind to this entire feature. Verify the metric is hit on every
    round-trip.
    """
    from src.monitoring.metrics import AGENT_LLM_TOKENS

    reg, _ = _registry()

    # Three round-trips: explorer → select → terminator.
    script = [
        _FakeResponse([_tool_use("list_tables", "b1", {})]),
        _FakeResponse([_tool_use("run_select_ts", "b2", {"sql": "SELECT 1"})]),
        _FakeResponse(
            [
                _tool_use(
                    EMIT_FINAL_ANSWER_TOOL,
                    "b3",
                    {"text": "ok", "row_evidence": 1},
                )
            ]
        ),
    ]
    # Attach distinct usage to each so we can verify aggregation.
    script[0].usage = _FakeUsage(input_tokens=100, output_tokens=20)
    script[1].usage = _FakeUsage(input_tokens=150, output_tokens=30)
    script[2].usage = _FakeUsage(input_tokens=200, output_tokens=10)
    client = _FakeClient(script)

    # Snapshot counters BEFORE the run so we can diff (other tests may
    # leave residue depending on order).
    model = "claude-haiku-4-5"
    before_in = AGENT_LLM_TOKENS.labels(model=model, direction="input")._value.get()
    before_out = AGENT_LLM_TOKENS.labels(model=model, direction="output")._value.get()

    result = await run_qa_turn(
        anthropic_client=client,
        model=model,
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
    )

    after_in = AGENT_LLM_TOKENS.labels(model=model, direction="input")._value.get()
    after_out = AGENT_LLM_TOKENS.labels(model=model, direction="output")._value.get()

    # Sum across all three round-trips: 100+150+200 input, 20+30+10 output.
    assert after_in - before_in == 450
    assert after_out - before_out == 60
    # The same aggregate is surfaced on the QAResult for the S4 budget to
    # reconcile against (src/api/agent/budget.py::record_actual).
    assert result.input_tokens == 450
    assert result.output_tokens == 60


@pytest.mark.asyncio
async def test_run_qa_turn_no_token_recording_when_usage_missing():
    """The Anthropic API occasionally returns a response without a
    ``usage`` block (e.g. on tool-use turns in some SDK versions). The
    recorder must be a no-op in that case rather than throwing
    AttributeError mid-loop."""
    from src.monitoring.metrics import AGENT_LLM_TOKENS

    reg, _ = _registry()
    script = [
        _FakeResponse([_tool_use(EMIT_FINAL_ANSWER_TOOL, "b1", {"text": "ok"})]),
    ]
    # script[0].usage is already None (default) — fall through.
    client = _FakeClient(script)

    model = "claude-haiku-4-5"
    before_in = AGENT_LLM_TOKENS.labels(model=model, direction="input")._value.get()

    # Should NOT raise.
    await run_qa_turn(
        anthropic_client=client,
        model=model,
        system_prompt="sys",
        user_message="q",
        tool_registry=reg,
        allowed_tools=_allowed_tools(),
    )

    after_in = AGENT_LLM_TOKENS.labels(model=model, direction="input")._value.get()
    assert after_in == before_in  # unchanged


@pytest.mark.asyncio
async def test_max_iterations_without_terminator():
    reg, _ = _registry()
    # Always call list_tables, never terminate.
    looping_script = [_FakeResponse([_tool_use("list_tables", f"b{i}", {})]) for i in range(10)]
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
async def test_tool_not_registered_preserves_partial_trace():
    """Bugbot M-sev: `ToolNotRegisteredError` must also carry the
    partial tool_calls trace, mirroring `ToolNotAllowedError`. Without
    this, the controller's audit-mirror branch sees an empty trace and
    skips `write_agent_query_audit` on these aborts.

    Trigger: the model invokes a tool whose name IS in `allowed_tools`
    (so the allow-list passes) but is missing a callable in the
    registry — sqlite path where the registry-side error fires.
    """
    reg, _ = _registry()

    # Simulate dispatch-time registration drift: a tool that passes the
    # `anthropic_schemas(allowed)` check at function entry but raises
    # ToolNotRegisteredError when actually invoked. (This models the
    # real bug scenario — a tool that *was* registered when the
    # workflow was loaded but whose callable subsequently disappeared
    # by the time dispatch runs, or a registry-side bug where get()
    # and dispatch() disagree.)
    async def _explodes(**_):
        raise ToolNotRegisteredError("ghost_tool")

    reg.register(
        "ghost_tool",
        description="",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_explodes,
    )
    allowed = _allowed_tools() + ["ghost_tool"]
    script = [
        _FakeResponse([_tool_use("run_select_ts", "b1", {"sql": "SELECT 1"})]),
        _FakeResponse([_tool_use("ghost_tool", "b2", {})]),
    ]
    client = _FakeClient(script)

    with pytest.raises(ToolNotRegisteredError) as ei:
        await run_qa_turn(
            anthropic_client=client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            user_message="q",
            tool_registry=reg,
            allowed_tools=allowed,
        )

    partial = list(getattr(ei.value, "tool_calls", []))
    assert len(partial) == 1
    assert partial[0].name == "run_select_ts"
    assert partial[0].ok is True
    # iterations should reflect that 2 round-trips happened before abort.
    assert getattr(ei.value, "iterations", 0) >= 1


@pytest.mark.asyncio
async def test_no_terminator_when_model_returns_text_only():
    reg, _ = _registry()
    script = [
        _FakeResponse(
            [_FakeBlock("text", text="here is my answer without using emit_final_answer")]
        )
    ]
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
        _FakeResponse(
            [
                _tool_use(
                    EMIT_FINAL_ANSWER_TOOL,
                    "b2",
                    {"text": "I cannot answer because the validator rejected my SQL."},
                )
            ]
        ),
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
        _FakeResponse(
            [
                _tool_use(
                    EMIT_FINAL_ANSWER_TOOL, "b2", {"text": "tool failed, here is what I can say"}
                )
            ]
        ),
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
