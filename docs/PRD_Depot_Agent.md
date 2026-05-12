# Depot Agent — Product Requirements Document

**Status:** Draft v0.1
**Owner:** [Founder / PM]
**Audience:** Engineering, design, founding team
**Purpose:** Define the depot agent product so that engineering can build from a shared spec, and so that Claude Code can use this as the canonical reference when generating, comparing, and standardising downstream design and implementation docs.

-----

## 1. Context

### 1.1 The problem

Heavy-duty EV fleet depots (transit buses, last-mile delivery vans, eventually heavy trucks) carry a meaningful operational overhead on top of the asset itself — broadly characterised in the industry as ~30% of total operations effort. This overhead is concentrated in a small number of repeated workflows the depot manager does manually every day:

- Confirming vehicles will be ready for the morning pull-out.
- Reacting to charger faults and coordinating vendor tickets.
- Reassigning routes / blocks when a vehicle or driver drops out.
- Replying to customers, dispatch, and internal stakeholders about ETAs and status.
- Compiling end-of-day, weekly, and monthly reports for asset owners and authorities.

These workflows are repetitive, data-rich, and time-bound — the right shape for an agent.

### 1.2 Why now

Our platform already provides:

- EV charging optimisation (live in customer depots).
- Flexibility trading into balancing markets with BRPs.
- A chat-based Claude integration in the existing UI.

We have the data, the trust, and the buyer relationship to extend from “save money on energy” to “run the depot.” The agent is the natural next product layer on the substrate we already operate.

### 1.3 Users and buyers (dual)

- **Daily user:** the depot manager. Wins via daily-use love — they log in, see silence, and act only when needed.
- **Economic buyer:** the asset owner / operations director. Wins via measurable savings, audit, and reporting.

Both must be served. The asset owner pays; the depot manager kills the deal if they don’t like it. Our strategy is to win the depot manager first while continuing to deliver the savings narrative.

### 1.4 Scope of this PRD

- In scope: the depot agent product — today view, workflows, operational graph, trust graduation, audit, evaluation harness, and tier-1 customer configurability.
- Out of scope: the charging optimiser and balancing-market trading (already shipped), the broader telematics product, billing.

Substrate technical specs (OCPP integration, MILP optimiser, ENTSO-E pricing, VDV 463, deployment, schema) live in the operational docs alongside this PRD — see §15.2.

-----

## 2. Vision and positioning

### 2.1 North star

The depot manager opens the app at 06:00 and sees “Everything is on track. 14 vehicles checked, all on plan.” If something is off, they see a proposed solution next to the problem. They approve or override. They never have to dig.

The asset owner opens the app on the first of the month and sees a complete consumption and savings report drafted, sourced, and ready to send.

### 2.2 Positioning

**Legora for logistics.** The agent does the legwork; the human keeps judgment. Workflows are explicit, reusable, and configurable. Every output is grounded in verifiable data.

### 2.3 What success looks like at 12 months

- ≥80% of mornings the depot manager takes no manual action.
- ≥3 workflows graduated to `act_and_notify` in at least one customer depot.
- Monthly consumption / savings report delivered without manual edits in ≥70% of cases.
- Net Revenue Retention >120% from existing customers expanding to additional depots.

-----

## 3. Product principles

These are non-negotiable design rules. Every workflow and feature must respect them.

1. **Triage and draft, never act first.** The default for any new workflow is “draft and wait.” Autonomy is earned per workflow per depot, not granted.
2. **Silence is the goal, but coverage is the proof.** The today view always shows what was checked, not only what was found. Empty exception lists are paired with explicit coverage counts.
3. **Trust graduates per workflow per depot.** A workflow autonomous in Depot A may still be advisory in Depot B. SOPs and tolerances are local.
4. **Every agent claim is grounded.** Every statement on screen is one click from the underlying data and the rule that produced it (“why” button).
5. **Hard operational constraints are never violated.** Departure SoC commitments, driver-hour limits, and contractual SLAs override every other objective, including market revenue.
6. **The evaluation harness ships before the workflow.** No workflow ships to a customer without ≥10 frozen evaluation scenarios.
7. **Email and other inbound content are untrusted input.** They are read, never executed as instructions. Outbound is always human-approved until graduated.
8. **Single agent, scoped tools.** One agent definition. Tool access is scoped per workflow. Multi-agent is a future decision, not a current architecture.

