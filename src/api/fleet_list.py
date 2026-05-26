"""Pure helpers for ``GET /depots/{id}/chargers`` and ``/vehicles``.

Every function here is pure: takes plain dicts / scalars and returns plain
dicts / scalars. The actual I/O lives in ``src/api/main.py``; this module
exists so the state-derivation rules are unit-testable without spinning up
asyncpg pools or FastAPI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

# Thresholds — kept module-level so tests can monkey-patch and reasoning is
# centralized. Re-tune here if production tells us the numbers are wrong.

#: Charger is "offline" if no connector has reported a status in this many
#: seconds. Aligned with the frontend's SSE-driven liveness threshold so the
#: REST-derived ``charger.status`` and the client-side stale-detection give
#: the same answer. Heartbeat interval is 300 s (negotiated via
#: BootNotification.conf), so 360 s = one missed heartbeat tolerance.
#:
#: NOTE: this is a fallback used at API-response time. The canonical live
#: signal is the SSE stream at ``GET /depots/{id}/liveness/stream``; the
#: frontend should treat the SSE-driven Map<stationId, lastInteractionAt>
#: as authoritative for offline detection. ``charger.status === "offline"``
#: from this REST response is a starting point on initial page load.
CHARGER_OFFLINE_AGE_S: float = 360.0

#: Vehicle is "offline" if telemetry hasn't arrived in this many seconds.
VEHICLE_OFFLINE_AGE_S: float = 30 * 60.0

#: Default SoC threshold for "ready" when no future ``schedules`` row exists.
DEFAULT_DEPARTURE_SOC: float = 0.95


ChargerStatus = Literal["charging", "idle", "offline", "fault"]
VehicleState = Literal["ready", "charging", "at_risk", "in_route", "offline", "unknown"]


def _isoformat(value: Any) -> Optional[str]:
    """Render a datetime / string / None as ISO 8601 (UTC) or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        # asyncpg returns timezone-aware datetimes; if naive, treat as UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _age_seconds(timestamp: Any, now: datetime) -> Optional[float]:
    """Seconds since ``timestamp`` relative to ``now``. None if no timestamp."""
    if timestamp is None:
        return None
    if not isinstance(timestamp, datetime):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now - timestamp).total_seconds()


def _latest(*timestamps: Any) -> Optional[datetime]:
    """Return the freshest (max) timestamp among the args, ignoring None.

    Naive datetimes are treated as UTC; non-datetime values are skipped.
    Used to combine several independent "last interaction" signals (the
    liveness pg_notify cache, the connector_status MAX, and the telemetry
    MAX) into the single freshest value — taking the max rather than a
    priority fallback so any one live signal keeps a charger from being
    falsely marked offline.
    """
    best: Optional[datetime] = None
    for ts in timestamps:
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if best is None or ts > best:
            best = ts
    return best


def derive_charger_status(
    *,
    ocpp_status: Optional[str],
    last_heartbeat_at: Any,
    has_open_session: bool,
    now: datetime,
) -> ChargerStatus:
    """Map OCPP state + heartbeat freshness to the 4-state pill.

    Priority (top wins):
      1. ``fault`` — latest StatusNotification was ``Faulted``.
      2. ``offline`` — no heartbeat within ``CHARGER_OFFLINE_AGE_S`` OR
         latest status is ``Unavailable``.
      3. ``charging`` — latest status is ``Charging`` OR an open session exists.
      4. ``idle`` — everything else (Available / Preparing / Suspended* /
         Reserved / Finishing / unknown).
    """
    if ocpp_status == "Faulted":
        return "fault"

    age = _age_seconds(last_heartbeat_at, now)
    if age is None or age > CHARGER_OFFLINE_AGE_S or ocpp_status == "Unavailable":
        return "offline"

    if ocpp_status == "Charging" or has_open_session:
        return "charging"

    return "idle"


