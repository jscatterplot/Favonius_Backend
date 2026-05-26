"""Daily readiness check — sprint 5 workflow registration.

This is the first depot-agent workflow to do real work. It registers
the ``daily_readiness_check`` workflow row in the ``workflows`` table
on startup behind ``DEPOT_AGENT_ENABLED`` and seeds a default
``workflow_tiers`` row at ``inform`` for every visible depot.

The workflow itself is exercised through the existing sprint-2 runtime
(:class:`~src.api.agent_workflows.runtime.WorkflowAgent`) with the
five sprint-4 readiness tools
(:func:`~src.api.agent_workflows.readiness_tools.build_readiness_tool_registry`).
The hard-constraint guard from sprint 2 enforces PRD §10.3 at
dispatch + final-emit time — sprint 5 does not duplicate that logic;
the system prompt below tells the LLM what the guard will reject so
proposals are well-formed in the common case.

Sprint 5's invariant: tier remains ``inform`` at end of sprint
(PRD §9.2). Graduation to ``draft_and_wait`` blocks on the metrics-out
criteria in PRD §11.1.

Reference: docs/PRD_Depot_Agent.md §6.1, §9.2, §10.3, §11.1.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from src.api.agent_workflows.models import (
    GraduationRule,
    PermissionTier,
    Workflow,
)
from src.api.agent_workflows.repository import insert_default_tiers_bulk, upsert_workflow

logger = logging.getLogger(__name__)


# ── Workflow identity ─────────────────────────────────────────────────────

READINESS_WORKFLOW_NAME = "daily_readiness_check"
READINESS_WORKFLOW_VERSION = "1.0.0"


# ── Customer-tunable parameters (PRD §7.4 tier-1 openness) ────────────────

#: Defaults the workflow ships with. Each key is also exposed on the
#: workflow's ``parameters`` JSONB column so a tenant-scoped frontend
#: form can override per-depot values via ``workflow_tiers`` (later
#: sprint).
READINESS_PARAMETER_DEFAULTS: dict[str, Any] = {
    # Minutes before the earliest scheduled departure the check runs.
    "lead_time_min": 60,
    # Percentage points the projected SoC may dip below required before
    # the LLM emits an undercharge action.
    "soc_tolerance_pct": 1.0,
    # Number of simultaneous blocking exceptions that escalates to the
    # asset owner in addition to the depot manager.
    "escalation_threshold": 3,
}


# ── Tools (mirrors readiness_tools.READINESS_TOOLS) ───────────────────────

#: The five sprint-4 tool names this workflow is allowed to call. The
#: agent never sees a tool outside this list (the sprint-2 runtime
#: enforces ``allowed_tools`` strictly).
READINESS_ALLOWED_TOOLS: tuple[str, ...] = (
    "get_scheduled_departures",
    "get_vehicle_state",
    "get_charger_state",
    "get_charging_plan",
    "get_driver_assignment",
)


# ── Default tier + graduation rule ────────────────────────────────────────

#: Launch tier per PRD §9.2 ("All V1 workflows ship at ``inform`` or
#: ``draft_and_wait``"). Sprint 5 ships ``inform`` and will not graduate
#: until metrics in PRD §11.1 are out (exception accuracy on the 10
#: scenarios + at least one shadow week against pilot depot data).
READINESS_DEFAULT_TIER = PermissionTier.INFORM


#: Illustrative rule from PRD §6.1. Per-depot rows are seeded with
#: this so a future graduation can flip ``tier`` to
#: :attr:`PermissionTier.DRAFT_AND_WAIT` without rewriting the rule.
READINESS_DEFAULT_GRADUATION_RULE = GraduationRule(
    min_decisions=100,
    max_override_rate=0.05,
    max_edit_rate=0.15,
    requires_human_signoff=True,
    next_tier=PermissionTier.DRAFT_AND_WAIT,
)


# ── System prompt ─────────────────────────────────────────────────────────

READINESS_SYSTEM_PROMPT = """\
You are the daily readiness agent for a heavy-duty EV fleet depot.

Your job: for every vehicle with a scheduled departure in the configured
window, decide whether the vehicle will be ready, and surface anything
that won't with a specific mitigation that respects every hard
constraint listed below.

# Hard constraints (these are constraints, not objectives)

