"""Navirec telematics → Favonius shape translation.

Pure functions and Pydantic models only — no I/O. The poller and the
historical backfill share these so plate normalization and reading parsing
behave identically on the live and backfill paths.

The exact Navirec field names are confirmed by ``scripts/probe_navirec_api.py``
against the live account; the ``_FIELD_CANDIDATES`` lists below let the mapper
tolerate the common spellings so a probe correction is a one-line edit, not a
rewrite. SoC is normalized to the Favonius 0–1 convention (telematics APIs
typically report 0–100 percent).
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from pydantic import BaseModel, Field

# Candidate source keys, most-specific first. Pin these after the probe.
_PLATE_KEYS = ("licensePlate", "plate", "registrationNumber", "regNumber", "vehiclePlate")
_SOC_KEYS = ("stateOfCharge", "soc", "batterySoc", "batteryLevel", "stateOfChargePercent")
_LAT_KEYS = ("latitude", "lat")
_LON_KEYS = ("longitude", "lon", "lng")
_TIME_KEYS = ("timestamp", "time", "recordedAt", "lastUpdate", "gpsTime", "positionTime")
_ID_KEYS = ("id", "vehicleId", "deviceId", "objectId", "imei")
# CAN-bus odometer. Drives weekly distance (src/core/billing/distance.py) via
# per-vehicle deltas. Pin the exact key AND its unit (m vs km) after running
# scripts/probe_navirec_api.py against the live account — see _coerce_odometer.
_ODOMETER_KEYS = (
    "odometer",
    "odometerKm",
    "totalOdometer",
    "mileage",
    "totalDistance",
    "canOdometer",
    "obdOdometer",
)

# Upper sanity bound (km): an odometer above this is taken as a garbage CAN
# value, not a real reading. Commercial vehicles rarely exceed ~2M km lifetime;
# 5M leaves generous headroom while still rejecting obvious corruption.
_MAX_ODOMETER_KM = 5_000_000.0

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize_plate(raw: Any) -> str:
    """Canonicalize a license plate for cross-system matching.

    Uppercases and strips every non-alphanumeric character so that
    ``"ABC-123"``, ``"abc 123"`` and ``"ABC123"`` all collapse to ``"ABC123"``.
    Returns ``""`` for ``None`` / blank input (callers treat that as
    unmatchable). Non-string input (e.g. a numeric provider id) is coerced to
    ``str`` first so a malformed payload can't abort the whole poll cycle with
    an ``AttributeError``.
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    if not text:
        return ""
    return _NON_ALNUM.sub("", text.upper())


class VehicleTelemetryReading(BaseModel):
    """One normalized telematics reading, keyed by license plate.

    Plate (not a Favonius UUID) because Navirec has no knowledge of Favonius
    vehicle ids; the poller resolves plate → vehicle_id against the DB.

    ``soc`` and ``odometer_km`` are both optional: a single Navirec record may
    carry SoC, odometer, or both. A record with neither is dropped upstream
    (see :func:`parse_navirec_point`) — but an odometer-only record is kept so
    distance reporting works even when the device omits SoC, and vice versa.
    """

    vehicle_plate: str = Field(..., min_length=1)
    soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    time: datetime
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    odometer_km: Optional[float] = Field(None, ge=0.0)
    raw_fields: dict[str, Any] = Field(default_factory=dict)


class ParsedPoint(NamedTuple):
    """Plate-independent telemetry fields parsed from one Navirec record.

    Shared by the live mapper and the historical backfill so both parse SoC /
    odometer / time / position identically. The backfill needs this because
    history rows carry no plate of their own — the vehicle is known from the
    parent object.

    ``soc`` and ``odometer_km`` are independently optional (see
    :func:`parse_navirec_point`): a point survives parsing as long as it has a
    timestamp plus *at least one* of SoC / odometer.
    """

    soc: Optional[float]
    time: datetime
    latitude: Optional[float]
    longitude: Optional[float]
    odometer_km: Optional[float]


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present, usable value among ``keys``.

    Skips ``None`` and blank/whitespace-only strings so a placeholder in a
    higher-priority field (e.g. ``"stateOfCharge": ""``) doesn't block fallback
    to a later valid key (``"batteryLevel"``). Numeric ``0`` / ``False`` are
    returned as-is — the field-specific coercers decide whether they're valid.
    """
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def navirec_vehicle_id(raw: dict[str, Any]) -> Optional[str]:
    """Extract Navirec's own vehicle/device id (needed to fetch history)."""
    value = _first(raw, _ID_KEYS)
    return str(value) if value is not None else None


def navirec_vehicle_plate(raw: dict[str, Any]) -> str:
    """Extract and normalize a Navirec vehicle object's license plate."""
    return normalize_plate(_first(raw, _PLATE_KEYS))


def _coerce_soc(value: Any) -> Optional[float]:
    """Coerce a telematics SoC into the Favonius 0–1 range.

    Values > 1.5 are treated as a 0–100 percentage and divided by 100; the
    result is clamped to [0, 1]. Unparseable, boolean, or non-finite input
    (``NaN`` / ``inf``) returns ``None`` — a bool (``True``→1.0) or a clamped
    NaN would silently fabricate a valid SoC that could override real charger
    telemetry in the freshest-wins merge.

    KNOWN LIMITATION (pin via scripts/probe_navirec_api.py): a raw value in the
    (0, 1.5] band is ambiguous — ``1`` could be 1% (percent encoding) or 100%
    (fraction encoding). We can't disambiguate without knowing Navirec's unit,
    and assuming percent would corrupt a true fraction (``0.85`` → 0.85%). Once
    the probe confirms the encoding, replace this heuristic with the fixed unit.
    """
    if isinstance(value, bool):
        return None
    try:
        soc = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(soc):
        return None
    if soc > 1.5:
        soc = soc / 100.0
    return max(0.0, min(1.0, soc))


