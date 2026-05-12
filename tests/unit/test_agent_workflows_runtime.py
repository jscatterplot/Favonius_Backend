"""Unit tests for :mod:`src.api.agent_workflows`.

The runtime is exercised end-to-end with a fake Anthropic client and
fake tools — there is no HTTP endpoint yet (per Sprint 2 scope). Each
test drives a canned sequence of LLM responses through
:meth:`WorkflowAgent.run_turn` and asserts on the resulting
:class:`Decision`, the metrics, or both.

Coverage focus:

1. **Allow-list enforcement.** A tool that isn't in
   ``workflow.allowed_tools`` must raise :class:`ToolNotAllowedError`
   *before* the registry dispatches it, even if it is registered.
2. **Decision shape.** The happy path writes one row with the correct
   workflow id, depot id, disposition, tool-call list, and a hash.
3. **Hard-constraint guard.** Tools whose inputs would violate
   constraints are rejected pre-dispatch; ``proposed_actions`` entries
   that would violate are filtered out of the final output.
4. **Hash stability.** Two runs with identical tool outputs produce
   identical ``inputs_hash`` values, even though the rest of the row
   (id, timestamp) differs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows import (
    EMIT_DECISION_TOOL_NAME,
    Decision,
    DepotConstraints,
    HardConstraintGuard,
    InMemoryDecisionRepo,
    PermissionTier,
    ToolCall,
    ToolNotAllowedError,
    ToolNotRegisteredError,
    ToolRegistry,
    Workflow,
    WorkflowAgent,
)
from src.api.agent_workflows.runtime import (
    _canonical_tool_calls_hash,
    _emit_decision_schema,
)

# ── Fake Anthropic content blocks / responses ──────────────────────────────


def _tool_use_block(name: str, *, id_: str, input_: dict[str, Any]) -> SimpleNamespace:
    """Build a fake content block that mimics ``ToolUseBlock``."""
    return SimpleNamespace(type="tool_use", name=name, id=id_, input=input_)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _response(
    blocks: list[SimpleNamespace],
    *,
    stop_reason: str = "tool_use",
    input_tokens: int = 12,
    output_tokens: int = 8,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _fake_client(responses: list[Any]) -> SimpleNamespace:
    """Mimic ``anthropic.AsyncAnthropic()``.

    Returns an object whose ``messages.create`` is an ``AsyncMock`` that
    yields the canned responses in order via ``side_effect``.
    """
    create = AsyncMock(side_effect=list(responses))
    messages = SimpleNamespace(create=create)
    return SimpleNamespace(messages=messages, _create_mock=create)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def depot_id() -> UUID:
    return UUID("00000000-0000-0000-0000-0000000000d1")


@pytest.fixture
def user_id() -> UUID:
    return UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def auth_context(user_id: UUID, depot_id: UUID) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        organization_id=UUID("00000000-0000-0000-0000-0000000000b1"),
        role="customer_admin",
        visible_depot_ids=[depot_id],
    )


@pytest.fixture
def workflow() -> Workflow:
    return Workflow(
        id="wf-readiness-v1",
        name="daily_readiness_check",
        version="1.0.0",
        description="Confirm vehicles will be ready for pull-out.",
        prompt="Check every scheduled departure and flag risks.",
        allowed_tools=["get_vehicle_state", "propose_plan_update"],
        parameters={"lead_minutes": 60},
        permission_tier=PermissionTier.DRAFT_AND_WAIT,
    )


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def get_vehicle_state(vehicle_id: str) -> dict[str, Any]:
        # Default-good vehicle state. Specific tests may unregister and
        # re-register with a different return value.
        return {"vehicle_id": vehicle_id, "soc": 0.92, "plugged_in": True}

    async def propose_plan_update(**kwargs: Any) -> dict[str, Any]:
        return {"accepted": True, "echoed": kwargs}

    registry.register(
        "get_vehicle_state",
        description="Return the current SoC and plug status of a vehicle.",
        input_schema={
            "type": "object",
            "properties": {"vehicle_id": {"type": "string"}},
            "required": ["vehicle_id"],
        },
        fn=get_vehicle_state,
    )
    registry.register(
        "propose_plan_update",
        description="Draft an adjustment to the charging plan.",
        input_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "vehicle_id": {"type": "string"},
                "departure_soc": {"type": "number"},
                "grid_kw": {"type": "number"},
            },
        },
        fn=propose_plan_update,
    )
    return registry


@pytest.fixture
def repo() -> InMemoryDecisionRepo:
    return InMemoryDecisionRepo()


# ── ToolRegistry basics ────────────────────────────────────────────────────


class TestToolRegistry:
    @pytest.mark.asyncio
    async def test_register_and_dispatch_roundtrip(self) -> None:
        registry = ToolRegistry()

        async def fn(x: int, y: int) -> dict[str, int]:
            return {"sum": x + y}

        registry.register(
            "add",
            description="Add two ints.",
            input_schema={"type": "object", "properties": {}},
            fn=fn,
        )
        assert registry.has("add")
        assert registry.get("add").fn is fn
        result = await registry.dispatch("add", {"x": 2, "y": 3})
        assert result == {"sum": 5}

    def test_register_duplicate_raises(self, tool_registry: ToolRegistry) -> None:
        with pytest.raises(ValueError, match="already registered"):
            tool_registry.register(
                "get_vehicle_state",
                description="duplicate",
                input_schema={"type": "object"},
                fn=AsyncMock(),
            )

    def test_get_missing_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolNotRegisteredError):
            registry.get("nope")

    def test_anthropic_schemas_orders_match_allow_list(self, tool_registry: ToolRegistry) -> None:
        schemas = tool_registry.anthropic_schemas(["propose_plan_update", "get_vehicle_state"])
        assert [s["name"] for s in schemas] == [
            "propose_plan_update",
            "get_vehicle_state",
        ]

    def test_anthropic_schemas_typo_fails_fast(self, tool_registry: ToolRegistry) -> None:
        with pytest.raises(ToolNotRegisteredError):
            tool_registry.anthropic_schemas(["get_vehicle_state", "typo_name"])

    def test_unregister_is_idempotent(self, tool_registry: ToolRegistry) -> None:
        tool_registry.unregister("does_not_exist")
        tool_registry.unregister("get_vehicle_state")
        assert not tool_registry.has("get_vehicle_state")


# ── HardConstraintGuard ────────────────────────────────────────────────────


class TestHardConstraintGuard:
    def test_passes_clean_action(self) -> None:
        guard = HardConstraintGuard(DepotConstraints(max_grid_kw=500))
        assert guard.validate_action({"vehicle_id": "BUS-1", "departure_soc": 0.99}) is None

    def test_rejects_soc_below_min_as_fraction(self) -> None:
        guard = HardConstraintGuard()
        violation = guard.validate_action({"departure_soc": 0.85})
        assert violation is not None
        assert violation.constraint == "departure_soc_min"

    def test_rejects_soc_below_min_as_percent(self) -> None:
        guard = HardConstraintGuard()
        violation = guard.validate_action({"target_soc": 85})
        assert violation is not None
        assert violation.constraint == "departure_soc_min"

    def test_accepts_soc_at_threshold(self) -> None:
        # Exactly at the 99% boundary must pass — the PRD says ≥ 99%.
        guard = HardConstraintGuard()
        assert guard.validate_action({"departure_soc": 0.99}) is None

    def test_rejects_grid_kw_over_max(self) -> None:
        guard = HardConstraintGuard(DepotConstraints(max_grid_kw=500.0))
        violation = guard.validate_action({"grid_kw": 600})
        assert violation is not None
        assert violation.constraint == "grid_power_max"

    def test_grid_kw_not_checked_without_cap(self) -> None:
        guard = HardConstraintGuard()  # max_grid_kw=None
        assert guard.validate_action({"grid_kw": 99999}) is None

    def test_filter_actions_splits_kept_and_violations(self) -> None:
        guard = HardConstraintGuard(DepotConstraints(max_grid_kw=500.0))
        kept, violations = guard.filter_actions(
            [
                {"vehicle_id": "BUS-1", "departure_soc": 1.0},
                {"vehicle_id": "BUS-2", "departure_soc": 0.8},
                {"vehicle_id": "BUS-3", "grid_kw": 600},
            ]
        )
        assert [k["vehicle_id"] for k in kept] == ["BUS-1"]
        assert {v.constraint for v in violations} == {
            "departure_soc_min",
            "grid_power_max",
        }

    def test_filter_drops_non_dict_entries(self) -> None:
        guard = HardConstraintGuard()
        kept, violations = guard.filter_actions(["not a dict", {"vehicle_id": "OK"}])
        assert kept == [{"vehicle_id": "OK"}]
        assert violations[0].constraint == "malformed_action"

    def test_bool_field_not_coerced_to_float(self) -> None:
        # ``True`` is a subclass of int; the guard must reject this kind
        # of malformed input rather than treat ``True`` as 1.0 SoC.
        guard = HardConstraintGuard()
        assert guard.validate_action({"departure_soc": True}) is None

    def test_string_numeric_is_coerced(self) -> None:
        guard = HardConstraintGuard()
        violation = guard.validate_action({"departure_soc": "0.50"})
        assert violation is not None
        assert violation.constraint == "departure_soc_min"


# ── _canonical_tool_calls_hash ─────────────────────────────────────────────


class TestCanonicalHash:
    def test_empty_hash_is_stable(self) -> None:
        assert _canonical_tool_calls_hash([]) == _canonical_tool_calls_hash([])

    def test_duration_does_not_change_hash(self) -> None:
        a = [
            ToolCall(
                name="t",
                input={"x": 1},
                output={"y": 2},
                duration_ms=10,
            )
        ]
        b = [
            ToolCall(
                name="t",
                input={"x": 1},
                output={"y": 2},
                duration_ms=999,
            )
        ]
        assert _canonical_tool_calls_hash(a) == _canonical_tool_calls_hash(b)

    def test_key_order_does_not_change_hash(self) -> None:
        a = [ToolCall(name="t", input={"x": 1, "y": 2}, output={"a": 1, "b": 2})]
        b = [ToolCall(name="t", input={"y": 2, "x": 1}, output={"b": 2, "a": 1})]
        assert _canonical_tool_calls_hash(a) == _canonical_tool_calls_hash(b)

    def test_different_inputs_produce_different_hashes(self) -> None:
        a = [ToolCall(name="t", input={"x": 1}, output={})]
        b = [ToolCall(name="t", input={"x": 2}, output={})]
        assert _canonical_tool_calls_hash(a) != _canonical_tool_calls_hash(b)


# ── WorkflowAgent.run_turn ─────────────────────────────────────────────────


class TestWorkflowAgentHappyPath:
    @pytest.mark.asyncio
    async def test_writes_decision_with_expected_shape(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Turn 1: model calls get_vehicle_state. Turn 2: model emits.
        responses = [
            _response(
                [
                    _tool_use_block(
                        "get_vehicle_state",
                        id_="tu_1",
                        input_={"vehicle_id": "BUS-1"},
                    )
                ]
            ),
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={
                            "summary": "All vehicles on plan for pull-out.",
                            "proposed_actions": [],
                            "rule_applied": "no_action_needed",
                        },
                    )
                ],
                stop_reason="end_turn",
            ),
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(
            anthropic_client=client,
            decision_repo=repo,
            constraints=DepotConstraints(max_grid_kw=500.0),
        )

        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )

        # Decision shape
        assert isinstance(decision, Decision)
        assert decision.workflow_id == workflow.id
        assert decision.depot_id == depot_id
        assert decision.disposition == "pending"
        assert decision.human_user_id == auth_context.user_id
        assert decision.rule_applied == "no_action_needed"

        # Tool calls captured
        assert len(decision.tool_calls) == 1
        tc = decision.tool_calls[0]
        assert tc.name == "get_vehicle_state"
        assert tc.input == {"vehicle_id": "BUS-1"}
        assert tc.output == {"vehicle_id": "BUS-1", "soc": 0.92, "plugged_in": True}
        assert tc.is_error is False

        # Hash present and non-empty (sha256 hex = 64 chars)
        assert len(decision.inputs_hash) == 64

        # Output captured from the terminator
        assert decision.output["summary"] == "All vehicles on plan for pull-out."
        assert decision.output["proposed_actions"] == []
        assert decision.output["filtered_violations"] == []

        # Persistence
        assert repo.decisions == [decision]
        assert repo.last is decision

    @pytest.mark.asyncio
    async def test_system_prompt_carries_cache_control(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # One-shot: model emits decision immediately, no intermediate tool use.
        responses = [
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={"summary": "ok", "proposed_actions": []},
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )

        # Inspect the call's keyword args. The system block must be a
        # single-element list with cache_control=ephemeral so subsequent
        # turns hit the prefix cache.
        call_kwargs = client._create_mock.call_args.kwargs
        system = call_kwargs["system"]
        assert isinstance(system, list) and len(system) == 1
        assert system[0]["type"] == "text"
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert workflow.name in system[0]["text"]
        # The depot id should NOT live in the cached system block — it
        # belongs in the per-turn user message so the cache stays hot.
        assert str(depot_id) not in system[0]["text"]

        # The tools array must include emit_decision plus exactly the
        # allow-listed user tools — nothing else.
        tool_names = {t["name"] for t in call_kwargs["tools"]}
        assert tool_names == {
            "get_vehicle_state",
            "propose_plan_update",
            EMIT_DECISION_TOOL_NAME,
        }

    @pytest.mark.asyncio
    async def test_user_input_appears_in_first_user_message(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        responses = [
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={"summary": "ok", "proposed_actions": []},
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
            user_input={"focus_window": "06:00-09:00"},
        )

        call_kwargs = client._create_mock.call_args.kwargs
        first_user = call_kwargs["messages"][0]
        assert first_user["role"] == "user"
        assert str(depot_id) in first_user["content"]
        assert "focus_window" in first_user["content"]


# ── Allow-list enforcement ─────────────────────────────────────────────────


class TestAllowListEnforcement:
    @pytest.mark.asyncio
    async def test_tool_not_in_allow_list_raises(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Register a "secret" tool that is NOT in workflow.allowed_tools.
        # If the LLM tries to call it, the runtime must abort before
        # dispatch and surface ToolNotAllowedError.
        async def secret(**_: Any) -> dict[str, Any]:
            raise AssertionError("secret tool must never be invoked")

        tool_registry.register(
            "secret_admin_tool",
            description="should not be reachable",
            input_schema={"type": "object"},
            fn=secret,
        )

        responses = [
            _response(
                [
                    _tool_use_block(
                        "secret_admin_tool",
                        id_="tu_secret",
                        input_={},
                    )
                ],
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)

        with pytest.raises(ToolNotAllowedError):
            await agent.run_turn(
                workflow=workflow,
                depot_id=depot_id,
                auth_context=auth_context,
                tool_registry=tool_registry,
            )

        # No Decision should have been written.
        assert repo.decisions == []

    @pytest.mark.asyncio
    async def test_only_allow_listed_tools_passed_to_model(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Register a tool that is NOT in workflow.allowed_tools.
        async def extra(**_: Any) -> dict[str, Any]:
            return {}

        tool_registry.register(
            "extra_tool",
            description="not allow-listed",
            input_schema={"type": "object"},
            fn=extra,
        )

        responses = [
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={"summary": "n/a", "proposed_actions": []},
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )

        # The tools array passed to Anthropic must include only the
        # allow-listed tools plus the runtime's terminator.
        call_kwargs = client._create_mock.call_args.kwargs
        tool_names = {t["name"] for t in call_kwargs["tools"]}
        assert "extra_tool" not in tool_names
        assert tool_names == {
            "get_vehicle_state",
            "propose_plan_update",
            EMIT_DECISION_TOOL_NAME,
        }


# ── Hard-constraint enforcement ────────────────────────────────────────────


class TestHardConstraintEnforcement:
    @pytest.mark.asyncio
    async def test_violating_tool_input_is_rejected_pre_dispatch(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Fresh registry where the "real" propose_plan_update would have
        # accepted any input. Wire a sentinel so we can assert it never
        # ran.
        registry = ToolRegistry()
        ran = []

        async def get_state(**_: Any) -> dict[str, Any]:
            # State observed by the model: SoC is dangerously low, max
            # grid is 500 kW. The LLM might be tempted to propose a
            # plan that violates either constraint — the guard must
            # reject that attempt before it can take effect.
            return {"vehicle_id": "BUS-1", "soc": 0.50, "max_grid_kw": 500.0}

        async def propose_plan_update(**kwargs: Any) -> dict[str, Any]:
            ran.append(kwargs)
            return {"accepted": True}

        registry.register(
            "get_vehicle_state",
            description="state",
            input_schema={"type": "object"},
            fn=get_state,
        )
        registry.register(
            "propose_plan_update",
            description="propose",
            input_schema={"type": "object"},
            fn=propose_plan_update,
        )

        responses = [
            # Turn 1 — the LLM tries to call propose_plan_update with a
            # SoC well below the hard 99% minimum.
            _response(
                [
                    _tool_use_block(
                        "propose_plan_update",
                        id_="tu_bad",
                        input_={
                            "vehicle_id": "BUS-1",
                            "departure_soc": 0.85,
                        },
                    )
                ]
            ),
            # Turn 2 — terminator (the LLM "gives up" and emits).
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={
                            "summary": "Could not propose without violating constraints.",
                            "proposed_actions": [],
                        },
                    )
                ],
                stop_reason="end_turn",
            ),
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(
            anthropic_client=client,
            decision_repo=repo,
            constraints=DepotConstraints(max_grid_kw=500.0),
        )

        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=registry,
        )

        # The real callable must never have run.
        assert ran == []

        # The audit row carries the rejected call.
        assert len(decision.tool_calls) == 1
        tc = decision.tool_calls[0]
        assert tc.name == "propose_plan_update"
        assert tc.is_error is True
        assert tc.output["error"] == "hard_constraint_violation"
        assert tc.output["constraint"] == "departure_soc_min"

        # The tool_result fed back to the LLM also had is_error=True so
        # the model could read the rejection and adjust. AsyncMock stores
        # the messages list by reference, so it has been mutated again
        # since the second call — locate the tool_result by shape rather
        # than position.
        call_kwargs = client._create_mock.call_args_list[1].kwargs
        tool_result_msgs = [
            m
            for m in call_kwargs["messages"]
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and m["content"]
            and isinstance(m["content"][0], dict)
            and m["content"][0].get("type") == "tool_result"
        ]
        assert tool_result_msgs, "second LLM call should include a tool_result"
        assert tool_result_msgs[0]["content"][0]["is_error"] is True

    @pytest.mark.asyncio
    async def test_violating_proposed_action_is_filtered_from_output(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # The LLM skips tools and proposes two actions: one violates
        # departure SoC, one violates grid_kw. Both must be filtered out.
        responses = [
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={
                            "summary": "Two proposals.",
                            "proposed_actions": [
                                {
                                    "type": "reassign_route",
                                    "vehicle_id": "BUS-1",
                                    "departure_soc": 0.85,
                                },
                                {
                                    "type": "schedule_charge",
                                    "vehicle_id": "BUS-2",
                                    "grid_kw": 650,
                                },
                                {
                                    "type": "ok_action",
                                    "vehicle_id": "BUS-3",
                                    "departure_soc": 1.0,
                                },
                            ],
                            "rule_applied": "rule-7",
                        },
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(
            anthropic_client=client,
            decision_repo=repo,
            constraints=DepotConstraints(max_grid_kw=500.0),
        )

        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )

        kept = decision.output["proposed_actions"]
        assert len(kept) == 1
        assert kept[0]["type"] == "ok_action"

        violations = decision.output["filtered_violations"]
        assert {v["constraint"] for v in violations} == {
            "departure_soc_min",
            "grid_power_max",
        }
        # disposition still pending — the agent itself never auto-executes.
        assert decision.disposition == "pending"


# ── Hash stability ────────────────────────────────────────────────────────


class TestHashStability:
    @pytest.mark.asyncio
    async def test_inputs_hash_stable_across_identical_runs(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Two registries returning identical outputs. Two clients with
        # the same canned responses. The Decision UUIDs and timestamps
        # will differ across runs; ``inputs_hash`` must not.
        def make_registry() -> ToolRegistry:
            r = ToolRegistry()

            async def state(**_: Any) -> dict[str, Any]:
                return {"vehicle_id": "BUS-1", "soc": 0.99, "plugged_in": True}

            async def propose(**kwargs: Any) -> dict[str, Any]:
                return {"accepted": True, "echoed": kwargs}

            r.register(
                "get_vehicle_state",
                description="",
                input_schema={"type": "object"},
                fn=state,
            )
            r.register(
                "propose_plan_update",
                description="",
                input_schema={"type": "object"},
                fn=propose,
            )
            return r

        def canned_responses() -> list[Any]:
            return [
                _response(
                    [
                        _tool_use_block(
                            "get_vehicle_state",
                            id_="tu_1",
                            input_={"vehicle_id": "BUS-1"},
                        )
                    ]
                ),
                _response(
                    [
                        _tool_use_block(
                            EMIT_DECISION_TOOL_NAME,
                            id_="tu_emit",
                            input_={"summary": "ok", "proposed_actions": []},
                        )
                    ],
                    stop_reason="end_turn",
                ),
            ]

        agent_a = WorkflowAgent(
            anthropic_client=_fake_client(canned_responses()), decision_repo=repo
        )
        agent_b = WorkflowAgent(
            anthropic_client=_fake_client(canned_responses()), decision_repo=repo
        )

        d_a = await agent_a.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=make_registry(),
        )
        d_b = await agent_b.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=make_registry(),
        )

        assert d_a.inputs_hash == d_b.inputs_hash
        # Sanity: the rest of the row differs.
        assert d_a.id != d_b.id


# ── Misc: tool failures, terminator-only, max iterations ───────────────────


class TestRuntimeDefensive:
    @pytest.mark.asyncio
    async def test_tool_callable_exception_is_captured_in_audit(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        repo: InMemoryDecisionRepo,
    ) -> None:
        registry = ToolRegistry()

        async def state(**_: Any) -> dict[str, Any]:
            raise RuntimeError("backend unavailable")

        async def propose(**_: Any) -> dict[str, Any]:
            return {"ok": True}

        registry.register(
            "get_vehicle_state",
            description="",
            input_schema={"type": "object"},
            fn=state,
        )
        registry.register(
            "propose_plan_update",
            description="",
            input_schema={"type": "object"},
            fn=propose,
        )

        responses = [
            _response(
                [
                    _tool_use_block(
                        "get_vehicle_state",
                        id_="tu_1",
                        input_={"vehicle_id": "BUS-1"},
                    )
                ]
            ),
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={"summary": "could not fetch state", "proposed_actions": []},
                    )
                ],
                stop_reason="end_turn",
            ),
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=registry,
        )
        assert decision.tool_calls[0].is_error is True
        assert decision.tool_calls[0].output["error"] == "tool_failure"
        assert "backend unavailable" in decision.tool_calls[0].output["detail"]

    @pytest.mark.asyncio
    async def test_unregistered_allow_listed_tool_fails_fast(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # workflow.allowed_tools mentions a tool the registry doesn't know.
        broken = Workflow(
            id="wf-broken",
            name="broken",
            version="1.0.0",
            description="x",
            prompt="x",
            allowed_tools=["nonexistent"],
            permission_tier=PermissionTier.INFORM,
        )
        responses = [
            _response(
                [
                    _tool_use_block(
                        EMIT_DECISION_TOOL_NAME,
                        id_="tu_emit",
                        input_={"summary": "won't get here", "proposed_actions": []},
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        with pytest.raises(ToolNotRegisteredError):
            await agent.run_turn(
                workflow=broken,
                depot_id=depot_id,
                auth_context=auth_context,
                tool_registry=ToolRegistry(),
            )

    @pytest.mark.asyncio
    async def test_model_emits_only_text_writes_decision_with_no_terminator_status(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Model returns only text and no tool_use — runtime should still
        # write a Decision with the text captured as summary.
        responses = [
            _response(
                [_text_block("I have nothing to do.")],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )
        assert decision.output["summary"] == "I have nothing to do."
        assert decision.output["proposed_actions"] == []
        assert decision.tool_calls == []

    @pytest.mark.asyncio
    async def test_end_turn_after_tool_use_without_terminator(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # Model calls a tool and the response carries stop_reason=end_turn
        # without ever calling emit_decision. The runtime should still
        # write a Decision row with the audit defaults filled in.
        responses = [
            _response(
                [
                    _tool_use_block(
                        "get_vehicle_state",
                        id_="tu_1",
                        input_={"vehicle_id": "BUS-1"},
                    )
                ],
                stop_reason="end_turn",
            )
        ]
        client = _fake_client(responses)
        agent = WorkflowAgent(anthropic_client=client, decision_repo=repo)
        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )
        assert decision.output == {
            "summary": "",
            "proposed_actions": [],
            "filtered_violations": [],
        }
        assert len(decision.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_max_iterations_loop_bounded(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        repo: InMemoryDecisionRepo,
    ) -> None:
        # An infinite-loop-style LLM that keeps calling get_vehicle_state
        # forever. With max_iterations=2 the runtime should bail out and
        # still produce a Decision row (no exception).
        def tool_only_response() -> SimpleNamespace:
            return _response(
                [
                    _tool_use_block(
                        "get_vehicle_state",
                        id_=f"tu_{uuid4()}",
                        input_={"vehicle_id": "BUS-1"},
                    )
                ]
            )

        responses = [tool_only_response() for _ in range(5)]
        client = _fake_client(responses)
        agent = WorkflowAgent(
            anthropic_client=client,
            decision_repo=repo,
            max_iterations=2,
        )
        decision = await agent.run_turn(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_registry=tool_registry,
        )
        # Two iterations means two LLM calls.
        assert client._create_mock.call_count == 2
        # Two tool calls captured.
        assert len(decision.tool_calls) == 2
        # Still pending, never auto_executed.
        assert decision.disposition == "pending"


# ── Terminator schema ─────────────────────────────────────────────────────


class TestEmitDecisionSchema:
    def test_terminator_schema_constants(self) -> None:
        schema = _emit_decision_schema()
        assert schema["name"] == EMIT_DECISION_TOOL_NAME
        assert "summary" in schema["input_schema"]["properties"]
        assert "proposed_actions" in schema["input_schema"]["properties"]
        assert schema["input_schema"]["required"] == ["summary"]
