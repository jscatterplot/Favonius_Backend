"""Workflow evaluation harness.

Per PRD principle 6, every workflow ships with at least 10 frozen
evaluation scenarios. Sprint 3 lands the harness in :mod:`runner` so
sprints 4–5 can drop their scenarios into ``tests/golden/workflows/``
and have CI block on them.

The harness drives Sprint 2's :class:`WorkflowAgent.run_turn` directly:

* Loads the scenario's ``graph_snapshot`` into a transactional savepoint
  on the **real** test database (PRD §10.4 plus a CI guard against
  schema drift — never a mocked schema).
* Builds a :class:`Workflow` (Pydantic) and an :class:`AuthContext`
  from the scenario YAML.
* Constructs a :class:`ToolRegistry` whose callables read from that
  savepoint, so allow-listed workflow tools see only the snapshot rows.
* Hands a :class:`FakeAnthropicClient` to the runtime that replays the
  scenario's ``llm_trace`` deterministically — no real LLM call.
* Asserts the resulting :class:`Decision.output` and
  :class:`Decision.tool_calls` match the scenario's ``expected`` block.
* Always rolls back the transaction.

Determinism: the runtime's own ``Decision.id`` (uuid4), ``timestamp``
(wall-clock), and ``inputs_hash`` (sha256 over tool_calls) are
non-deterministic by design; the harness never asserts against them.
Everything else is reproducible across runs.
"""

from src.api.agent_workflows.eval.runner import (
    EvalResult,
    FakeAnthropicClient,
    ScenarioLoadError,
    diff_actual_vs_expected,
    load_scenario,
    load_snapshot,
    run_scenario,
)

__all__ = [
    "EvalResult",
    "FakeAnthropicClient",
    "ScenarioLoadError",
    "diff_actual_vs_expected",
    "load_scenario",
    "load_snapshot",
    "run_scenario",
]
