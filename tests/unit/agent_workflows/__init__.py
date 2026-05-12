"""Unit tests for the workflow runtime + eval harness.

Importing this package registers the ``eval_demo_readiness`` workflow
into the process-wide registry. The example scenarios under
``tests/golden/workflows/_examples/`` reference that workflow name, so
the integration path picks it up automatically once this package has
been imported (the workflow-golden test module imports it explicitly).
"""

from __future__ import annotations

from typing import Any

from src.api.agent_workflows.runtime import (
    WorkflowContext,
    register_workflow,
    workflow_registry,
)


async def _demo_readiness(ctx: WorkflowContext) -> dict[str, Any]:
    """A minimal but realistic readiness check used by the example scenarios.

    The shape of the returned dict mirrors PRD §6.1's today-view
    payload: ``status``, ``coverage`` counts, and an ``exceptions``
    list with ``vehicle_id`` + ``issue`` + ``proposed_action``.

    The logic is intentionally simple — it's just enough to exercise
    every branch of the assertion code in the runner:

    * For every vehicle scheduled to depart in the next 24 h, compute
      "projected SoC" as just the most recent telemetry SoC. Real
      workflows will also factor in charging plans + remaining time;
      this stub is the smallest thing that makes the harness real.
    * Flag every vehicle whose plugged-in charger is in a Faulted /
      Unavailable state with a ``swap_charger`` proposal.
    * Flag every vehicle whose projected SoC is below the schedule's
      required_soc with a ``reassign_to_route`` proposal.
    """
    static_pool = ctx.static_pool
    ts_pool = ctx.ts_pool

    # Coverage counts — read straight from the DB so they reflect what
    # actually loaded.
    vehicles = await static_pool.fetch(
        "SELECT vehicle_id FROM vehicles WHERE depot_id = $1",
        ctx.depot_id,
    )
    chargers = await static_pool.fetch(
        "SELECT charger_id, status FROM chargers WHERE depot_id = $1",
        ctx.depot_id,
    )
    schedules = await static_pool.fetch(
        """
        SELECT s.vehicle_id, s.route_id, s.departure_time,
               s.required_soc
        FROM schedules s
        JOIN vehicles v ON v.vehicle_id = s.vehicle_id
        WHERE v.depot_id = $1
        """,
        ctx.depot_id,
    )

    coverage = {
        "vehicles_checked": len(vehicles),
        "chargers_checked": len(chargers),
        "routes_checked": len(schedules),
    }

    faulted = {
        c["charger_id"]
        for c in chargers
        if c["status"] in ("Faulted", "Unavailable")
    }

    exceptions: list[dict[str, Any]] = []
    for sched in schedules:
        vehicle_id = sched["vehicle_id"]
        latest = await ts_pool.fetchrow(
            """
            SELECT soc, charger_id
            FROM telemetry
            WHERE vehicle_id = $1
            ORDER BY time DESC
            LIMIT 1
            """,
            vehicle_id,
        )

        # Charger-fault branch.
        if latest is not None:
            charger_id = latest.get("charger_id")
            if charger_id is not None and charger_id in faulted:
                exceptions.append({
                    "vehicle_id": str(vehicle_id),
                    "issue": "Plugged into a faulted charger",
                    "proposed_action": {"type": "swap_charger"},
                })
                continue

        # SoC-deficit branch.
        soc = float(latest["soc"]) if latest is not None else 0.0
        required = float(sched["required_soc"])
        if soc < required:
            exceptions.append({
                "vehicle_id": str(vehicle_id),
                "issue": (
                    f"Projected SoC {soc:.0%} < required {required:.0%}"
                    f" at {sched['departure_time'].isoformat()}"
                ),
                "proposed_action": {"type": "reassign_to_route"},
            })

    status = "all_clear" if not exceptions else "exceptions_present"
    return {
        "depot_id": str(ctx.depot_id),
        "scenario_now": ctx.now.isoformat(),
        "coverage": coverage,
        "status": status,
        "exceptions": exceptions,
    }


# Register on import. The function itself is exported so test modules
# can also `import _demo_readiness` to ensure the side-effect runs.
register_workflow(
    "eval_demo_readiness",
    _demo_readiness,
    description="Demo readiness check used by example scenarios + harness tests.",
)


__all__ = ["_demo_readiness"]
