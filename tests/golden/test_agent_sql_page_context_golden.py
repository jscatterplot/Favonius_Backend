"""Agent-SQL golden gate for the on-demand page-context tool.

Companion to ``test_agent_sql_golden.py``. Scenarios live in the SEPARATE
``tests/golden/agent_sql_page_context.yaml`` so they don't perturb the main
suite's pinned 20-question shape. Each scenario drives the real
``run_qa_turn`` loop with a deterministic ``llm_trace`` that calls
``get_page_context`` (returning the scenario's parked ``page_context``) before
querying — pinning that the ambiguity → context → query path works end to end.

The DB-backed gate requires the TimescaleDB + Supabase test pair and skips
locally when it's unreachable. ``test_page_context_scenarios_validate`` runs
WITHOUT a DB so a malformed scenario is caught on every run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.golden.test_agent_sql_golden import (
    ScenarioError,
    _validate_against_schema,
    run_agent_sql_scenario,
)

_SCENARIOS_PATH = Path(__file__).parent / "agent_sql_page_context.yaml"


def _load_scenarios() -> list[dict[str, Any]]:
    if not _SCENARIOS_PATH.is_file():
        return []
    data = yaml.safe_load(_SCENARIOS_PATH.read_text()) or []
    return list(data)


_SCENARIOS = _load_scenarios()
_SCENARIO_IDS = [str(s.get("id", f"scenario_{i}")) for i, s in enumerate(_SCENARIOS)]


def test_page_context_scenarios_exist() -> None:
    assert _SCENARIOS, "expected at least one page-context golden scenario"


def test_page_context_scenarios_validate() -> None:
    """Schema-validate without a DB so malformed scenarios fail fast."""
    failures: list[str] = []
    for scenario in _SCENARIOS:
        try:
            _validate_against_schema(scenario)
        except ScenarioError as exc:
            failures.append(str(exc))
    assert not failures, "schema-invalid scenarios:\n  " + "\n  ".join(failures)


def test_page_context_scenarios_call_the_tool() -> None:
    """Each scenario's trace must actually call get_page_context (the point)."""
    for scenario in _SCENARIOS:
        names = [
            block.get("name")
            for turn in scenario.get("llm_trace") or []
            for block in (turn.get("content") or [])
            if block.get("type") == "tool_use"
        ]
        assert "get_page_context" in names, f"[{scenario.get('id')}] never calls get_page_context"


@pytest.mark.agent_sql_golden
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=_SCENARIO_IDS or None)
async def test_agent_sql_page_context_golden(
    scenario: dict[str, Any],
    agent_sql_ts_pool: Any,
    agent_sql_static_pool: Any,
    request: pytest.FixtureRequest,
) -> None:
    """Run one page-context scenario end-to-end and assert it passes."""
    result = await run_agent_sql_scenario(
        scenario, ts_pool=agent_sql_ts_pool, static_pool=agent_sql_static_pool
    )
    if not result.passed and request.config.getoption("--diag"):
        from tests.golden.test_agent_sql_golden import _diag_report

        print(_diag_report(scenario, result))
    assert result.passed, f"[{result.scenario_id}] " + "; ".join(result.failures)
