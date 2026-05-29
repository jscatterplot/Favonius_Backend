"""Shared snapshot loader for the deterministic-intent golden gates.

The readiness and savings fast-path intents answer from real SQL (no LLM),
so their golden gates seed a ``graph_snapshot`` into the same TimescaleDB +
Supabase test pair the agent-SQL gate uses, then call the intent handler and
assert the rendered answer. This module is the loader + a thin
``asyncpg.Pool``-shaped facade over a transaction-bound connection.

It is intentionally separate from ``test_agent_sql_golden.py``'s loader: the
intents need tables that gate doesn't seed (``schedules``, ``telemetry``,
``vehicle_telemetry``, and a real ``optimization_runs.schedule_json``), and
keeping the proven SQL-gate loader untouched avoids destabilising it.

All timestamps may be ISO-8601 strings or ``±<n>{s,m,h,d}`` offsets relative
to the scenario's ``scenario_now`` (so "5 minutes ago" is ``-5m``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

_DURATION_RE = re.compile(r"^([+-])(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def coerce_uuid(value: Any, *, field_name: str = "") -> UUID:
    """Coerce ``value`` to ``UUID``; ``field_name`` adds context on a bad value."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError) as exc:
        loc = f"{field_name}: " if field_name else ""
        raise ValueError(f"{loc}invalid UUID {value!r}") from exc


def maybe_uuid(value: Any, *, field_name: str = "") -> Optional[UUID]:
    return None if value is None else coerce_uuid(value, field_name=field_name)