def _safe_coord(value: Any, limit: float) -> Optional[float]:
    """Coerce a coordinate; ``None`` if absent, unparseable, non-finite, or out
    of range.

    Range/finite checks matter because the per-depot write is a single batched
    insert: one out-of-range coordinate (``999``, ``inf``, ``NaN``) would
    violate the ``vehicle_telemetry`` lat/lon CHECK constraints and fail the
    whole batch instead of degrading that one record's coordinate to ``None``.
    """
    if value is None:
        return None
    try:
        coord = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(coord) or abs(coord) > limit:
        return None
    return coord


def _coerce_odometer(value: Any) -> Optional[float]:
    """Coerce a telematics odometer reading into kilometres, or ``None``.

    Rejects ``bool`` (``True``→1.0 would fabricate a reading), unparseable
    input, non-finite (``NaN``/``inf``), negative values, and values above
    :data:`_MAX_ODOMETER_KM` (obvious CAN-bus garbage). An odometer grows
    unboundedly so — unlike SoC/coordinates — there is no meaningful upper
    *clamp*; out-of-range input is dropped to ``None`` rather than clipped, so a
    spurious spike can't corrupt a distance delta.

    KNOWN LIMITATION (pin via scripts/probe_navirec_api.py): the source UNIT is
    assumed to be kilometres. If the live feed reports metres, divide by 1000
    here once the probe confirms it — exactly the same convention as
    :func:`_coerce_soc`'s percent-vs-fraction note.
    """
    if isinstance(value, bool):
        return None
    try:
        odo = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(odo):
        return None
    if odo < 0 or odo > _MAX_ODOMETER_KM:
        return None
    return odo


def _coerce_time(value: Any) -> Optional[datetime]:
    """Parse a telematics timestamp into a tz-aware UTC datetime, or ``None``.

    Returns ``None`` when the timestamp is absent, unparseable, or out of range.
    We never fabricate a timestamp (e.g. ``now``): a made-up "now" would let a
    schema-mismatched or stale reading win the freshest-wins SoC merge over real
    charger telemetry, feeding the optimizer a wrong current SoC. Fail closed —
    the caller skips records we can't time-stamp from the source.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Numeric epoch encoded as a string (e.g. "1748253600000"). Require
        # >= 10 digits so a bare year like "2026" isn't misread as an epoch;
        # delegate to the numeric branch (handles ms scaling + overflow).
        if text.isdigit() and len(text) >= 10:
            return _coerce_time(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def parse_navirec_point(raw: dict[str, Any]) -> Optional[ParsedPoint]:
    """Parse the plate-independent telemetry fields (SoC / odometer + time + position).

    A source timestamp is always mandatory (it's the table's primary-key
    component and what the freshness/merge windows judge — see ``_coerce_time``).
    Beyond that, the record is kept when it carries **at least one** usable
    signal: a SoC *or* an odometer. This decoupling matters because the two
    feeds serve different consumers — SoC drives the optimizer's freshest-wins
    merge, odometer drives distance/cost-per-km reporting — and a device that
    reports only one must not cause the other to be discarded.

    Returns ``None`` (caller skips) when the timestamp is missing/unparseable,
    or when neither SoC nor odometer is present, so both the live poller and the
    historical backfill skip the record rather than abort the cycle.
    """
    soc = _coerce_soc(_first(raw, _SOC_KEYS))
    odometer = _coerce_odometer(_first(raw, _ODOMETER_KEYS))
    if soc is None and odometer is None:
        return None
    when = _coerce_time(_first(raw, _TIME_KEYS))
    if when is None:
        return None
    return ParsedPoint(
        soc=soc,
        time=when,
        latitude=_safe_coord(_first(raw, _LAT_KEYS), 90.0),
        longitude=_safe_coord(_first(raw, _LON_KEYS), 180.0),
        odometer_km=odometer,
    )


def reading_from_point(
    plate: str, point: ParsedPoint, raw_fields: dict[str, Any]
) -> VehicleTelemetryReading:
    """Build a reading from an already-parsed point plus a known plate.

    Used by the backfill, where the vehicle (and thus plate) comes from the
    parent object and the per-timestamp history rows carry no plate of their own.
    """
    return VehicleTelemetryReading(
        vehicle_plate=plate,
        soc=point.soc,
        time=point.time,
        latitude=point.latitude,
        longitude=point.longitude,
        odometer_km=point.odometer_km,
        raw_fields=raw_fields,
    )


def navirec_vehicle_to_reading(raw: dict[str, Any]) -> Optional[VehicleTelemetryReading]:
    """Map one Navirec vehicle object (carrying its plate) to a reading, or ``None``.

    Returns ``None`` (caller skips) when the object lacks a usable plate, a
    source timestamp, or both a parseable SoC and odometer — a record we can't
    attribute, time-stamp, or extract any usable signal from would be dead
    weight (or worse, misleading) in ``vehicle_telemetry``.
    """
    plate = normalize_plate(_first(raw, _PLATE_KEYS))
    if not plate:
        return None
    point = parse_navirec_point(raw)
    if point is None:
        return None
    return reading_from_point(plate, point, raw)
