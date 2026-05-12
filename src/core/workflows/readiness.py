"""Daily readiness check workflow (PRD §6.1).

Produces the "today view" structured payload: coverage counts plus a
list of exceptions (vehicles whose projected SoC won't meet the
route's required SoC at departure, chargers in fault, etc.). Each
run is captured as a :class:`ReadinessDecision` and persisted to the
``workflow_decisions`` table by the caller.

Tools — the readonly graph queries the workflow consumes — are
defined in :mod:`src.core.workflows.tools`. The orchestrator records
one :class:`ToolCall` per invocation so the "why" view can replay the
exact data the agent saw. The graph queries are pure read paths over
the assembler/state pipeline so this module never writes to the OCPP
or schedules tables — every action is at most a *proposal* surfaced in
``exceptions[].proposed_action``.

Hard departure SoC constraint: PRD Section 8.1 — ≥99% by default,
overridable per vehicle via VDV463-supplied
``vehicle_departure_soc_min``. The workflow does not relax this
threshold; an under-projected SoC always flags an exception.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


WORKFLOW_NAME = "daily_readiness_check"
WORKFLOW_VERSION = "v1"


@dataclass
class ToolCall:
    """One read-only tool invocation made during a workflow run.

    The :class:`ReadinessDecision` carries an ordered list of these so
    the "why" view (PRD §7.2) can show every input the workflow saw,
    in invocation order, alongside the corresponding output.

    Attributes:
        name: Tool name (e.g. ``get_scheduled_departures``).
        args: JSON-serializable kwargs passed to the tool.
        result_summary: Compact JSON-serializable summary of what came
            back (row count, first-N keys). The full result is NOT
            stored here — it lives in the workflow's input snapshot.
        duration_ms: Wall-clock duration of the call. ``None`` means
            "did not record" (e.g. synchronous helpers that don't time
            themselves).
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result_summary: dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "result_summary": self.result_summary,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ReadinessException:
    """One flagged item in the §6.1 ``exceptions`` array."""

    vehicle_id: str
    issue: str
    evidence: dict[str, Any] = field(default_factory=dict)
    proposed_action: Optional[dict[str, Any]] = None
    permission_required: str = "inform"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "vehicle_id": self.vehicle_id,
            "issue": self.issue,
            "evidence": self.evidence,
            "permission_required": self.permission_required,
        }
        if self.proposed_action is not None:
            out["proposed_action"] = self.proposed_action
        return out


@dataclass
class ReadinessOutput:
    """Structured "today view" payload — verbatim §6.1 shape."""

    depot_id: str
    window: str
    coverage: dict[str, int]
    status: str  # "all_clear" | "exceptions_present"
    exceptions: list[ReadinessException] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "depot_id": self.depot_id,
            "window": self.window,
            "coverage": dict(self.coverage),
            "status": self.status,
            "exceptions": [e.to_dict() for e in self.exceptions],
        }


