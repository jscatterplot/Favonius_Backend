"""Deterministic ``readiness`` intent for the depot chat agent.

Answers the depot manager's most important daily question (PRD §6.1):
"are we ready to depart?". For every vehicle scheduled to leave in the
look-ahead window it checks whether the vehicle will clear its route's
``required_soc`` (the PRD §8.1 ≥99% departure constraint), and rolls the
per-vehicle results into a depot verdict.

Why a deterministic fast path (no LLM):
  * the verdict is safety-relevant — the numbers must be reproducible and
    never hallucinated;
  * it is cheap and answerable from three **set-based** queries (no per-
    vehicle round-trips), so it does not need the SQL tool loop.

This module holds only the pure pieces — the SQL strings, the plan parser,
the verdict reducer, and the renderer. The handler in
``src/api/agent/controller.py`` runs the SQL (one static-pool departures
query + two TS-pool queries) and feeds the rows here. That split keeps the
verdict logic unit-testable without a database.

Charger faults are deliberately out of scope here — they surface through the
alerts intent (``agent_views.alerts`` carries the cause), so readiness stays
focused on the departure-SoC question and its three-query budget.

Reuse:
  * the SoC query mirrors ``StateAssembler._get_vehicle_socs`` (merging
    ``telemetry`` + ``vehicle_telemetry``, freshest-per-vehicle);
  * the plan shape mirrors ``readiness_tools._make_get_charging_plan``;
  * the staleness threshold is ``data_freshness.MAX_TELEMETRY_AGE``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.api.agent_workflows.plan_parsing import extract_vehicle_plan
from src.api.timezone import safe_zone

# PRD §8.1 hard constraint: vehicles must leave at ≥99% SoC. Used when a
# schedule row leaves required_soc NULL.
READINESS_DEFAULT_REQUIRED_SOC = 0.99

# Verdict statuses (worst-case rollup order: at_risk > unknown > ready).
STATUS_READY = "ready"
STATUS_AT_RISK = "at_risk"
STATUS_UNKNOWN = "unknown"

_STATUS_SEVERITY = {STATUS_READY: 0, STATUS_UNKNOWN: 1, STATUS_AT_RISK: 2}


# ── SQL (executed by the controller; kept here next to the parsing) ─────────

# Upcoming departures across the caller's visible depots. depot_id comes from
# the vehicle's site_id so the result can be joined to the per-depot plan.
DEPARTURES_SQL = """
    SELECT
        s.vehicle_id::text  AS vehicle_id,
        v.site_id::text     AS depot_id,
        s.route_id,
        s.departure_time,
        s.required_soc
    FROM schedules s
    JOIN vehicles v ON v.id = s.vehicle_id
    WHERE v.site_id = ANY($1::uuid[])
      AND s.departure_time >= $2
      AND s.departure_time <  $3
    ORDER BY s.departure_time
"""

# The freshest-SoC-per-vehicle merge (telemetry + vehicle_telemetry, with the
# vehicle_telemetry-missing fallback) is shared with the optimizer's
# StateAssembler via src.db.queries.fetch_freshest_vehicle_socs — the
# controller's _fetch_readiness_socs calls it.

# Latest *usable* optimization run per depot — its schedule_json carries the
# per-vehicle projected SoC trajectory. We EXCLUDE the two failure markers
# ('infeasible'/'timeout') rather than allow-listing success values: the
# codebase persists successful runs as 'completed' (milp_model.py) — and
# sometimes 'optimal'/'feasible'/'degraded' — so an allow-list would wrongly
# drop the real plans (optimization_runs.status has no CHECK; the vocabulary
# is inconsistent, so a denylist of the genuine failures is the robust filter).
LATEST_PLAN_SQL = """
    SELECT DISTINCT ON (depot_id)
        depot_id::text AS depot_id,
        schedule_json,
        horizon_start,
        horizon_end
    FROM optimization_runs
    WHERE depot_id = ANY($1::uuid[])
      AND status NOT IN ('infeasible', 'timeout')
    ORDER BY depot_id, run_time DESC
"""


# ── Inputs / outputs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SocReading:
    """A vehicle's freshest SoC plus the instant it was reported."""

    soc: float
    time: datetime


@dataclass(frozen=True)
class PlanContext:
    """One depot's latest optimization plan plus the horizon it covers.

    ``horizon_start`` / ``horizon_end`` let the reader align a per-timestep
    SoC trajectory to a departure instant (and reject a plan whose horizon
    doesn't cover the departure — a stale run).
    """

    schedule_json: Any
    horizon_start: Optional[datetime]
    horizon_end: Optional[datetime]


