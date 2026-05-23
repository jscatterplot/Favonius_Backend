"""Unit tests for ``src.core.scheduling.recurring``.

Covers the pure expansion + merge logic without touching the DB. Every
edge case the spec calls out is asserted here:

  * weekday filter
  * start_date / end_date bounds (inclusive on both ends)
  * cancellation rows skip the matching occurrence
  * ``active=False`` templates contribute nothing
  * crosses-midnight returns the following depot-local day
  * DST spring-forward — nonexistent local times shift forward
  * DST fall-back — ambiguous local times pick the first occurrence
  * manual-vs-recurring tiebreaker per (vehicle, depot_local_date)

The frontend ships in PR #119; this suite is the contract that backend +
frontend must agree on.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from src.core.scheduling.recurring import (
    DAYS_OF_WEEK,
    RecurringTemplate,
    ScheduleCancellation,
    expand_recurring_templates,
    merge_recurring_with_manual,
)

VILNIUS = ZoneInfo("Europe/Vilnius")
UTC = ZoneInfo("UTC")


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_template(
    *,
    vehicle_id: UUID | None = None,
    template_id: UUID | None = None,
    departure: time = time(7, 30),
    return_: time = time(19, 0),
    days: tuple[str, ...] = DAYS_OF_WEEK,
    start: date = date(2026, 1, 1),
    end: date | None = None,
    active: bool = True,
    route_id: str = "R1",
    energy_kwh: float | None = 180.0,
    created_at: datetime | None = None,
) -> RecurringTemplate:
    return RecurringTemplate(
        template_id=template_id or uuid4(),
        depot_id=uuid4(),
        vehicle_id=vehicle_id or uuid4(),
        route_id=route_id,
        departure_time_of_day=departure,
        return_time_of_day=return_,
        days_of_week=days,
        start_date=start,
        end_date=end,
        required_soc=1.0,
        energy_kwh=energy_kwh,
        active=active,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── Weekday filter ──────────────────────────────────────────────────────────


def test_template_only_emits_on_listed_weekdays():
    # Vilnius Mon = 2026-05-18, ..., Sun = 2026-05-24.
    tpl = _make_template(days=("mon", "wed", "fri"))
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 25),
    )
    local_dates = sorted(
        row["departure_time"].astimezone(VILNIUS).date() for row in out
    )
    assert local_dates == [date(2026, 5, 18), date(2026, 5, 20), date(2026, 5, 22)]


# ── start_date / end_date bounds ────────────────────────────────────────────


def test_start_date_is_inclusive():
    tpl = _make_template(start=date(2026, 5, 19), days=("mon", "tue"))
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 21),
    )
    local_dates = {row["departure_time"].astimezone(VILNIUS).date() for row in out}
    # Mon 5/18 is before start → skipped; Tue 5/19 is the inclusive start.
    assert local_dates == {date(2026, 5, 19)}


def test_end_date_is_inclusive_and_open_when_null():
    tpl_closed = _make_template(
        end=date(2026, 5, 20), days=("mon", "tue", "wed", "thu", "fri")
    )
    out_closed = expand_recurring_templates(
        [tpl_closed], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 25),
    )
    closed_dates = {r["departure_time"].astimezone(VILNIUS).date() for r in out_closed}
    assert closed_dates == {date(2026, 5, 18), date(2026, 5, 19), date(2026, 5, 20)}

    tpl_open = _make_template(end=None, days=("mon", "tue", "wed", "thu", "fri"))
    out_open = expand_recurring_templates(
        [tpl_open], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 25),
    )
    open_dates = {r["departure_time"].astimezone(VILNIUS).date() for r in out_open}
    assert open_dates == {
        date(2026, 5, 18),
        date(2026, 5, 19),
        date(2026, 5, 20),
        date(2026, 5, 21),
        date(2026, 5, 22),
    }


# ── Cancellations ───────────────────────────────────────────────────────────


def test_cancellation_skips_one_occurrence():
    tpl = _make_template(days=("mon", "tue", "wed"))
    cancellation = ScheduleCancellation(
        template_id=tpl.template_id, occurrence_date=date(2026, 5, 19)
    )
    out = expand_recurring_templates(
        [tpl], [cancellation],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 21),
    )
    dates = {r["departure_time"].astimezone(VILNIUS).date() for r in out}
    assert dates == {date(2026, 5, 18), date(2026, 5, 20)}


def test_cancellation_for_different_template_does_not_affect():
    tpl = _make_template(days=("mon",))
    cancellation = ScheduleCancellation(
        template_id=uuid4(),  # different template
        occurrence_date=date(2026, 5, 18),
    )
    out = expand_recurring_templates(
        [tpl], [cancellation],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 19),
    )
    assert len(out) == 1


# ── active=False / paused ───────────────────────────────────────────────────


def test_paused_template_emits_nothing():
    tpl = _make_template(days=("mon", "tue"), active=False)
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 21),
    )
    assert out == []


# ── crosses_midnight ────────────────────────────────────────────────────────


def test_crosses_midnight_returns_next_local_day():
    # Late-night run: 22:00 → 03:00 local. Return must land on the day AFTER
    # departure in the depot timezone.
    tpl = _make_template(
        departure=time(22, 0), return_=time(3, 0), days=("mon",),
        start=date(2026, 5, 18),
    )
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 20),
    )
    assert len(out) == 1
    dep_local = out[0]["departure_time"].astimezone(VILNIUS)
    ret_local = out[0]["return_time"].astimezone(VILNIUS)
    assert dep_local.date() == date(2026, 5, 18)
    assert dep_local.hour == 22
    assert ret_local.date() == date(2026, 5, 19)
    assert ret_local.hour == 3


def test_crosses_midnight_property_is_derivable():
    tpl = _make_template(departure=time(22, 0), return_=time(3, 0))
    assert tpl.crosses_midnight is True
    tpl2 = _make_template(departure=time(7, 0), return_=time(19, 0))
    assert tpl2.crosses_midnight is False
    # Equal times: PRD CHECK constraint forbids this, but the property
    # still answers deterministically.
    tpl3 = _make_template(departure=time(12, 0), return_=time(12, 0))
    assert tpl3.crosses_midnight is True  # <= comparison


# ── DST: spring-forward ─────────────────────────────────────────────────────


def test_spring_forward_nonexistent_local_time_shifts_forward(caplog):
    """Vilnius spring-forward 2026: clock jumps 02:59 → 04:00 on 2026-03-29.

    A template at 03:30 on that Sunday must shift forward to 04:00 with a
    warning logged.
    """
    tpl = _make_template(
        departure=time(3, 30),
        return_=time(10, 0),
        days=("sun",),
        start=date(2026, 3, 28),
    )
    import logging
    with caplog.at_level(logging.WARNING, logger="src.core.scheduling.recurring"):
        out = expand_recurring_templates(
            [tpl], [],
            depot_tz=VILNIUS,
            horizon_start=_utc(2026, 3, 29),
            horizon_end=_utc(2026, 3, 30),
        )
    assert len(out) == 1
    dep_local = out[0]["departure_time"].astimezone(VILNIUS)
    assert dep_local.hour == 4 and dep_local.minute == 0
    assert any(
        "did not exist" in record.getMessage() for record in caplog.records
    ), "spring-forward shift must be logged"


# ── DST: fall-back ──────────────────────────────────────────────────────────


def test_fall_back_ambiguous_local_time_picks_first_occurrence():
    """Vilnius fall-back 2026: 04:00 → 03:00 local on 2026-10-25.

    Local 03:30 happens twice. fold=0 selects the first (pre-shift) — UTC
    is 00:30 on the EEST side (+03:00).
    """
    tpl = _make_template(
        departure=time(3, 30),
        return_=time(10, 0),
        days=("sun",),
        start=date(2026, 10, 24),
    )
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 10, 25),
        horizon_end=_utc(2026, 10, 26),
    )
    assert len(out) == 1
    departure_utc = out[0]["departure_time"]
    assert departure_utc == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


# ── Tiebreaker: manual vs recurring on the same vehicle/day ─────────────────


def test_manual_wins_when_newer_than_template():
    vehicle_id = uuid4()
    tpl = _make_template(
        vehicle_id=vehicle_id,
        days=("fri",),
        start=date(2026, 5, 22),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),  # older
    )
    manual = {
        "vehicle_id": str(vehicle_id),
        "departure_time": _utc(2026, 5, 22, 8, 0),
        "return_time": _utc(2026, 5, 22, 17, 0),
        "estimated_energy_kwh": 220.0,
        "route_id": "R-manual",
        "created_at": datetime(2026, 5, 20, tzinfo=UTC),  # newer → wins
    }
    recurring = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 22),
        horizon_end=_utc(2026, 5, 23),
    )
    merged = merge_recurring_with_manual([manual], recurring, depot_tz=VILNIUS)
    assert len(merged) == 1
    assert merged[0]["route_id"] == "R-manual"


def test_recurring_wins_when_newer_than_manual():
    vehicle_id = uuid4()
    tpl = _make_template(
        vehicle_id=vehicle_id,
        days=("fri",),
        start=date(2026, 5, 22),
        created_at=datetime(2026, 5, 21, tzinfo=UTC),  # newer → wins
    )
    manual = {
        "vehicle_id": str(vehicle_id),
        "departure_time": _utc(2026, 5, 22, 8, 0),
        "return_time": _utc(2026, 5, 22, 17, 0),
        "estimated_energy_kwh": 220.0,
        "route_id": "R-manual",
        "created_at": datetime(2026, 5, 1, tzinfo=UTC),
    }
    recurring = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 22),
        horizon_end=_utc(2026, 5, 23),
    )
    merged = merge_recurring_with_manual([manual], recurring, depot_tz=VILNIUS)
    assert len(merged) == 1
    assert merged[0]["route_id"] == tpl.route_id


def test_tiebreaker_per_vehicle_independent():
    """The tiebreaker is per (vehicle, day): one vehicle's manual win
    should not silence another vehicle's recurring trip on the same day."""
    vehicle_a = uuid4()
    vehicle_b = uuid4()
    tpl_a = _make_template(
        vehicle_id=vehicle_a, days=("fri",), start=date(2026, 5, 22),
        created_at=datetime(2026, 5, 1, tzinfo=UTC), route_id="A-rec",
    )
    tpl_b = _make_template(
        vehicle_id=vehicle_b, days=("fri",), start=date(2026, 5, 22),
        created_at=datetime(2026, 5, 21, tzinfo=UTC), route_id="B-rec",
    )
    manual_a = {
        "vehicle_id": str(vehicle_a),
        "departure_time": _utc(2026, 5, 22, 8, 0),
        "return_time": _utc(2026, 5, 22, 17, 0),
        "estimated_energy_kwh": 220.0,
        "route_id": "A-manual",
        "created_at": datetime(2026, 5, 20, tzinfo=UTC),
    }
    recurring = expand_recurring_templates(
        [tpl_a, tpl_b], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 22),
        horizon_end=_utc(2026, 5, 23),
    )
    merged = merge_recurring_with_manual([manual_a], recurring, depot_tz=VILNIUS)
    routes = sorted(r["route_id"] for r in merged)
    # A: manual (newer) wins. B: only recurring. Both must appear.
    assert routes == ["A-manual", "B-rec"]


