"""Unit tests for report-schedule timing math + payload validation.

Pure-stdlib module, so these run without any DB / app dependencies. The DST
cases are the teeth behind PRD acceptance criteria #3 and #8.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from src.api.report_schedule_timing import (
    ScheduleValidationError,
    compute_next_run_at,
    compute_report_period,
    format_hh_mm,
    map_provider_status_to_wire,
    normalize_create_payload,
    normalize_patch_payload,
    parse_hh_mm,
    previous_month_bounds,
)

VILNIUS = "Europe/Vilnius"


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ── DST: monthly 06:00 Europe/Vilnius (AC #3, #8) ─────────────────────────────


def test_monthly_vilnius_summer_is_0300z():
    # Europe/Vilnius is EEST (UTC+3) in summer → 06:00 local == 03:00Z.
    # NOTE: the task text says 04:00Z, but Vilnius is UTC+2/+3 (EET/EEST), so the
    # correct value is 03:00Z. We follow the IANA database, not the example.
    nxt = compute_next_run_at(
        _utc(2026, 5, 15, 12),
        frequency="monthly",
        time_of_day=time(6, 0),
        tz_name=VILNIUS,
        day_of_month=1,
    )
    assert nxt == _utc(2026, 6, 1, 3, 0)
    assert nxt.isoformat().replace("+00:00", "Z") == "2026-06-01T03:00:00Z"


def test_monthly_vilnius_winter_is_0400z():
    # EET (UTC+2) in winter → 06:00 local == 04:00Z.
    nxt = compute_next_run_at(
        _utc(2026, 11, 15, 12),
        frequency="monthly",
        time_of_day=time(6, 0),
        tz_name=VILNIUS,
        day_of_month=1,
    )
    assert nxt == _utc(2026, 12, 1, 4, 0)


def test_monthly_wall_clock_invariant_across_dst():
    # AC #8: the depot-local wall clock stays 06:00 either side of the DST flip.
    tz = ZoneInfo(VILNIUS)
    for now in (_utc(2026, 5, 15, 12), _utc(2026, 11, 15, 12)):
        nxt = compute_next_run_at(
            now,
            frequency="monthly",
            time_of_day=time(6, 0),
            tz_name=VILNIUS,
            day_of_month=1,
        )
        local = nxt.astimezone(tz)
        assert (local.hour, local.minute) == (6, 0)
        assert local.day == 1


# ── monthly cadence edges ─────────────────────────────────────────────────────


def test_monthly_today_before_time_fires_today():
    nxt = compute_next_run_at(
        _utc(2026, 6, 1, 1),  # 01:00Z == 04:00 local, before 06:00 local
        frequency="monthly",
        time_of_day=time(6, 0),
        tz_name=VILNIUS,
        day_of_month=1,
    )
    assert nxt == _utc(2026, 6, 1, 3, 0)


def test_monthly_today_after_time_rolls_to_next_month():
    nxt = compute_next_run_at(
        _utc(2026, 6, 1, 5),  # 05:00Z == 08:00 local, after 06:00 local
        frequency="monthly",
        time_of_day=time(6, 0),
        tz_name=VILNIUS,
        day_of_month=1,
    )
    assert nxt == _utc(2026, 7, 1, 3, 0)


def test_monthly_day_28_every_month_present():
    nxt = compute_next_run_at(
        _utc(2026, 2, 10, 12),
        frequency="monthly",
        time_of_day=time(9, 30),
        tz_name="UTC",
        day_of_month=28,
    )
    assert nxt == _utc(2026, 2, 28, 9, 30)


# ── weekly ─────────────────────────────────────────────────────────────────────


def test_weekly_next_sunday_utc():
    # dayOfWeek 0 = Sunday. now is Wed 2026-05-20.
    nxt = compute_next_run_at(
        _utc(2026, 5, 20, 12),
        frequency="weekly",
        time_of_day=time(8, 0),
        tz_name="UTC",
        day_of_week=0,
    )
    assert nxt == _utc(2026, 5, 24, 8, 0)  # Sunday


def test_weekly_same_day_time_passed_rolls_a_week():
    # now is Sunday 2026-05-24 09:00Z, target Sunday 08:00 already passed.
    nxt = compute_next_run_at(
        _utc(2026, 5, 24, 9),
        frequency="weekly",
        time_of_day=time(8, 0),
        tz_name="UTC",
        day_of_week=0,
    )
    assert nxt == _utc(2026, 5, 31, 8, 0)


def test_weekly_monday_mapping():
    # dayOfWeek 1 = Monday. now Wed 2026-05-20 → next Monday 2026-05-25.
    nxt = compute_next_run_at(
        _utc(2026, 5, 20, 12),
        frequency="weekly",
        time_of_day=time(7, 0),
        tz_name="UTC",
        day_of_week=1,
    )
    assert nxt == _utc(2026, 5, 25, 7, 0)


# ── quarterly ───────────────────────────────────────────────────────────────


def test_quarterly_picks_next_calendar_quarter_month():
    nxt = compute_next_run_at(
        _utc(2026, 5, 15, 12),
        frequency="quarterly",
        time_of_day=time(6, 0),
        tz_name="UTC",
        day_of_month=1,
    )
    assert nxt == _utc(2026, 7, 1, 6, 0)


def test_quarterly_wraps_to_next_year():
    nxt = compute_next_run_at(
        _utc(2026, 11, 15, 12),
        frequency="quarterly",
        time_of_day=time(6, 0),
        tz_name="UTC",
        day_of_month=1,
    )
    assert nxt == _utc(2027, 1, 1, 6, 0)


# ── period computation ────────────────────────────────────────────────────────


def test_previous_month_bounds():
    start, end, label = previous_month_bounds(datetime(2026, 6, 1, 6, 0))
    assert (start, end, label) == ("2026-05-01", "2026-05-31", "May 2026")


def test_compute_report_period_monthly():
    assert compute_report_period("monthly", datetime(2026, 6, 1, 6, 0)) == (
        "2026-05-01",
        "2026-05-31",
    )


def test_compute_report_period_weekly():
    # Fired on 2026-05-25 → previous 7 days = 2026-05-18..2026-05-24.
    assert compute_report_period("weekly", datetime(2026, 5, 25, 7, 0)) == (
        "2026-05-18",
        "2026-05-24",
    )


def test_compute_report_period_quarterly():
    # Fired in Q3 (July) → previous quarter = Q2 (Apr-Jun).
    assert compute_report_period("quarterly", datetime(2026, 7, 1, 6, 0)) == (
        "2026-04-01",
        "2026-06-30",
    )


# ── time parsing / formatting ─────────────────────────────────────────────────


def test_parse_and_format_hh_mm_roundtrip():
    assert format_hh_mm(parse_hh_mm("06:00")) == "06:00"
    assert format_hh_mm(parse_hh_mm("23:59")) == "23:59"


@pytest.mark.parametrize("bad", ["6:00", "24:00", "06:60", "06", "ab:cd", "", "06:00:00"])
def test_parse_hh_mm_rejects_bad(bad):
    with pytest.raises(ScheduleValidationError):
        parse_hh_mm(bad)


# ── provider status mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider,wire",
    [
        ("sent", "sent"),
        ("delivered", "sent"),
        ("bounced", "bounced"),
        ("complained", "suppressed"),
        ("failed", "failed"),
        ("totally_unknown", "failed"),
    ],
)
def test_provider_status_mapping(provider, wire):
    assert map_provider_status_to_wire(provider) == wire


# ── payload validation ────────────────────────────────────────────────────────


def _valid_monthly_input():
    return {
        "name": "Monthly electricity consumption",
        "kind": "monthly_consumption",
        "groupBy": "card",
        "frequency": "monthly",
        "dayOfMonth": 1,
        "timeOfDay": "06:00",
        "autonomyMode": "auto_silent",
        "isActive": True,
        "recipients": [{"emailAddress": "manager@depot.example", "format": "pdf"}],
    }


def test_normalize_create_happy_path():
    norm = normalize_create_payload(_valid_monthly_input())
    assert norm.kind == "monthly_consumption"
    assert norm.frequency == "monthly"
    assert norm.day_of_month == 1
    assert norm.day_of_week is None
    assert norm.time_of_day == time(6, 0)
    assert norm.autonomy_mode == "auto_silent"
    assert len(norm.recipients) == 1
    assert norm.recipients[0].position == 0
    assert norm.recipients[0].format == "pdf"


def test_normalize_create_requires_group_by_for_consumption():
    payload = _valid_monthly_input()
    del payload["groupBy"]
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)


def test_normalize_create_weekly_requires_day_of_week():
    payload = _valid_monthly_input()
    payload.update(kind="weekly_ops", groupBy=None, frequency="weekly", dayOfMonth=None)
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)
    payload["dayOfWeek"] = 1
    norm = normalize_create_payload(payload)
    assert norm.day_of_week == 1 and norm.day_of_month is None


def test_normalize_create_rejects_weekly_with_day_of_month():
    payload = _valid_monthly_input()
    payload.update(kind="weekly_ops", groupBy=None, frequency="weekly", dayOfWeek=2)
    # dayOfMonth still set from the monthly base → invalid combo.
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)


def test_normalize_create_rejects_bad_day_of_month():
    payload = _valid_monthly_input()
    payload["dayOfMonth"] = 31  # schema caps at 28
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)


def test_normalize_create_rejects_duplicate_recipient():
    payload = _valid_monthly_input()
    payload["recipients"] = [
        {"emailAddress": "a@b.com", "format": "pdf"},
        {"emailAddress": "a@b.com", "format": "pdf"},
    ]
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)


def test_normalize_create_allows_same_email_different_format():
    payload = _valid_monthly_input()
    payload["recipients"] = [
        {"emailAddress": "a@b.com", "format": "pdf"},
        {"emailAddress": "a@b.com", "format": "csv"},
    ]
    norm = normalize_create_payload(payload)
    assert len(norm.recipients) == 2


@pytest.mark.parametrize("bad_email", ["nope", "a@", "@b.com", "a@b", "a@b."])
def test_normalize_create_rejects_bad_email(bad_email):
    payload = _valid_monthly_input()
    payload["recipients"] = [{"emailAddress": bad_email, "format": "pdf"}]
    with pytest.raises(ScheduleValidationError):
        normalize_create_payload(payload)


def test_normalize_create_missing_recipients_is_empty_list():
    payload = _valid_monthly_input()
    del payload["recipients"]
    norm = normalize_create_payload(payload)
    assert norm.recipients == []


# ── patch overlay ──────────────────────────────────────────────────────────────


def _current_row():
    return {
        "name": "Monthly electricity consumption",
        "kind": "monthly_consumption",
        "group_by": "card",
        "frequency": "monthly",
        "day_of_month": 1,
        "day_of_week": None,
        "time_of_day": time(6, 0),
        "autonomy_mode": "auto_silent",
        "is_active": True,
    }


def test_patch_overlays_single_field():
    norm = normalize_patch_payload({"timeOfDay": "07:30"}, current=_current_row())
    assert norm.time_of_day == time(7, 30)
    assert norm.name == "Monthly electricity consumption"  # unchanged
    assert norm.autonomy_mode == "auto_silent"


def test_patch_switch_to_weekly_clears_day_of_month():
    norm = normalize_patch_payload({"frequency": "weekly", "dayOfWeek": 3}, current=_current_row())
    assert norm.frequency == "weekly"
    assert norm.day_of_week == 3
    assert norm.day_of_month is None


def test_patch_switch_to_weekly_without_day_of_week_fails():
    with pytest.raises(ScheduleValidationError):
        normalize_patch_payload({"frequency": "weekly"}, current=_current_row())


def test_patch_recipients_validated_when_present():
    norm = normalize_patch_payload(
        {"recipients": [{"emailAddress": "x@y.com", "format": "csv"}]},
        current=_current_row(),
    )
    assert len(norm.recipients) == 1
    assert norm.recipients[0].format == "csv"