@dataclass(frozen=True)
class VehicleReadiness:
    """Per-vehicle readiness outcome."""

    vehicle_id: str
    depot_id: str
    route_id: Optional[str]
    departure_time: datetime
    required_soc: float
    status: str
    reason: str
    current_soc: Optional[float] = None
    projected_soc: Optional[float] = None

    @property
    def label(self) -> str:
        """Operator-facing label — route first (how depots think), else id."""
        if self.route_id:
            return f"route {self.route_id}"
        return f"vehicle {self.vehicle_id[:8]}"


@dataclass(frozen=True)
class ReadinessVerdict:
    """Depot-wide readiness rollup."""

    window_label: str
    total: int
    ready: int
    at_risk: int
    unknown: int
    vehicles: list[VehicleReadiness] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if self.at_risk:
            return STATUS_AT_RISK
        if self.unknown:
            return STATUS_UNKNOWN
        return STATUS_READY


def projected_soc_at_departure(
    plan: Optional[PlanContext],
    vehicle_id: str,
    departure_time: datetime,
) -> Optional[float]:
    """Projected SoC for one vehicle AT its departure instant.

    Aligns the per-timestep ``soc`` trajectory to ``departure_time`` using the
    run's horizon, rather than taking the trajectory peak — ``max(soc)`` would
    credit charge scheduled *after* the vehicle has already left, marking a
    near-term departure ready on the strength of a late-horizon charge.

    Returns ``None`` (→ caller falls back to current SoC) when the vehicle has
    no plan, the plan carries no usable horizon, or the horizon does not cover
    ``departure_time`` (a stale / out-of-range run). The str-vs-dict /
    schedule / per-vehicle walk is shared with
    ``readiness_tools._make_get_charging_plan`` via
    :func:`~src.api.agent_workflows.plan_parsing.extract_vehicle_plan`.
    """
    if plan is None:
        return None
    per_vehicle = extract_vehicle_plan(plan.schedule_json, vehicle_id)
    if per_vehicle is None:
        return None
    socs = per_vehicle.get("soc")
    if not isinstance(socs, list) or not socs:
        return None
    hs, he = plan.horizon_start, plan.horizon_end
    if hs is None or he is None:
        return None
    span = (he - hs).total_seconds()
    if span <= 0 or departure_time < hs or departure_time >= he:
        return None
    frac = (departure_time - hs).total_seconds() / span
    idx = min(int(frac * len(socs)), len(socs) - 1)
    value = socs[idx]
    return float(value) if isinstance(value, (int, float)) else None


def summarize_readiness(
    departures: list[dict[str, Any]],
    socs: dict[str, SocReading],
    plans_by_depot: dict[str, Any],
    *,
    now: datetime,
    max_age: timedelta,
    window_label: str,
    required_soc_default: float = READINESS_DEFAULT_REQUIRED_SOC,
) -> ReadinessVerdict:
    """Reduce departures + SoC + plans into a depot readiness verdict.

    Per vehicle, in priority order:
      * **plan exists** → ``ready`` iff the projected SoC clears the required
        SoC, else ``at_risk`` (the optimiser cannot get it there).
      * **no plan, fresh SoC** → judge on the current SoC (``ready`` /
        ``at_risk``), flagged as "no plan yet" so the operator knows the
        estimate is pre-optimisation.
      * **no plan, stale or missing SoC** → ``unknown`` (we cannot vouch for
        readiness; surface the gap rather than guess).

    Args:
        departures: Rows from :data:`DEPARTURES_SQL` (dicts with
            ``vehicle_id``, ``depot_id``, ``route_id``, ``departure_time``,
            ``required_soc``).
        socs: ``vehicle_id`` → :class:`SocReading` (freshest reading).
        plans_by_depot: ``depot_id`` → :class:`PlanContext` for the depot's
            latest optimization run.
        now: Current instant (UTC-aware) — the staleness anchor.
        max_age: Telemetry staleness threshold (``MAX_TELEMETRY_AGE``).
        window_label: Human label for the look-ahead window.
        required_soc_default: Fallback when a schedule row's ``required_soc``
            is NULL (PRD §8.1 floor, 0.99).

    Returns:
        A :class:`ReadinessVerdict`.
    """
    # Evaluate EVERY in-window departure against its own departure-time SoC,
    # then collapse to ONE verdict per vehicle = its WORST departure. This
    # counts unique vehicles (not schedule rows) while still flagging a vehicle
    # that is fine for an early low-SoC trip but at risk for a later trip that
    # needs a higher SoC.
    worst: dict[str, VehicleReadiness] = {}
    for row in departures:
        vehicle_id = str(row["vehicle_id"])
        depot_id = str(row["depot_id"])
        route_id = row.get("route_id")
        required = row.get("required_soc")
        required_soc = float(required) if required is not None else required_soc_default
        departure_time = row["departure_time"]

        reading = socs.get(vehicle_id)
        fresh = reading is not None and (now - reading.time) <= max_age
        current_soc = reading.soc if reading is not None else None
        projected = projected_soc_at_departure(
            plans_by_depot.get(depot_id), vehicle_id, departure_time
        )

        status, reason = _judge_vehicle(
            required_soc=required_soc,
            projected=projected,
            current_soc=current_soc,
            fresh=fresh,
        )
        candidate = VehicleReadiness(
            vehicle_id=vehicle_id,
            depot_id=depot_id,
            route_id=route_id,
            departure_time=departure_time,
            required_soc=required_soc,
            status=status,
            reason=reason,
            current_soc=current_soc,
            projected_soc=projected,
        )
        incumbent = worst.get(vehicle_id)
        if incumbent is None or _STATUS_SEVERITY[status] > _STATUS_SEVERITY[incumbent.status]:
            worst[vehicle_id] = candidate

    vehicles = list(worst.values())
    ready = sum(1 for v in vehicles if v.status == STATUS_READY)
    at_risk = sum(1 for v in vehicles if v.status == STATUS_AT_RISK)
    unknown = sum(1 for v in vehicles if v.status == STATUS_UNKNOWN)
    return ReadinessVerdict(
        window_label=window_label,
        total=len(vehicles),
        ready=ready,
        at_risk=at_risk,
        unknown=unknown,
        vehicles=sorted(vehicles, key=lambda v: -_STATUS_SEVERITY[v.status]),
    )