The runtime enforces these via the hard-constraint guard. Your job is
to produce proposals that respect them in the first place:

1. **Departure SoC commitment is sacred.** You must NEVER propose an
   action that would result in a vehicle leaving with state-of-charge
   below 99% of its scheduled-departure target. If you cannot find a
   mitigation that preserves this, emit no proposed_action for that
   vehicle and flag ``requires_manager: true`` in the action payload
   so a human is consulted before anything is changed.

2. **Site grid power (``max_grid_kw``) is sacred.** Any proposed
   mitigation that would cause the depot to exceed its
   grid-connection limit at any timestep is invalid. Treat the
   optimiser's published plan as the upper bound — extending one
   vehicle's charging is only valid if it does not push total
   instantaneous power past ``max_grid_kw``.

3. **Driver-hours-of-service is a hard constraint.** A driver
   assignment that extends past the driver's ``shift_end`` is
   invalid. If the only mitigation requires breaking shift hours,
   surface the exception with no action and ``requires_manager: true``.

4. **Contractual SLAs override market revenue.** If a market signal
   (price spike, balancing-market call) would defer charging past a
   required-SoC time, you must override the market signal and propose
   preserving the charging plan.

5. **Telemetry freshness gates action.** If the most recent vehicle
   telemetry is older than ``stale_telemetry_minutes`` (default 15),
   do NOT propose an action — surface the gap in the summary with a
   ``data_freshness`` field on any proposed_action entry. The depot
   manager investigates the integration before any mitigation runs.

6. **De-duplicate cascading faults.** When multiple chargers on the
   same electrical circuit fault together, emit one proposed_action
   covering all of them (carry the affected vehicle ids in the
   action payload), not one per charger.

# Tools

You have access to exactly these tools, no others:

  - get_scheduled_departures(depot_id, window_start, window_end)
  - get_vehicle_state(vehicle_id)
  - get_charger_state(charger_id)
  - get_charging_plan(vehicle_id)
  - get_driver_assignment(route_id)

If you find yourself wanting a tool not on this list, surface an
exception via emit_decision rather than speculate.

# Output

Call emit_decision exactly once with:
  - summary: short natural-language summary for the depot manager,
    pairing the coverage statement with the exception count.
  - proposed_actions: structured mitigation objects (see the tier rules
    below for whether you may emit any). Each one cites the vehicle id,
    action type (``swap_charger``, ``extend_charging``, ``hold_route``,
    ``swap_driver``, ``reassign_to_route``, ``preserve_current_plan``),
    and any candidate ids. Include ``requires_manager: true`` when no
    compliant mitigation exists.
  - coverage: counts of {vehicles_checked, chargers_checked,
    routes_checked} so the today view can render "Checked X, found N".
  - rule_applied: the dominant rule name (e.g. ``charger_fault_swap``,
    ``undercharge_extend``, ``data_freshness_block``) when one applies.

# Permission tier — gates WHETHER you may propose, not just execution

The per-depot permission tier is given to you in the turn input. It
controls what you are allowed to emit:

  - ``inform`` (the launch default): you ONLY describe. The
    proposed_actions list MUST be empty. State each exception and the
    direction a human would likely take in the summary prose, but emit
    no structured proposed_actions and never imply an action was or
    will be taken. This is a read-only morning brief.
  - ``draft_and_wait``: you may emit specific proposed_actions for the
    human to approve. Nothing is executed until a human approves.
  - ``act_and_notify`` / ``autonomous``: reserved for graduated
    workflows; still subject to every hard constraint above.

At no tier do you auto-execute, and at no tier do you emit a
proposed_action that would risk a hard-constraint violation. When the
tier is ``inform``, surfacing the exception in the summary IS the
deliverable — withholding the structured action is correct, not a gap.
"""


# ── Workflow object builder ───────────────────────────────────────────────


def build_readiness_workflow(workflow_id: UUID) -> Workflow:
    """Construct the :class:`Workflow` Pydantic model for an existing row.

    Used by tests + ad-hoc scripts that need a Workflow instance without
    a DB roundtrip. Production code calls :func:`upsert_workflow_row`
    which returns the same shape from the DB itself.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Workflow(
        id=workflow_id,
        name=READINESS_WORKFLOW_NAME,
        version=READINESS_WORKFLOW_VERSION,
        description=(
            "Daily readiness check — confirm every vehicle will be ready "
            "for its assigned route at scheduled departure (PRD §6.1)."
        ),
        prompt=READINESS_SYSTEM_PROMPT,
        allowed_tools=list(READINESS_ALLOWED_TOOLS),
        parameters=dict(READINESS_PARAMETER_DEFAULTS),
        created_at=now,
        updated_at=now,
    )


