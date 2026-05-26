"""Unit tests for Navirec mapping helpers (pure functions)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.adapters.navirec.mapping import (
    VehicleTelemetryReading,
    navirec_vehicle_to_reading,
    normalize_plate,
)

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ABC-123", "ABC123"),
        (" abc 123 ", "ABC123"),
        ("ab.c/1", "ABC1"),
        ("ABC123", "ABC123"),
        ("å-ä-ö-9", "9"),  # non-ASCII letters stripped after upper()
        (None, ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected


def test_reading_percent_soc_normalized():
    r = navirec_vehicle_to_reading(
        {"licensePlate": "ABC-123", "stateOfCharge": 85, "timestamp": "2026-05-26T10:00:00Z"},
        now=_NOW,
    )
    assert r is not None
    assert r.vehicle_plate == "ABC123"
    assert r.soc == pytest.approx(0.85)
    assert r.time == datetime(2026, 5, 26, 10, 0, 0, tzinfo=timezone.utc)


def test_reading_fraction_soc_passthrough():
    r = navirec_vehicle_to_reading({"plate": "X1", "soc": 0.42}, now=_NOW)
    assert r is not None and r.soc == pytest.approx(0.42)
    # No timestamp → defaults to now.
    assert r.time == _NOW


def test_reading_soc_clamped():
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 150}, now=_NOW).soc == 1.0
    assert navirec_vehicle_to_reading({"plate": "X", "soc": -5}, now=_NOW).soc == 0.0


def test_reading_missing_plate_returns_none():
    assert navirec_vehicle_to_reading({"soc": 50}, now=_NOW) is None


def test_reading_missing_or_bad_soc_returns_none():
    assert navirec_vehicle_to_reading({"plate": "X"}, now=_NOW) is None
    assert navirec_vehicle_to_reading({"plate": "X", "soc": "n/a"}, now=_NOW) is None


def test_reading_epoch_millis_timestamp():
    r = navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": 1748253600000}, now=_NOW)
    assert r.time == datetime(2025, 5, 26, 10, 0, 0, tzinfo=timezone.utc)


def test_reading_lat_lon_aliases():
    r = navirec_vehicle_to_reading({"plate": "X", "soc": 50, "lat": 54.7, "lng": 25.3}, now=_NOW)
    assert (r.latitude, r.longitude) == (pytest.approx(54.7), pytest.approx(25.3))


def test_reading_field_precedence():
    # licensePlate beats plate; stateOfCharge beats batteryLevel.
    r = navirec_vehicle_to_reading(
        {"licensePlate": "AAA111", "plate": "BBB222", "stateOfCharge": 30, "batteryLevel": 90},
        now=_NOW,
    )
    assert r.vehicle_plate == "AAA111"
    assert r.soc == pytest.approx(0.30)


def test_raw_fields_retained():
    raw = {"plate": "X", "soc": 50, "vendorField": "keep-me"}
    r = navirec_vehicle_to_reading(raw, now=_NOW)
    assert r.raw_fields["vendorField"] == "keep-me"


def test_model_rejects_out_of_range_soc():
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="X", soc=2.0, time=_NOW)
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="", soc=0.5, time=_NOW)
