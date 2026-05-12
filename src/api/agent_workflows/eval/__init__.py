"""Workflow evaluation harness.

Per PRD principle 6, every workflow ships with at least 10 frozen
evaluation scenarios. The harness in :mod:`runner` is the substrate
those scenarios execute against.

The harness's responsibility is narrow:

* Load a YAML scenario into a transactional savepoint on the real test
  database — never a mocked schema, so schema drift surfaces in CI.
* Build a :class:`WorkflowContext` whose ``now`` is the scenario's
  declared ``scenario_now`` (so output is deterministic).
* Invoke :meth:`WorkflowAgent.run_turn` against the registered workflow.
* Assert the resulting :class:`Decision.output` matches the scenario's
  ``expected`` block.

A pytest plugin in :mod:`tests.golden.workflows._plugin` exposes the
``workflow_golden`` marker that parametrises across every YAML file in
``tests/golden/workflows/``.
"""

from src.api.agent_workflows.eval.runner import (
    EvalResult,
    ScenarioLoadError,
    diff_actual_vs_expected,
    load_scenario,
    run_scenario,
)

__all__ = [
    "EvalResult",
    "ScenarioLoadError",
    "diff_actual_vs_expected",
    "load_scenario",
    "run_scenario",
]
