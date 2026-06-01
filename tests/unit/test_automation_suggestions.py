"""Unit tests for the pure automation-suggestion detector.

These exercise ``detect_suggestions`` and its helpers in isolation — no DB, no
metrics, no network. The module's heavy imports (metrics / auth / report
rendering) are lazy, so this file needs only the standard library plus the
third-party-free ``report_schedule_timing`` (to assert the produced
``schedule_input`` is actually creatable).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api.agent.automation_suggestions import (
    SuggestionConfig,
    build_summary,
    detect_suggestions,
)
from src.api.report_schedule_timing import normalize_create_payload

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


def _make_run(
    *,
    days_ago: int = 1,
    relative: str | None = "last_week",
    from_iso: str | None = None,
    to_iso: str | None = None,
    message: str = "energy by John",
    group_by: list[str] | None = None,
    subjects: list[dict] | None = None,
    status: str = "success",
    intent: str = "consumption_by_user",
    include_plan: bool = True,
    steps_as_string: bool = False,
) -> dict:
    """Build a synthetic agent_runs row with an extract_plan step."""
    if from_iso is not None or to_iso is not None:
        time_window = {"kind": "absolute", "from_iso": from_iso, "to_iso": to_iso}
    else:
        time_window = {"kind": "relative", "relative": relative}
    plan = {
        "intent": "consumption_by_user",
        "subjects": subjects if subjects is not None else [{"kind": "driver", "text": "John"}],
        "time_window": time_window,
        "group_by": group_by or [],
        "depot_wide": False,
    }
    steps: list = [{"name": "planner_decision", "payload": {"route": "consumption_by_user"}}]
    if include_plan:
        steps.append({"name": "extract_plan", "payload": plan})
    return {
        "run_id": "00000000-0000-0000-0000-000000000000",
        "user_message": message,
        "final_intent": intent,
        "status": status,
        "created_at": NOW - timedelta(days=days_ago),
        "steps_json": json.dumps(steps) if steps_as_string else steps,
    }


def _detect(runs, *, config=None, email="ops@example.com"):
    return detect_suggestions(
        runs, config=config or SuggestionConfig(), now=NOW, requester_email=email
    )


# ── Happy paths ───────────────────────────────────────────────────────────────


def test_monthly_by_card_from_driver_subject():
    runs = [
        _make_run(days_ago=2, relative="last_month", message="how much did John use last month"),
        _make_run(days_ago=12, relative="last_month", message="John consumption last month"),
        _make_run(days_ago=25, relative="this_month", message="John usage this month"),
    ]
    out = _detect(runs)
    assert len(out) == 1
    s = out[0]
    assert s.signature.group_by == "card"
    assert s.signature.cadence == "monthly"
    assert s.occurrence_count == 3
    si = s.schedule_input
    assert si["kind"] == "monthly_consumption"
    assert si["groupBy"] == "card"
    assert si["frequency"] == "monthly"
    assert si["dayOfMonth"] == 1
    assert si["dayOfWeek"] is None
    assert si["timeOfDay"] == "08:00"
    assert si["recipients"] == [{"emailAddress": "ops@example.com", "format": "pdf"}]
    assert "on day 1 of each month at 08:00" in s.summary
    assert "Should I set this up?" in s.summary


def test_weekly_by_card_from_driver_subject():
    runs = [_make_run(days_ago=d, relative="last_week") for d in (1, 8, 15)]
    out = _detect(runs)
    assert len(out) == 1
    si = out[0].schedule_input
    assert out[0].signature.cadence == "weekly"
    assert si["frequency"] == "weekly"
    assert si["dayOfWeek"] == 1  # Monday (spec 0=Sun)
    assert si["dayOfMonth"] is None
    assert "every Monday at 08:00" in out[0].summary


def test_weekly_by_vehicle_from_group_by():
    runs = [
        _make_run(days_ago=d, relative="this_week", group_by=["vehicle"], message=f"bus {d}")
        for d in (1, 9, 20)
    ]
    out = _detect(runs)
    assert len(out) == 1
    assert out[0].signature.group_by == "vehicle"
    assert out[0].schedule_input["groupBy"] == "vehicle"
    assert "by vehicle" in out[0].summary


def test_vehicle_subject_maps_to_vehicle():
    runs = [
        _make_run(
            days_ago=d, relative="last_month", subjects=[{"kind": "vehicle", "text": "bus 42"}]
        )
        for d in (1, 10, 20)
    ]
    out = _detect(runs)
    assert len(out) == 1
    assert out[0].signature.group_by == "vehicle"


# ── Thresholds + windows ──────────────────────────────────────────────────────


def test_below_threshold_yields_nothing():
    runs = [_make_run(days_ago=d, relative="last_week") for d in (1, 8)]
    assert _detect(runs) == []


def test_min_occurrences_is_configurable():
    runs = [_make_run(days_ago=d, relative="last_week") for d in (1, 8)]
    out = _detect(runs, config=SuggestionConfig(min_occurrences=2))
    assert len(out) == 1


def test_today_and_yesterday_are_skipped():
    runs = [
        _make_run(days_ago=1, relative="today"),
        _make_run(days_ago=2, relative="yesterday"),
        _make_run(days_ago=3, relative="today"),
    ]
    assert _detect(runs) == []


def test_lookback_excludes_old_runs():
    # Two recent + one older than the 30-day window → below threshold.
    runs = [
        _make_run(days_ago=1, relative="last_week"),
        _make_run(days_ago=5, relative="last_week"),
        _make_run(days_ago=40, relative="last_week"),
    ]
    assert _detect(runs) == []


@pytest.mark.parametrize(
    ("from_iso", "to_iso", "expected"),
    [
        ("2026-04-01", "2026-05-01", "monthly"),  # ~30 days
        ("2026-05-01", "2026-05-08", "weekly"),  # 7 days
        ("2026-05-01", "2026-05-15", None),  # 14 days → ambiguous → skip
    ],
)
def test_absolute_span_mapping(from_iso, to_iso, expected):
    runs = [
        _make_run(days_ago=d, relative=None, from_iso=from_iso, to_iso=to_iso) for d in (1, 2, 3)
    ]
    out = _detect(runs)
    if expected is None:
        assert out == []
    else:
        assert len(out) == 1
        assert out[0].signature.cadence == expected


# ── Exclusions ────────────────────────────────────────────────────────────────


def test_non_success_runs_excluded():
    runs = [_make_run(days_ago=d, relative="last_week", status="error") for d in (1, 8, 15)]
    assert _detect(runs) == []


def test_wrong_intent_excluded():
    runs = [_make_run(days_ago=d, relative="last_week", intent="sql_general") for d in (1, 8, 15)]
    assert _detect(runs) == []


def test_missing_extract_plan_step_excluded():
    runs = [_make_run(days_ago=d, relative="last_week", include_plan=False) for d in (1, 8, 15)]
    assert _detect(runs) == []


def test_mixed_group_dims_split_below_threshold():
    # 2 card-ish + 2 vehicle → two signatures, neither reaching 3.
    runs = [
        _make_run(days_ago=1, relative="last_week"),
        _make_run(days_ago=8, relative="last_week"),
        _make_run(days_ago=2, relative="last_week", group_by=["vehicle"]),
        _make_run(days_ago=9, relative="last_week", group_by=["vehicle"]),
    ]
    assert _detect(runs) == []


# ── Email + evidence + ordering ───────────────────────────────────────────────


def test_no_email_yields_empty_recipients_and_team_wording():
    runs = [_make_run(days_ago=d, relative="last_month") for d in (1, 10, 20)]
    out = _detect(runs, email=None)
    assert len(out) == 1
    assert out[0].schedule_input["recipients"] == []
    assert "your team" in out[0].summary


def test_string_encoded_steps_json_tolerated():
    runs = [_make_run(days_ago=d, relative="last_week", steps_as_string=True) for d in (1, 8, 15)]
    assert len(_detect(runs)) == 1


def test_evidence_fields_populated():
    runs = [
        _make_run(days_ago=20, relative="last_week", message="energy by card"),
        _make_run(days_ago=10, relative="last_week", message="ENERGY BY CARD"),  # dupe (case)
        _make_run(days_ago=2, relative="last_week", message="card usage last week"),
    ]
    out = _detect(runs)
    assert len(out) == 1
    s = out[0]
    assert s.occurrence_count == 3
    assert s.lookback_days == 30
    # Deduped (case-insensitive) + capped + most-recent-first.
    assert s.sample_messages[0] == "card usage last week"
    assert len(s.sample_messages) <= SuggestionConfig().max_sample_messages
    assert s.first_seen is not None and s.last_seen is not None
    assert s.first_seen < s.last_seen  # ISO strings sort chronologically


def test_results_sorted_by_count_then_key():
    # 3 weekly-card + 4 monthly-vehicle → monthly-vehicle first (higher count).
    runs = [_make_run(days_ago=d, relative="last_week") for d in (1, 8, 15)]
    runs += [
        _make_run(days_ago=d, relative="last_month", group_by=["vehicle"]) for d in (2, 9, 16, 22)
    ]
    out = _detect(runs)
    assert len(out) == 2
    assert out[0].occurrence_count == 4
    assert out[0].signature.cadence == "monthly"
    assert out[1].occurrence_count == 3


# ── Contract: every produced schedule_input is creatable ──────────────────────


@pytest.mark.parametrize("relative", ["last_week", "last_month"])
@pytest.mark.parametrize("group_by", [None, ["vehicle"]])
def test_schedule_input_passes_real_validator(relative, group_by):
    runs = [_make_run(days_ago=d, relative=relative, group_by=group_by) for d in (1, 10, 20)]
    out = _detect(runs)
    assert len(out) == 1
    # Must not raise — locks the contract with report_schedule_timing.
    norm = normalize_create_payload(out[0].schedule_input)
    assert norm.kind == "monthly_consumption"


def test_build_summary_depot_label_optional():
    runs = [_make_run(days_ago=d, relative="last_week") for d in (1, 8, 15)]
    s = _detect(runs)[0]
    labelled = build_summary(s.signature, s.schedule_input, depot_label="Europe/Vilnius")
    assert "(Europe/Vilnius)" in labelled
