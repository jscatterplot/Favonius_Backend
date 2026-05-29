"""Unit tests for the deterministic ``savings`` intent window logic.

Covers the DST-aware overnight window (:func:`overnight_window_utc`), the
phrase classifier / window resolver (:mod:`src.api.agent.intents.savings`),
and the renderer — no database, no LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.api.agent.intents.savings import (
    classify_savings_window,
    render_savings_answer,
    resolve_savings_window,
)
from src.api.agent.plan import TimeWindow
from src.api.agent.resolve import resolve_time_window
from src.api.savings import overnight_window_utc

VILNIUS = ZoneInfo("Europe/Vilnius")
_SENTINEL = __import__("uuid").UUID("00000000-0000-0000-0000-000000000000")


# ── overnight_window_utc ─────────────────────────────────────────────────────


def _local(dt_utc, tz=VILNIUS):
    return dt_utc.astimezone(tz)


def test_overnight_winter_window_edges_and_duration():
    now = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Europe/Vilnius")
    # Wall-clock edges: 17:00 previous local day → 07:00 current local day.
    assert (_local(start).hour, _local(start).minute) == (17, 0)
    assert (_local(end).hour, _local(end).minute) == (7, 0)
    assert _local(start).date() == datetime(2026, 1, 14).date()
    assert _local(end).date() == datetime(2026, 1, 15).date()
    # Winter (no DST change in the window) → exactly 14 hours.
    assert end - start == timedelta(hours=14)
    assert start < end


def test_overnight_before_end_hour_returns_current_night_window():
    # 03:00 local (UTC+2 winter) is still the 17:00→07:00 night in progress.
    now = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Europe/Vilnius")
    assert _local(start) == datetime(2026, 1, 14, 17, 0, tzinfo=VILNIUS)
    assert _local(end) == datetime(2026, 1, 15, 7, 0, tzinfo=VILNIUS)


def test_overnight_summer_window_edges():
    now = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Europe/Vilnius")
    assert (_local(start).hour, _local(end).hour) == (17, 7)
    assert end - start == timedelta(hours=14)


def test_overnight_spring_forward_is_one_hour_short():
    # Vilnius springs forward 2026-03-29 03:00→04:00; the night 28→29 March
    # loses an hour, so the 17:00→07:00 wall-clock window is 13 UTC hours.
    now = datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Europe/Vilnius")
    assert (_local(start).hour, _local(end).hour) == (17, 7)
    assert end - start == timedelta(hours=13)


def test_overnight_fall_back_is_one_hour_long():
    # Vilnius falls back 2026-10-25 04:00→03:00; the night 24→25 Oct gains an
    # hour, so the window is 15 UTC hours while staying 17:00→07:00 local.
    now = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Europe/Vilnius")
    assert (_local(start).hour, _local(end).hour) == (17, 7)
    assert end - start == timedelta(hours=15)


def test_overnight_missing_tz_falls_back_to_utc():
    now = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, None)
    assert start == datetime(2026, 1, 14, 17, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


def test_overnight_invalid_tz_falls_back_to_utc():
    now = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    start, end = overnight_window_utc(now, "Not/AZone")
    assert start == datetime(2026, 1, 14, 17, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


# ── classify_savings_window ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("how much did we save overnight?", "overnight"),
        ("savings last night", "overnight"),
        ("what will we save tonight", "overnight"),
        ("how much did we save yesterday?", "yesterday"),
        ("savings last week", "last_week"),
        ("how much saved this week", "this_week"),
        ("savings last month", "last_month"),
        ("how much have we saved this month?", "month_to_date"),
        ("month to date savings", "month_to_date"),
        ("what did we save today", "today"),
        ("how much have we saved?", "month_to_date"),  # default
    ],
)
def test_classify_savings_window(message, expected):
    assert classify_savings_window(message) == expected


# ── resolve_savings_window ───────────────────────────────────────────────────


def test_resolve_overnight_matches_helper():
    # 09:00 Vilnius (06:00Z, EEST): the canonical 17:00→07:00 window already
    # ended, so the now-clamp is a no-op and the resolved window equals the
    # raw helper output.
    now = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
    win = resolve_savings_window("save overnight", now, "Europe/Vilnius")
    assert win.kind == "overnight" and win.label == "overnight"
    assert (win.period_start, win.period_end) == overnight_window_utc(now, "Europe/Vilnius")


def test_resolve_overnight_clamps_end_to_now_before_morning():
    # 04:00 Vilnius (01:00Z, EEST): the night is still in progress, so the raw
    # 17:00→07:00 window ends in the future (07:00 local). The resolved window
    # must clamp period_end back to `now` so the baseline price-average doesn't
    # include not-yet-charged hours.
    now = datetime(2026, 5, 28, 1, 0, tzinfo=timezone.utc)
    raw_start, raw_end = overnight_window_utc(now, "Europe/Vilnius")
    win = resolve_savings_window("save overnight", now, "Europe/Vilnius")
    assert win.kind == "overnight"
    assert raw_end > now  # the canonical window genuinely extends past now
    assert win.period_end == now  # …and we clamp it back
    assert win.period_start == raw_start < win.period_end


def test_resolve_month_to_date_ends_at_now():
    now = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
    win = resolve_savings_window("how much have we saved", now, "Europe/Vilnius")
    assert win.kind == "month_to_date"
    assert win.period_end == now
    # Month start is Vilnius-local midnight on the 1st → 2026-04-30 21:00Z (UTC+3 DST).
    assert win.period_start == datetime(2026, 4, 30, 21, 0, tzinfo=timezone.utc)


def test_resolve_relative_matches_resolve_time_window():
    now = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
    win = resolve_savings_window("savings last week", now, "Europe/Vilnius")
    expected = resolve_time_window(
        TimeWindow(kind="relative", relative="last_week"),
        [_SENTINEL],
        {_SENTINEL: "Europe/Vilnius"},
        now=now,
    )
    assert win.kind == "last_week"
    assert win.period_start == expected.start_utc
    assert win.period_end == expected.end_utc


def test_resolve_missing_tz_does_not_crash():
    now = datetime(2026, 5, 28, 6, 0, tzinfo=timezone.utc)
    win = resolve_savings_window("save overnight", now, None)
    assert win.period_start < win.period_end


def test_resolve_today_clamps_end_to_now():
    # 'today' spans future hours; period_end must be clamped to now so the
    # baseline price-average doesn't include not-yet-charged hours.
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    win = resolve_savings_window("how much did we save today?", now, "Europe/Vilnius")
    assert win.kind == "today"
    assert win.period_end == now
    assert win.period_start < now


def test_resolve_invalid_tz_relative_window_does_not_crash():
    # A bad sites.timezone must degrade to UTC for relative windows too, not
    # just for overnight (regression: month_to_date used to 500).
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    for message in ("how much have we saved this month?", "savings last week", "save today"):
        win = resolve_savings_window(message, now, "Not/AZone")
        assert win.period_start < win.period_end


# ── render_savings_answer ────────────────────────────────────────────────────


def test_render_positive_saving():
    text = render_savings_answer(
        actual_eur=80.0, baseline_eur=100.0, saved_eur=20.0, saved_pct=20.0, label="overnight"
    )
    assert "saved about €20.00 (20.0%)" in text
    assert "€80.00" in text and "€100.00" in text and "overnight" in text


def test_render_negative_saving_reads_as_more():
    text = render_savings_answer(
        actual_eur=120.0, baseline_eur=100.0, saved_eur=-20.0, saved_pct=-20.0, label="last week"
    )
    assert "€20.00 (20.0%) more" in text


def test_render_unknown_baseline_reports_spend_only():
    text = render_savings_answer(
        actual_eur=12.0,
        baseline_eur=0.0,
        saved_eur=0.0,
        saved_pct=0.0,
        label="overnight",
        baseline_known=False,
    )
    assert "€12.00" in text
    assert "can't estimate the savings" in text


def test_render_no_activity():
    text = render_savings_answer(
        actual_eur=0.0,
        baseline_eur=0.0,
        saved_eur=0.0,
        saved_pct=0.0,
        label="overnight",
        baseline_known=False,
    )
    assert "No charging was recorded overnight" in text


def test_render_known_zero_baseline_is_not_spend_only():
    # Multi-depot baselines can cancel to 0 (negative prices) — a KNOWN
    # baseline of 0 must NOT render as "no price data".
    text = render_savings_answer(
        actual_eur=50.0,
        baseline_eur=0.0,
        saved_eur=-50.0,
        saved_pct=0.0,
        label="overnight",
        baseline_known=True,
    )
    assert "can't estimate" not in text
    assert "€50.00" in text


def test_render_multi_depot_scope():
    text = render_savings_answer(
        actual_eur=80.0,
        baseline_eur=100.0,
        saved_eur=20.0,
        saved_pct=20.0,
        label="this month so far",
        depot_count=3,
    )
    assert "across 3 depots" in text
