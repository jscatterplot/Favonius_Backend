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

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize_plate(raw: Optional[str]) -> str:
    """Canonicalize a license plate for cross-system matching.

    Uppercases and strips every non-alphanumeric character so that
    ``"ABC-123"``, ``"abc 123"`` and ``"ABC123"`` all collapse to ``"ABC123"``.
    Returns ``""`` for ``None`` / blank input (callers treat that as unmatchable).
    """
    if not raw:
        return ""
    return _NON_ALNUM.sub("", raw.upper())


class VehicleTelemetryReading(BaseModel):
    """One normalized telematics reading, keyed by license plate.

    Plate (not a Favonius UUID) because Navirec has no knowledge of Favonius
    vehicle ids; the poller resolves plate → vehicle_id against the DB.
    """

    vehicle_plate: str = Field(..., min_length=1)
    soc: float = Field(..., ge=0.0, le=1.0)
    time: datetime
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw_fields: dict[str, Any] = Field(default_factory=dict)


class ParsedPoint(NamedTuple):
    """Plate-independent telemetry fields parsed from one Navirec record.

    Shared by the live mapper and the historical backfill so both parse SoC /
    time / position identically. The backfill needs this because history rows
    carry no plate of their own — the vehicle is known from the parent object.
    """

    soc: float
    time: datetime
    latitude: Optional[float]
    longitude: Optional[float]


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present, non-null value among ``keys``."""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
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
    result is clamped to [0, 1]. Unparseable input returns ``None``.
    """
    try:
        soc = float(value)
    except (TypeError, ValueError):
        return None
    if soc > 1.5:
        soc = soc / 100.0
    return max(0.0, min(1.0, soc))


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float coercion; ``None`` when absent or unparseable.

    Keeps one malformed coordinate (e.g. ``""`` / ``"N/A"``) from aborting the
    whole poll/backfill cycle — callers don't isolate per-record float errors.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    """Parse the plate-independent telemetry fields (SoC + time + position).

    Returns ``None`` when SoC or timestamp is missing/unparseable so both the
    live poller and the historical backfill skip the record rather than abort
    the cycle. SoC and a source timestamp are mandatory (see ``_coerce_time``).
    """
    soc = _coerce_soc(_first(raw, _SOC_KEYS))
    if soc is None:
        return None
    when = _coerce_time(_first(raw, _TIME_KEYS))
    if when is None:
        return None
    return ParsedPoint(
        soc=soc,
        time=when,
        latitude=_safe_float(_first(raw, _LAT_KEYS)),
        longitude=_safe_float(_first(raw, _LON_KEYS)),
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
        raw_fields=raw_fields,
    )


def navirec_vehicle_to_reading(raw: dict[str, Any]) -> Optional[VehicleTelemetryReading]:
    """Map one Navirec vehicle object (carrying its plate) to a reading, or ``None``.

    Returns ``None`` (caller skips) when the object lacks a usable plate, a
    parseable SoC, or a source timestamp — the optimizer only consumes SoC, so
    a record we can't attribute or time-stamp would be dead weight (or worse,
    misleading) in ``vehicle_telemetry``.
    """
    plate = normalize_plate(_first(raw, _PLATE_KEYS))
    if not plate:
        return None
    point = parse_navirec_point(raw)
    if point is None:
        return None
    return reading_from_point(plate, point, raw)
