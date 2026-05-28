"""Unit tests for the deterministic ``readiness`` intent (pure logic).

Covers the verdict reducer (:func:`summarize_readiness`), the plan parser
(:func:`projected_soc_from_plan`) and the renderer
(:func:`render_readiness_answer`) — no database, no LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.api.agent.intents.readiness import (
    READINESS_DEFAULT_REQUIRED_SOC,
    STATUS_AT_RISK,
    STATUS_READY,
    STATUS_UNKNOWN,
    SocReading,
    projected_soc_from_plan,
    render_readiness_answer,
    summarize_readiness,
)

NOW = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(minutes=15)
FRESH = NOW - timedelta(minutes=5)
STALE = NOW - timedelta(minutes=45)

DEPOT = "11111111-1111-4111-8111-111111111111"
WINDOW = "in the next 24 hours"


def _dep(vehicle_id, *, route_id="R1", required_soc=0.99, depot_id=DEPOT, depart_h=8):
    return {
        "vehicle_id": vehicle_id,
        "depot_id": depot_id,
        "route_id": route_id,
        "departure_time": NOW.replace(hour=depart_h),
        "required_soc": required_soc,
    }


def _plan(vehicle_id, soc_list):
    return {"schedule": {vehicle_id: {"soc": soc_list, "charging_power": [10.0] * len(soc_list)}}}


# ── projected_soc_from_plan ──────────────────────────────────────────────────


def test_projected_soc_returns_max_of_trajectory():
    plan = _plan("v1", [0.50, 0.80, 0.99, 0.97])
    assert projected_soc_from_plan(plan, "v1") == 0.99


def test_projected_soc_accepts_json_string():
    plan = json.dumps(_plan("v1", [0.40, 0.95]))
    assert projected_soc_from_plan(plan, "v1") == 0.95


def test_projected_soc_missing_vehicle_returns_none():
    assert projected_soc_from_plan(_plan("v1", [0.9]), "other") is None


def test_projected_soc_handles_garbage():
    assert projected_soc_from_plan(None, "v1") is None
    assert projected_soc_from_plan("not json", "v1") is None
    assert projected_soc_from_plan({"schedule": {}}, "v1") is None
    assert projected_soc_from_plan({"schedule": {"v1": {"soc": []}}}, "v1") is None


# ── summarize_readiness ──────────────────────────────────────────────────────


def _summarize(departures, socs, plans):
    return summarize_readiness(
        departures, socs, plans, now=NOW, max_age=MAX_AGE, window_label=WINDOW
    )


def test_plan_meets_required_is_ready():
    v = _summarize([_dep("v1")], {}, {DEPOT: _plan("v1", [0.5, 0.99])})
    assert v.total == 1 and v.ready == 1 and v.at_risk == 0 and v.unknown == 0
    assert v.vehicles[0].status == STATUS_READY
    assert v.overall == STATUS_READY


def test_plan_below_required_is_at_risk():
    v = _summarize([_dep("v1")], {}, {DEPOT: _plan("v1", [0.5, 0.94])})
    assert v.at_risk == 1
    assert v.vehicles[0].status == STATUS_AT_RISK
    assert "94%" in v.vehicles[0].reason and "99%" in v.vehicles[0].reason


def test_no_plan_fresh_high_soc_is_ready_with_no_plan_reason():
    v = _summarize([_dep("v1")], {"v1": SocReading(0.99, FRESH)}, {})
    assert v.ready == 1
    assert v.vehicles[0].status == STATUS_READY
    assert "no plan yet" in v.vehicles[0].reason


def test_no_plan_fresh_low_soc_is_at_risk():
    v = _summarize([_dep("v1")], {"v1": SocReading(0.60, FRESH)}, {})
    assert v.at_risk == 1
    assert v.vehicles[0].status == STATUS_AT_RISK
    assert "no plan yet" in v.vehicles[0].reason and "60%" in v.vehicles[0].reason


def test_no_plan_stale_soc_is_unknown():
    v = _summarize([_dep("v1")], {"v1": SocReading(0.99, STALE)}, {})
    assert v.unknown == 1
    assert v.vehicles[0].status == STATUS_UNKNOWN
    assert "stale" in v.vehicles[0].reason


def test_no_plan_no_soc_is_unknown():
    v = _summarize([_dep("v1")], {}, {})
    assert v.unknown == 1
    assert v.vehicles[0].status == STATUS_UNKNOWN
    assert "no recent SoC" in v.vehicles[0].reason


def test_plan_present_ignores_stale_telemetry():
    # A plan exists → judge on the plan even when telemetry is stale.
    v = _summarize([_dep("v1")], {"v1": SocReading(0.10, STALE)}, {DEPOT: _plan("v1", [0.2, 0.99])})
    assert v.ready == 1 and v.vehicles[0].status == STATUS_READY


def test_null_required_soc_defaults_to_prd_floor():
    # required_soc NULL → 0.99 floor; a plan reaching 0.98 is at risk.
    v = _summarize([_dep("v1", required_soc=None)], {}, {DEPOT: _plan("v1", [0.5, 0.98])})
    assert v.at_risk == 1
    assert v.vehicles[0].required_soc == READINESS_DEFAULT_REQUIRED_SOC


def test_overall_rollup_at_risk_dominates():
    deps = [_dep("v1", route_id="R1"), _dep("v2", route_id="R2"), _dep("v3", route_id="R3")]
    socs = {"v1": SocReading(0.99, FRESH)}
    plans = {DEPOT: {"schedule": {"v2": {"soc": [0.5, 0.90]}}}}  # v2 at risk; v3 no plan/no soc
    v = _summarize(deps, socs, plans)
    assert v.total == 3 and v.ready == 1 and v.at_risk == 1 and v.unknown == 1
    assert v.overall == STATUS_AT_RISK
    # Worst-case first in the ordered list.
    assert v.vehicles[0].status == STATUS_AT_RISK


def test_empty_departures():
    v = _summarize([], {}, {})
    assert v.total == 0 and v.overall == STATUS_READY


# ── render_readiness_answer ──────────────────────────────────────────────────


def test_render_no_departures():
    v = _summarize([], {}, {})
    assert render_readiness_answer(v) == f"No departures are scheduled {WINDOW}."


def test_render_all_ready():
    v = _summarize([_dep("v1")], {"v1": SocReading(0.99, FRESH)}, {})
    text = render_readiness_answer(v)
    assert "1 of 1 vehicles are ready to depart" in text
    assert "at risk" not in text


def test_render_lists_at_risk_and_unknown_by_route():
    deps = [_dep("v1", route_id="A"), _dep("v2", route_id="B"), _dep("v3", route_id="C")]
    socs = {"v1": SocReading(0.99, FRESH)}
    plans = {DEPOT: {"schedule": {"v2": {"soc": [0.5, 0.80]}}}}
    text = render_readiness_answer(_summarize(deps, socs, plans))
    assert "1 of 3 vehicles are ready" in text
    assert "at risk" in text and "route B" in text
    assert "need a check" in text or "needs a check" in text
    assert "route C" in text