@dataclass
class ReadinessDecision:
    """A complete workflow run — Decision record per PRD §5.3."""

    decision_id: UUID
    workflow_name: str
    workflow_version: str
    depot_id: UUID
    organization_id: Optional[UUID]
    triggered_by: str  # 'manual' | 'scheduler' | 'event'
    triggered_by_user_id: Optional[UUID]
    inputs_hash: str
    tool_calls: list[ToolCall]
    output: ReadinessOutput
    permission_tier: str = "inform"
    status: str = "success"  # 'success' | 'degraded' | 'error'
    duration_ms: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute a deterministic sha256 over the workflow's inputs bundle.

    Canonicalises the dict by sorting keys and using ``default=str`` for
    datetimes/UUIDs so two runs with identical state produce the same
    hash. This is what backs the 60-second idempotency cache: the
    router compares the request's freshly-computed hash against the
    most recent ``workflow_decisions.inputs_hash`` for the same depot,
    and returns the cached Decision when they match within the window.
    """
    canonical = json.dumps(inputs, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Pure logic helpers ────────────────────────────────────────────────────


def _project_soc(
    current_soc: float,
    *,
    plugged_in: bool,
    charger_power_kw: float,
    minutes_until_departure: float,
    battery_kwh: float,
    max_charge_kw: float,
) -> float:
    """Project SoC forward to departure assuming the current plan holds.

    Linear projection from current SoC + (charging power × time) capped
    at 1.0. Charging power is the *plan's* delivered power — fault
    handling caps at 0 kW since a faulted charger delivers nothing.

    Returns:
        Float SoC in [0.0, 1.0]. Capped at 1.0 — vehicles cannot
        overfill — and at 0.0 from below to defend against malformed
        telemetry.
    """
    if minutes_until_departure <= 0 or not plugged_in or battery_kwh <= 0:
        return max(0.0, min(1.0, current_soc))
    effective_kw = max(0.0, min(charger_power_kw, max_charge_kw))
    hours = minutes_until_departure / 60.0
    delta_soc = (effective_kw * hours) / battery_kwh
    projected = current_soc + delta_soc
    return max(0.0, min(1.0, projected))


def _format_window(start: datetime, end: datetime) -> str:
    """Format the §6.1 ``window`` string in local-naive ISO-minute form.

    Matches the example literal in the PRD
    (``"2026-05-13T05:00 → 09:00"``): start in full ISO without
    timezone, then a `` → `` then end's ``HH:MM``. Caller is expected
    to pass datetimes already converted to the depot's local zone.
    """
    start_part = start.strftime("%Y-%m-%dT%H:%M")
    end_part = end.strftime("%H:%M")
    return f"{start_part} → {end_part}"


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


# ── Orchestrator ──────────────────────────────────────────────────────────


def run_daily_readiness_check(
    *,
    depot_id: UUID,
    organization_id: Optional[UUID],
    triggered_by: str,
    triggered_by_user_id: Optional[UUID],
    window_start_local: datetime,
    window_end_local: datetime,
    departures: list[dict[str, Any]],
    vehicle_states: dict[str, dict[str, Any]],
    charger_states: dict[str, dict[str, Any]],
    charging_plans: dict[str, dict[str, Any]],
    driver_assignments: dict[str, dict[str, Any]],
    alternate_chargers: Optional[list[dict[str, Any]]] = None,
    tool_calls: Optional[list[ToolCall]] = None,
    duration_ms: Optional[int] = None,
) -> ReadinessDecision:
    """Run the daily readiness check on already-assembled inputs.

    Pure function — no IO. The caller (router or scheduler) is
    responsible for fetching ``departures``, ``vehicle_states``,
    ``charger_states``, ``charging_plans``, ``driver_assignments``
    using the helpers in :mod:`src.core.workflows.tools` and for
    persisting the returned :class:`ReadinessDecision`.

    Args:
        depot_id: Depot UUID.
        organization_id: Owning organization (``None`` for cross-org
            admin runs).
        triggered_by: ``'manual' | 'scheduler' | 'event'``.
        triggered_by_user_id: UUID of the user when ``triggered_by ==
            'manual'``; ``None`` otherwise.
        window_start_local: Start of the readiness window in depot
            local time (naive datetime). Used only for formatting the
            §6.1 ``window`` string.
        window_end_local: End of the readiness window in depot local
            time.
        departures: List of dicts shaped like
            ``{vehicle_id, route_id, departure_time, required_soc,
            charger_id}``. ``departure_time`` is a tz-aware UTC
            ``datetime``; ``required_soc`` is in [0,1].
        vehicle_states: ``vehicle_id`` → ``{soc, plugged_in,
            charger_id, battery_kwh, max_charge_kw}``.
        charger_states: ``charger_id`` → ``{status, current_power_kw,
            fault_code, rated_kw}``. ``status`` is the OCPP
            StatusNotification string (``Available`` / ``Charging`` /
            ``Faulted`` / …).
        charging_plans: ``vehicle_id`` → ``{planned_power_kw,
            planned_end_time}``.
        driver_assignments: ``route_id`` → ``{driver_id, valid}``.
            ``valid=False`` means the assigned driver has a shift
            conflict.
        alternate_chargers: Optional list of free chargers usable for
            mitigation proposals.
        tool_calls: The :class:`ToolCall` trace the caller built while
            collecting the above. Replayed verbatim into the
            :class:`ReadinessDecision`.
        duration_ms: Total wall-clock duration of the workflow run
            (gathering + reasoning). Set by the caller.

    Returns:
        A :class:`ReadinessDecision` ready to be persisted.
    """
    if window_end_local <= window_start_local:
        raise ValueError("window_end_local must be after window_start_local")
    if triggered_by not in ("manual", "scheduler", "event"):
        raise ValueError(f"invalid triggered_by: {triggered_by!r}")

    exceptions: list[ReadinessException] = []
    chargers_seen: set[str] = set()
    routes_seen: set[str] = set()

    for dep in departures:
        vid = str(dep["vehicle_id"])
        route_id = str(dep["route_id"])
        routes_seen.add(route_id)
        required_soc = float(dep["required_soc"])
        departure_time: datetime = dep["departure_time"]
        if departure_time.tzinfo is None:
            departure_time = departure_time.replace(tzinfo=timezone.utc)

        vstate = vehicle_states.get(vid, {})
        plan = charging_plans.get(vid, {})
        charger_id = dep.get("charger_id") or vstate.get("charger_id")
        cstate = charger_states.get(str(charger_id), {}) if charger_id else {}
        if charger_id:
            chargers_seen.add(str(charger_id))

        # Driver assignment.
        driver = driver_assignments.get(route_id, {})
        if not driver.get("driver_id"):
            exceptions.append(
                ReadinessException(
                    vehicle_id=vid,
                    issue=f"No driver assigned to route {route_id} for {departure_time.isoformat()}",
                    evidence={"route_id": route_id, "departure_time": departure_time.isoformat()},
                    proposed_action={
                        "type": "assign_driver",
                        "route_id": route_id,
                        "justification": "Route has no driver in the roster system.",
                    },
                    permission_required="draft_and_wait",
                )
            )
            continue
        if driver.get("valid") is False:
            exceptions.append(
                ReadinessException(
                    vehicle_id=vid,
                    issue=(
                        f"Driver {driver.get('driver_id')} shift conflicts with route "
                        f"{route_id} departure"
                    ),
                    evidence={
                        "route_id": route_id,
                        "driver_id": driver.get("driver_id"),
                        "departure_time": departure_time.isoformat(),
                    },
                    proposed_action={
                        "type": "swap_driver",
                        "route_id": route_id,
                        "justification": "Assigned driver has a shift conflict.",
                    },
                    permission_required="draft_and_wait",
                )
            )
            continue

        # Charger fault check.
        charger_status = str(cstate.get("status", "")).lower() if cstate else ""
        if charger_status in {"faulted", "unavailable"} or cstate.get("fault_code"):
            alt = (alternate_chargers or [{}])[0] if alternate_chargers else {}
            exceptions.append(
                ReadinessException(
                    vehicle_id=vid,
                    issue=(
                        f"Charger {charger_id} is {cstate.get('status', 'Faulted')} "
                        f"(fault_code={cstate.get('fault_code') or 'n/a'})"
                    ),
                    evidence={
                        "charger_id": charger_id,
                        "status": cstate.get("status"),
                        "fault_code": cstate.get("fault_code"),
                    },
                    proposed_action=(
                        {
                            "type": "swap_charger",
                            "candidate_charger_id": alt.get("charger_id"),
                            "justification": "Move session to an available charger.",
                        }
                        if alt.get("charger_id")
                        else {
                            "type": "manual_intervention",
                            "justification": "No alternate charger free in window.",
                        }
                    ),
                    permission_required="draft_and_wait",
                )
            )
            continue

        # SoC projection check — the hard PRD §8.1 constraint.
        battery_kwh = float(vstate.get("battery_kwh", 0.0))
        max_charge_kw = float(vstate.get("max_charge_kw", 0.0))
        planned_power_kw = float(plan.get("planned_power_kw", 0.0))
        # If the charger's reported current power exceeds the planned
        # power (e.g. plan says 0 but it's actually charging at 22kW),
        # take the smaller of the two — we project the *plan*, not
        # current behaviour, because the plan determines what we will
        # do between now and departure.
        effective_power_kw = planned_power_kw

        current_soc = float(vstate.get("soc", 0.0))
        # Convert any percent (>1) representation defensively to fraction.
        if current_soc > 1.0:
            current_soc = current_soc / 100.0
        if required_soc > 1.0:
            required_soc = required_soc / 100.0

        now = datetime.now(timezone.utc)
        minutes_until_departure = max(0.0, (departure_time - now).total_seconds() / 60.0)
        projected_soc = _project_soc(
            current_soc,
            plugged_in=bool(vstate.get("plugged_in", False)),
            charger_power_kw=effective_power_kw,
            minutes_until_departure=minutes_until_departure,
            battery_kwh=battery_kwh,
            max_charge_kw=max_charge_kw,
        )

        if projected_soc + 1e-6 < required_soc:
            alt = (alternate_chargers or [{}])[0] if alternate_chargers else {}
            exceptions.append(
                ReadinessException(
                    vehicle_id=vid,
                    issue=(
                        f"Projected SoC {projected_soc * 100:.0f}% < required "
                        f"{required_soc * 100:.0f}% at "
                        f"{departure_time.strftime('%H:%M')} departure"
                    ),
                    evidence={
                        "route_id": route_id,
                        "current_soc": round(current_soc, 4),
                        "projected_soc": round(projected_soc, 4),
                        "required_soc": round(required_soc, 4),
                        "plugged_in": bool(vstate.get("plugged_in", False)),
                        "charger_id": charger_id,
                        "minutes_until_departure": round(minutes_until_departure, 1),
                        "planned_power_kw": round(planned_power_kw, 2),
                    },
                    proposed_action=(
                        {
                            "type": "swap_charger",
                            "candidate_charger_id": alt.get("charger_id"),
                            "justification": (
                                "Move to a higher-power charger to meet required SoC by "
                                f"{departure_time.strftime('%H:%M')}."
                            ),
                        }
                        if alt.get("charger_id")
                        else {
                            "type": "reassign_to_route",
                            "candidate_route_id": None,
                            "justification": (
                                "Cannot meet required SoC with current plan and no faster "
                                "charger free; propose swapping to a lower-energy route."
                            ),
                        }
                    ),
                    permission_required="draft_and_wait",
                )
            )
            continue

    status = "all_clear" if not exceptions else "exceptions_present"
    output = ReadinessOutput(
        depot_id=str(depot_id),
        window=_format_window(window_start_local, window_end_local),
        coverage={
            "vehicles_checked": len({d["vehicle_id"] for d in departures}),
            "chargers_checked": len(chargers_seen),
            "routes_checked": len(routes_seen),
        },
        status=status,
        exceptions=exceptions,
    )

    inputs_hash = compute_inputs_hash(
        {
            "depot_id": str(depot_id),
            "window_start": window_start_local.isoformat(),
            "window_end": window_end_local.isoformat(),
            "departures": [
                {
                    "vehicle_id": str(d["vehicle_id"]),
                    "route_id": str(d["route_id"]),
                    "departure_time": d["departure_time"].isoformat(),
                    "required_soc": float(d["required_soc"]),
                    "charger_id": str(d["charger_id"]) if d.get("charger_id") else None,
                }
                for d in sorted(departures, key=lambda r: str(r["vehicle_id"]))
            ],
            "vehicle_states": {
                k: {
                    "soc": round(float(v.get("soc", 0.0)), 4),
                    "plugged_in": _bool_to_int(bool(v.get("plugged_in", False))),
                    "charger_id": str(v.get("charger_id")) if v.get("charger_id") else None,
                    "battery_kwh": round(float(v.get("battery_kwh", 0.0)), 2),
                    "max_charge_kw": round(float(v.get("max_charge_kw", 0.0)), 2),
                }
                for k, v in sorted(vehicle_states.items())
            },
            "charger_states": {
                k: {
                    "status": v.get("status"),
                    "fault_code": v.get("fault_code"),
                    "current_power_kw": round(float(v.get("current_power_kw", 0.0)), 2),
                }
                for k, v in sorted(charger_states.items())
            },
            "charging_plans": {
                k: {
                    "planned_power_kw": round(float(v.get("planned_power_kw", 0.0)), 2),
                }
                for k, v in sorted(charging_plans.items())
            },
            "driver_assignments": {
                k: {
                    "driver_id": str(v.get("driver_id")) if v.get("driver_id") else None,
                    "valid": bool(v.get("valid", True)),
                }
                for k, v in sorted(driver_assignments.items())
            },
        }
    )

    return ReadinessDecision(
        decision_id=uuid4(),
        workflow_name=WORKFLOW_NAME,
        workflow_version=WORKFLOW_VERSION,
        depot_id=depot_id,
        organization_id=organization_id,
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
        inputs_hash=inputs_hash,
        tool_calls=list(tool_calls or []),
        output=output,
        permission_tier="inform",
        status="success",
        duration_ms=duration_ms,
    )