-----

## 4. Architecture overview

### 4.1 Layers

```
┌────────────────────────────────────────────────────┐
│ Surfaces: Today view · Workflow detail · Audit log │
├────────────────────────────────────────────────────┤
│ Agent: single agent, workflow-scoped tools         │
├────────────────────────────────────────────────────┤
│ Workflows: first-class objects with eval + state   │
├────────────────────────────────────────────────────┤
│ Tools: typed functions over the operational graph  │
├────────────────────────────────────────────────────┤
│ Operational graph: live joined fleet state         │
├────────────────────────────────────────────────────┤
│ Integrations: CSMS · telematics · scheduling · email│
└────────────────────────────────────────────────────┘
```

### 4.2 Operational graph

The substrate. A live, query-able joined view of:

- Sites (depots) and their power constraints.
- Chargers, with status, fault state, and OCPP capability.
- Vehicles, with battery spec, current SoC, scheduled departure, assigned route/block.
- Drivers, with shift and roster info.
- Routes/blocks/duties, with energy demand estimates.
- Contracts (PTAs, customers) and their SLAs.
- Energy / market context: time-of-use tariff, demand-charge bands, BRP commitments.

This is not (yet) a text-derived knowledge graph. It is a structured graph layer over our existing data (Supabase static tables, TimescaleDB time-series; see `docs/ARCHITECTURE.md` and the schema overview in `CLAUDE.md`), exposed to the agent as typed tool calls.

### 4.3 Agent

A single agent definition (Claude). Per invocation, it is given:

- The active workflow’s system prompt.
- A scoped tool allow-list.
- The current operational-graph context relevant to the workflow.
- The workflow’s current permission tier.

The agent does not freely roam tools across workflows. This is the biggest reliability lever we have at this stage.

### 4.4 Workflows as first-class objects

Each workflow is a named object with:

- A prompt.
- An allowed tool set.
- A permission tier (per depot).
- A graduation rule (per depot).
- An evaluation set.
- An audit history.
- An optional set of customer-tunable parameters.

This makes the eventual move to multi-agent painless (a specialist agent is just a workflow cluster with its own context) and keeps each workflow independently testable.

### 4.5 Audit / decision log

Every agent action produces an immutable record: inputs read, tools called, rule applied, output produced, human disposition (approved/edited/rejected). Used for trust, compliance, and graduation.

### 4.6 Evaluation harness

A library of frozen real scenarios (replayable graph state + expected outcome) that runs on every prompt, tool, or workflow change. CI-blocking.

-----

## 5. Data model

Sketched as Python-style dataclasses for clarity. The persistent representation is database-defined separately.

### 5.1 Operational graph (illustrative)

```python
@dataclass
class Site:
    id: str
    name: str
    timezone: str
    grid_connection_kw: float
    demand_charge_bands: list[DemandBand]

@dataclass
class Charger:
    id: str
    site_id: str
    type: Literal["AC", "DC"]
    rated_kw: float
    status: Literal["available", "charging", "fault", "offline"]
    fault_code: Optional[str]
    ocpp_version: str

@dataclass
class Vehicle:
    id: str
    site_id: str
    battery_kwh: float
    current_soc: float          # 0-1
    plugged_in_to: Optional[str] # charger_id
    assigned_route_id: Optional[str]
    next_departure: Optional[datetime]
    required_soc_at_departure: float

@dataclass
class Route:
    id: str
    energy_estimate_kwh: float
    start_time: datetime
    end_time: datetime
    contract_id: Optional[str]

@dataclass
class Driver:
    id: str
    shift_start: datetime
    shift_end: datetime
    assigned_route_id: Optional[str]
```

### 5.2 Workflow object