def derive_vehicle_state(
    *,
    last_seen_at: Any,
    has_open_session: bool,
    has_active_schedule: bool,
    current_soc: Optional[float],
    required_soc: Optional[float],
    now: datetime,
) -> VehicleState:
    """Map runtime signals to the 6-state vehicle pill.

    Priority (top wins):
      1. ``offline`` — no telemetry or stale by ``VEHICLE_OFFLINE_AGE_S``.
      2. ``charging`` — open ``charging_sessions`` row exists.
      3. ``in_route`` — within an active schedule window.
      4. ``ready`` — ``current_soc >= required_soc`` (default 0.95 if none).
      5. ``at_risk`` — known SoC below threshold.
      6. ``unknown`` — charger hasn't reported SoC, so risk can't be assessed.
    """
    age = _age_seconds(last_seen_at, now)
    if age is None or age > VEHICLE_OFFLINE_AGE_S:
        return "offline"

    if has_open_session:
        return "charging"

    if has_active_schedule:
        return "in_route"

    if current_soc is None:
        return "unknown"

    threshold = required_soc if required_soc is not None else DEFAULT_DEPARTURE_SOC
    if current_soc >= threshold:
        return "ready"
    return "at_risk"


def format_charger_session(session: Optional[dict]) -> Optional[dict]:
    """Render a runtime session row as the ``current_session`` sub-object.

    Returns None when no open session exists. ``id_tag`` is the raw OCPP
    idTag the charger sent on StartTransaction, surfaced so the frontend
    can show "Unknown vehicle charging · RFID 0C923A35" when the
    cards-only authorization path didn't resolve a ``vehicle_id`` (no row
    in ``rfid_card_vehicle_assignments`` for this card).
    """
    if session is None:
        return None
    return {
        "session_id": str(session["session_id"]),
        "vehicle_id": session.get("vehicle_id"),
        "id_tag": session.get("id_tag"),
        "started_at": _isoformat(session.get("started_at")),
        "current_power_kw": _float_or_none(session.get("current_power_kw")),
        "current_soc": _float_or_none(session.get("current_soc")),
        "target_soc": _float_or_none(session.get("target_soc")),
        "estimated_end_at": _isoformat(session.get("estimated_end_at")),
    }


def format_charger_item(
    static_row: dict,
    *,
    connector_status: Optional[dict],
    open_session: Optional[dict],
    now: datetime,
    last_interaction_override: Optional[datetime] = None,
    telemetry_last_seen: Optional[datetime] = None,
) -> dict:
    """Build one charger response item from static + runtime data.

    ``last_interaction`` is the freshest of three independent signals, so a
    charger that is demonstrably alive on any one of them is never falsely
    marked offline:

      1. ``last_interaction_override`` — the in-memory ``LivenessHub`` cache,
         fed by every OCPP frame via pg_notify. Cold right after an API
         replica restart, and absent entirely when the pg_notify bridge is
         down.
      2. ``connector_status`` MAX — advances only on StatusNotification
         (connector state changes), so it lags for a charger that's steadily
         charging without changing state.
      3. ``telemetry_last_seen`` — MAX(``telemetry``.time), which gets a row
         on every MeterValues frame. This is the signal that keeps an
         actively-metering charger "online" even when (1) is unavailable and
         (2) is stale — the regression where a charging charger showed
         ``offline`` while MeterValues were flowing.

    Taking the max (not a priority fallback) means the strongest live signal
    always wins. Both ``last_interaction_at`` and the derived ``status`` are
    computed from the same combined value so REST stays self-consistent.
    """
    ocpp_status = connector_status.get("ocpp_status") if connector_status else None
    db_last_interaction = (
        (connector_status.get("last_interaction_at") or connector_status.get("last_heartbeat_at"))
        if connector_status
        else None
    )
    last_interaction = _latest(last_interaction_override, db_last_interaction, telemetry_last_seen)
    # Cap to now — charger-supplied MeterValues timestamps can be in the
    # future (clock skew), which would make age() negative and keep a
    # disconnected charger falsely online until wall-clock catches up.
    if last_interaction is not None and last_interaction > now:
        last_interaction = now
    status = derive_charger_status(
        ocpp_status=ocpp_status,
        last_heartbeat_at=last_interaction,
        has_open_session=open_session is not None,
        now=now,
    )

    last_interaction_iso = _isoformat(last_interaction)
    return {
        "id": static_row["id"],
        "depot_id": static_row["depot_id"],
        "ocpp_id": static_row["ocpp_id"],
        "display_name": static_row.get("display_name"),
        "vendor": static_row.get("vendor"),
        "model": static_row.get("model"),
        "serial_number": static_row.get("serial_number"),
        "firmware": static_row.get("firmware"),
        "rated_kw": _float_or_none(static_row.get("rated_kw")),
        "efficiency": _float_or_none(static_row.get("efficiency")),
        "connector_type": static_row.get("connector_type"),
        "connector_count": int(static_row.get("connector_count") or 1),
        "connector_ids": list(static_row.get("connector_ids") or []),
        "auth_required": bool(static_row.get("auth_required")),
        "status": status,
        "ocpp_connector_status": ocpp_status,
        "network_notes": static_row.get("network_notes"),
        "created_at": _isoformat(static_row.get("created_at")) or "",
        # Canonical field — frontend should read this. Source: most
        # recent connector_status.timestamp at API-response time;
        # superseded live by the `/depots/{id}/liveness/stream` SSE
        # stream once the WS handler emits a notification.
        "last_interaction_at": last_interaction_iso,
        # Transitional alias — drop after frontend rollout.
        "last_heartbeat_at": last_interaction_iso,
        "current_session": format_charger_session(open_session),
    }