def test_merge_strips_internal_metadata_keys():
    vehicle_id = uuid4()
    tpl = _make_template(
        vehicle_id=vehicle_id, days=("fri",), start=date(2026, 5, 22),
    )
    recurring = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 22),
        horizon_end=_utc(2026, 5, 23),
    )
    merged = merge_recurring_with_manual([], recurring, depot_tz=VILNIUS)
    assert merged, "should emit at least one row"
    row_keys = set(merged[0].keys())
    # Match _get_schedules() return shape — no underscore-internal keys
    # leaking out, no extraneous created_at.
    assert all(not k.startswith("_") for k in row_keys)
    assert "created_at" not in row_keys
    assert row_keys == {
        "vehicle_id", "departure_time", "return_time",
        "estimated_energy_kwh", "route_id",
    }


# ── Horizon edge cases ──────────────────────────────────────────────────────


def test_empty_inputs_return_empty():
    out = expand_recurring_templates(
        [], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 22),
        horizon_end=_utc(2026, 5, 23),
    )
    assert out == []


def test_zero_or_inverted_horizon_returns_empty():
    tpl = _make_template(days=("mon",))
    out = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 18),
        horizon_end=_utc(2026, 5, 18),
    )
    assert out == []
    out2 = expand_recurring_templates(
        [tpl], [],
        depot_tz=VILNIUS,
        horizon_start=_utc(2026, 5, 20),
        horizon_end=_utc(2026, 5, 19),
    )
    assert out2 == []


def test_naive_horizon_raises():
    tpl = _make_template(days=("mon",))
    with pytest.raises(ValueError, match="timezone-aware"):
        expand_recurring_templates(
            [tpl], [],
            depot_tz=VILNIUS,
            horizon_start=datetime(2026, 5, 18),  # naive
            horizon_end=_utc(2026, 5, 19),
        )
