"""Unit tests for Navirec mapping helpers (pure functions)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.adapters.navirec.mapping import (
    VehicleTelemetryReading,
    navirec_vehicle_to_reading,
    normalize_plate,
    parse_navirec_point,
)

_TS = "2026-05-26T10:00:00Z"
_TS_DT = datetime(2026, 5, 26, 10, 0, 0, tzinfo=timezone.utc)


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
        (12345, "12345"),  # non-string id coerced, not crashed
    ],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected


def test_reading_percent_soc_normalized():
    r = navirec_vehicle_to_reading(
        {"licensePlate": "ABC-123", "stateOfCharge": 85, "timestamp": _TS}
    )
    assert r is not None
    assert r.vehicle_plate == "ABC123"
    assert r.soc == pytest.approx(0.85)
    assert r.time == _TS_DT


def test_reading_fraction_soc_passthrough():
    r = navirec_vehicle_to_reading({"plate": "X1", "soc": 0.42, "timestamp": _TS})
    assert r is not None and r.soc == pytest.approx(0.42)


def test_reading_soc_clamped():
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 150, "timestamp": _TS}).soc == 1.0
    assert navirec_vehicle_to_reading({"plate": "X", "soc": -5, "timestamp": _TS}).soc == 0.0


def test_reading_missing_plate_returns_none():
    assert navirec_vehicle_to_reading({"soc": 50, "timestamp": _TS}) is None


def test_reading_missing_or_bad_soc_returns_none():
    assert navirec_vehicle_to_reading({"plate": "X", "timestamp": _TS}) is None
    assert navirec_vehicle_to_reading({"plate": "X", "soc": "n/a", "timestamp": _TS}) is None


def test_reading_missing_timestamp_returns_none():
    # Fail closed: never fabricate a timestamp (would override charger telemetry).
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 50}) is None


def test_reading_invalid_timestamp_returns_none():
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": "not-a-date"}) is None
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": ""}) is None


def test_reading_overflow_epoch_returns_none():
    # datetime.fromtimestamp raises OverflowError/OSError for absurd epochs.
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": 10**18}) is None


def test_reading_epoch_millis_timestamp():
    r = navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": 1748253600000})
    assert r.time == datetime(2025, 5, 26, 10, 0, 0, tzinfo=timezone.utc)


def test_reading_string_epoch_timestamp():
    # Epoch encoded as a string (common API encoding) must parse, not drop.
    r_ms = navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": "1748253600000"})
    assert r_ms.time == datetime(2025, 5, 26, 10, 0, 0, tzinfo=timezone.utc)
    r_s = navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": "1748253600"})
    assert r_s.time == datetime(2025, 5, 26, 10, 0, 0, tzinfo=timezone.utc)


def test_reading_short_numeric_string_not_epoch():
    # A bare "2026" is not a 10+ digit epoch and isn't a valid datetime → drop.
    assert navirec_vehicle_to_reading({"plate": "X", "soc": 50, "timestamp": "2026"}) is None


def test_reading_lat_lon_aliases():
    r = navirec_vehicle_to_reading(
        {"plate": "X", "soc": 50, "lat": 54.7, "lng": 25.3, "timestamp": _TS}
    )
    assert (r.latitude, r.longitude) == (pytest.approx(54.7), pytest.approx(25.3))


def test_reading_bad_latlon_degrades_to_none_not_crash():
    # A malformed coordinate must not abort the record; just drop the coord.
    r = navirec_vehicle_to_reading(
        {"plate": "X", "soc": 50, "lat": "N/A", "lon": "", "timestamp": _TS}
    )
    assert r is not None
    assert r.latitude is None and r.longitude is None


def test_reading_non_finite_soc_returns_none():
    # NaN/inf must not clamp into a bogus valid SoC.
    assert navirec_vehicle_to_reading({"plate": "X", "soc": "nan", "timestamp": _TS}) is None
    assert navirec_vehicle_to_reading({"plate": "X", "soc": float("inf"), "timestamp": _TS}) is None


def test_reading_boolean_soc_returns_none():
    # bool is an int subclass; True/False must not be read as 1.0/0.0 SoC.
    assert navirec_vehicle_to_reading({"plate": "X", "soc": True, "timestamp": _TS}) is None
    assert navirec_vehicle_to_reading({"plate": "X", "soc": False, "timestamp": _TS}) is None


def test_reading_out_of_range_or_non_finite_coord_degrades_to_none():
    # Out-of-range / non-finite coords would violate the vehicle_telemetry CHECK
    # constraints and fail the batch insert — drop the coord instead.
    r = navirec_vehicle_to_reading(
        {"plate": "X", "soc": 50, "lat": 999, "lon": float("inf"), "timestamp": _TS}
    )
    assert r is not None
    assert r.latitude is None and r.longitude is None


def test_reading_field_precedence():
    # licensePlate beats plate; stateOfCharge beats batteryLevel.
    r = navirec_vehicle_to_reading(
        {
            "licensePlate": "AAA111",
            "plate": "BBB222",
            "stateOfCharge": 30,
            "batteryLevel": 90,
            "timestamp": _TS,
        }
    )
    assert r.vehicle_plate == "AAA111"
    assert r.soc == pytest.approx(0.30)


def test_raw_fields_retained():
    raw = {"plate": "X", "soc": 50, "timestamp": _TS, "vendorField": "keep-me"}
    r = navirec_vehicle_to_reading(raw)
    assert r.raw_fields["vendorField"] == "keep-me"


def test_parse_navirec_point_plate_independent():
    # History rows have no plate; parse_navirec_point still yields SoC + time.
    p = parse_navirec_point({"soc": 73, "timestamp": _TS})
    assert p is not None
    assert p.soc == pytest.approx(0.73)
    assert p.time == _TS_DT


def test_parse_navirec_point_requires_soc_and_time():
    assert parse_navirec_point({"timestamp": _TS}) is None  # no soc
    assert parse_navirec_point({"soc": 50}) is None  # no timestamp


def test_model_rejects_out_of_range_soc():
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="X", soc=2.0, time=_TS_DT)
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="", soc=0.5, time=_TS_DT)