def format_vehicle_next_departure(row: Optional[dict]) -> Optional[dict]:
    """Render a future ``schedules`` row as the ``next_departure`` sub-object."""
    if row is None:
        return None
    return {
        "schedule_id": str(row["schedule_id"]),
        "route_id": row.get("route_id"),
        "departure_time": _isoformat(row.get("departure_time")) or "",
        "return_time": _isoformat(row.get("return_time")) or "",
        "required_soc": _float_or_none(row.get("required_soc")),
        "energy_kwh": _float_or_none(row.get("energy_kwh")),
    }


def format_vehicle_item(
    static_row: dict,
    *,
    telemetry: Optional[dict],
    open_session: Optional[dict],
    next_departure: Optional[dict],
    active_schedule: Optional[dict],
    charger_id_by_ocpp_id: dict[str, str],
    now: datetime,
) -> dict:
    """Build one vehicle response item from static + runtime data."""
    last_seen_at = telemetry.get("last_seen_at") if telemetry else None
    current_soc = _float_or_none(telemetry.get("current_soc")) if telemetry else None
    current_power_kw = _float_or_none(telemetry.get("current_power_kw")) if telemetry else None
    required_soc = _float_or_none(next_departure.get("required_soc")) if next_departure else None

    state = derive_vehicle_state(
        last_seen_at=last_seen_at,
        has_open_session=open_session is not None,
        has_active_schedule=active_schedule is not None,
        current_soc=current_soc,
        required_soc=required_soc,
        now=now,
    )

    connected_charger_id: Optional[str] = None
    connected_session_id: Optional[str] = None
    if open_session is not None:
        connected_session_id = str(open_session["session_id"])
        ocpp_id = open_session.get("ocpp_id")
        if ocpp_id is not None:
            connected_charger_id = charger_id_by_ocpp_id.get(ocpp_id)

    return {
        "id": static_row["id"],
        "depot_id": static_row["depot_id"],
        "external_id": static_row["external_id"],
        "display_name": static_row.get("display_name"),
        "vehicle_type": static_row.get("vehicle_type"),
        "vin": static_row.get("vin"),
        "license_plate": static_row.get("license_plate"),
        "id_tag": static_row.get("id_tag"),
        "battery_capacity_kwh": _float_or_none(static_row.get("battery_capacity_kwh")),
        "max_charge_rate_kw": _float_or_none(static_row.get("max_charge_rate_kw")),
        "max_discharge_rate_kw": _float_or_none(static_row.get("max_discharge_rate_kw")),
        "v2g_capable": bool(static_row.get("v2g_capable")),
        "make": static_row.get("make"),
        "model": static_row.get("model"),
        "year": static_row.get("year"),
        "status": static_row.get("status"),
        "created_at": _isoformat(static_row.get("created_at")) or "",
        "current_state": {
            "state": state,
            "current_soc": current_soc,
            "current_power_kw": current_power_kw,
            "connected_charger_id": connected_charger_id,
            "connected_session_id": connected_session_id,
            "last_seen_at": _isoformat(last_seen_at),
        },
        "next_departure": format_vehicle_next_departure(next_departure),
    }


def _float_or_none(value: Any) -> Optional[float]:
    """Coerce numeric / Decimal / None to float (or None) without raising."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