def _judge_vehicle(
    *,
    required_soc: float,
    projected: Optional[float],
    current_soc: Optional[float],
    fresh: bool,
) -> tuple[str, str]:
    """Classify one vehicle. See :func:`summarize_readiness`."""
    req_pct = round(required_soc * 100)
    if projected is not None:
        if projected >= required_soc:
            return STATUS_READY, f"plan reaches {round(projected * 100)}% (needs {req_pct}%)"
        return (
            STATUS_AT_RISK,
            f"plan only reaches {round(projected * 100)}% (needs {req_pct}%)",
        )
    # No optimization plan for this vehicle yet — fall back to current SoC.
    if current_soc is not None and fresh:
        if current_soc >= required_soc:
            return (
                STATUS_READY,
                f"no plan yet; SoC {round(current_soc * 100)}% already meets {req_pct}%",
            )
        return (
            STATUS_AT_RISK,
            f"no plan yet and SoC {round(current_soc * 100)}% is below {req_pct}%",
        )
    if current_soc is not None and not fresh:
        return STATUS_UNKNOWN, "no plan yet and the latest SoC reading is stale"
    return STATUS_UNKNOWN, "no plan yet and no recent SoC reading"


def render_readiness_answer(verdict: ReadinessVerdict) -> str:
    """Render a readiness verdict as an operator-facing reply (no LLM)."""
    if verdict.total == 0:
        return f"No departures are scheduled {verdict.window_label}."

    lines = [
        f"{verdict.ready} of {verdict.total} vehicles are ready to depart "
        f"{verdict.window_label}."
    ]

    flagged = [v for v in verdict.vehicles if v.status == STATUS_AT_RISK]
    if flagged:
        details = "; ".join(f"{v.label} — {v.reason}" for v in flagged)
        noun = "vehicle is" if len(flagged) == 1 else "vehicles are"
        lines.append(f"{len(flagged)} {noun} at risk: {details}.")

    uncertain = [v for v in verdict.vehicles if v.status == STATUS_UNKNOWN]
    if uncertain:
        details = "; ".join(f"{v.label} — {v.reason}" for v in uncertain)
        verb = "needs" if len(uncertain) == 1 else "need"
        lines.append(f"{len(uncertain)} {verb} a check: {details}.")

    return " ".join(lines)


_READINESS_DEFAULT_HOURS = 24


def resolve_readiness_window(
    message: str,
    now: datetime,
    tz_name: Optional[str],
) -> tuple[datetime, datetime, str]:
    """Resolve the readiness look-ahead window + label from the message.

    Honours the day phrase the planner already routes here:
      * ``tomorrow`` → all of tomorrow (depot-local calendar day);
      * ``today`` / ``this morning|afternoon|evening`` / ``tonight`` →
        ``now`` → end of today (depot-local);
      * otherwise → ``[now, now + 24h)`` labelled "in the next 24 hours".

    ``tz_name`` (the depot's timezone, when a single depot is in scope)
    anchors the calendar-day boundaries; ``None`` degrades to UTC.
    """
    text = (message or "").lower()
    tz = safe_zone(tz_name)
    local_now = now.astimezone(tz)

    if re.search(r"\btomorrow\b", text):
        d = (local_now + timedelta(days=1)).date()
        start_local = datetime(d.year, d.month, d.day, tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), "tomorrow"

    if re.search(r"\b(today|this morning|this afternoon|this evening|tonight)\b", text):
        d = local_now.date()
        end_local = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
        return now, end_local.astimezone(timezone.utc), "for the rest of today"

    return now, now + timedelta(hours=_READINESS_DEFAULT_HOURS), "in the next 24 hours"
