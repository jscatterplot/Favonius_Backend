"""Unit tests for the deterministic ``readiness`` intent (pure logic).

Covers the verdict reducer (:func:`summarize_readiness`), the departure-time
plan alignment (:func:`projected_soc_at_departure`), the window resolver
(:func:`resolve_readiness_window`) and the renderer
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
    PlanContext,
    SocReading,
    projected_soc_at_departure,
    render_readiness_answer,
    resolve_readiness_window,
    summarize_readiness,
)

NOW = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(minutes=15)
FRESH = NOW - timedelta(minutes=5)
STALE = NOW - timedelta(minutes=45)

# Plan horizon covering the same-day departures the helpers use.
HS = NOW - timedelta(hours=1)
HE = NOW + timedelta(hours=23)

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


def _plan(schedule, *, hs=HS, he=HE):
    """PlanContext from a {vehicle_id: soc_list} mapping over [hs, he)."""
    sched = {
        vid: {"soc": socs, "charging_power": [10.0] * len(socs)} for vid, socs in schedule.items()
    }
    return PlanContext(schedule_json={"schedule": sched}, horizon_start=hs, horizon_end=he)


# ── projected_soc_at_departure ───────────────────────────────────────────────


def test_projected_soc_aligns_to_departure_not_max():
    # horizon [NOW, NOW+4h], 4 steps; departure at NOW+1h → idx 1 → 0.70,
    # NOT the trajectory peak (0.99) which is reached after departure.
    plan = PlanContext(
        {"schedule": {"v1": {"soc": [0.50, 0.70, 0.90, 0.99]}}},
        NOW,
        NOW + timedelta(hours=4),
    )
    assert projected_soc_at_departure(plan, "v1", NOW + timedelta(hours=1)) == 0.70


def test_projected_soc_accepts_json_string():
    plan = PlanContext(
        json.dumps({"schedule": {"v1": {"soc": [0.40, 0.95]}}}),
        NOW,
        NOW + timedelta(hours=2),
    )
    assert projected_soc_at_departure(plan, "v1", NOW + timedelta(hours=1)) == 0.95


def test_projected_soc_out_of_horizon_returns_none():
    plan = _plan({"v1": [0.9, 0.9]})
    assert projected_soc_at_departure(plan, "v1", HE + timedelta(hours=1)) is None
    assert projected_soc_at_departure(plan, "v1", HS - timedelta(hours=1)) is None


def test_projected_soc_no_horizon_returns_none():
    plan = PlanContext({"schedule": {"v1": {"soc": [0.9]}}}, None, None)
    assert projected_soc_at_departure(plan, "v1", NOW) is None


def test_projected_soc_handles_garbage():
    dep = NOW + timedelta(hours=1)
    assert projected_soc_at_departure(None, "v1", dep) is None
    assert projected_soc_at_departure(_plan({"v1": [0.9]}), "other", dep) is None
    assert projected_soc_at_departure(PlanContext("not json", HS, HE), "v1", dep) is None
    assert projected_soc_at_departure(PlanContext({"schedule": {}}, HS, HE), "v1", dep) is None
    assert (
        projected_soc_at_departure(
            PlanContext({"schedule": {"v1": {"soc": []}}}, HS, HE), "v1", dep
        )
        is None
    )


# ── summarize_readiness ──────────────────────────────────────────────────────


def _summarize(departures, socs, plans):
    return summarize_readiness(
        departures, socs, plans, now=NOW, max_age=MAX_AGE, window_label=WINDOW
    )


def test_plan_meets_required_is_ready():
    v = _summarize([_dep("v1")], {}, {DEPOT: _plan({"v1": [0.99, 0.99]})})
    assert v.total == 1 and v.ready == 1 and v.at_risk == 0 and v.unknown == 0
    assert v.vehicles[0].status == STATUS_READY
    assert v.overall == STATUS_READY


def test_plan_below_required_is_at_risk():
    v = _summarize([_dep("v1")], {}, {DEPOT: _plan({"v1": [0.94, 0.94]})})
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
    # A plan covering the departure → judge on the plan even when telemetry is stale.
    v = _summarize(
        [_dep("v1")], {"v1": SocReading(0.10, STALE)}, {DEPOT: _plan({"v1": [0.99, 0.99]})}
    )
    assert v.ready == 1 and v.vehicles[0].status == STATUS_READY


def test_stale_plan_out_of_horizon_falls_back_to_current_soc():
    # Plan horizon ends before the departure → plan ignored, current SoC used.
    old = PlanContext(
        {"schedule": {"v1": {"soc": [0.99, 0.99]}}},
        NOW - timedelta(days=2),
        NOW - timedelta(days=1),
    )
    v = _summarize([_dep("v1")], {"v1": SocReading(0.60, FRESH)}, {DEPOT: old})
    assert v.at_risk == 1
    assert "no plan yet" in v.vehicles[0].reason  # fell back to current SoC


def test_null_required_soc_defaults_to_prd_floor():
    v = _summarize([_dep("v1", required_soc=None)], {}, {DEPOT: _plan({"v1": [0.98, 0.98]})})
    assert v.at_risk == 1
    assert v.vehicles[0].required_soc == READINESS_DEFAULT_REQUIRED_SOC


def test_overall_rollup_at_risk_dominates():
    deps = [_dep("v1", route_id="R1"), _dep("v2", route_id="R2"), _dep("v3", route_id="R3")]
    socs = {"v1": SocReading(0.99, FRESH)}
    plans = {DEPOT: _plan({"v2": [0.90, 0.90]})}  # v2 at risk; v3 no plan/no soc
    v = _summarize(deps, socs, plans)
    assert v.total == 3 and v.ready == 1 and v.at_risk == 1 and v.unknown == 1
    assert v.overall == STATUS_AT_RISK
    assert v.vehicles[0].status == STATUS_AT_RISK  # worst-case first


def test_multiple_departures_keep_worst_per_vehicle():
    # One vehicle, two in-window departures: an early trip needing 90% (ready
    # at 95%) and a later trip needing 99% (at risk at 95%). The vehicle is
    # counted ONCE, judged on its WORST departure (the 99% one).
    deps = [
        _dep("v1", route_id="EARLY", required_soc=0.90, depart_h=8),
        _dep("v1", route_id="LATE", required_soc=0.99, depart_h=18),
    ]
    v = _summarize(deps, {}, {DEPOT: _plan({"v1": [0.95, 0.95]})})
    assert v.total == 1 and v.at_risk == 1 and v.ready == 0
    assert v.vehicles[0].route_id == "LATE"
    assert v.vehicles[0].required_soc == 0.99


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
    plans = {DEPOT: _plan({"v2": [0.80, 0.80]})}
    text = render_readiness_answer(_summarize(deps, socs, plans))
    assert "1 of 3 vehicles are ready" in text
    assert "at risk" in text and "route B" in text
    assert "need a check" in text or "needs a check" in text
    assert "route C" in text


# ── resolve_readiness_window ─────────────────────────────────────────────────


def test_window_default_next_24h():
    start, end, label = resolve_readiness_window("are we ready to depart?", NOW, "Europe/Vilnius")
    assert label == "in the next 24 hours"
    assert start == NOW and end == NOW + timedelta(hours=24)


def test_window_tomorrow_is_local_calendar_day():
    # Vilnius is UTC+3 in May; NOW 06:00Z = 09:00 local 2026-05-28, so tomorrow
    # (local) is 2026-05-29 → 21:00Z on the 28th through 21:00Z on the 29th.
    start, end, label = resolve_readiness_window(
        "are we ready for tomorrow?", NOW, "Europe/Vilnius"
    )
    assert label == "tomorrow"
    assert start == datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 29, 21, 0, tzinfo=timezone.utc)


def test_window_today_ends_at_local_midnight():
    start, end, label = resolve_readiness_window("is the fleet ready today?", NOW, "Europe/Vilnius")
    assert label == "for the rest of today"
    assert start == NOW
    assert end == datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc)


def test_window_invalid_tz_does_not_crash():
    start, end, _ = resolve_readiness_window("ready for tomorrow", NOW, "Not/AZone")
    assert start < end
