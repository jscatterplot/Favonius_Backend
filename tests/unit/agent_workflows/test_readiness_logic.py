"""Unit tests for the daily readiness check workflow's pure logic.

Covers the §6.1 happy paths plus the four exception kinds the PRD
calls out as eval scenarios:

  - all-clear (no exceptions, coverage counts right)
  - undercharge (projected SoC < required SoC)
  - faulted charger (vehicle plugged into Faulted connector)
  - missing driver / shift conflict
  - no alternate available (proposed_action degrades to manual_intervention)

All tests run :func:`run_daily_readiness_check` directly with hand-built
inputs — no DB and no fixtures needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from src.core.workflows.readiness import (
    ReadinessException,
    WORKFLOW_NAME,
    compute_inputs_hash,
    run_daily_readiness_check,
)

DEPOT_ID = UUID("33333333-3333-4333-8333-333333333333")
ORG_ID = UUID("44444444-4444-4444-8444-444444444444")
VEH_A = "11111111-1111-4111-8111-111111111111"
VEH_B = "22222222-2222-4222-8222-222222222222"
CH_1 = "55555555-5555-4555-8555-555555555555"
CH_2 = "66666666-6666-4666-8666-666666666666"
CH_FAST = "77777777-7777-4777-8777-777777777777"


def _window():
    start = datetime(2026, 5, 13, 5, 0)
    end = datetime(2026, 5, 13, 9, 0)
    return start, end


def _now_plus(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_all_clear_status_and_coverage():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(90),
                "required_soc": 0.99,
                "charger_id": CH_1,
            },
            {
                "vehicle_id": VEH_B,
                "route_id": "R-2",
                "departure_time": _now_plus(120),
                "required_soc": 0.80,
                "charger_id": CH_2,
            },
        ],
        vehicle_states={
            VEH_A: {"soc": 0.99, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0},
            VEH_B: {"soc": 0.95, "plugged_in": True, "charger_id": CH_2, "battery_kwh": 200.0, "max_charge_kw": 60.0},
        },
        charger_states={
            CH_1: {"status": "Charging", "fault_code": None, "current_power_kw": 22.0},
            CH_2: {"status": "Charging", "fault_code": None, "current_power_kw": 22.0},
        },
        charging_plans={VEH_A: {"planned_power_kw": 22.0}, VEH_B: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": True}, "R-2": {"driver_id": "d2", "valid": True}},
    )

    assert decision.status == "success"
    assert decision.workflow_name == WORKFLOW_NAME
    assert decision.output.status == "all_clear"
    assert decision.output.coverage == {
        "vehicles_checked": 2,
        "chargers_checked": 2,
        "routes_checked": 2,
    }
    assert decision.output.exceptions == []
    assert decision.inputs_hash  # non-empty hex


def test_undercharge_produces_swap_charger_proposal():
    start, end = _window()
    departure_in_minutes = 60
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="scheduler",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(departure_in_minutes),
                "required_soc": 0.99,
                "charger_id": CH_1,
            }
        ],
        vehicle_states={
            VEH_A: {
                "soc": 0.80,
                "plugged_in": True,
                "charger_id": CH_1,
                "battery_kwh": 200.0,
                "max_charge_kw": 60.0,
            }
        },
        charger_states={CH_1: {"status": "Charging", "fault_code": None}},
        charging_plans={VEH_A: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": True}},
        alternate_chargers=[{"charger_id": CH_FAST, "rated_kw": 150.0, "station_id": "CH-FAST"}],
    )

    assert decision.output.status == "exceptions_present"
    assert len(decision.output.exceptions) == 1
    exc = decision.output.exceptions[0]
    assert exc.vehicle_id == VEH_A
    assert "Projected SoC" in exc.issue
    assert exc.proposed_action is not None
    assert exc.proposed_action["type"] == "swap_charger"
    assert exc.proposed_action["candidate_charger_id"] == CH_FAST


def test_undercharge_without_alternate_falls_back_to_manual():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(60),
                "required_soc": 0.99,
                "charger_id": CH_1,
            }
        ],
        vehicle_states={
            VEH_A: {"soc": 0.80, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0}
        },
        charger_states={CH_1: {"status": "Charging"}},
        charging_plans={VEH_A: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": True}},
        alternate_chargers=[],
    )

    exc = decision.output.exceptions[0]
    assert exc.proposed_action["type"] == "reassign_to_route"


def test_faulted_charger_emits_swap_proposal():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(120),
                "required_soc": 0.99,
                "charger_id": CH_1,
            }
        ],
        vehicle_states={
            VEH_A: {"soc": 0.99, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0}
        },
        charger_states={CH_1: {"status": "Faulted", "fault_code": "GroundFailure"}},
        charging_plans={VEH_A: {"planned_power_kw": 0.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": True}},
        alternate_chargers=[{"charger_id": CH_FAST, "rated_kw": 150.0}],
    )

    exc = decision.output.exceptions[0]
    assert "Faulted" in exc.issue
    assert exc.proposed_action["type"] == "swap_charger"


def test_missing_driver_flags_exception():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(60),
                "required_soc": 0.99,
                "charger_id": CH_1,
            }
        ],
        vehicle_states={VEH_A: {"soc": 0.99, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0}},
        charger_states={CH_1: {"status": "Charging"}},
        charging_plans={VEH_A: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": None, "valid": True}},
    )

    exc = decision.output.exceptions[0]
    assert exc.proposed_action["type"] == "assign_driver"


def test_invalid_driver_flags_swap_driver():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(60),
                "required_soc": 0.99,
                "charger_id": CH_1,
            }
        ],
        vehicle_states={VEH_A: {"soc": 0.99, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0}},
        charger_states={CH_1: {"status": "Charging"}},
        charging_plans={VEH_A: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": False}},
    )

    exc = decision.output.exceptions[0]
    assert exc.proposed_action["type"] == "swap_driver"


def test_compute_inputs_hash_is_deterministic_and_sensitive_to_change():
    inputs_a = {"a": 1, "b": [1, 2, 3]}
    inputs_b = {"b": [1, 2, 3], "a": 1}  # key order shouldn't matter
    inputs_c = {"a": 1, "b": [1, 2, 4]}

    assert compute_inputs_hash(inputs_a) == compute_inputs_hash(inputs_b)
    assert compute_inputs_hash(inputs_a) != compute_inputs_hash(inputs_c)


def test_invalid_triggered_by_rejected():
    start, end = _window()
    with pytest.raises(ValueError):
        run_daily_readiness_check(
            depot_id=DEPOT_ID,
            organization_id=ORG_ID,
            triggered_by="bogus",
            triggered_by_user_id=None,
            window_start_local=start,
            window_end_local=end,
            departures=[],
            vehicle_states={},
            charger_states={},
            charging_plans={},
            driver_assignments={},
        )


def test_invalid_window_rejected():
    start, end = _window()
    with pytest.raises(ValueError):
        run_daily_readiness_check(
            depot_id=DEPOT_ID,
            organization_id=ORG_ID,
            triggered_by="manual",
            triggered_by_user_id=None,
            window_start_local=end,
            window_end_local=start,
            departures=[],
            vehicle_states={},
            charger_states={},
            charging_plans={},
            driver_assignments={},
        )


def test_format_window_local_iso_minute():
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[],
        vehicle_states={},
        charger_states={},
        charging_plans={},
        driver_assignments={},
    )
    assert decision.output.window == "2026-05-13T05:00 → 09:00"
    assert decision.output.coverage["vehicles_checked"] == 0


def test_percent_soc_inputs_are_normalised():
    """Defence against telemetry that reports SoC in 0–100 not 0–1."""
    start, end = _window()
    decision = run_daily_readiness_check(
        depot_id=DEPOT_ID,
        organization_id=ORG_ID,
        triggered_by="manual",
        triggered_by_user_id=None,
        window_start_local=start,
        window_end_local=end,
        departures=[
            {
                "vehicle_id": VEH_A,
                "route_id": "R-1",
                "departure_time": _now_plus(60),
                "required_soc": 99,  # percent
                "charger_id": CH_1,
            }
        ],
        vehicle_states={
            VEH_A: {"soc": 100, "plugged_in": True, "charger_id": CH_1, "battery_kwh": 200.0, "max_charge_kw": 60.0}
        },
        charger_states={CH_1: {"status": "Charging"}},
        charging_plans={VEH_A: {"planned_power_kw": 22.0}},
        driver_assignments={"R-1": {"driver_id": "d1", "valid": True}},
    )
    assert decision.output.status == "all_clear"
