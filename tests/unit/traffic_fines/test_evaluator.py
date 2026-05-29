"""Unit tests for the traffic-fine early-payment evaluator (pure, no I/O)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.core.traffic_fines.evaluator import (
    DEFAULT_ALERT_WINDOW_HOURS,
    evaluate_early_payment,
    humanize_remaining,
)
from src.core.traffic_fines.models import TrafficFineExtraction

# A fixed "now" so every assertion is deterministic.
NOW = datetime(2026, 5, 30, 6, 0, 0, tzinfo=timezone.utc)


def _fine(**kw: object) -> TrafficFineExtraction:
    base: dict[str, object] = dict(
        is_traffic_fine=True,
        fine_reference="M-2026-0042",
        currency="EUR",
        full_amount=150.0,
        early_payment_amount=120.0,
        # 36 hours after NOW -> inside the 48h window.
        early_payment_deadline="2026-05-31T18:00:00+00:00",
        iban="DE89370400440532013000",
    )
    base.update(kw)
    return TrafficFineExtraction(**base)


def test_within_window_exact_message():
    ev = evaluate_early_payment(_fine(), now=NOW)
    assert ev.kind == "within_window"
    assert ev.within_window is True
    assert ev.discount_amount == 30.0
    assert ev.message == (
        "Priority: Early payment discount for Fine #M-2026-0042 "
        "expires in 2 days. Automate payment now to save €30?"
    )


def test_expired():
    ev = evaluate_early_payment(_fine(early_payment_deadline="2026-05-29T06:00:00+00:00"), now=NOW)
    assert ev.kind == "expired"
    assert ev.within_window is False
    assert ev.message is None


def test_not_yet():
    ev = evaluate_early_payment(_fine(early_payment_deadline="2026-06-10T06:00:00+00:00"), now=NOW)
    assert ev.kind == "not_yet"
    assert ev.message is None
    assert ev.hours_remaining is not None
    assert ev.hours_remaining > DEFAULT_ALERT_WINDOW_HOURS


def test_no_deadline_missing():
    ev = evaluate_early_payment(_fine(early_payment_deadline=None), now=NOW)
    assert ev.kind == "no_deadline"


def test_no_deadline_unparseable():
    ev = evaluate_early_payment(_fine(early_payment_deadline="sometime soon"), now=NOW)
    assert ev.kind == "no_deadline"


def test_not_a_fine():
    ev = evaluate_early_payment(_fine(is_traffic_fine=False), now=NOW)
    assert ev.kind == "not_a_fine"


def test_date_only_resolves_end_of_day_local():
    tz = "Europe/Vilnius"
    ev = evaluate_early_payment(_fine(early_payment_deadline="2026-05-31"), now=NOW, depot_tz=tz)
    expected = datetime(2026, 5, 31, 23, 59, 59, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    assert ev.deadline_utc == expected
    assert ev.kind == "within_window"  # ~39h out from NOW


def test_naive_datetime_assumed_depot_tz():
    tz = "Europe/Berlin"
    ev = evaluate_early_payment(
        _fine(early_payment_deadline="2026-05-31T12:00:00"), now=NOW, depot_tz=tz
    )
    expected = datetime(2026, 5, 31, 12, 0, 0, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    assert ev.deadline_utc == expected


def test_aware_datetime_offset_honored():
    ev = evaluate_early_payment(_fine(early_payment_deadline="2026-05-31T18:00:00+02:00"), now=NOW)
    assert ev.deadline_utc == datetime(2026, 5, 31, 16, 0, 0, tzinfo=timezone.utc)


def test_stated_discount_used_when_no_amounts():
    ev = evaluate_early_payment(
        _fine(full_amount=None, early_payment_amount=None, stated_discount_amount=25.0),
        now=NOW,
    )
    assert ev.discount_amount == 25.0
    assert ev.message is not None
    assert "save €25?" in ev.message


def test_unknown_discount_message():
    ev = evaluate_early_payment(
        _fine(full_amount=None, early_payment_amount=None, stated_discount_amount=None),
        now=NOW,
    )
    assert ev.discount_amount is None
    assert ev.message is not None
    assert ev.message.endswith("Automate payment now to keep the early-payment discount?")


def test_fallback_fine_id():
    ev = evaluate_early_payment(_fine(fine_reference=None), now=NOW, fallback_fine_id="abc12345")
    assert ev.message is not None
    assert "Fine #abc12345" in ev.message


@pytest.mark.parametrize(
    "currency,expected",
    [
        ("EUR", "€30"),
        ("USD", "$30"),
        ("GBP", "£30"),
        ("PLN", "zł 30"),
        ("RON", "RON 30"),
    ],
)
def test_currency_symbols(currency: str, expected: str):
    ev = evaluate_early_payment(_fine(currency=currency), now=NOW)
    assert ev.message is not None
    assert f"save {expected}?" in ev.message


def test_decimal_amount_formatting():
    ev = evaluate_early_payment(_fine(full_amount=150.50, early_payment_amount=120.0), now=NOW)
    assert ev.message is not None
    assert "save €30.50?" in ev.message


def test_window_boundary_inclusive():
    deadline = (NOW + timedelta(hours=48)).isoformat()
    ev = evaluate_early_payment(_fine(early_payment_deadline=deadline), now=NOW)
    assert ev.kind == "within_window"

    just_over = (NOW + timedelta(hours=48, seconds=1)).isoformat()
    ev2 = evaluate_early_payment(_fine(early_payment_deadline=just_over), now=NOW)
    assert ev2.kind == "not_yet"


def test_custom_window_hours():
    deadline = (NOW + timedelta(hours=36)).isoformat()
    ev = evaluate_early_payment(_fine(early_payment_deadline=deadline), now=NOW, window_hours=24)
    assert ev.kind == "not_yet"


def test_naive_now_assumed_utc():
    naive_now = datetime(2026, 5, 30, 6, 0, 0)
    ev = evaluate_early_payment(_fine(), now=naive_now)
    assert ev.kind == "within_window"


@pytest.mark.parametrize(
    "hours,phrase",
    [
        (0.5, "1 hour"),
        (1, "1 hour"),
        (1.5, "2 hours"),
        (12, "12 hours"),
        (24, "1 day"),
        (36, "2 days"),
        (48, "2 days"),
        (72, "3 days"),
    ],
)
def test_humanize_remaining(hours: float, phrase: str):
    assert humanize_remaining(hours) == phrase