```python
@dataclass
class Workflow:
    id: str
    name: str
    version: str
    description: str
    prompt: str
    allowed_tools: list[str]
    parameters: dict           # customer-tunable (tier-1 openness)
    permission_tier: PermissionTier   # per (workflow, depot)
    graduation_rule: GraduationRule
    eval_set_id: str

class PermissionTier(Enum):
    INFORM = "inform"
    DRAFT_AND_WAIT = "draft_and_wait"
    ACT_AND_NOTIFY = "act_and_notify"
    AUTONOMOUS = "autonomous"

@dataclass
class GraduationRule:
    min_decisions: int
    max_override_rate: float
    max_edit_rate: float
    requires_human_signoff: bool
    next_tier: PermissionTier
```

### 5.3 Decision / audit record

```python
@dataclass
class Decision:
    id: str
    workflow_id: str
    depot_id: str
    timestamp: datetime
    inputs_hash: str           # hash of operational-graph snapshot used
    tool_calls: list[ToolCall]
    output: dict               # the artifact (brief, draft, action proposal)
    rule_applied: Optional[str]
    disposition: Literal["pending", "approved", "edited", "rejected", "auto_executed"]
    human_user_id: Optional[str]
    diff_if_edited: Optional[str]
```

### 5.4 Evaluation scenario

```python
@dataclass
class EvalScenario:
    id: str
    workflow_id: str
    description: str
    graph_snapshot: GraphSnapshot
    expected: ExpectedOutcome   # may be exact, range, or rubric-based
    severity: Literal["blocking", "warning", "info"]
```

-----

## 6. Workflows — V1

Three workflows ship in V1. Each is specified below with the same template.

### 6.1 Workflow: Daily readiness check

**Goal.** Confirm every vehicle will be ready for its assigned route at its scheduled departure, and surface anything that won’t.

**Trigger.** Time-based, per depot. Default: 60 minutes before the earliest scheduled departure. Configurable parameter.

**Default permission tier.** `inform` (read-only brief) at launch. Graduates to `draft_and_wait` for proposed reassignments.

**Inputs (tools).**

- `get_scheduled_departures(depot_id, window)` → list of `(vehicle_id, route_id, departure_time, required_soc)`
- `get_vehicle_state(vehicle_id)` → SoC, plugged-in status, charger
- `get_charger_state(charger_id)` → status, current power, fault
- `get_charging_plan(vehicle_id)` → projected SoC trajectory
- `get_driver_assignment(route_id)` → driver, shift validity

**Logic (high level).**

1. For each scheduled departure in window: compute projected SoC at departure from current SoC + charging plan.
2. Flag if projected SoC < required SoC, or charger is in fault, or no driver assigned, or driver shift conflicts.
3. For each flagged item, identify candidate mitigations using the operational graph (available reserve vehicle, free DC charger, swap to a lower-energy route).
4. Produce the morning brief: coverage statement + exception list + proposed mitigation per exception.

**Output artifact.** A single structured “today view” payload:

```python
{
  "depot_id": "VLN-01",
  "window": "2026-05-13T05:00 → 09:00",
  "coverage": {"vehicles_checked": 18, "chargers_checked": 22, "routes_checked": 16},
  "status": "all_clear" | "exceptions_present",
  "exceptions": [
    {
      "vehicle_id": "BUS-014",
      "issue": "Projected SoC 71% < required 85% at 06:30 departure",
      "evidence": {...},               # links to live graph data
      "proposed_action": {
         "type": "reassign_to_route",
         "candidate_route_id": "R-244",
         "justification": "..."
      },
      "permission_required": "draft_and_wait"
    }
  ]
}
```

**Graduation rule (illustrative).**

```python
GraduationRule(
  min_decisions=100,
  max_override_rate=0.05,
  max_edit_rate=0.15,
  requires_human_signoff=True,
  next_tier=PermissionTier.ACT_AND_NOTIFY
)
```

**Eval scenarios (minimum 10 to ship).** Examples:

- All vehicles healthy, all chargers healthy → expect `all_clear` with correct coverage counts.
- One vehicle plugged into a faulted charger → expect exception with charger swap proposal.
- One driver assigned to a vehicle that won’t be ready → expect either vehicle reassignment or driver-route swap proposal, never both.
- Hard constraint test: market signal would defer charging past required SoC time → expect plan to override the market signal.

**Edge cases.**

