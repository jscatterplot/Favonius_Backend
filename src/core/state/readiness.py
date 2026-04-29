"""Optimization pre-flight readiness evaluation.

The ``GET /depots/{depot_id}/optimization/readiness`` endpoint and the
``POST /optimize`` gate both call :func:`evaluate_readiness` to determine
whether an optimization run can proceed and, if so, whether it will run with
assumptions.

The contract returned here is what the frontend's Optimization Readiness panel
renders. The string enums in :data:`MISSING_INPUT_VALUES`,
:data:`DEGRADED_REASON_VALUES`, and :data:`BUILDING_LOAD_SOURCE_VALUES` are
treated as a closed set by the UI — do not introduce new values without
coordinating a contract change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from ...db.pools import DatabasePools

logger = logging.getLogger(__name__)


# --- Public enums (closed sets — mirror the API contract) -------------------

MissingInput = Literal[
    "vehicles",
    "chargers",
    "prices",
    "schedules",
    "charger_vehicle_access",
    "building_load",
]

DegradedReason = Literal[
    "building_load_meter_unavailable",
    "telemetry_all_defaulted",
]

BuildingLoadSource = Literal["meter", "forecast_fallback", "absent"]

ReadinessStatus = Literal["ready", "degraded", "not_ready"]


DEFAULT_SOC = 0.5  # mirrors StateAssembler._get_vehicle_socs() fallback
TELEMETRY_FRESHNESS_MINUTES = 15  # PRD §9 — vehicle SoC max age
PRICE_LOOKBACK_HOURS = 24  # mirrors existing setup readiness check
BUILDING_LOAD_LOOKBACK_HOURS = 24  # mirrors existing setup readiness check


@dataclass
class ReadinessResult:
    """Internal readiness payload, serialized to ReadinessResponse by the API."""

    depot_id: str
    status: ReadinessStatus
    missing_inputs: list[MissingInput]
    degraded_reasons: list[DegradedReason]
    building_load_source: BuildingLoadSource
    horizon_hours: int
    captured_at: datetime
    assumptions: dict = field(default_factory=dict)
    snapshot_id: Optional[str] = None


async def evaluate_readiness(
    pools: DatabasePools,
    depot_id: str,
    horizon_hours: int,
    *,
    persist: bool = False,
) -> ReadinessResult:
    """Evaluate whether the depot has the inputs needed for an optimization run.

    Returns a :class:`ReadinessResult` whose fields map 1:1 onto the public
    ``ReadinessResponse`` shape. When ``persist=True`` an audit row is written
    to ``optimization_input_snapshots`` and ``snapshot_id`` is populated.

    Database errors and timeouts propagate to the caller so the API layer can
    decide between 503 and 500.
    """

    captured_at = datetime.now(timezone.utc)

    inputs = await _check_inputs(pools, depot_id)
    telemetry = await _check_telemetry(pools, depot_id)
    building_load_source = _resolve_building_load_source(
        has_meter_data=inputs.has_building_load_meter,
        configured_type=inputs.building_load_configured_type,
    )

    missing_inputs: list[MissingInput] = []
    if not inputs.has_vehicles:
        missing_inputs.append("vehicles")
    if not inputs.has_chargers:
        missing_inputs.append("chargers")
    if not inputs.has_charger_vehicle_access:
        missing_inputs.append("charger_vehicle_access")
    if not inputs.has_schedules:
        missing_inputs.append("schedules")
    if not inputs.has_prices:
        missing_inputs.append("prices")
    if building_load_source == "absent":
        missing_inputs.append("building_load")

    degraded_reasons: list[DegradedReason] = []
    assumptions: dict = {}

    if building_load_source == "forecast_fallback":
        degraded_reasons.append("building_load_meter_unavailable")
        assumptions["building_load"] = {
            "source": "forecast_fallback",
            "note": (
                "Building load meter data is not available for the selected "
                "horizon; the optimizer will use the forecast pattern for "
                "site load."
            ),
        }

    # Telemetry-defaulted detection only matters when we actually have vehicles
    # to optimize. If vehicles are missing, that's already the blocker.
    if inputs.has_vehicles and telemetry.all_defaulted:
        degraded_reasons.append("telemetry_all_defaulted")
        assumptions["telemetry"] = {
            "source": "default_soc",
            "default_value": DEFAULT_SOC,
            "vehicles": telemetry.defaulted_vehicle_ids,
        }

    if missing_inputs:
        status: ReadinessStatus = "not_ready"
    elif degraded_reasons:
        status = "degraded"
    else:
        status = "ready"

    result = ReadinessResult(
        depot_id=depot_id,
        status=status,
        missing_inputs=missing_inputs,
        degraded_reasons=degraded_reasons,
        building_load_source=building_load_source,
        horizon_hours=horizon_hours,
        captured_at=captured_at,
        assumptions=assumptions,
        snapshot_id=None,
    )

    if persist:
        result.snapshot_id = await _persist_snapshot(pools, result)

    return result


# --- Helpers ----------------------------------------------------------------


@dataclass
class _InputAvailability:
    has_vehicles: bool
    has_chargers: bool
    has_charger_vehicle_access: bool
    has_schedules: bool
    has_prices: bool
    has_building_load_meter: bool
    building_load_configured_type: Optional[str]


@dataclass
class _TelemetryStatus:
    all_defaulted: bool
    defaulted_vehicle_ids: list[str]


async def _check_inputs(pools: DatabasePools, depot_id: str) -> _InputAvailability:
    """Probe the static + timeseries DBs for input availability."""

    async with pools.static.acquire() as conn:
        has_vehicles = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM vehicles WHERE depot_id = $1::uuid)",
                depot_id,
            )
        )
        has_chargers = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM chargers WHERE depot_id = $1::uuid)",
                depot_id,
            )
        )
        has_charger_vehicle_access = bool(
            await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM charger_vehicle_access cva
                    JOIN chargers c ON c.charger_id = cva.charger_id
                    WHERE c.depot_id = $1::uuid AND cva.is_accessible = TRUE
                )
                """,
                depot_id,
            )
        )
        has_schedules = bool(
            await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM schedules s
                    JOIN vehicles v ON v.vehicle_id = s.vehicle_id
                    WHERE v.depot_id = $1::uuid
                      AND s.departure_time >= NOW() - INTERVAL '1 hour'
                )
                """,
                depot_id,
            )
        )
        building_load_configured = await conn.fetchval(
            "SELECT building_load_source FROM depots WHERE depot_id = $1::uuid",
            depot_id,
        )

    has_prices = False
    has_building_load_meter = False
    if pools.ts is not None:
        async with pools.ts.acquire() as conn:
            has_prices = bool(
                await conn.fetchval(
                    f"""
                    SELECT EXISTS(
                        SELECT 1 FROM prices
                        WHERE depot_id = $1::uuid
                          AND time >= NOW() - INTERVAL '{PRICE_LOOKBACK_HOURS} hours'
                    )
                    """,
                    depot_id,
                )
            )
            # Only "meter" source counts as live-meter data; "forecast" / "api"
            # rows are surfaced as forecast_fallback.
            has_building_load_meter = bool(
                await conn.fetchval(
                    f"""
                    SELECT EXISTS(
                        SELECT 1 FROM building_load
                        WHERE depot_id = $1::uuid
                          AND time >= NOW() - INTERVAL '{BUILDING_LOAD_LOOKBACK_HOURS} hours'
                          AND source = 'meter'
                    )
                    """,
                    depot_id,
                )
            )

    configured_type = _extract_building_load_type(building_load_configured)

    return _InputAvailability(
        has_vehicles=has_vehicles,
        has_chargers=has_chargers,
        has_charger_vehicle_access=has_charger_vehicle_access,
        has_schedules=has_schedules,
        has_prices=has_prices,
        has_building_load_meter=has_building_load_meter,
        building_load_configured_type=configured_type,
    )


async def _check_telemetry(pools: DatabasePools, depot_id: str) -> _TelemetryStatus:
    """Detect whether SoC values would all be defaulted for this depot."""

    if pools.ts is None:
        return _TelemetryStatus(all_defaulted=False, defaulted_vehicle_ids=[])

    async with pools.static.acquire() as static_conn:
        vehicle_rows = await static_conn.fetch(
            "SELECT vehicle_id FROM vehicles WHERE depot_id = $1::uuid",
            depot_id,
        )
    vehicle_ids = [str(row["vehicle_id"]) for row in vehicle_rows]
    if not vehicle_ids:
        return _TelemetryStatus(all_defaulted=False, defaulted_vehicle_ids=[])

    async with pools.ts.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT DISTINCT vehicle_id::text AS vehicle_id
            FROM telemetry
            WHERE depot_id = $1::uuid
              AND time >= NOW() - INTERVAL '{TELEMETRY_FRESHNESS_MINUTES} minutes'
              AND soc IS NOT NULL
            """,
            depot_id,
        )
    fresh_ids = {row["vehicle_id"] for row in rows}
    defaulted = [vid for vid in vehicle_ids if vid not in fresh_ids]
    all_defaulted = len(defaulted) == len(vehicle_ids)
    return _TelemetryStatus(all_defaulted=all_defaulted, defaulted_vehicle_ids=defaulted)


