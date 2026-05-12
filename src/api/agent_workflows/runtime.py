"""Workflow runtime contract.

A workflow is a callable that takes a :class:`WorkflowContext` (the live
operational-graph state plus auth scope) and returns a :class:`Decision`
(the audit-grade record described in PRD §5.3). The substrate ships in
sprint 2; this module is the public surface every sprint downstream
codes against.

Two reasons it lives in its own file:

1. The eval harness in :mod:`src.api.agent_workflows.eval.runner` must
   import the runtime without triggering any real workflow module's
   side-effects (each workflow self-registers on import — see the
   ``register_workflow`` call at the bottom of every workflow file).
2. Tests register fake workflows the same way real ones do, so the
   harness has no second code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import UUID, uuid4


# ── Errors ────────────────────────────────────────────────────────────────


class WorkflowNotRegisteredError(KeyError):
    """Raised when the harness asks for a workflow name nobody registered."""


# ── Data shapes ───────────────────────────────────────────────────────────


@dataclass
class WorkflowContext:
    """The substrate handed to a workflow when it runs.

    The same object is used in production (built from the live database
    plus the caller's JWT) and in the eval harness (built by the runner
    from a frozen ``graph_snapshot``). The shape is identical so that the
    workflow code path is bit-for-bit the same.

    Attributes:
        depot_id: The depot whose state is in scope.
        now: Logical timestamp the workflow should treat as "now". In
            production this is :func:`datetime.now`; in the harness it is
            the scenario's ``scenario_now``. Workflows MUST NOT call
            ``datetime.now`` directly — they must read ``ctx.now``.
        visible_depot_ids: Auth-scoped list of depot UUIDs the caller is
            allowed to read. Workflows should treat this as the same
            sandbox the chat agent uses (see ``AuthContext`` in
            ``src.api.agent.auth_context``).
        static_pool: Asyncpg pool against the static (Supabase) schema —
            depots/chargers/vehicles/drivers/schedules.
        ts_pool: Asyncpg pool against the TimescaleDB time-series
            schema — telemetry, prices, building_load. May be the same
            underlying pool as ``static_pool`` in single-DB deployments.
        parameters: Customer-tunable workflow parameters (see PRD §7.4).
    """

    depot_id: UUID
    now: datetime
    visible_depot_ids: list[UUID]
    static_pool: Any
    ts_pool: Any
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Audit-grade record of a single workflow invocation.

    Mirrors the dataclass sketched in PRD §5.3 but trimmed to the
    fields the runtime actually produces. The full audit row (with
    disposition, edits, etc.) lives in the ``decisions`` table — this
    object is what ``WorkflowAgent.run_turn`` hands back to the caller,
    and what the eval harness asserts against.

    Attributes:
        decision_id: Stable UUID generated at the start of the turn.
        workflow_name: Name the workflow was registered under.
        depot_id: Depot scope for the run.
        scenario_now: The logical "now" used for the run. The harness
            asserts deterministic output, so this is anchored to the
            scenario, not wall-clock time.
        output: The workflow's structured artifact. Shape is workflow-
            specific (see PRD §6.1 for the readiness-check shape).
        tool_calls: Audit trail of tool invocations made during the turn.
        rule_applied: Optional identifier of the rule (SoC tolerance,
            tariff band, etc.) that produced the conclusion.
    """

    decision_id: UUID
    workflow_name: str
    depot_id: UUID
    scenario_now: datetime
    output: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    rule_applied: Optional[str] = None


# ── Workflow protocol ─────────────────────────────────────────────────────


WorkflowCallable = Callable[[WorkflowContext], Awaitable[dict[str, Any]]]


@dataclass
class Workflow:
    """A registered workflow.

    Holds the callable plus minimum metadata. The full :class:`Workflow`
    object described in PRD §5.2 (permission tier, graduation rule,
    eval set id) is layered on top of this in sprint 4 — keeping the
    runtime narrow until then.
    """

    name: str
    description: str
    handler: WorkflowCallable


class _Registry:
    """Process-wide workflow registry.

    Wrapped in a class so the eval harness can construct a fresh
    registry for tests (``isolate=True``) without leaking fake workflows
    into the production module-level registry.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Workflow] = {}

    def register(self, workflow: Workflow) -> None:
        # Idempotent on re-registration of the same workflow object — but
        # raises on a name collision with a different handler. This keeps
        # the workflow surface unambiguous when modules are imported more
        # than once (Python re-imports are common in test runners).
        existing = self._by_name.get(workflow.name)
        if existing is not None and existing is not workflow:
            if existing.handler is workflow.handler:
                return
            raise ValueError(
                f"Workflow {workflow.name!r} already registered with a different handler"
            )
        self._by_name[workflow.name] = workflow

    def get(self, name: str) -> Workflow:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise WorkflowNotRegisteredError(name) from exc

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def unregister(self, name: str) -> None:
        # Used by the harness teardown so a per-test workflow doesn't
        # outlive its test.
        self._by_name.pop(name, None)

    def snapshot(self) -> dict[str, Workflow]:
        return dict(self._by_name)


_REGISTRY = _Registry()


def workflow_registry() -> _Registry:
    """Return the process-wide registry singleton."""
    return _REGISTRY


def register_workflow(
    name: str,
    handler: WorkflowCallable,
    *,
    description: str = "",
    registry: Optional[_Registry] = None,
) -> Workflow:
    """Register a workflow handler under ``name``.

    Production workflow modules call this at module-import time. Tests
    call it inside fixtures (with an explicit ``registry`` to scope the
    registration to the test).
    """
    workflow = Workflow(name=name, description=description, handler=handler)
    (registry or _REGISTRY).register(workflow)
    return workflow


# ── WorkflowAgent — the single, scoped agent ─────────────────────────────


class WorkflowAgent:
    """The depot agent (PRD §4.3) — a thin dispatcher over the registry.

    The agent itself is intentionally small: per PRD principle 8
    ("Single agent, scoped tools") all reasoning happens inside the
    workflow's own prompt + tool allow-list. The agent's job is to look
    up the right workflow, hand it the operational context, and return
    the resulting :class:`Decision`.

    Tests inject ``registry`` to swap in a fake workflow without
    touching the production registry. Production code uses the default.
    """

    def __init__(self, registry: Optional[_Registry] = None) -> None:
        self._registry = registry or _REGISTRY

    async def run_turn(self, workflow_name: str, ctx: WorkflowContext) -> Decision:
        """Execute ``workflow_name`` against ``ctx`` and return a Decision."""
        workflow = self._registry.get(workflow_name)
        output = await workflow.handler(ctx)
        return Decision(
            decision_id=uuid4(),
            workflow_name=workflow.name,
            depot_id=ctx.depot_id,
            scenario_now=ctx.now,
            output=output,
        )