- Vehicle returning late from previous shift: SoC reading must use most recent telematics, not last-known.
- Daylight-saving transitions on departure time math.
- Manual driver overrides that aren’t yet in the roster system.

-----

### 6.2 Workflow: Monthly consumption report

**Goal.** Produce a complete, accurate monthly consumption and savings report for the asset owner, drafted and ready to send.

**Trigger.** First day of each month, per depot. Also on-demand from the asset-owner-facing UI.

**Default permission tier.** `draft_and_wait` — never auto-sent.

**Inputs (tools).**

- `aggregate_energy_consumption(depot_id, period)` → kWh per vehicle, per charger, per tariff band.
- `aggregate_costs(depot_id, period)` → energy cost, demand charges, balancing-market revenue.
- `get_baseline_comparison(depot_id, period)` → counterfactual diesel or unmanaged-charging baseline.
- `get_uptime_stats(depot_id, period)` → charger uptime, missed-departure incidents.
- `list_notable_events(depot_id, period)` → faults, planned maintenance, exceptional days.
- `render_report(template_id, data)` → produces PDF / docx / dashboard view.

**Logic.**

1. Pull all aggregates for the period.
2. Identify the 3–5 most consequential narrative items (biggest savings driver, biggest cost surprise, any SLA breach, any market revenue highlight).
3. Draft the executive summary in plain language grounded in the aggregates.
4. Render the full report with charts and tables.
5. Present the draft to the depot manager (or the configured recipient) with edit + send buttons.

**Output artifact.** A multi-page report (PDF + structured data), plus a 5-bullet executive summary and a draft email to the asset owner.

**Eval scenarios.**

- A month with significant balancing-market revenue: it must be present in the executive summary with correct attribution.
- A month with an SLA breach: the breach must appear in the executive summary, not buried.
- Two-month comparability: aggregates must reconcile against the previous month’s report.

**Edge cases.**

- Mid-month tariff changes.
- Chargers added or removed mid-month.
- Holidays and reduced-service days.

-----

### 6.3 Workflow: Charger fault triage

**Goal.** When a charger reports a fault, draft a vendor ticket, propose an immediate mitigation in the operational plan, and alert the depot manager only if intervention is required.

**Trigger.** Event-based: any charger transition to `fault` status, or unusual undercharging pattern flagged by the optimiser.

**Default permission tier.** `draft_and_wait` for the vendor ticket; `act_and_notify` for the in-plan mitigation (e.g. moving a scheduled session to another charger) provided no human-impacting change is required.

**Inputs (tools).**

- `get_charger_state(charger_id)` with fault code and recent history.
- `lookup_fault_code(ocpp_code, vendor)` → vendor-specific interpretation (initially a static map; later, the document KG layer).
- `get_vendor_ticket_template(vendor)` → vendor’s required ticket fields.
- `find_alternate_charger(depot_id, kw_required, by_time)` → free, capable chargers.
- `update_charging_plan(...)` → adjust the plan (writes; permissioned).
- `create_vendor_ticket_draft(...)` → drafts the email/portal submission.

**Logic.**

1. On fault: classify (transient, recoverable by retry, hard fault).
2. If transient: attempt the platform’s existing recovery; log decision; do not alert.
3. If hard: identify all vehicles affected (currently or upcoming sessions), find alternate chargers, propose plan update, draft vendor ticket.
4. Notify depot manager only if (a) a human-impacting change is required or (b) no clean mitigation exists.

**Output artifact.** A “charger incident” object with: fault summary, affected vehicles, proposed plan delta, draft vendor ticket, recommended manager action (if any).

**Eval scenarios.**

- Transient fault that auto-recovers: expect no manager alert; expect audit record.
- Hard fault with clean alternate available: expect plan delta + draft ticket + no manager action required.
- Hard fault with no alternate: expect manager alert with the cleanest available compromise.

**Edge cases.**

- Cascading faults across multiple chargers on the same circuit.
- Fault codes the vendor’s docs don’t cover.
- Faults reported by telematics but not by the CSMS, or vice versa.

-----

### 6.4 Workflows to add next (not in V1, but design must accommodate)

