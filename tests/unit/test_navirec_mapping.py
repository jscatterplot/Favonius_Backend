"""Unit tests for Navirec mapping helpers (pure functions)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.adapters.navirec.mapping import (
    _MAX_ODOMETER_KM,
    VehicleTelemetryReading,
    _coerce_odometer,
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


def test_parse_navirec_point_requires_signal_and_time():
    # A timestamp plus at least one signal (soc OR odometer) is required.
    assert parse_navirec_point({"timestamp": _TS}) is None  # no soc, no odometer
    assert parse_navirec_point({"soc": 50}) is None  # no timestamp
    assert parse_navirec_point({"odometer": 1000}) is None  # no timestamp


def test_blank_higher_priority_field_falls_through():
    # A placeholder in a higher-priority key must not block a later valid key.
    r = navirec_vehicle_to_reading({"plate": "X", "stateOfCharge": "", "soc": 50, "timestamp": _TS})
    assert r is not None and r.soc == pytest.approx(0.50)
    r2 = navirec_vehicle_to_reading(
        {"licensePlate": "   ", "plate": "ABC123", "soc": 50, "timestamp": _TS}
    )
    assert r2.vehicle_plate == "ABC123"


def test_model_rejects_out_of_range_soc():
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="X", soc=2.0, time=_TS_DT)
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="", soc=0.5, time=_TS_DT)


# ── Odometer (CAN-bus mileage) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (12345.6, pytest.approx(12345.6)),
        (0, pytest.approx(0.0)),
        ("98765", pytest.approx(98765.0)),  # string-encoded numeric
        (-1, None),  # negative odometer is impossible
        (float("nan"), None),  # non-finite never fabricates a reading
        (float("inf"), None),
        (True, None),  # bool must not read as 1.0 km
        (False, None),
        (_MAX_ODOMETER_KM + 1, None),  # above sanity ceiling → garbage
        ("n/a", None),  # unparseable
        (None, None),
    ],
)
def test_coerce_odometer(raw, expected):
    assert _coerce_odometer(raw) == expected


def test_reading_with_odometer():
    r = navirec_vehicle_to_reading(
        {"plate": "X1", "soc": 80, "odometer": 54321.0, "timestamp": _TS}
    )
    assert r is not None
    assert r.soc == pytest.approx(0.80)
    assert r.odometer_km == pytest.approx(54321.0)


def test_reading_odometer_only_no_soc_is_kept():
    # The key decoupling: an odometer-only record (no SoC) must survive so
    # distance reporting works even when the device omits SoC.
    r = navirec_vehicle_to_reading({"plate": "X1", "odometer": 12000, "timestamp": _TS})
    assert r is not None
    assert r.soc is None
    assert r.odometer_km == pytest.approx(12000.0)


def test_reading_soc_only_no_odometer_is_kept():
    # And the reverse: a SoC-only record (no odometer) is still kept, with
    # odometer_km left None.
    r = navirec_vehicle_to_reading({"plate": "X1", "soc": 55, "timestamp": _TS})
    assert r is not None
    assert r.soc == pytest.approx(0.55)
    assert r.odometer_km is None


def test_reading_neither_soc_nor_odometer_returns_none():
    assert navirec_vehicle_to_reading({"plate": "X1", "timestamp": _TS}) is None


def test_reading_bad_odometer_with_valid_soc_keeps_reading_drops_odometer():
    # A garbage odometer must not discard an otherwise-valid SoC reading.
    r = navirec_vehicle_to_reading({"plate": "X1", "soc": 60, "odometer": -99, "timestamp": _TS})
    assert r is not None
    assert r.soc == pytest.approx(0.60)
    assert r.odometer_km is None


def test_reading_odometer_key_aliases():
    for key in ("odometerKm", "totalOdometer", "mileage", "totalDistance"):
        r = navirec_vehicle_to_reading({"plate": "X1", key: 4242, "timestamp": _TS})
        assert r is not None, key
        assert r.odometer_km == pytest.approx(4242.0), key


def test_parse_navirec_point_odometer_only():
    # History rows may carry only an odometer (no SoC); keep them.
    p = parse_navirec_point({"odometer": 8000, "timestamp": _TS})
    assert p is not None
    assert p.soc is None
    assert p.odometer_km == pytest.approx(8000.0)


def test_model_accepts_none_soc_with_odometer():
    # soc is now optional on the model; an odometer-only reading is valid.
    m = VehicleTelemetryReading(vehicle_plate="X", time=_TS_DT, odometer_km=100.0)
    assert m.soc is None and m.odometer_km == pytest.approx(100.0)


def test_model_rejects_negative_odometer():
    with pytest.raises(ValidationError):
        VehicleTelemetryReading(vehicle_plate="X", time=_TS_DT, odometer_km=-1.0)
