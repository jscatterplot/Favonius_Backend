"""Optimization readiness validation and input snapshot construction.

Reference: migration 019_optimization_input_snapshots.sql, PRD Section 9.4
(building load required for grid power calc).

The readiness checker inspects the depot configuration plus the assembled
``DepotState`` and produces:

    1. A :class:`ReadinessReport` describing whether the optimization should
       run as-is, run in degraded mode (with explicit assumptions recorded),
       or be refused outright.
    2. A :class:`OptimizationInputSnapshot` that captures the full input
       bundle for persistence and replay.

Building load is the canonical "degrade" trigger: per PRD 9.4 it is required
for the grid-power calculation, but historic deployments still need to keep
running when meter data drops out. Rather than silently substituting the
forecast pattern (the previous behaviour of ``StateAssembler``), the checker
now records the substitution as an explicit assumption and downgrades the
optimization run status to ``degraded``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from ..models import (
    DepotConfig,
    DepotState,
    OptimizationInputSnapshot,
    ReadinessReport,
)

logger = logging.getLogger(__name__)


# Sources for the building_load column in the snapshot table.
BUILDING_LOAD_METER = "meter"
BUILDING_LOAD_FORECAST = "forecast_fallback"
BUILDING_LOAD_ABSENT = "absent"


def evaluate_readiness(
    config: DepotConfig,
    state: DepotState,
    *,
    building_load_source: str,
    schedules_present: bool,
) -> ReadinessReport:
    """Assess whether the optimization inputs are complete.

    Args:
        config: Depot configuration loaded from the static DB.
        state: Assembled depot state (post any in-memory fallbacks).
        building_load_source: One of ``meter`` / ``forecast_fallback`` /
            ``absent`` — describes where the values in
            ``state.building_power`` came from.
        schedules_present: True if at least one route schedule was loaded
            from the database for the horizon. The state assembler
            collapses missing schedules into "always available", so this
            cannot be inferred from ``state`` alone.

    Returns:
        A :class:`ReadinessReport`. ``status='not_ready'`` is a hard
        veto; ``status='degraded'`` means the run will proceed under
        explicit assumptions.
    """
    missing: list[str] = []
    degraded: list[str] = []
    assumptions: dict[str, object] = {}

    # Hard prerequisites — the solver cannot do anything useful without these.
    if not config.vehicle_capacities:
        missing.append("vehicles")
    if not config.charger_groups or sum(config.charger_groups.values()) == 0:
        missing.append("chargers")
    if not state.prices:
        missing.append("prices")
    if not schedules_present:
        missing.append("schedules")
    if not config.charger_vehicle_access:
        # Without an access matrix we can't enforce physical pairing
        # constraints. Treat as a hard miss rather than silently
        # assuming any-charger-fits-any-vehicle.
        missing.append("charger_vehicle_access")

    # Soft prerequisites — degrade rather than block.
    if building_load_source == BUILDING_LOAD_FORECAST:
        degraded.append("building_load_meter_unavailable")
        assumptions["building_load"] = {
            "source": BUILDING_LOAD_FORECAST,
            "note": "meter data missing; substituted business-hours forecast pattern",
        }
    elif building_load_source == BUILDING_LOAD_ABSENT:
        # Forecast was not even applied (e.g. n_steps=0). Building load is
        # required per PRD 9.4 — refuse.
        missing.append("building_load")

    # Telemetry: the assembler defaults missing SoCs to 0.5; record that
    # as an assumption so replays can flag stale state. We only know "any
    # vehicle has telemetry" by checking whether all SoCs are exactly 0.5
    # — that's a heuristic so we just record the count.
    if state.vehicle_socs:
        defaulted = [
            vid
            for vid, soc in state.vehicle_socs.items()
            if soc == 0.5
        ]
        if defaulted and len(defaulted) == len(state.vehicle_socs):
            degraded.append("telemetry_all_defaulted")
            assumptions["telemetry"] = {
                "source": "default_soc",
                "default_value": 0.5,
                "vehicles": defaulted,
            }

    if missing:
        status = "not_ready"
    elif degraded:
        status = "degraded"
    else:
        status = "ready"

    return ReadinessReport(
        status=status,
        missing_inputs=missing,
        degraded_reasons=degraded,
        assumptions=assumptions,
        building_load_source=building_load_source,
    )


def _serialize_dt(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_schedule_row(row: dict) -> dict:
    return {k: _serialize_dt(v) for k, v in row.items()}


def build_snapshot(
    *,
    depot_id: str | UUID,
    organization_id: Optional[str | UUID],
    config: DepotConfig,
    state: DepotState,
    horizon_start: datetime,
    horizon_end: datetime,
    schedules: list[dict],
    weather_features: Optional[list[dict]] = None,
    weather_forecast_id: Optional[UUID] = None,
    readiness: ReadinessReport,
    depot_metadata: Optional[dict] = None,
) -> OptimizationInputSnapshot:
    """Build a replayable input snapshot from the assembled state.

    Pure function — no IO. The persistence layer turns the returned
    snapshot into a row in ``optimization_input_snapshots``.
    """
    depot_uuid = depot_id if isinstance(depot_id, UUID) else UUID(str(depot_id))
    org_uuid: Optional[UUID]
    if organization_id is None:
        org_uuid = None
    elif isinstance(organization_id, UUID):
        org_uuid = organization_id
    else:
        org_uuid = UUID(str(organization_id))

    depot_payload: dict[str, object] = {
        "depot_id": str(depot_uuid),
        "max_site_power_kw": config.max_site_power,
        "delta_t_hours": config.delta_t,
        "n_timesteps": config.n_timesteps,
    }
    if depot_metadata:
        depot_payload.update(depot_metadata)

    vehicles_payload = [
        {
            "vehicle_id": vid,
            "battery_kwh": config.vehicle_capacities.get(vid),
            "max_charge_kw": config.vehicle_max_charge_kw.get(vid),
            "current_soc": state.vehicle_socs.get(vid),
            "energy_requirement_kwh": state.energy_requirements.get(vid),
            "departure_timestep": state.departure_times.get(vid),
            "departure_soc_min": state.vehicle_departure_soc_min.get(vid),
            "departure_soc_max": state.vehicle_departure_soc_max.get(vid),
            "priority": state.vehicle_priorities.get(vid),
        }
        for vid in sorted(config.vehicle_capacities.keys())
    ]

    chargers_payload = {
        "groups": {str(k): v for k, v in config.charger_groups.items()},
        "efficiency": config.charger_efficiency,
        "total": sum(config.charger_groups.values()),
    }

    access_payload = {
        charger_id: sorted(vehicle_ids)
        for charger_id, vehicle_ids in config.charger_vehicle_access.items()
    }

    schedules_payload = [_serialize_schedule_row(s) for s in schedules]

    incoming_payload = [
        {
            "vehicle_id": str(iv.vehicle_id),
            "external_id": iv.external_id,
            "expected_soc": iv.expected_soc,
            "arrival_time": iv.arrival_time.isoformat(),
            "battery_kwh": iv.battery_kwh,
            "max_charge_kw": iv.max_charge_kw,
            "origin_depot_id": str(iv.origin_depot_id),
        }
        for iv in state.incoming_vehicles
    ]

    n_steps = len(state.building_power)
    avg_load = (
        sum(state.building_power) / n_steps if n_steps else 0.0
    )
    building_payload = {
        "source": readiness.building_load_source,
        "values_kw": list(state.building_power),
        "average_kw": avg_load,
        "n_timesteps": n_steps,
    }

    weather_payload = list(weather_features or [])
    # weather_forecast_id is meaningful only when we actually captured
    # features. The DB column has ON DELETE SET NULL so an empty
    # feature set + NULL FK is the canonical "no weather" state.
    forecast_id = weather_forecast_id if weather_payload else None

    return OptimizationInputSnapshot(
        snapshot_id=uuid4(),
        depot_id=depot_uuid,
        organization_id=org_uuid,
        captured_at=datetime.utcnow(),
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        readiness=readiness,
        depot=depot_payload,
        vehicles=vehicles_payload,
        chargers=chargers_payload,
        charger_vehicle_access=access_payload,
        schedules=schedules_payload,
        prices=list(state.prices),
        telemetry=dict(state.vehicle_socs),
        building_load=building_payload,
        weather_features=weather_payload,
        weather_forecast_id=forecast_id,
        incoming_vehicles=incoming_payload,
    )


def snapshot_to_payload(snapshot: OptimizationInputSnapshot) -> dict[str, object]:
    """Build the JSONB payload column from a snapshot.

    Kept separate from the row-level columns (depot_id, run_id, etc.) so
    the payload itself is self-contained for replay.
    """
    return {
        "schema": snapshot.payload_schema,
        "depot": snapshot.depot,
        "organization_id": (
            str(snapshot.organization_id) if snapshot.organization_id else None
        ),
        "horizon": {
            "start": snapshot.horizon_start.isoformat(),
            "end": snapshot.horizon_end.isoformat(),
        },
        "readiness": {
            "status": snapshot.readiness.status,
            "missing_inputs": list(snapshot.readiness.missing_inputs),
            "degraded_reasons": list(snapshot.readiness.degraded_reasons),
            "assumptions": dict(snapshot.readiness.assumptions),
            "building_load_source": snapshot.readiness.building_load_source,
        },
        "vehicles": snapshot.vehicles,
        "chargers": snapshot.chargers,
        "charger_vehicle_access": snapshot.charger_vehicle_access,
        "schedules": snapshot.schedules,
        "prices": snapshot.prices,
        "telemetry": snapshot.telemetry,
        "building_load": snapshot.building_load,
        "weather_features": snapshot.weather_features,
        "weather_forecast_id": (
            str(snapshot.weather_forecast_id)
            if snapshot.weather_forecast_id
            else None
        ),
        "incoming_vehicles": snapshot.incoming_vehicles,
    }


def replay_payload(payload: dict[str, object]) -> dict[str, object]:
    """Round-trip helper: validate that a stored payload contains every
    field needed to reconstruct an optimization input.

    Returns the payload unchanged on success; raises ``ValueError`` listing
    missing keys otherwise. The set of required keys is the contract that
    persistence + tests both enforce.
    """
    required = {
        "schema",
        "depot",
        "horizon",
        "readiness",
        "vehicles",
        "chargers",
        "charger_vehicle_access",
        "schedules",
        "prices",
        "telemetry",
        "building_load",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"snapshot payload missing required keys: {missing}")
    readiness = payload["readiness"]
    if not isinstance(readiness, dict):
        raise ValueError("snapshot payload 'readiness' must be a dict")
    if "building_load_source" not in readiness:
        raise ValueError(
            "snapshot payload 'readiness.building_load_source' is required"
        )
    return payload