def _extract_building_load_type(raw: object) -> Optional[str]:
    """Pull the ``type`` field out of ``depots.building_load_source`` JSON."""

    if raw is None:
        return None
    if isinstance(raw, dict):
        value = raw.get("type")
        return str(value) if value is not None else None
    if isinstance(raw, str):
        # Some drivers return JSONB columns as raw strings; tolerate that.
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            value = parsed.get("type")
            return str(value) if value is not None else None
    return None


def _resolve_building_load_source(
    *,
    has_meter_data: bool,
    configured_type: Optional[str],
) -> BuildingLoadSource:
    """Map (live-meter availability, configured source type) to the API enum."""

    if has_meter_data:
        return "meter"
    # Depots configured with "none" or with no setup metadata yet can't fall back.
    if configured_type in (None, "none"):
        return "absent"
    return "forecast_fallback"


async def _persist_snapshot(pools: DatabasePools, result: ReadinessResult) -> str:
    """Insert an audit row and return its snapshot_id."""

    async with pools.static.acquire() as conn:
        snapshot_id = await conn.fetchval(
            """
            INSERT INTO optimization_input_snapshots (
                depot_id, captured_at, horizon_hours, status,
                missing_inputs, degraded_reasons, assumptions,
                building_load_source
            ) VALUES (
                $1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8
            )
            RETURNING snapshot_id
            """,
            result.depot_id,
            result.captured_at,
            result.horizon_hours,
            result.status,
            json.dumps(result.missing_inputs),
            json.dumps(result.degraded_reasons),
            json.dumps(result.assumptions),
            result.building_load_source,
        )
    return str(snapshot_id)