- Customer ETA reply drafts (last-mile delivery).
- Driver no-show reassignment proposals.
- End-of-day reconciliation report.
- BRP/market-driven decision explainers (this exists implicitly inside the planner; we should expose its rationale through the agent).
- Weekly “what changed and why” summary for the asset owner.
- Vendor coordination drafts for utility, telematics provider, charger OEM.

-----

## 7. UX surfaces

### 7.1 Today view (landing page)

**Layout, top to bottom:**

1. Status banner: “All on track” or “N items need you.”
2. Coverage statement: “Checked X vehicles, Y chargers, Z routes.”
3. Exception queue (if any): each item has summary, evidence link (“why”), proposed action, and approve/edit/reject controls.
4. Recently auto-handled items (collapsed by default): what the agent did without asking, with audit links.

### 7.2 Workflow detail and “why”

Every exception and every auto-handled item links to a detail view showing:

- Inputs used (with timestamps and links into the operational graph).
- Tool calls in order.
- Rule or workflow step that produced the conclusion.
- The full draft / action.
- History of edits, if any.

### 7.3 Audit log

Filterable by depot, workflow, time, disposition. Exportable for compliance and customer review. Immutable.

### 7.4 Workflow configuration (Tier 1 openness)

Per workflow, customer-tunable parameters surfaced as a simple form:

- Daily readiness check: lead time, SoC tolerance, notification recipients, escalation threshold.
- Monthly consumption report: recipients, template, comparison baseline.
- Charger fault triage: vendor contacts, vendors’ preferred channels, escalation rules.

A workflow author UI (Tier 2/3) is **not** in V1. We will collect customer requests informally and add curated workflows ourselves.

### 7.5 Asset-owner surface

A lighter view focused on monthly/quarterly artifacts, savings narrative, and audit summaries. Same data substrate; different presentation.

-----

## 8. Integrations

### 8.1 Existing (already in production)

- CSMS / OCPP for charger telemetry and control.
- Telematics for vehicle state.
- Charging optimiser.
- BRP / balancing-market interface.

### 8.2 New for the agent

- **Route / block scheduling system.** Read-only in V1. Required for the readiness workflow. Vendor coverage TBD per customer (likely 2-3 systems in pilot).
- **Email (inbound).** IMAP / Microsoft Graph / Gmail API depending on customer. Inbound only in V1. Email content treated as untrusted input — read for context, never executed.
- **Email (outbound) and other comms.** Draft-only in V1. The agent never sends without human approval, regardless of trust tier, in V1.
- **Utility tariff feeds.** Already partially integrated via the optimiser; expose to the agent for explanations.

### 8.3 Deferred to later versions

- Calendar / meetings.
- Vendor portals beyond email-based ticketing.
- Driver-facing app.

-----

## 9. Permission and trust model

### 9.1 Four tiers

Every action belongs to one tier, scoped per (workflow, depot):

1. `inform` — the agent only describes; no actions are proposed.
2. `draft_and_wait` — the agent proposes a specific action; a human approves before execution.
3. `act_and_notify` — the agent executes; the human sees what was done and can roll back.
4. `autonomous` — the agent executes silently; visible only in the audit log.

### 9.2 Default tiers at launch

All V1 workflows ship at `inform` or `draft_and_wait`. No workflow ships at `autonomous` in V1.

### 9.3 Graduation

Workflows graduate based on:

- A minimum number of decisions (typically 100).
- Override rate below threshold (typically 5%).
- Edit rate below threshold (typically 15%).
- Explicit human sign-off from a depot manager and an internal reviewer.

Graduation is per workflow per depot. A workflow autonomous in one depot does not auto-graduate in another.

### 9.4 Demotion

Any of:

- A single safety incident (missed departure, contractual SLA breach).
- A spike in override rate over a rolling window.
- Manual demotion by depot manager.

Demotion is one-click for the depot manager and irreversible without re-graduation.

-----

## 10. Safety and security

### 10.1 Input trust

- Email and any other inbound content are **untrusted**. They are read for context and never interpreted as instructions. The agent system prompt enforces this explicitly.
- Customer-authored workflow parameters are **trusted but sandboxed** — they tune behaviour, they cannot expand the tool allow-list.