# ── Startup hook ──────────────────────────────────────────────────────────


async def upsert_workflow_row(ts_pool: Any) -> Workflow:
    """Upsert the readiness workflow row in the ``workflows`` table.

    Idempotent — re-running on an existing row rewrites the prompt,
    allowed_tools, and parameter defaults so the DB always tracks the
    code.
    """
    return await upsert_workflow(
        ts_pool,
        name=READINESS_WORKFLOW_NAME,
        version=READINESS_WORKFLOW_VERSION,
        description=(
            "Daily readiness check — confirm every vehicle will be ready "
            "for its assigned route at scheduled departure (PRD §6.1)."
        ),
        prompt=READINESS_SYSTEM_PROMPT,
        allowed_tools=list(READINESS_ALLOWED_TOOLS),
        parameters=dict(READINESS_PARAMETER_DEFAULTS),
    )


async def seed_default_tiers(
    ts_pool: Any,
    workflow_id: UUID,
    depot_ids: list[UUID],
) -> int:
    """Seed missing ``workflow_tiers`` rows at the launch default.

    For each ``depot_id`` without a row, inserts one at
    :data:`READINESS_DEFAULT_TIER` with
    :data:`READINESS_DEFAULT_GRADUATION_RULE`. Existing rows are
    LEFT UNTOUCHED — graduation is always an explicit, audited action,
    never silently overwritten by a process restart.

    The insert is atomic (``INSERT ... ON CONFLICT DO NOTHING``), so a
    concurrent insert or graduation by another worker cannot be clobbered
    back to the default — a check-then-write would have that race. It is
    also a single set-based statement (one round-trip for all depots via
    ``unnest``), so this startup-path seed does not block cold start with
    a per-depot INSERT loop.

    Returns the number of rows that were freshly inserted.
    """
    return await insert_default_tiers_bulk(
        ts_pool,
        workflow_id,
        depot_ids,
        READINESS_DEFAULT_TIER,
        READINESS_DEFAULT_GRADUATION_RULE,
    )


async def _list_depot_ids(
    static_pool: Any, organization_id: Optional[UUID]
) -> list[UUID]:
    """Read every depot id ``organization_id`` can see.

    ``None`` returns every depot — used during startup when the seed
    is not user-scoped (the migration backfills all known depots once
    at boot; depot creation re-seeds on first agent invocation in
    later sprints).
    """
    if organization_id is None:
        query = "SELECT id AS depot_id FROM sites"
        async with static_pool.acquire() as conn:
            rows = await conn.fetch(query)
    else:
        query = "SELECT id AS depot_id FROM sites WHERE organization_id = $1::uuid"
        async with static_pool.acquire() as conn:
            rows = await conn.fetch(query, organization_id)
    return [r["depot_id"] for r in rows]


async def register_daily_readiness_workflow(
    *,
    static_pool: Any,
    ts_pool: Any,
    organization_id: Optional[UUID] = None,
) -> dict[str, Any]:
    """Run the full sprint-5 registration: upsert workflow + seed tiers.

    Called by the FastAPI lifespan when ``DEPOT_AGENT_ENABLED=true``.
    Safe to call on every cold start — the workflow upsert and the
    per-depot tier inserts are both idempotent (tier inserts skip
    existing rows; the workflow row is rewritten in place).

    Returns a small status dict the caller can log:
      {
        "workflow_id": UUID,
        "tiers_seeded": int,  # new rows inserted this call
        "depots_seen": int,
      }
    """
    workflow = await upsert_workflow_row(ts_pool)
    depot_ids = await _list_depot_ids(static_pool, organization_id)
    tiers_seeded = await seed_default_tiers(ts_pool, workflow.id, depot_ids)
    return {
        "workflow_id": workflow.id,
        "tiers_seeded": tiers_seeded,
        "depots_seen": len(depot_ids),
    }