def parse_scenario_now(raw: Any) -> datetime:
    if raw is None:
        return datetime(2026, 5, 28, 9, 0, 0, tzinfo=timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_dt(value: Any, scenario_now: datetime) -> datetime:
    """ISO string / numeric-second offset / ``±N{smhd}`` duration → UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return scenario_now + timedelta(seconds=float(value))
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            sign, magnitude, unit = match.groups()
            seconds = int(magnitude) * _UNIT_SECONDS[unit]
            return scenario_now + timedelta(seconds=(-seconds if sign == "-" else seconds))
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"unsupported time value {value!r}")


# ── asyncpg.Pool-shaped facade over one transaction-bound connection ─────────


class _AcquireCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class TxPool:
    """Hands the intent handler the scenario's transaction-bound connection.

    The handlers do ``async with pool.acquire() as conn`` (audit writers) and
    direct ``pool.fetch(...)`` (the intent SQL); both land on the one bound
    connection so the seeded snapshot is visible and everything rolls back.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._conn.execute(query, *args)


# ── Loaders ──────────────────────────────────────────────────────────────────


async def load_static(
    conn: Any, snapshot: dict[str, Any], default_org: UUID, scenario_now: datetime
) -> None:
    """Load depots / vehicles / drivers / chargers / schedules into Supabase test DB."""
    orgs = {default_org}
    for depot in snapshot.get("depots") or []:
        if depot.get("organization_id"):
            orgs.add(coerce_uuid(depot["organization_id"]))
    for org in orgs:
        await conn.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org,
            "Eval org",
        )

    for depot in snapshot.get("depots") or []:
        depot_id = coerce_uuid(depot["depot_id"])
        tariff = (
            json.dumps({"entsoe_zone": depot["entsoe_zone"]}) if depot.get("entsoe_zone") else None
        )
        await conn.execute(
            """
            INSERT INTO sites (id, organization_id, name, timezone, currency,
                               max_grid_kw, tariff_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            depot_id,
            (
                coerce_uuid(depot.get("organization_id"))
                if depot.get("organization_id")
                else default_org
            ),
            depot.get("name", "Depot"),
            depot.get("timezone", "Europe/Vilnius"),
            depot.get("currency", "EUR"),
            depot.get("max_grid_kw"),
            tariff,
        )

    for v in snapshot.get("vehicles") or []:
        await conn.execute(
            """
            INSERT INTO vehicles (id, site_id, vin, license_plate, battery_capacity_kwh,
                                  max_charge_rate_kw, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            coerce_uuid(v["vehicle_id"]),
            coerce_uuid(v["depot_id"]),
            v.get("vin"),
            v.get("license_plate"),
            v.get("battery_capacity_kwh"),
            v.get("max_charge_rate_kw"),
            v.get("status", "active"),
        )

    for d in snapshot.get("drivers") or []:
        await conn.execute(
            """
            INSERT INTO drivers (id, site_id, display_name, external_driver_id, email, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            coerce_uuid(d["driver_id"]),
            coerce_uuid(d["depot_id"]),
            d.get("display_name"),
            d.get("external_driver_id"),
            d.get("email"),
            d.get("status", "active"),
        )

    for c in snapshot.get("chargers") or []:
        await conn.execute(
            """
            INSERT INTO charging_stations (id, site_id, station_id, max_power_kw, connector_type)
            VALUES ($1, $2, $3, $4, $5)
            """,
            coerce_uuid(c["charger_id"]),
            coerce_uuid(c["depot_id"]),
            c.get("ocpp_id"),
            c.get("rated_kw"),
            c.get("connector_type", "CCS"),
        )

    for s in snapshot.get("schedules") or []:
        await conn.execute(
            """
            INSERT INTO schedules (id, vehicle_id, driver_id, route_id, departure_time,
                                   return_time, required_soc, energy_kwh)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8)
            """,
            maybe_uuid(s.get("schedule_id")),
            coerce_uuid(s["vehicle_id"]),
            maybe_uuid(s.get("driver_id")),
            s.get("route_id"),
            resolve_dt(s["departure_time"], scenario_now),
            (
                resolve_dt(s["return_time"], scenario_now)
                if s.get("return_time") is not None
                else None
            ),
            s.get("required_soc"),
            s.get("energy_kwh"),
        )


async def load_ts(conn: Any, snapshot: dict[str, Any], scenario_now: datetime) -> None:
    """Load sessions / telemetry / vehicle_telemetry / optimization_runs / prices into TS test DB."""
    for i, s in enumerate(snapshot.get("sessions") or []):
        start = resolve_dt(s["start_time"], scenario_now)
        end = resolve_dt(s["end_time"], scenario_now) if s.get("end_time") is not None else None
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id, vehicle_id, driver_id,
                card_id, site_id, start_time, end_time, energy_delivered_kwh,
                cost_total, cost_total_source, source)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            maybe_uuid(s.get("session_id")),
            str(s.get("station_id", f"CP-{i + 1:02d}")),
            1,
            1,
            str(s["vehicle_id"]) if s.get("vehicle_id") is not None else None,
            maybe_uuid(s.get("driver_id")),
            maybe_uuid(s.get("card_id")),
            coerce_uuid(s["depot_id"]),
            start,
            end,
            s.get("energy_kwh"),
            s.get("cost_total"),
            s.get("cost_total_source", "granular"),
            s.get("source", "live"),
        )

    for i, t in enumerate(snapshot.get("telemetry") or []):
        # telemetry PK is (time, station_id, connector_id) — vehicle_id is NOT
        # part of it — so two readings at the same `time` (e.g. one per vehicle
        # in a multi-vehicle scenario) collide unless each carries a distinct
        # station_id. Default to a per-row-unique synthetic id; the readiness
        # SoC query keys on vehicle_id, so the station_id value is immaterial.
        await conn.execute(
            """
            INSERT INTO telemetry (time, station_id, connector_id, vehicle_id, charger_id,
                                   soc, is_plugged, charging_kw)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            resolve_dt(t["time"], scenario_now),
            str(t.get("station_id") or f"tele-{i + 1:02d}"),
            int(t.get("connector_id", 1)),
            coerce_uuid(t["vehicle_id"]),
            maybe_uuid(t.get("charger_id")),
            t.get("soc"),
            t.get("is_plugged"),
            t.get("charging_kw"),
        )

    for t in snapshot.get("vehicle_telemetry") or []:
        await conn.execute(
            """
            INSERT INTO vehicle_telemetry (time, vehicle_id, soc, source)
            VALUES ($1, $2, $3, $4)
            """,
            resolve_dt(t["time"], scenario_now),
            coerce_uuid(t["vehicle_id"]),
            t.get("soc"),
            t.get("source", "navirec"),
        )

    for r in snapshot.get("optimization_runs") or []:
        run_time = resolve_dt(r["run_time"], scenario_now)
        await conn.execute(
            """
            INSERT INTO optimization_runs (
                run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                solve_time_s, peak_demand_kw, status, solver_used, schedule_json)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            """,
            maybe_uuid(r.get("run_id")),
            coerce_uuid(r["depot_id"]),
            run_time,
            r.get("trigger_reason", "scheduled"),
            resolve_dt(r["horizon_start"], scenario_now) if r.get("horizon_start") else run_time,
            (
                resolve_dt(r["horizon_end"], scenario_now)
                if r.get("horizon_end")
                else run_time + timedelta(hours=24)
            ),
            r.get("solve_time_s", 1.0),
            r.get("peak_demand_kw", 0.0),
            r.get("status", "optimal"),
            r.get("solver_used", "gurobi"),
            json.dumps(r.get("schedule_json") or {}),
        )

    for p in snapshot.get("electricity_prices") or []:
        await conn.execute(
            """
            INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh)
            VALUES ($1, $2, $3, $4)
            """,
            resolve_dt(p["time"], scenario_now),
            str(p["node_id"]),
            p.get("market_type", "ENTSOE_DAM"),
            p["lmp_price_mwh"],
        )