### 10.2 Outbound communication

- All outbound emails, vendor tickets, and customer messages are drafted in V1. No tier graduates outbound-comms to autonomy in V1.

### 10.3 Hard operational constraints

The agent and the planner share a constraints layer. These are constraints, not objectives. No optimisation (market revenue, energy cost) may violate them, and no agent action — at any permission tier — may propose violating them. The constraint layer is the same code path used by the existing optimiser.

**Concrete values (substrate-enforced):**

| Constraint | Value | Source |
|---|---|---|
| Vehicle departure SoC | ≥ 99% of scheduled-departure target SoC | Optimiser hard constraint |
| Optimisation solve time | < 60 seconds wall-clock | Optimiser SLO |
| Site grid power | Never exceeds `max_grid_kw` for the site | Optimiser hard constraint |
| Primary charger protocol | OCPP 1.6 (2.0.1 future-ready) | CSMS integration |
| MVP connector type | CCS only | Physical / charger fleet |

Additional constraints that originate outside the optimiser but are equally binding for the agent:

- Driver hours of service (per applicable jurisdiction).
- Contractual SLAs per route / contract.
- Regulatory and security constraints (geo-blocking, wireless-module prohibition, NIS2 obligations) — see `docs/COMPLIANCE_GAP_ANALYSIS.md`, `docs/SECURITY_NETWORK_ARCHITECTURE.md`, `docs/WIRELESS_PROHIBITION_POLICY.md`.

### 10.4 Audit immutability

Decision records are append-only. Edits are recorded as new records referencing the original. No deletion.

### 10.5 Authentication and access

- Per-depot RBAC. Depot managers see their depots only.
- Asset-owner roles see all their depots, read-only on operational data, read-write on report distribution.
- Internal reviewer role for graduation sign-offs.

-----

## 11. Metrics

### 11.1 Product metrics (per depot)

- **Silence rate:** % of mornings the depot manager takes no action.
- **Exception accuracy:** % of surfaced exceptions that the manager confirms as real.
- **Edit rate:** % of agent drafts edited before approval.
- **Override rate:** % of agent proposals rejected.
- **Auto-handled coverage:** count of items handled without manager involvement (with their audit links).
- **Time-to-approve:** median time from exception surfacing to manager disposition.

### 11.2 Workflow metrics

- Decisions per workflow per depot.
- Per-tier distribution and graduation progress.
- Eval pass rate on the latest prompt/tool/version.

### 11.3 Business metrics

- Monthly active depot managers.
- NRR / depot expansion.
- Savings reported per depot per month (already tracked).
- Workflows-per-depot at maturity.

-----

## 12. Phasing

### 12.1 V1 (target: first two pilot depots, current quarter)

- Operational graph: vehicles, chargers, drivers, routes, contracts.
- Single agent with workflow-scoped tools.
- Workflows: daily readiness check, monthly consumption report, charger fault triage.
- Today view, workflow detail with “why,” audit log.
- Tier-1 openness: parameter configuration only.
- Evaluation harness with ≥30 scenarios across the three workflows.
- Default permissions at `inform` / `draft_and_wait`.

### 12.2 V2 (next quarter)

- Add: customer ETA reply drafts, driver no-show reassignment, end-of-day reconciliation.
- First graduations to `act_and_notify` based on V1 data.
- Inbound email integration (read-only).
- Asset-owner surface launched separately from the depot view.

### 12.3 V3 (following quarter)

- Tier-2 openness: workflow composition from primitives.
- Document knowledge graph layer for SOPs, fault-code interpretation, incident retrieval.
- Expansion workflows for heavy trucking pilots.
- Begin shared / templated workflow library across customers.

### 12.4 Beyond V3

- Tier-3 openness (free-form workflow authoring).
- Specialist agents under a coordinator (if and only if a workflow requires it).
- Multi-depot reasoning (cross-depot rebalancing, shared driver pools).

-----

## 13. Non-goals

To prevent scope drift, the following are explicitly **not** in V1 or V2:

