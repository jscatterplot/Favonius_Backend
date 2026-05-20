"""LLM-facing data catalogue for the depot chat agent's SQL mode.

Single source of truth for both:

1. The contents of the ``list_tables`` / ``describe_table`` tools.
2. The static catalogue block inlined into the system prompt
   (P1 — collapses the explorer phase).

Each entry describes one ``agent_views.<name>($1)`` table-function with
columns + business meaning + at least one example query. The catalogue
is intentionally narrow — only the curated views — so the LLM does not
need to learn the full schema.

The catalogue is split by pool because the validator's allowlist is per
pool: the LLM uses ``run_select_ts`` against TimescaleDB views and
``run_select_static`` against Supabase views.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    note: str = ""


@dataclass(frozen=True)
class FunctionSpec:
    name: str               # e.g. "sessions" — UNQUALIFIED
    purpose: str
    columns: tuple[ColumnSpec, ...]
    examples: tuple[str, ...] = field(default_factory=tuple)
    requires_time_predicate: bool = False


# ── TimescaleDB pool ─────────────────────────────────────────────────────

TS_FUNCTIONS: tuple[FunctionSpec, ...] = (
    FunctionSpec(
        name="sessions",
        purpose=(
            "One row per completed or open charging session. Energy delivered "
            "and cost per session, with FKs back to vehicle/driver/charger/depot."
        ),
        columns=(
            ColumnSpec("session_id", "uuid", "Primary key."),
            ColumnSpec("vehicle_id", "varchar", "FK to vehicles."),
            ColumnSpec("driver_id", "uuid", "Nullable — set when session was authorized via driver-bound RFID."),
            ColumnSpec("card_id", "uuid", "Nullable — RFID card that started the session."),
            ColumnSpec("station_id", "varchar", "OCPP station id (textual), not a UUID."),
            ColumnSpec("depot_id", "uuid", "FK to depots."),
            ColumnSpec("start_time", "timestamptz", "Session start (UTC)."),
            ColumnSpec("end_time", "timestamptz", "NULL while the session is open."),
            ColumnSpec("energy_kwh", "numeric", "Total energy delivered, kWh."),
            ColumnSpec("cost_total", "numeric", "Customer-facing cost in depot currency. NULL if unpriceable."),
            ColumnSpec("source", "varchar", "'live' = OCPP, 'import' = XLSX backfill."),
        ),
        examples=(
            "SELECT depot_id, SUM(energy_kwh) AS total_kwh "
            "FROM agent_views.sessions($1) "
            "WHERE start_time >= now() - interval '30 days' "
            "GROUP BY depot_id",
            "SELECT driver_id, COUNT(*) AS n_sessions, SUM(cost_total) "
            "FROM agent_views.sessions($1) "
            "WHERE start_time >= '2026-04-01' AND start_time < '2026-05-01' "
            "GROUP BY driver_id ORDER BY n_sessions DESC LIMIT 10",
        ),
    ),
    FunctionSpec(
        name="telemetry_hourly",
        purpose=(
            "Hourly rollup of vehicle telemetry. Use this for peak-power or "
            "SoC trend questions. REQUIRES a time predicate on `hour`."
        ),
        columns=(
            ColumnSpec("vehicle_id", "uuid"),
            ColumnSpec("charger_id", "uuid", "Nullable — vehicle may not be plugged."),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("hour", "timestamptz", "Hour-bucket start (UTC)."),
            ColumnSpec("avg_soc", "double precision", "Average SoC for the hour (0.0–1.0)."),
            ColumnSpec("peak_charging_kw", "double precision", "Max instantaneous charging power for the hour."),
            ColumnSpec("charging_minutes", "double precision", "Approx minutes the vehicle was actively charging."),
        ),
        examples=(
            "SELECT hour, MAX(peak_charging_kw) AS site_peak_kw "
            "FROM agent_views.telemetry_hourly($1) "
            "WHERE hour >= now() - interval '7 days' "
            "GROUP BY hour ORDER BY site_peak_kw DESC LIMIT 5",
        ),
        requires_time_predicate=True,
    ),
    FunctionSpec(
        name="optimization_runs",
        purpose="One row per MILP solver invocation per depot.",
        columns=(
            ColumnSpec("run_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("run_time", "timestamptz"),
            ColumnSpec("trigger_reason", "varchar", "'scheduled' | 'price_spike' | 'soc_deviation' | 'return_time_deviation' | 'interdepot_handoff'."),
            ColumnSpec("horizon_start", "timestamptz"),
            ColumnSpec("horizon_end", "timestamptz"),
            ColumnSpec("solve_time_s", "double precision"),
            ColumnSpec("peak_demand_kw", "double precision"),
            ColumnSpec("status", "varchar", "'optimal' | 'feasible' | 'degraded' | 'infeasible' | 'timeout'."),
            ColumnSpec("solver_used", "varchar", "'gurobi' | 'highs'."),
        ),
        examples=(
            "SELECT trigger_reason, COUNT(*) FROM agent_views.optimization_runs($1) "
            "WHERE run_time >= now() - interval '24 hours' GROUP BY trigger_reason",
        ),
    ),
    FunctionSpec(
        name="alerts",
        purpose="Per-depot notification alerts (charger faults, low SoC, etc.).",
        columns=(
            ColumnSpec("alert_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("created_at", "timestamptz"),
            ColumnSpec("severity_level", "smallint", "1=info, 2=warning, 3=critical."),
            ColumnSpec("status", "text", "'active' | 'acknowledged' | 'resolved'."),
            ColumnSpec("alert_type", "text"),
        ),
        examples=(
            "SELECT severity_level, COUNT(*) FROM agent_views.alerts($1) "
            "WHERE created_at >= now() - interval '7 days' GROUP BY severity_level",
        ),
    ),
    FunctionSpec(
        name="prices_hourly",
        purpose=(
            "Hourly day-ahead electricity prices. depot_id may be NULL when "
            "the price source is a bidding-zone feeder rather than a per-depot "
            "tariff. REQUIRES a time predicate on `hour`."
        ),
        columns=(
            ColumnSpec("depot_id", "uuid", "May be NULL when sourced from bidding-zone feeder."),
            ColumnSpec("hour", "timestamptz"),
            ColumnSpec("price_per_kwh", "numeric"),
            ColumnSpec("currency", "text"),
            ColumnSpec("market_type", "text", "Free-form, e.g. 'DAM' for day-ahead market."),
        ),
        examples=(
            "SELECT hour, AVG(price_per_kwh) FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '24 hours' GROUP BY hour ORDER BY hour",
        ),
        requires_time_predicate=True,
    ),
    FunctionSpec(
        name="building_load_hourly",
        purpose=(
            "Hourly non-EV building load per depot. Used for grid-headroom and "
            "demand-charge analysis. REQUIRES a time predicate on `hour`."
        ),
        columns=(
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("hour", "timestamptz"),
            ColumnSpec("avg_kw", "double precision"),
            ColumnSpec("peak_kw", "double precision"),
        ),
        examples=(
            "SELECT depot_id, MAX(peak_kw) FROM agent_views.building_load_hourly($1) "
            "WHERE hour >= now() - interval '30 days' GROUP BY depot_id",
        ),
        requires_time_predicate=True,
    ),
    FunctionSpec(
        name="connector_status_latest",
        purpose=(
            "Latest known status per (station, connector). Use this to see "
            "which chargers are currently faulted/available."
        ),
        columns=(
            ColumnSpec("station_id", "varchar", "OCPP station id."),
            ColumnSpec("connector_id", "integer", "OCPP connector index (1, 2, …)."),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("status", "varchar", "OCPP connector status, e.g. 'Available', 'Charging', 'Faulted', 'Unavailable'."),
            ColumnSpec("error_code", "varchar", "OCPP error code; blank when status is healthy."),
            ColumnSpec("last_changed_at", "timestamptz"),
        ),
        examples=(
            "SELECT depot_id, status, COUNT(*) FROM agent_views.connector_status_latest($1) "
            "GROUP BY depot_id, status",
        ),
    ),
)


# ── Supabase (static) pool ───────────────────────────────────────────────

STATIC_FUNCTIONS: tuple[FunctionSpec, ...] = (
    FunctionSpec(
        name="depots",
        purpose="Per-depot configuration (the backend calls these 'depots'; in Supabase the table is 'sites').",
        columns=(
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("name", "text"),
            ColumnSpec("timezone", "text", "IANA tz, e.g. 'Europe/Vilnius'."),
            ColumnSpec("currency", "text", "ISO 4217 code."),
            ColumnSpec("max_grid_kw", "double precision", "Hard grid power ceiling."),
            ColumnSpec("address", "text"),
            ColumnSpec("latitude", "double precision"),
            ColumnSpec("longitude", "double precision"),
        ),
        examples=("SELECT depot_id, name, timezone FROM agent_views.depots($1)",),
    ),
    FunctionSpec(
        name="vehicles",
        purpose="Per-vehicle roster — battery capacity, max charge rate, status.",
        columns=(
            ColumnSpec("vehicle_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("vin", "text"),
            ColumnSpec("license_plate", "text"),
            ColumnSpec("battery_capacity_kwh", "double precision"),
            ColumnSpec("max_charge_rate_kw", "double precision"),
            ColumnSpec("v2g_capable", "boolean"),
            ColumnSpec("status", "text", "'active' | 'inactive' | 'retired'."),
        ),
        examples=(
            "SELECT depot_id, COUNT(*) FROM agent_views.vehicles($1) "
            "WHERE status = 'active' GROUP BY depot_id",
        ),
    ),
    FunctionSpec(
        name="chargers",
        purpose="Per-charger configuration. Note the OCPP station id is `ocpp_id`, distinct from `charger_id` UUID.",
        columns=(
            ColumnSpec("charger_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("ocpp_id", "text", "Free-form OCPP station id used in StatusNotification, etc."),
            ColumnSpec("rated_kw", "double precision"),
            ColumnSpec("connector_type", "text", "MVP is CCS only."),
            ColumnSpec("vendor", "text"),
            ColumnSpec("display_name", "text"),
        ),
        examples=("SELECT depot_id, AVG(rated_kw) FROM agent_views.chargers($1) GROUP BY depot_id",),
    ),
    FunctionSpec(
        name="drivers",
        purpose="Per-driver roster.",
        columns=(
            ColumnSpec("driver_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("display_name", "text"),
            ColumnSpec("external_driver_id", "text", "Customer-supplied employee/badge ID."),
            ColumnSpec("email", "text"),
            ColumnSpec("status", "text", "'active' | 'inactive'."),
        ),
        examples=("SELECT driver_id, display_name FROM agent_views.drivers($1) WHERE status = 'active'",),
    ),
    FunctionSpec(
        name="schedules_recent",
        purpose=(
            "Vehicle schedules over a bounded window (last 14 days + next 14 days). "
            "Use this for 'who departs when' or 'who returned late' analyses."
        ),
        columns=(
            ColumnSpec("schedule_id", "uuid"),
            ColumnSpec("depot_id", "uuid"),
            ColumnSpec("vehicle_id", "uuid"),
            ColumnSpec("driver_id", "uuid", "Nullable."),
            ColumnSpec("route_id", "text", "Customer-supplied route identifier."),
            ColumnSpec("departure_time", "timestamptz"),
            ColumnSpec("return_time", "timestamptz", "Scheduled return."),
            ColumnSpec("actual_return_time", "timestamptz", "Observed return (may differ from scheduled)."),
            ColumnSpec("energy_kwh", "double precision", "Estimated energy demand for the route."),
            ColumnSpec("required_soc", "double precision", "Target SoC at departure (0.0–1.0)."),
        ),
        examples=(
            "SELECT date_trunc('day', departure_time) AS day, COUNT(*) "
            "FROM agent_views.schedules_recent($1) "
            "WHERE departure_time >= now() AND departure_time < now() + interval '7 days' "
            "GROUP BY day ORDER BY day",
        ),
    ),
)


# Business glossary — short rules of thumb that close the semantic gap
# between "consumption" / "savings" / "current driver" and what those
# mean against the actual columns.
GLOSSARY: tuple[str, ...] = (
    "consumption = SUM(energy_kwh) in agent_views.sessions over the window of interest.",
    "cost / spend = SUM(cost_total) in agent_views.sessions; rows where cost_total IS NULL are 'unpriceable' and should be excluded from the headline number.",
    "peak demand = MAX(peak_charging_kw + peak_kw) when combining telemetry_hourly and building_load_hourly on the same depot+hour. If only one is needed, use that one directly.",
    "depot-local day boundaries: use `start_time AT TIME ZONE (SELECT timezone FROM agent_views.depots($1) WHERE depot_id = …)`.",
    "current driver of an RFID card: NOT in agent_views directly; use the lookup_entity tool with kind='rfid'.",
    "an 'open' session has end_time IS NULL.",
    "the cross-DB boundary: vehicles/drivers/chargers/depots/schedules_recent live on the static pool; sessions/telemetry/prices/alerts/optimization_runs live on the TS pool. You cannot JOIN across; resolve IDs via one pool, then query the other.",
    "the LLM must NEVER include the org id / depot ids as a literal — the server binds those automatically via $1.",
)


# ── Catalogue rendering ──────────────────────────────────────────────────


def render_catalogue_markdown() -> str:
    """Return the catalogue as a single markdown blob for the system prompt.

    Cache-friendly: the function returns the same string for the same
    process lifetime, suitable for ``cache_control: ephemeral`` keying.
    """
    parts: list[str] = []
    parts.append("## TimescaleDB pool — `run_select_ts(sql)`\n")
    parts.append(_render_pool(TS_FUNCTIONS))
    parts.append("\n## Supabase pool — `run_select_static(sql)`\n")
    parts.append(_render_pool(STATIC_FUNCTIONS))
    parts.append("\n## Business glossary\n")
    for line in GLOSSARY:
        parts.append(f"- {line}")
    return "\n".join(parts)


def _render_pool(funcs: tuple[FunctionSpec, ...]) -> str:
    lines: list[str] = []
    for fn in funcs:
        sig = f"agent_views.{fn.name}($1)"
        time_note = " — REQUIRES time predicate." if fn.requires_time_predicate else ""
        lines.append(f"\n### `{sig}`{time_note}\n")
        lines.append(f"{fn.purpose}\n")
        lines.append("| column | type | note |")
        lines.append("|---|---|---|")
        for col in fn.columns:
            lines.append(f"| `{col.name}` | `{col.type}` | {col.note} |")
        if fn.examples:
            lines.append("\nExamples:")
            for ex in fn.examples:
                lines.append(f"```sql\n{ex}\n```")
    return "\n".join(lines)


def describe_function(name: str) -> dict | None:
    """Return a JSON-serializable description of one function, for the
    ``describe_table`` tool. ``name`` is the unqualified function name."""
    name = name.lower().strip()
    if name.startswith("agent_views."):
        name = name.split(".", 1)[1]
    if "(" in name:
        name = name.split("(", 1)[0]
    for spec in TS_FUNCTIONS + STATIC_FUNCTIONS:
        if spec.name == name:
            return {
                "name": f"agent_views.{spec.name}",
                "purpose": spec.purpose,
                "columns": [
                    {"name": c.name, "type": c.type, "note": c.note}
                    for c in spec.columns
                ],
                "requires_time_predicate": spec.requires_time_predicate,
                "examples": list(spec.examples),
            }
    return None


def list_functions_summary() -> list[dict]:
    """Return a compact list-of-objects for the ``list_tables`` tool."""
    return [
        {
            "name": f"agent_views.{f.name}",
            "pool": "ts" if f in TS_FUNCTIONS else "static",
            "purpose": f.purpose.splitlines()[0],
            "requires_time_predicate": f.requires_time_predicate,
        }
        for f in TS_FUNCTIONS + STATIC_FUNCTIONS
    ]
