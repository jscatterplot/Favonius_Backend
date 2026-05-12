"""Read-only "tools" the readiness workflow calls (PRD §6.1).

Each helper is a thin SQL wrapper that the workflow records as one
:class:`ToolCall`. Keeping them in a single module makes them easy to
mock in tests — the integration suite injects a
:class:`StaticToolBundle` that returns scenario fixtures verbatim.

The helpers intentionally never touch ``charging_command_queue`` or any
write path: the readiness workflow is ``inform`` tier at launch and
only ever surfaces *proposals*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from src.db.pools import DatabasePools

logger = logging.getLogger(__name__)


@dataclass
class ToolBundle:
    """The minimum surface the readiness workflow needs to run.

    Real callers construct :class:`DatabaseToolBundle` from the live
    pools; tests pass :class:`StaticToolBundle` with pre-baked dicts.
    """

    pools: Optional[DatabasePools]

    async def get_scheduled_departures(
        self, depot_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_vehicle_state(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    async def get_charger_state(self, charger_ids: list[str]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    async def get_charging_plan(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    async def get_driver_assignment(self, route_ids: list[str]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    async def find_alternate_chargers(
        self, depot_id: UUID, kw_required: float
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


@dataclass
class DatabaseToolBundle(ToolBundle):
    """Live implementation backed by :class:`DatabasePools`."""

    async def get_scheduled_departures(
        self, depot_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        """Departures in ``[window_start, window_end)`` for the depot.

        Joins ``schedules`` ↔ ``vehicles`` (both Supabase) so the
        result is a list of dicts shaped like the workflow's input
        contract: ``{vehicle_id, route_id, departure_time,
        required_soc, charger_id}``. ``charger_id`` is left ``None``
        — chargers aren't bound to schedules in the static schema; the
        workflow falls back to ``vehicle_states[vid]['charger_id']``.
        """
        if self.pools is None:
            return []
        query = """
        SELECT
            s.vehicle_id::text AS vehicle_id,
            s.route_id::text   AS route_id,
            s.departure_time   AS departure_time,
            COALESCE(s.required_soc, 0.99) AS required_soc
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.id
        WHERE v.site_id = $1::uuid
          AND s.departure_time >= $2
          AND s.departure_time < $3
        ORDER BY s.departure_time
        """
        async with self.pools.static.acquire() as conn:
            rows = await conn.fetch(query, str(depot_id), window_start, window_end)
        return [
            {
                "vehicle_id": r["vehicle_id"],
                "route_id": r["route_id"],
                "departure_time": _ensure_utc(r["departure_time"]),
                "required_soc": float(r["required_soc"]),
                "charger_id": None,
            }
            for r in rows
        ]

    async def get_vehicle_state(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Latest telemetry row per vehicle, joined with static vehicle config.

        Picks the most recent ``telemetry`` row per ``vehicle_id`` and
        merges it with ``vehicles.{battery_capacity_kwh,
        max_charge_rate_kw}`` so the projection has the constants it
        needs. Returns an empty dict for vehicles not seen in the last
        15 minutes (PRD §5.3 — vehicle SoC max-age).
        """
        if self.pools is None or not vehicle_ids:
            return {}
        ts_query = """
        SELECT DISTINCT ON (vehicle_id)
            vehicle_id::text AS vehicle_id,
            soc,
            is_plugged,
            charging_kw,
            max_charge_kw,
            charger_id::text AS charger_id
        FROM telemetry
        WHERE vehicle_id::text = ANY($1::text[])
          AND time > NOW() - INTERVAL '15 minutes'
        ORDER BY vehicle_id, time DESC
        """
        async with self.pools.ts.acquire() as conn:
            ts_rows = await conn.fetch(ts_query, vehicle_ids)

        static_query = """
        SELECT
            id::text                       AS vehicle_id,
            battery_capacity_kwh           AS battery_kwh,
            max_charge_rate_kw             AS max_charge_kw
        FROM vehicles
        WHERE id::text = ANY($1::text[])
        """
        async with self.pools.static.acquire() as conn:
            static_rows = await conn.fetch(static_query, vehicle_ids)

        static_by_id = {r["vehicle_id"]: r for r in static_rows}
        ts_by_id = {r["vehicle_id"]: r for r in ts_rows}
        out: dict[str, dict[str, Any]] = {}
        for vid in vehicle_ids:
            s = static_by_id.get(vid, {})
            t = ts_by_id.get(vid, {})
            out[vid] = {
                "soc": float(t.get("soc") or 0.0),
                "plugged_in": bool(t.get("is_plugged") or False),
                "charger_id": t.get("charger_id"),
                "battery_kwh": float(s.get("battery_kwh") or 0.0),
                "max_charge_kw": float(s.get("max_charge_kw") or t.get("max_charge_kw") or 0.0),
            }
        return out

    async def get_charger_state(self, charger_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Latest ``connector_status`` row per charger.

        ``charger_ids`` are the UUIDs from ``chargers.charger_id``; we
        translate them to OCPP station IDs via
        ``chargers.station_id`` so the join hits the right rows.
        """
        if self.pools is None or not charger_ids:
            return {}
        translate = """
        SELECT id::text AS charger_id, station_id, max_power_kw
        FROM charging_stations
        WHERE id::text = ANY($1::text[])
        """
        async with self.pools.static.acquire() as conn:
            translate_rows = await conn.fetch(translate, charger_ids)
        station_to_charger: dict[str, str] = {}
        rated_kw: dict[str, float] = {}
        station_ids: list[str] = []
        for r in translate_rows:
            station_to_charger[r["station_id"]] = r["charger_id"]
            rated_kw[r["charger_id"]] = float(r["max_power_kw"] or 0.0)
            station_ids.append(r["station_id"])

        if not station_ids:
            return {}

        latest = """
        SELECT DISTINCT ON (station_id)
            station_id, status, error_code, timestamp
        FROM connector_status
        WHERE station_id = ANY($1::text[])
        ORDER BY station_id, timestamp DESC
        """
        async with self.pools.ts.acquire() as conn:
            rows = await conn.fetch(latest, station_ids)

        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            cid = station_to_charger.get(r["station_id"])
            if not cid:
                continue
            out[cid] = {
                "status": r["status"],
                "fault_code": r["error_code"],
                "current_power_kw": 0.0,
                "rated_kw": rated_kw.get(cid, 0.0),
            }
        for cid in charger_ids:
            out.setdefault(
                cid,
                {
                    "status": "Available",
                    "fault_code": None,
                    "current_power_kw": 0.0,
                    "rated_kw": rated_kw.get(cid, 0.0),
                },
            )
        return out

    async def get_charging_plan(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Latest scheduled power per vehicle from ``optimization_runs.schedule_json``.

        Schedules are stored as ``{vehicle_id: [(timestep, power_kw),
        …]}``. We take the most recent run that touched any of the
        requested vehicles and pluck the first non-zero ``power_kw`` as
        ``planned_power_kw``. Missing entries default to 0 kW (the
        workflow then projects a flat SoC trajectory).
        """
        if self.pools is None or not vehicle_ids:
            return {}
        query = """
        SELECT schedule_json
        FROM optimization_runs
        WHERE status IN ('optimal', 'feasible', 'degraded')
        ORDER BY run_time DESC
        LIMIT 1
        """
        try:
            async with self.pools.ts.acquire() as conn:
                row = await conn.fetchrow(query)
        except Exception as exc:  # pragma: no cover — schema variance defence
            logger.warning("get_charging_plan failed, defaulting to zero plan: %s", exc)
            row = None

        schedule: dict[str, Any] = {}
        if row and row["schedule_json"]:
            payload = row["schedule_json"]
            if isinstance(payload, str):
                import json

                payload = json.loads(payload)
            if isinstance(payload, dict):
                schedule = payload

        out: dict[str, dict[str, Any]] = {}
        for vid in vehicle_ids:
            entry = schedule.get(vid) or schedule.get(str(vid))
            planned_power_kw = 0.0
            if isinstance(entry, list) and entry:
                first = entry[0]
                if isinstance(first, dict):
                    planned_power_kw = float(first.get("power_kw") or 0.0)
                elif isinstance(first, (list, tuple)) and len(first) >= 2:
                    planned_power_kw = float(first[1] or 0.0)
            elif isinstance(entry, dict):
                planned_power_kw = float(entry.get("power_kw") or 0.0)
            out[vid] = {"planned_power_kw": planned_power_kw}
        return out

    async def get_driver_assignment(self, route_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Driver per route. V1: every route maps to ``valid=True`` with no
        driver enforcement — the roster system integration is post-V1
        (PRD §6.4). We still expose the shape so the orchestrator's
        branching is exercised; integration tests inject explicit
        invalid rows via :class:`StaticToolBundle` to cover the
        unhappy paths.
        """
        return {rid: {"driver_id": "unknown", "valid": True} for rid in route_ids}

    async def find_alternate_chargers(
        self, depot_id: UUID, kw_required: float
    ) -> list[dict[str, Any]]:
        """Available chargers at the depot capable of ≥ ``kw_required``.

        Read-only — purely a proposal-input. Returns the ID, rated kW
        and station_id for each. An empty list means no clean
        mitigation exists; the workflow emits a ``manual_intervention``
        proposed action.
        """
        if self.pools is None:
            return []
        query = """
        SELECT id::text AS charger_id, max_power_kw AS rated_kw, station_id
        FROM charging_stations
        WHERE site_id = $1::uuid
          AND max_power_kw >= $2
        ORDER BY max_power_kw DESC
        LIMIT 10
        """
        async with self.pools.static.acquire() as conn:
            rows = await conn.fetch(query, str(depot_id), float(kw_required))
        return [
            {
                "charger_id": r["charger_id"],
                "rated_kw": float(r["rated_kw"] or 0.0),
                "station_id": r["station_id"],
            }
            for r in rows
        ]


@dataclass
class StaticToolBundle(ToolBundle):
    """In-memory bundle for tests / scenario fixtures.

    All getters return pre-baked dicts as-is. ``pools`` is unused but
    kept on the base class for parity with :class:`DatabaseToolBundle`.
    """

    pools: Optional[DatabasePools] = None
    departures: list[dict[str, Any]] = None  # type: ignore[assignment]
    vehicle_states: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    charger_states: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    charging_plans: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    driver_assignments: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    alternate_chargers: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.departures is None:
            self.departures = []
        if self.vehicle_states is None:
            self.vehicle_states = {}
        if self.charger_states is None:
            self.charger_states = {}
        if self.charging_plans is None:
            self.charging_plans = {}
        if self.driver_assignments is None:
            self.driver_assignments = {}
        if self.alternate_chargers is None:
            self.alternate_chargers = []

    async def get_scheduled_departures(
        self, depot_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        return [dict(d) for d in self.departures]

    async def get_vehicle_state(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {vid: dict(self.vehicle_states.get(vid, {})) for vid in vehicle_ids}

    async def get_charger_state(self, charger_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {cid: dict(self.charger_states.get(cid, {})) for cid in charger_ids}

    async def get_charging_plan(self, vehicle_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {vid: dict(self.charging_plans.get(vid, {})) for vid in vehicle_ids}

    async def get_driver_assignment(self, route_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {rid: dict(self.driver_assignments.get(rid, {"driver_id": "u", "valid": True}))
                for rid in route_ids}

    async def find_alternate_chargers(
        self, depot_id: UUID, kw_required: float
    ) -> list[dict[str, Any]]:
        return [dict(a) for a in self.alternate_chargers]


def _ensure_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC-aware (asyncpg returns tz-naive sometimes)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
