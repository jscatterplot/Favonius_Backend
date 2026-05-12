"""Integration test: scenario 02 (one undercharge) → §6.1 payload.

Drives the workflow through :func:`execute_readiness_workflow` with the
YAML-driven :class:`StaticToolBundle` from
``tests/golden/depot_agent/scenarios/02_undercharge.yaml`` and asserts
the response matches the ``expected`` block in the fixture.

This is the seam between unit tests (which build inputs in Python) and
the end-to-end test (which talks to the HTTP surface).
"""

from __future__ import annotations

import json

import pytest

from src.core.workflows.orchestrator import execute_readiness_workflow
from tests.golden.depot_agent.loader import load_scenario


@pytest.mark.asyncio
async def test_scenario_02_produces_one_undercharge_exception():
    scenario = load_scenario("02_undercharge")

    decision = await execute_readiness_workflow(
        depot_id=scenario.depot_id,
        organization_id=scenario.organization_id,
        triggered_by="manual",
        triggered_by_user_id=None,
        tools=scenario.tools,
        depot_timezone=scenario.timezone,
        window_start_utc=scenario.window_start_utc,
        window_end_utc=scenario.window_end_utc,
    )

    payload = decision.output.to_payload()
    expected = scenario.expected

    assert payload["status"] == expected["status"]
    assert payload["coverage"] == expected["coverage"]
    assert len(payload["exceptions"]) == expected["exception_count"]

    exc = payload["exceptions"][0]
    assert exc["vehicle_id"] == expected["exception_vehicle_id"]
    assert exc["proposed_action"]["type"] == expected["exception_action_type"]
    assert exc["proposed_action"]["candidate_charger_id"] == expected[
        "exception_candidate_charger_id"
    ]


@pytest.mark.asyncio
async def test_scenario_02_records_tool_call_trace():
    """The decision must capture one ToolCall per data-gathering step."""
    scenario = load_scenario("02_undercharge")
    decision = await execute_readiness_workflow(
        depot_id=scenario.depot_id,
        organization_id=scenario.organization_id,
        triggered_by="manual",
        triggered_by_user_id=None,
        tools=scenario.tools,
        depot_timezone=scenario.timezone,
        window_start_utc=scenario.window_start_utc,
        window_end_utc=scenario.window_end_utc,
    )

    names = [tc.name for tc in decision.tool_calls]
    assert "get_scheduled_departures" in names
    assert "get_vehicle_state" in names
    assert "get_charger_state" in names
    assert "get_charging_plan" in names
    assert "get_driver_assignment" in names
    assert "find_alternate_chargers" in names

    # Trace is JSON-serialisable (this is what gets persisted).
    json.dumps([tc.to_dict() for tc in decision.tool_calls], default=str)
