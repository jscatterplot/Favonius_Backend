"""Unit tests for the depot-agent workflow runtime.

The tests build a :class:`WorkflowAgent` from in-process fakes:

- The Anthropic client is a scripted stub that yields a fixed sequence
  of responses, so we can reproduce the tool-use loop deterministically.
- The :class:`ToolRegistry` is populated per-test with async handlers
  that return canned values (or that we wire to inspect call arguments).
- The :class:`DecisionRepo` is the in-memory implementation.

Together these let us cover every behaviour described in
``docs/PRD_Depot_Agent.md`` §4.3, §4.4, §10.3, §10.4 without touching
the live LLM or the database.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows import (
    SUBMIT_DECISION_TOOL_NAME,
    Decision,
    DecisionLoopError,
    DepotConstraints,
    GraduationRule,
    HardConstraintGuard,
    InMemoryDecisionRepo,
    PermissionTier,
    Tool,
    ToolNotAllowedError,
    ToolNotRegisteredError,
    ToolRegistry,
    Workflow,
    WorkflowAgent,
)
from src.api.agent_workflows.constraints import (
    DEPARTURE_SOC_FLOOR,
    PROPOSED_ACTION_KINDS,
)
from src.api.agent_workflows.repo import AsyncpgDecisionRepo
from src.api.agent_workflows.runtime import (
    WorkflowAgentConfig,
    _canonical_inputs_hash,
)
from src.api.agent_workflows.schemas import ToolCallRecord

DEPOT_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")
ORG_ID = UUID("33333333-3333-3333-3333-333333333333")


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_workflow(*, allowed: tuple[str, ...] = ("get_vehicle_state",)) -> Workflow:
    return Workflow(
        id="readiness_check",
        name="Daily readiness",
        version="1.0.0",
        description="",
        prompt="You are the readiness-check workflow.",
        allowed_tools=allowed,
        permission_tier=PermissionTier.INFORM,
        graduation_rule=GraduationRule(
            min_decisions=100,
            max_override_rate=0.05,
            max_edit_rate=0.15,
            requires_human_signoff=True,
            next_tier=PermissionTier.ACT_AND_NOTIFY,
        ),
    )


def _auth_context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        role="customer_operator",
        visible_depot_ids=[DEPOT_ID],
    )


def _constraints(*, max_grid_kw: float = 250.0) -> DepotConstraints:
    return DepotConstraints(depot_id=DEPOT_ID, max_grid_kw=max_grid_kw)


async def _load_constraints(_depot_id: UUID) -> DepotConstraints:
    return _constraints()


def _tool_use_block(*, name: str, input_data: dict[str, Any], block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input_data, id=block_id)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _make_response(
    *,
    content: list[Any],
    stop_reason: str,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


class _ScriptedClient:
    """Anthropic client double whose ``messages.create`` returns scripted responses.

    The class records every call so tests can assert which tools were
    exposed in the ``tools=`` argument and which messages were sent.
    ``messages`` is shallow-copied at capture time — the runtime mutates
    the same list across iterations, so storing a reference would make
    every call observe the final state. ``content`` lists inside each
    message are NOT deep-copied (they reference SDK content blocks that
    we never mutate).
    """

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = [dict(m) for m in snapshot["messages"]]
        self.calls.append(snapshot)
        if not self._responses:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._responses.pop(0)


def _registry_with_get_vehicle_state(
    return_value: dict[str, Any] | None = None,
) -> tuple[ToolRegistry, AsyncMock]:
    registry = ToolRegistry()
    handler = AsyncMock(return_value=return_value or {"soc": 0.85, "vehicle_id": "BUS-014"})
    registry.register(
        Tool(
            name="get_vehicle_state",
            description="Return SoC + charger state for one vehicle",
            input_schema={
                "type": "object",
                "properties": {"vehicle_id": {"type": "string"}},
                "required": ["vehicle_id"],
            },
            handler=handler,
        )
    )
    return registry, handler


def _make_agent(
    *,
    client: _ScriptedClient,
    repo: InMemoryDecisionRepo | None = None,
    config: WorkflowAgentConfig | None = None,
) -> tuple[WorkflowAgent, InMemoryDecisionRepo]:
    repo = repo or InMemoryDecisionRepo()
    agent = WorkflowAgent(
        anthropic_client=client,
        repo=repo,
        depot_constraints_loader=_load_constraints,
        config=config or WorkflowAgentConfig(max_tool_iterations=4),
    )
    return agent, repo


# ── Schema / registry tests ─────────────────────────────────────────────


def test_workflow_is_frozen() -> None:
    wf = _make_workflow()
    with pytest.raises(Exception):
        wf.id = "different"  # type: ignore[misc]


def test_registry_rejects_sync_handler() -> None:
    registry = ToolRegistry()

    def sync_handler(**_kwargs: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        registry.register(
            Tool(
                name="sync_tool",
                description="x",
                input_schema={"type": "object"},
                handler=sync_handler,  # type: ignore[arg-type]
            )
        )


def test_registry_duplicate_registration_raises() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    with pytest.raises(ValueError):
        registry.register(
            Tool(
                name="get_vehicle_state",
                description="dup",
                input_schema={"type": "object"},
                handler=AsyncMock(),
            )
        )


def test_registry_filtered_for_workflow_rejects_unknown_tool() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow(allowed=("get_vehicle_state", "does_not_exist"))
    with pytest.raises(ToolNotRegisteredError):
        registry.filtered_for_workflow(workflow)


def test_registry_filtered_for_workflow_rejects_duplicates() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow(allowed=("get_vehicle_state", "get_vehicle_state"))
    with pytest.raises(ValueError):
        registry.filtered_for_workflow(workflow)


def test_registry_register_function_convenience() -> None:
    registry = ToolRegistry()
    handler = AsyncMock(return_value={"ok": True})
    tool = registry.register_function(
        name="ping",
        description="ping",
        input_schema={"type": "object"},
        handler=handler,
    )
    assert "ping" in registry
    assert "missing" not in registry
    assert registry.get("ping") is tool
    assert tool.to_anthropic_tool()["name"] == "ping"
    assert registry.names() == {"ping"}


def test_registry_get_unknown_raises_tool_not_registered() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotRegisteredError):
        registry.get("nope")


# ── Hard-constraint guard ───────────────────────────────────────────────


def test_guard_passes_through_unrelated_actions() -> None:
    guard = HardConstraintGuard()
    output = {
        "summary": "all clear",
        "proposed_actions": [
            {"type": "draft_email", "to": "ops@x", "subject": "hi", "body": "y"},
        ],
    }
    filtered, violations = guard.filter_output(output, _constraints())
    assert violations == []
    assert filtered["proposed_actions"] == output["proposed_actions"]


def test_guard_blocks_departure_soc_violation() -> None:
    guard = HardConstraintGuard()
    output = {
        "proposed_actions": [
            {"type": "set_departure_soc", "vehicle_id": "BUS-1", "target_soc": 0.90},
            {"type": "set_departure_soc", "vehicle_id": "BUS-2", "target_soc": 0.995},
        ]
    }
    filtered, violations = guard.filter_output(output, _constraints())
    assert len(filtered["proposed_actions"]) == 1
    assert filtered["proposed_actions"][0]["vehicle_id"] == "BUS-2"
    assert len(violations) == 1
    assert "departure SoC floor" in violations[0]


def test_guard_blocks_grid_violation() -> None:
    guard = HardConstraintGuard()
    output = {
        "proposed_actions": [
            {"type": "set_grid_power_kw", "value_kw": 500.0},
        ]
    }
    filtered, violations = guard.filter_output(output, _constraints(max_grid_kw=250.0))
    assert filtered["proposed_actions"] == []
    assert len(violations) == 1
    assert "max_grid_kw" in violations[0]


def test_guard_blocks_charger_setpoint_above_grid_cap() -> None:
    guard = HardConstraintGuard()
    output = {
        "proposed_actions": [
            {
                "type": "set_charger_power_kw",
                "charger_id": "CP-1",
                "value_kw": 999.0,
            }
        ]
    }
    filtered, violations = guard.filter_output(output, _constraints(max_grid_kw=250.0))
    assert filtered["proposed_actions"] == []
    assert violations and "max_grid_kw" in violations[0]


def test_guard_rejects_missing_numeric_fields() -> None:
    guard = HardConstraintGuard()
    output = {
        "proposed_actions": [
            {"type": "set_departure_soc", "vehicle_id": "BUS-1"},
            {"type": "set_grid_power_kw"},
            {"type": "set_charger_power_kw", "charger_id": "CP-1"},
        ]
    }
    filtered, violations = guard.filter_output(output, _constraints())
    assert filtered["proposed_actions"] == []
    assert len(violations) == 3


def test_guard_skips_non_dict_action_entries() -> None:
    guard = HardConstraintGuard()
    output = {"proposed_actions": ["junk", 42, None]}
    filtered, violations = guard.filter_output(output, _constraints())
    assert filtered["proposed_actions"] == []
    assert len(violations) == 3


def test_guard_handles_malformed_proposed_actions() -> None:
    guard = HardConstraintGuard()
    output = {"summary": "x", "proposed_actions": "not a list"}
    filtered, violations = guard.filter_output(output, _constraints())
    assert filtered["proposed_actions"] == []
    assert violations and "not a list" in violations[0]


def test_guard_passes_valid_grid_and_charger_setpoints() -> None:
    """Setpoints under the cap must pass the guard untouched."""
    guard = HardConstraintGuard()
    output = {
        "proposed_actions": [
            {"type": "set_grid_power_kw", "value_kw": 200.0},
            {
                "type": "set_charger_power_kw",
                "charger_id": "CP-1",
                "value_kw": 120.0,
            },
            {"type": "set_departure_soc", "vehicle_id": "BUS-1", "target_soc": 1.0},
        ]
    }
    filtered, violations = guard.filter_output(output, _constraints(max_grid_kw=250.0))
    assert violations == []
    assert len(filtered["proposed_actions"]) == 3


def test_guard_floor_constant_is_99_percent() -> None:
    # Locking the PRD §10.3 constant into a test so accidental edits
    # to the constraints layer must consciously break this assertion.
    assert DEPARTURE_SOC_FLOOR == 0.99
    assert "set_departure_soc" in PROPOSED_ACTION_KINDS


# ── Runtime: happy path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_writes_pending_decision() -> None:
    registry, handler = _registry_with_get_vehicle_state(
        return_value={"vehicle_id": "BUS-1", "soc": 0.92}
    )
    workflow = _make_workflow()

    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name="get_vehicle_state",
                        input_data={"vehicle_id": "BUS-1"},
                        block_id="use_1",
                    )
                ],
                stop_reason="tool_use",
            ),
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={
                            "summary": "BUS-1 will be ready at 06:30.",
                            "proposed_actions": [],
                            "rule_applied": "soc_projection_v1",
                        },
                        block_id="use_2",
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    agent, repo = _make_agent(client=client)

    decision = await agent.run_turn(
        workflow=workflow,
        depot_id=DEPOT_ID,
        auth_context=_auth_context(),
        tool_registry=registry,
    )

    assert isinstance(decision, Decision)
    assert decision.disposition == "pending"
    assert decision.workflow_id == "readiness_check"
    assert decision.workflow_version == "1.0.0"
    assert decision.depot_id == DEPOT_ID
    assert decision.user_id == USER_ID
    assert decision.organization_id == ORG_ID
    assert decision.permission_tier == PermissionTier.INFORM
    assert decision.rule_applied == "soc_projection_v1"
    assert decision.output["summary"] == "BUS-1 will be ready at 06:30."
    assert decision.output["proposed_actions"] == []
    assert decision.constraint_violations == []
    assert decision.input_tokens > 0
    assert decision.output_tokens > 0
    assert decision.latency_ms >= 0
    assert decision.model_id == client.calls[0]["model"]
    assert decision.stop_reason == "tool_use"

    handler.assert_awaited_once_with(vehicle_id="BUS-1")
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].name == "get_vehicle_state"
    assert decision.tool_calls[0].output == {"vehicle_id": "BUS-1", "soc": 0.92}
    assert decision.tool_calls[0].error is None

    assert repo.rows == [decision]


@pytest.mark.asyncio
async def test_only_allow_listed_tools_passed_to_anthropic() -> None:
    """The model must literally not see tools outside the allow-list."""
    registry, _ = _registry_with_get_vehicle_state()
    # Register a second tool that the workflow does NOT permit.
    registry.register(
        Tool(
            name="set_grid_power_kw",
            description="dangerous",
            input_schema={"type": "object"},
            handler=AsyncMock(return_value={}),
        )
    )
    workflow = _make_workflow(allowed=("get_vehicle_state",))

    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={"summary": "noop"},
                        block_id="x",
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    agent, _repo = _make_agent(client=client)

    await agent.run_turn(
        workflow=workflow,
        depot_id=DEPOT_ID,
        auth_context=_auth_context(),
        tool_registry=registry,
    )

    sent_tools = [t["name"] for t in client.calls[0]["tools"]]
    assert "get_vehicle_state" in sent_tools
    assert SUBMIT_DECISION_TOOL_NAME in sent_tools
    assert "set_grid_power_kw" not in sent_tools


@pytest.mark.asyncio
async def test_system_prompt_carries_cache_control() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow()
    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={"summary": "done"},
                        block_id="x",
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    agent, _repo = _make_agent(client=client)
    await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)

    system = client.calls[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert workflow.prompt in system[0]["text"]
    # The workflow id+version must appear so two workflows with identical
    # prompt text don't collide on the prefix cache key.
    assert workflow.id in system[0]["text"]
    assert workflow.version in system[0]["text"]


@pytest.mark.asyncio
async def test_user_input_dict_is_rendered_into_first_message() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow()
    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={"summary": "done"},
                        block_id="x",
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    agent, _repo = _make_agent(client=client)
    await agent.run_turn(
        workflow=workflow,
        depot_id=DEPOT_ID,
        auth_context=_auth_context(),
        tool_registry=registry,
        user_input={"window": "06:00-09:00"},
    )

    first_user_message = client.calls[0]["messages"][0]
    assert first_user_message["role"] == "user"
    assert "06:00-09:00" in first_user_message["content"]


# ── Runtime: allow-list enforcement at dispatch ─────────────────────────


@pytest.mark.asyncio
async def test_allow_list_enforcement_when_model_hallucinates_tool() -> None:
    """If the model somehow names a tool outside the allow-list at
    dispatch time, the runtime must raise ToolNotAllowedError.

    This is a defence-in-depth check: the ``tools=`` array sent to
    Anthropic shouldn't contain the disallowed tool to begin with, but
    we still validate per-block.
    """
    registry, _ = _registry_with_get_vehicle_state()
    # Add a registered-but-not-allowed tool.
    registry.register(
        Tool(
            name="set_grid_power_kw",
            description="dangerous",
            input_schema={"type": "object"},
            handler=AsyncMock(return_value={}),
        )
    )
    workflow = _make_workflow(allowed=("get_vehicle_state",))

    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name="set_grid_power_kw",
                        input_data={"value_kw": 999},
                        block_id="use_1",
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    agent, _repo = _make_agent(client=client)

    with pytest.raises(ToolNotAllowedError):
        await agent.run_turn(
            workflow=workflow,
            depot_id=DEPOT_ID,
            auth_context=_auth_context(),
            tool_registry=registry,
        )


@pytest.mark.asyncio
async def test_workflow_construction_fails_fast_for_unregistered_tool() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow(allowed=("get_vehicle_state", "no_such_tool"))
    client = _ScriptedClient([])
    agent, _repo = _make_agent(client=client)

    with pytest.raises(ToolNotRegisteredError):
        await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)
    # Anthropic must not have been called once we know the workflow is
    # mis-configured.
    assert client.calls == []


# ── Runtime: hard-constraint enforcement ───────────────────────────────


@pytest.mark.asyncio
async def test_hard_constraint_filters_unsafe_actions() -> None:
    """The agent may PROPOSE a violation; the runtime must not LET it through."""
    registry = ToolRegistry()
    # Tool that returns a constraint-violating state — simulating a
    # depot that the LLM might 'fix' by lowering departure SoC.
    registry.register(
        Tool(
            name="get_scheduled_departures",
            description="x",
            input_schema={"type": "object"},
            handler=AsyncMock(
                return_value=[
                    {
                        "vehicle_id": "BUS-1",
                        "required_soc": 0.99,
                        "projected_soc": 0.75,
                    }
                ]
            ),
        )
    )
    workflow = _make_workflow(allowed=("get_scheduled_departures",))

    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name="get_scheduled_departures",
                        input_data={"depot_id": str(DEPOT_ID)},
                        block_id="u1",
                    )
                ],
                stop_reason="tool_use",
            ),
            # The LLM proposes two illegal actions: depart at 80% SoC,
            # and let grid power hit 1 MW on a 250 kW site. The guard
            # must strip both and record violations.
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={
                            "summary": "Recommend deferring charging.",
                            "proposed_actions": [
                                {
                                    "type": "set_departure_soc",
                                    "vehicle_id": "BUS-1",
                                    "target_soc": 0.80,
                                },
                                {
                                    "type": "set_grid_power_kw",
                                    "value_kw": 1000.0,
                                },
                                {
                                    "type": "notify_manager",
                                    "summary": "BUS-1 can't be ready in time",
                                },
                            ],
                        },
                        block_id="u2",
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    agent, repo = _make_agent(client=client)

    decision = await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)

    assert len(decision.constraint_violations) == 2
    assert any("departure SoC" in v for v in decision.constraint_violations)
    assert any("max_grid_kw" in v for v in decision.constraint_violations)

    # The remaining proposed_action (notify_manager) is benign and survives.
    surviving = decision.output["proposed_actions"]
    assert len(surviving) == 1
    assert surviving[0]["type"] == "notify_manager"

    # rule_applied is auto-stamped when the guard fires.
    assert decision.rule_applied == "hard_constraint_violation"
    assert decision.disposition == "pending"
    assert repo.rows == [decision]


# ── Runtime: inputs_hash determinism ────────────────────────────────────


@pytest.mark.asyncio
async def test_inputs_hash_stable_across_runs_with_identical_tool_outputs() -> None:
    workflow = _make_workflow()

    async def run_once() -> Decision:
        registry, _ = _registry_with_get_vehicle_state(
            return_value={"vehicle_id": "BUS-1", "soc": 0.92}
        )
        client = _ScriptedClient(
            [
                _make_response(
                    content=[
                        _tool_use_block(
                            name="get_vehicle_state",
                            input_data={"vehicle_id": "BUS-1"},
                            block_id="u1",
                        )
                    ],
                    stop_reason="tool_use",
                    input_tokens=11,
                    output_tokens=22,
                ),
                _make_response(
                    content=[
                        _tool_use_block(
                            name=SUBMIT_DECISION_TOOL_NAME,
                            input_data={"summary": "BUS-1 ready"},
                            block_id="u2",
                        )
                    ],
                    stop_reason="tool_use",
                    input_tokens=13,
                    output_tokens=24,
                ),
            ]
        )
        agent, _repo = _make_agent(client=client)
        return await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)

    d1 = await run_once()
    d2 = await run_once()
    assert d1.inputs_hash == d2.inputs_hash
    assert len(d1.inputs_hash) == 64


def test_inputs_hash_ignores_duration_and_error_fields() -> None:
    a = ToolCallRecord(
        name="t",
        input={"x": 1},
        output={"y": 2},
        duration_ms=10,
    )
    b = ToolCallRecord(
        name="t",
        input={"x": 1},
        output={"y": 2},
        duration_ms=9999,
        error="something",
    )
    assert _canonical_inputs_hash([a]) == _canonical_inputs_hash([b])


def test_inputs_hash_differs_when_outputs_differ() -> None:
    a = ToolCallRecord(name="t", input={"x": 1}, output={"y": 2})
    b = ToolCallRecord(name="t", input={"x": 1}, output={"y": 3})
    assert _canonical_inputs_hash([a]) != _canonical_inputs_hash([b])


def test_inputs_hash_is_sorted_canonical_json() -> None:
    a = ToolCallRecord(name="t", input={"a": 1, "b": 2}, output={"x": 1})
    b = ToolCallRecord(name="t", input={"b": 2, "a": 1}, output={"x": 1})
    assert _canonical_inputs_hash([a]) == _canonical_inputs_hash([b])


# ── Runtime: tool error handling ────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_exceptions_are_surfaced_to_model_not_raised() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="get_vehicle_state",
            description="x",
            input_schema={"type": "object"},
            handler=AsyncMock(side_effect=RuntimeError("telematics offline")),
        )
    )
    workflow = _make_workflow()
    client = _ScriptedClient(
        [
            _make_response(
                content=[
                    _tool_use_block(
                        name="get_vehicle_state",
                        input_data={"vehicle_id": "BUS-1"},
                        block_id="u1",
                    )
                ],
                stop_reason="tool_use",
            ),
            _make_response(
                content=[
                    _tool_use_block(
                        name=SUBMIT_DECISION_TOOL_NAME,
                        input_data={"summary": "tool failed; flagged manager"},
                        block_id="u2",
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    agent, _repo = _make_agent(client=client)
    decision = await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].error == "telematics offline"
    # The runtime sent an is_error tool_result back to the model on the
    # following turn so the agent could recover.
    second_call_messages = client.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["is_error"] is True


# ── Runtime: iteration budget exhaustion ───────────────────────────────


@pytest.mark.asyncio
async def test_iteration_budget_exhaustion_persists_partial_trace_and_raises() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow()

    # Every response calls the same tool again — the model never gives up.
    looping_response = _make_response(
        content=[
            _tool_use_block(
                name="get_vehicle_state",
                input_data={"vehicle_id": "BUS-X"},
                block_id="u",
            )
        ],
        stop_reason="tool_use",
    )
    client = _ScriptedClient([looping_response] * 3)
    agent, repo = _make_agent(
        client=client,
        config=WorkflowAgentConfig(max_tool_iterations=3),
    )

    with pytest.raises(DecisionLoopError):
        await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)

    # A partial decision row is still written so the audit trail isn't
    # silent (PRD §10.4).
    assert len(repo.rows) == 1
    assert repo.rows[0].rule_applied == "iteration_budget_exhausted"
    assert repo.rows[0].output == {}
    assert len(repo.rows[0].tool_calls) == 3


@pytest.mark.asyncio
async def test_model_stops_without_finaliser_raises() -> None:
    """If the model emits no tool_use at all, the loop ends and we raise.

    Stopping with prose only is a workflow-prompt bug; we don't want to
    silently invent an empty decision.
    """
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow()
    client = _ScriptedClient(
        [
            _make_response(
                content=[_text_block("I'm sorry, I can't help with that.")],
                stop_reason="end_turn",
            )
        ]
    )
    agent, repo = _make_agent(client=client)

    with pytest.raises(DecisionLoopError):
        await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)

    assert len(repo.rows) == 1
    assert repo.rows[0].rule_applied == "iteration_budget_exhausted"


# ── Repo behaviour ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_memory_repo_rejects_duplicate_writes() -> None:
    repo = InMemoryDecisionRepo()
    d = Decision(
        workflow_id="w",
        workflow_version="1",
        depot_id=DEPOT_ID,
        user_id=USER_ID,
        permission_tier=PermissionTier.INFORM,
        inputs_hash="a" * 64,
        model_id="m",
        latency_ms=1,
    )
    await repo.write(d)
    with pytest.raises(ValueError):
        await repo.write(d)


@pytest.mark.asyncio
async def test_asyncpg_repo_emits_expected_sql_payload() -> None:
    """The asyncpg repo serialises JSONB fields and binds positional params."""
    captured: dict[str, Any] = {}

    class _Conn:
        async def execute(self, sql: str, *params: Any) -> None:
            captured["sql"] = sql
            captured["params"] = params

    class _Pool:
        def acquire(self) -> Any:
            class _Ctx:
                async def __aenter__(self_inner) -> _Conn:  # noqa: N805
                    return _Conn()

                async def __aexit__(self_inner, *_a: Any) -> None:  # noqa: N805
                    return None

            return _Ctx()

    repo = AsyncpgDecisionRepo(pool=_Pool())
    d = Decision(
        workflow_id="readiness_check",
        workflow_version="1.0.0",
        depot_id=DEPOT_ID,
        user_id=USER_ID,
        organization_id=ORG_ID,
        permission_tier=PermissionTier.DRAFT_AND_WAIT,
        inputs_hash="b" * 64,
        tool_calls=[
            ToolCallRecord(name="t", input={"k": "v"}, output={"x": 1}, duration_ms=5),
        ],
        output={"summary": "ok", "proposed_actions": []},
        rule_applied="r1",
        constraint_violations=["v1"],
        model_id="claude-sonnet-4-6",
        latency_ms=123,
        input_tokens=10,
        output_tokens=20,
        stop_reason="tool_use",
    )
    await repo.write(d)

    assert "INSERT INTO decisions" in captured["sql"]
    params = captured["params"]
    # 20 positional params on the INSERT.
    assert len(params) == 20
    assert params[1] == "readiness_check"
    assert params[6] == "draft_and_wait"
    tool_calls_blob = json.loads(params[8])
    assert tool_calls_blob[0]["name"] == "t"
    output_blob = json.loads(params[9])
    assert output_blob["summary"] == "ok"
    violations_blob = json.loads(params[12])
    assert violations_blob == ["v1"]


# ── Anthropic response defensiveness ────────────────────────────────────


@pytest.mark.asyncio
async def test_response_without_usage_does_not_crash_metrics() -> None:
    registry, _ = _registry_with_get_vehicle_state()
    workflow = _make_workflow()

    no_usage = SimpleNamespace(
        content=[
            _tool_use_block(
                name=SUBMIT_DECISION_TOOL_NAME,
                input_data={"summary": "ok"},
                block_id="u",
            )
        ],
        stop_reason="tool_use",
        usage=None,
    )
    client = _ScriptedClient([no_usage])
    agent, repo = _make_agent(client=client)
    decision = await agent.run_turn(workflow, DEPOT_ID, _auth_context(), registry)
    assert decision.input_tokens == 0
    assert decision.output_tokens == 0
    assert repo.rows == [decision]