- A driver-facing mobile app.
- A no-code or natural-language workflow builder.
- Replacing the existing charging optimiser or balancing-market trading logic.
- Predictive maintenance / battery-health modelling beyond what is needed to flag departure-readiness risk.
- A general-purpose chat interface positioned as the primary surface. Chat remains an escape hatch from the today view.

-----

## 14. Open questions

1. Which route / block scheduling systems must we integrate with for the two pilot depots? Concrete vendors and APIs.
2. What is the customer-side approval threshold for graduating a workflow to `act_and_notify`? Are we comfortable making this a depot-manager-only decision, or does the asset owner need to co-sign?
3. How is the workflow library versioned and rolled out? Per-customer pinning, or auto-update with opt-out?
4. Where does the agent live operationally — same service as the optimiser, or a separate service with its own scaling characteristics?
5. Data residency: are any pilot customers under jurisdictions that constrain where decision records or email content can be stored?
6. Pricing model: does agent usage flow through existing per-depot pricing, or is it a separate line item that we can attach savings to?

-----

## 15. Appendix

### 15.1 Glossary

- **BRP** — Balancing Responsible Party. The entity that takes financial responsibility for imbalances on the grid and that we sell flexibility to.
- **CSMS** — Charging Station Management System. The software layer that talks to physical chargers, typically over OCPP.
- **OCPP** — Open Charge Point Protocol. The standard protocol for charger / management-system communication.
- **SoC** — State of Charge. The battery’s fill level, expressed 0–1 or 0–100%.
- **Demand charge** — A fee on a utility bill based on the highest instantaneous power draw in a billing period; often a larger cost than the energy itself.
- **Block / duty** — Transit term for a vehicle’s scheduled day of work; one block is a sequence of trips assigned to one vehicle.
- **Pull-out** — Morning departure of vehicles from the depot for their first scheduled service.

### 15.2 Substrate references (in-repo)

This PRD describes the product layer. The underlying systems are documented separately and remain authoritative for their respective domains:

- **System architecture:** `docs/ARCHITECTURE.md` — service topology (API service + WebSocket Handler), data flow, DB layout.
- **Component interaction:** `docs/INTERACTION_DIAGRAM.md` — visual map of component relationships.
- **Control loop & optimiser:** `docs/CONTROL_LOOP.md` — DepotController, TriggerMonitor, MILP dispatch.
- **REST + WebSocket APIs:** `docs/API.md` — current endpoint reference, OCPP wiring, agent endpoints.
- **Data analyst guide:** `docs/DATA_ANALYST_GUIDE.md` — optimisation engine files, state assembly, dispatch.
- **Deployment & env vars:** `docs/DEPLOYMENT.md`, `docs/RAILWAY_ENV_VARIABLES.md` — Railway two-service deploy.
- **Simulation harness:** `docs/SIMULATION.md` — scenarios, metrics, optimiser integration.
- **Testing:** `docs/TESTING.md` — categories, markers, coverage targets.
- **Auth & tenancy:** `docs/AUTH_HOOK_SETUP.md`, the tenant-mirroring and Favonius staff auto-promotion sections of `CLAUDE.md`.
- **OCPP runbook & smoke tests:** `docs/PILOT_RUNBOOK.md`, `docs/EVEREST_TESTING.md`.
- **OCPP local auth list roadmap:** `docs/plans/ocpp_local_auth_list_roadmap.md`.
- **Manual charger authorize (frontend):** `docs/frontend/manual_charger_authorize.md`.
- **Regulatory / security:** `docs/COMPLIANCE_GAP_ANALYSIS.md` (Article 73-3, NIS2/TIS2, IEC 62443), `docs/SECURITY_NETWORK_ARCHITECTURE.md`, `docs/SECURITY_DECLARATION_ESO.md`, `docs/WIRELESS_PROHIBITION_POLICY.md`, `docs/VULNERABILITY_DISCLOSURE_POLICY.md`.
- **Engineering preferences & repository conventions:** `CLAUDE.md` (project root).
- **Customer pilot scopes:** to be added per pilot.

### 15.3 Document history

- v0.1 — Initial draft from founder + agent design conversation. Supersedes the previous `PRD_v2_7_Building_Integration.md` for product-level direction; substrate technical specs continue to live in the documents listed in §15.2.
