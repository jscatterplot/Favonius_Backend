# API Specifications

This document is the in-repo reference for the REST + WebSocket APIs and is treated as the source of truth for endpoint surface area. Product-level direction lives in [PRD_Depot_Agent.md](PRD_Depot_Agent.md); see also `CLAUDE.md` for the live endpoint table and rate limits.

## REST API Endpoints

### POST /optimize
Trigger optimization for a depot.

**Request:**
```json
{
    "depot_id": "uuid",
    "horizon_hours": 24,
    "force": false
}
```

**Response:**
```json
{
    "run_id": "uuid",
    "depot_id": "uuid",
    "status": "optimal",
    "objective_value": 1234.56,
    "solve_time_s": 12.3,
    "peak_demand_kw": 450.0,
    "solver_used": "gurobi",
    "schedule": {
        "bus_1": {
            "charging_power": [0, 0, 80, 80, ...],
            "soc": [0.3, 0.3, 0.35, 0.40, ...]
        }
    }
}
```

**Response Fields:**
- `solver_used`: Which solver was used ('gurobi' or 'highs'). Tracks fallback events for monitoring.

**Status Values:**
- `optimal`: Optimal solution found
- `feasible`: Feasible solution found (may have hit time limit)
- `degraded`: Solution found with relaxed constraints (some vehicles may not reach target SoC)
- `infeasible`: No feasible solution found
- `timeout`: Optimization exceeded time limit

**Solver Reliability:**
- Primary solver: Gurobi (commercial, high performance)
- Fallback solver: HiGHS (open-source, automatic fallback if Gurobi fails)
- The `solver_used` field indicates which solver was used for this optimization
- Fallback occurs automatically on Gurobi license failure or connection errors
- See PRD Section 8.2 for detailed solver configuration

**Error Codes:**
- 400: Invalid request (missing depot_id, etc.)
- 404: Depot not found
- 500: Optimization failed (infeasible, timeout)

---

### GET /depots/{depot_id}/state
Get current depot state.

**Response:**
```json
{
    "depot_id": "uuid",
    "timestamp": "2025-12-04T10:00:00Z",
    "vehicle_socs": {
        "bus_1": 0.45,
        "bus_2": 0.82
    },
    "battery_soc": 0.55,
    "current_month_peak_kw": 380.0,
    "current_price_kwh": 0.15,
    "building_load_kw": 45.0,
    "incoming_vehicles": [
        {
            "vehicle_id": "uuid",
            "external_id": "bus_201",
            "expected_soc": 0.35,
            "arrival_time": "2025-12-04T14:30:00Z"
        }
    ]
}
```

---

### GET /depots/{depot_id}/schedule
Get current charging schedule.

**Response:**
```json
{
    "depot_id": "uuid",
    "run_id": "uuid",
    "generated_at": "2025-12-04T09:00:00Z",
    "horizon_start": "2025-12-04T09:00:00Z",
    "horizon_end": "2025-12-05T09:00:00Z",
    "schedule": { ... }
}
```

---

### GET /depots/{depot_id}/alerts
Get active charger faults, last optimization outcome, and pipeline alerts for ops visibility (PRD §7.1, §10.5, AT-16, AT-17).

**Response:**
```json
{
    "depot_id": "uuid",
    "timestamp": "2025-12-04T10:00:00Z",
    "charger_faults": [
        {
            "charger_id": "uuid",
            "ocpp_id": "CP001",
            "connector_id": 1,
            "fault_code": "PowerMeterFailure",
            "timestamp": "2025-12-04T09:55:00Z"
        }
    ],
    "last_optimization": {
        "run_id": "uuid",
        "status": "optimal",
        "solver_used": "gurobi",
        "solve_time_s": 12.3,
        "timestamp": "2025-12-04T09:00:00Z"
    },
    "notification_alerts": [
        {
            "id": "uuid",
            "alert_type": "charger_fault",
            "severity": "critical",
            "title": "Charger CP001 connector 1: Faulted",
            "detail": {"station_id": "CP001", "connector_id": 1, "status": "Faulted", "error_code": "PowerMeterFailure"},
            "status": "active",
            "first_occurrence_at": "2025-12-04T09:55:00+00:00",
            "last_occurrence_at": "2025-12-04T10:00:00+00:00",
            "last_notified_at": "2025-12-04T09:55:30+00:00"
        }
    ]
}
```

- `charger_faults`: Active faults from OCPP StatusNotification (legacy view; raw rows from `connector_status`).
- `last_optimization`: Most recent run; `status` is `optimal`, `feasible`, `degraded`, `infeasible`, `timeout`, or `error`. Omitted if no run exists for the depot.
- `notification_alerts`: Aggregated alerts from the alerts pipeline. `status` is `active` or `acknowledged`; `resolved` rows are not returned. `severity` is `info | warning | critical`. `last_notified_at` is null until the dispatcher first emails it.

**Error Codes:** 400 (invalid depot_id), 404 (depot not found), 500 (server error).

---

### POST /depots/{depot_id}/alerts/{alert_id}/acknowledge
Transition an active notification alert to `acknowledged` (alerts pipeline, AT-17).

**Response:**
```json
{
    "id": "uuid",
    "status": "acknowledged",
    "acknowledged_at": "2025-12-04T10:05:00Z"
}
```

**Error Codes:** 404 (alert not found or doesn't belong to this depot), 409 (alert is not in `active` state — already acknowledged or resolved).

---

### GET /admin/organizations/{org_id}/notification_recipients
List the email recipients subscribed to alerts for an organization. Allowed to `favonius_admin` (cross-tenant; writes `admin.read`) or to a `customer_admin` whose JWT `organization_id` matches the path. Other callers receive 403, NOT 404.

**Query parameters:** `include_inactive=true` returns inactive rows too.

**Response:**
```json
{
    "organization_id": "uuid",
    "recipients": [
        {
            "id": "uuid",
            "organization_id": "uuid",
            "email": "ops@example.com",
            "display_name": "Ops Team",
            "alert_types": ["*"],
            "min_severity": "warning",
            "active": true
        }
    ],
    "count": 1
}
```

---

### POST /admin/organizations/{org_id}/notification_recipients
Create a notification recipient (alerts pipeline). Same RBAC as the GET above.

**Request:**
```json
{
    "email": "ops@example.com",
    "display_name": "Ops Team",
    "alert_types": ["charger_fault"],
    "min_severity": "warning"
}
```

`alert_types` defaults to `["*"]` (all types). `min_severity` defaults to `warning` and is one of `info | warning | critical`.

**Response:** 201 with the created `NotificationRecipientItem`. 409 if `(organization_id, email)` already exists. 400 for an unknown `min_severity`.

---

### PATCH /admin/organizations/{org_id}/notification_recipients/{recipient_id}
Patch a notification recipient. Only `display_name`, `alert_types`, `min_severity`, `active` are mutable. 404 if the recipient doesn't exist or belongs to another org.

---

### DELETE /admin/organizations/{org_id}/notification_recipients/{recipient_id}
Hard delete (cascades `notification_deliveries`). Returns 204 on success, 404 if absent.

---

### POST /webhooks/resend
Resend webhook receiver (alerts pipeline). Public endpoint, signature-verified via `X-Resend-Signature` (or `Svix-Signature`) header using `RESEND_WEBHOOK_SECRET`. Updates `notification_deliveries.status` from `email.delivered | email.bounced | email.complained | email.failed | email.sent` events. Other event types are silently acknowledged.

**Error codes:** 401 (invalid or missing signature), 400 (invalid JSON).

---

### POST /depots/{depot_id}/vehicles/{vehicle_id}/handoff
Send inter-depot handoff message.

**Request:**
```json
{
    "dest_depot_id": "uuid",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
}
```

**Response:**
```json
{
    "message_id": "uuid",
    "status": "sent"
}
```

---

### POST /depots/{depot_id}/handoff/receive
Receive inter-depot handoff message (called by origin depot).

**Request:**
```json
{
    "message_id": "uuid",
    "origin_depot_id": "uuid",
    "vehicle_id": "uuid",
    "external_id": "bus_201",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
}
```

**Response:**
```json
{
    "status": "acknowledged",
    "acknowledged_at": "2025-12-04T10:00:05Z"
}
```

---

### GET /depots/{depot_id}/agent-actions
List depot agent-proposed actions surfaced on the today view. Polled by the frontend; ordered by `created_at` desc. Wire format is camelCase to match the frontend `AgentActionSchema`.

**Response:**
```json
[
    {
        "id": "uuid",
        "depotId": "uuid",
        "agentType": "reporting",
        "actionClass": "report_draft",
        "mode": "proposed",
        "status": "pending",
        "summary": "Generate April 2026 consumption report by RFID card",
        "entityType": null,
        "entityId": null,
        "createdAt": "2026-05-01T00:00:00Z",
        "resolvedAt": null,
        "payload": {
            "kind": "monthly_consumption",
            "groupBy": "card",
            "periodStart": "2026-04-01",
            "periodEnd": "2026-04-30",
            "title": "Monthly consumption — April 2026 (by card)"
        }
    }
]
```

`status` values: `pending | executed | rejected | rolled_back | failed | shadow`. `mode` values: `shadow | proposed | auto_notify | auto_silent`.

---

### GET /depots/{depot_id}/autonomy-settings
Per-depot autonomy matrix. Defaults are returned for the five known action classes (`charger_restart`, `session_reassign`, `price_reoptimize`, `soc_guardrail`, `report_draft`); any rows persisted in `agent_autonomy_settings` (migration 043) layer on top and any extra `actionClass` values surface as additional rows.

**Response:**
```json
{
    "rows": [
        {"actionClass": "charger_restart", "level": "shadow"},
        {"actionClass": "price_reoptimize", "level": "proposed"},
        {"actionClass": "report_draft", "level": "auto_silent"},
        {"actionClass": "session_reassign", "level": "proposed"},
        {"actionClass": "soc_guardrail", "level": "proposed"}
    ],
    "asOf": "2026-05-22T10:00:00Z"
}
```

`level` values: `shadow | proposed | auto_notify | auto_silent`. Single-row writes go through `agents.autonomy.set` on `POST /commands/execute`; a full-matrix replace goes through `PUT` below.

---

### PUT /depots/{depot_id}/autonomy-settings
Replace the **entire** persisted autonomy matrix for the depot, atomically. Requires `depot:manage` (operator+). The request body is the row set to persist; unspecified action classes fall back to defaults in the response (the response shape matches `GET`). Each `level` must be one of `shadow | proposed | auto_notify | auto_silent`; a missing/invalid level or a duplicate `actionClass` returns `422`.

**Request:**
```json
{
    "rows": [
        {"actionClass": "charger_restart", "level": "auto_notify"},
        {"actionClass": "report_draft", "level": "shadow"}
    ]
}
```

An empty `rows` array clears all persisted overrides (the matrix returns to defaults).

---

### GET /depots/{depot_id}/reports/{report_id}/export
Render an approved report. `?format=pdf` (default) returns `application/pdf`; `?format=csv` streams the aggregated rows as `text/csv`. Only available for `approved` reports that carry stored data (404 otherwise); an unknown `format` returns `422`. `Content-Disposition` carries the filename.

---

### POST /commands/execute
Unified command dispatcher (see CLAUDE.md endpoint table for the full command list). Depot agent commands:

| Command | Params | Permission |
|---|---|---|
| `agents.action.approve` | `{actionId}` | `depot:manage` |
| `agents.action.reject` | `{actionId}` | `depot:manage` |
| `agents.action.rollback` | `{actionId}` | `depot:manage` |
| `agents.autonomy.set` | `{actionClass, level}` | `depot:manage` |

`agents.action.approve` on a schedule-originated `report_draft` (payload carries `runId`+`scheduleId`) delivers the already-generated report and flips the originating run to `succeeded`. On a **one-off** `report_draft` (no `scheduleId`) it generates the report and flips the action to `executed`. It does **not** create a recurring schedule — the frontend does that by firing a separate `reports.schedule.create` (monthly, `auto_notify`, recipient = the approving user) right after the approve, and owns the "is one already active?" idempotency check. `agents.autonomy.set` upserts a single row in `agent_autonomy_settings`; `level` must be one of `shadow | proposed | auto_notify | auto_silent` (use `PUT /depots/{id}/autonomy-settings` to replace the whole matrix). Set `dry_run: true` to validate without writes. Every execution writes `COMMAND_EXECUTED` to the security audit log.

**Request:**
```json
{
    "command": "agents.autonomy.set",
    "depot_id": "uuid",
    "params": {"actionClass": "charger_restart", "level": "auto_notify"},
    "dry_run": false
}
```

**Response (real execution):**
```json
{
    "status": "ok",
    "command": "agents.autonomy.set",
    "depot_id": "uuid",
    "result": {
        "actionClass": "charger_restart",
        "level": "auto_notify",
        "updatedAt": "2026-05-22T10:00:00Z"
    }
}
```

---

### GET /health
Health check endpoint.

**Response:**
```json
{
    "status": "healthy",
    "timestamp": "2025-12-04T10:00:00Z",
    "components": {
        "database": "healthy",
        "ocpp_server": "healthy",
        "gurobi_license": "valid",
        "highs_available": "true"
    }
}
```

---

## WebSocket API (OCPP)

The platform implements an OCPP 1.6 Central System at `ws://<host>:9000/{ocpp_id}`.

**Supported Messages:**

| Direction | Message | Purpose |
|-----------|---------|---------|
| CP → CS | BootNotification | Charger registration |
| CP → CS | StatusNotification | Charger status updates |
| CP → CS | MeterValues | Energy, SoC, and max_charge_kw readings |
| CP → CS | StartTransaction | Charging session start |
| CP → CS | StopTransaction | Charging session end |
| CS → CP | SetChargingProfile | Dispatch charging schedule |
| CS → CP | RemoteStartTransaction | Initiate charging |
| CS → CP | RemoteStopTransaction | Stop charging |

**Additional CS-initiated operations (MVP, PRD §7.2):** The following OCPP 1.6/2.0.1 operations are supported for remote control and maintenance. Chargers may support a subset; unsupported requests may return Rejected.

| CS → CP | Purpose |
|---------|---------|
| Reset | Soft or hard reset of the charge point |
| UnlockConnector | Unlock connector (e.g. after session end) |
| ChangeAvailability | Set connector/charge point to Available or Unavailable |
| TriggerMessage | Request charger to send BootNotification, StatusNotification, MeterValues, etc. |
| GetVariables | Read device configuration (OCPP 2.0.1 style) |
| SetVariables | Write device configuration (OCPP 2.0.1 style) |
| UpdateFirmware | Initiate firmware update (URL provided by platform) |

Faults from StatusNotification are exposed via GET /depots/{id}/alerts.

OCPP wiring details live in `src/websocket_handler/` and `src/adapters/ocpp/`; the CLAUDE.md "OCPP Implementation" section is the live operational summary.

---

---

## Depot Chat Agent (`/agent/*`)

The agent endpoints expose the depot chat interface that converts plain-English
questions about charging data into SQL-sourced answers.  They are mounted only
when `AGENT_SEARCH_ENABLED=true` (default **on** since sprint B6).  All three
share the global JWT auth middleware and the geo-block middleware; the two turn
endpoints additionally enforce a **10 req/min per user** rate limit (same cadence
as `POST /optimize`).

Implementation lives in `src/api/agent/`; this feature is positioned as a precursor to the broader Depot Agent product (`docs/PRD_Depot_Agent.md`).

---

### POST /agent/turn

Run one chat turn synchronously.  Blocks until the answer is ready, then returns
the complete `AgentReply`.

**Rate limit:** 10 req/min per authenticated user.

**Request:**
```json
{
    "message": "Why is this one down?",
    "context": {
        "depotId": "uuid",
        "focus": {"type": "charger", "id": "CP-7"},
        "view": {"page": "charger_detail", "filters": {"status": "faulted"}}
    }
}
```

| Field | Type | Constraints |
|---|---|---|
| `message` | `string` | 1–2000 characters |
| `context` | `object` | Optional. UI state describing what the user has open. |
| `context.depotId` | `uuid` | Optional. Selected depot; intersected server-side with the caller's visible depots (can only narrow scope, never widen). |
| `context.focus` | `object` | Optional `{type, id}` for the focused item (e.g. an open charger). Treated as an unverified hint. |
| `context.view` | `object` | Optional free-form hint blob (page name, filters, time-range, labels). Serialized size capped at 4 KB → `422` if exceeded. |

**About `context`:** it is **not** injected into the prompt. On the general-analytics
(SQL) path the agent pulls it on demand via a `get_page_context` tool **only when a
question is ambiguous** (e.g. "why is *this* one down?"), then runs its own depot-scoped
query. The consumption fast path ignores it. Every answer still comes from the database
through the existing auth fence — the context only steers interpretation. Omit it and the
agent behaves exactly as before.

**Response — success:**
```json
{
    "run_id": "uuid",
    "status": "success",
    "text": "John Smith consumed 83.9 kWh last month across 2 sessions.",
    "intent": "consumption_by_user",
    "candidates": [],
    "not_found": []
}
```

**Response — disambiguation** (multiple drivers match the name):
```json
{
    "run_id": "uuid",
    "status": "disambiguation",
    "text": "I found multiple matches. Please clarify which one you mean:\n- John Smith (Vilnius)\n- John Petrauskas (Vilnius)",
    "intent": null,
    "candidates": [
        {"kind": "driver", "display": "John Smith (Vilnius)", "primary_id": "uuid"},
        {"kind": "driver", "display": "John Petrauskas (Vilnius)", "primary_id": "uuid"}
    ],
    "not_found": []
}
```

**Response — not_found:**
```json
{
    "run_id": "uuid",
    "status": "not_found",
    "text": "I couldn't find 'driver 999' in your depots. Double-check the spelling…",
    "intent": null,
    "candidates": [],
    "not_found": ["driver 999"]
}
```

**Response — error** (LLM or DB failure):
```json
{
    "run_id": "uuid",
    "status": "error",
    "text": "Something went wrong handling your request. Please try again.",
    "intent": null,
    "candidates": [],
    "not_found": []
}
```

**`status` values:** `success` | `disambiguation` | `not_found` | `error`

**Error codes:**
- 401 — missing or invalid JWT
- 422 — `message` length out of range, or `context.view` exceeds the 4 KB cap
- 429 — rate limit exceeded (headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`)
- 502 — orchestrator failed (details logged server-side only)
- 503 — database pool not initialised

---

### POST /agent/turn/stream

Run one chat turn and stream step events via **Server-Sent Events** (SSE).
Returns `200 OK` with `Content-Type: text/event-stream` immediately; the
orchestrator runs in a background task and emits events as it completes each
phase.  On failure an `error` event is emitted and the stream closes — the HTTP
status remains `200` because headers were already sent.

**Rate limit:** 10 req/min per authenticated user (same bucket as `/agent/turn`).

**Request body:** identical to `POST /agent/turn`.

**SSE event contract:**

Each event is encoded as:
```
event: <name>\n
data: <json>\n
\n
```

| Event name | When emitted | `data` shape |
|---|---|---|
| `step` | After each pipeline phase completes | `{"name": "<phase>", "summary": "<user-facing progress text>"}` |
| `answer` | After the final phase | Full `AgentReply` JSON (same shape as the sync endpoint) |
| `error` | On unrecoverable failure | `{"status": <http_code>, "detail": "<message>"}` |

**Phase names** (consumption fast path, emitted in order): `planner_decision` →
`extract_plan` → `resolve_entities` → `compile` → `execute` → *(answer emitted
next)*.  The `compile` and `execute` steps are skipped when the turn
short-circuits at disambiguation or not-found.  In SQL mode the planner routes
to a tool-use loop instead: each tool call is emitted as a step with
`name: "tool_call"` and a per-tool `summary`.

The `summary` is user-facing progress text meant for direct display (e.g.
"Querying charging & telemetry data", "Composing your answer").  The full
technical trace for each step is persisted to `agent_runs.steps_json` and
returned by `GET /agent/runs/{id}` — it is not duplicated into the SSE summary.

**Required client headers for proxies:**
```
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```
These are set in the response automatically (`SSE_HEADERS` in `src/api/agent/stream.py`).

**Example SSE sequence (success):**
```
event: step
data: {"name": "planner_decision", "summary": "Understanding your question"}

event: step
data: {"name": "extract_plan", "summary": "Working out what you're asking"}

event: step
data: {"name": "resolve_entities", "summary": "Finding who and what you mentioned"}

event: step
data: {"name": "compile", "summary": "Preparing the query"}

event: step
data: {"name": "execute", "summary": "Fetching the data"}

event: answer
data: {"run_id": "…", "status": "success", "text": "John Smith consumed 83.9 kWh…", "intent": "consumption_by_user", "candidates": [], "not_found": []}
```

---

### GET /agent/runs/{run_id}

Fetch the stored `agent_runs` row for a completed turn.  Useful for rendering
the collapsible reasoning panel in the UI.

**Access control:** The row's `user_id` must match the caller's `sub` JWT claim,
**or** the caller must be `favonius_admin`.  A mismatched tenant user receives
`404` (not `403`) to avoid run-ID enumeration.

**Path parameter:** `run_id` — UUID of the run.

**Response:**
```json
{
    "run_id": "uuid",
    "user_id": "uuid",
    "organization_id": "uuid",
    "depot_id": null,
    "user_message": "How much did John charge last month?",
    "final_intent": "consumption_by_user",
    "steps_json": [
        {"name": "extract_plan", "payload": {"intent": "consumption_by_user", …}},
        {"name": "resolve_entities", "payload": […]},
        {"name": "compile", "payload": {"intent": "consumption_by_user", "param_shapes": […]}},
        {"name": "execute", "payload": {"row_count": 2}}
    ],
    "status": "success",
    "duration_ms": 3241,
    "created_at": "2026-05-05T12:34:56.789Z"
}
```

| Field | Notes |
|---|---|
| `status` | `running` \| `success` \| `disambiguation` \| `not_found` \| `error` |
| `steps_json` | Ordered list of step records appended during the turn |
| `duration_ms` | Wall-clock time from open to close; `null` while `status='running'` |
| `depot_id` | Currently always `null` (reserved for multi-depot scoping in v1) |

**Error codes:**
- 401 — invalid JWT
- 404 — run not found, or belongs to another user

---

## Scheduled Reports

Configurable per-depot report schedules. The worker fires due schedules every
minute, generates a `Report` (visible at `GET /depots/{id}/reports`), renders a
PDF/CSV, and emails it to the schedule's recipients. All timestamps are
ISO-8601 UTC (`…Z`); all body fields are camelCase.

### GET /depots/{depot_id}/report-schedules

List schedules for the depot. Any depot member. Returns `[]` (not 404) when the
depot has no schedules. Each item is a `ReportSchedule`:

```json
{
  "id": "uuid", "depotId": "uuid", "name": "Monthly electricity consumption",
  "kind": "monthly_consumption", "groupBy": "card", "frequency": "monthly",
  "dayOfMonth": 1, "dayOfWeek": null, "timeOfDay": "06:00",
  "autonomyMode": "auto_silent", "isActive": true,
  "recipients": [
    {"recipientId": "uuid", "emailAddress": "manager@depot.example",
     "format": "pdf", "lastDelivery": {"status": "sent", "attemptedAt": "2026-06-01T03:00:05Z"}}
  ],
  "nextRunAt": "2026-07-01T03:00:00Z", "lastRunAt": "2026-06-01T03:00:00Z",
  "lastRunStatus": "succeeded", "createdAt": "2026-05-20T09:00:00Z",
  "createdBy": "uuid", "updatedAt": "2026-05-20T09:00:00Z"
}
```

`nextRunAt`/`lastRunAt`/`lastRunStatus` and `recipients[].lastDelivery` are
always present (may be `null`); `recipients` is always an array.

### GET /depots/{depot_id}/report-schedules/{schedule_id}

Single `ReportSchedule`. 404 when not found.

### GET /depots/{depot_id}/report-schedules/{schedule_id}/runs

`ScheduleRun[]`, most-recent first:

```json
{
  "runId": "uuid", "scheduleId": "uuid", "reportId": "uuid",
  "triggeredAt": "2026-06-01T03:00:00Z", "completedAt": "2026-06-01T03:00:06Z",
  "status": "succeeded",
  "deliveries": [
    {"recipientId": "uuid", "emailAddress": "manager@depot.example", "format": "pdf",
     "status": "sent", "attemptedAt": "2026-06-01T03:00:05Z",
     "providerMessageId": "…", "error": null}
  ],
  "errorMessage": null
}
```

### Mutations — POST /commands/execute

`customer_admin` or higher (`ADMIN_CONFIG`). The handler's domain object is
returned as `result`; the dispatcher wraps it as `{status, command, depot_id, result}`.

| `command` | `params` | `result` |
|---|---|---|
| `reports.schedule.create` | `{ input: ScheduleCreatePayload }` | `ReportSchedule` (with computed `nextRunAt`) |
| `reports.schedule.update` | `{ scheduleId, patch: SchedulePatchPayload }` | updated `ReportSchedule` |
| `reports.schedule.delete` | `{ scheduleId }` | `{ scheduleId, deleted: true }` |
| `reports.schedule.run_now` | `{ scheduleId }` | `ScheduleRun` (with populated `reportId`) |

`ScheduleCreatePayload` = `ReportSchedule` minus server-assigned fields. In a
`SchedulePatchPayload` all fields are optional; `recipients[]` replaces the full
list when present (absent = unchanged). Enums: `ScheduleRunStatus` =
`succeeded|failed|skipped|pending_approval`; `DeliveryStatus` =
`sent|failed|bounced|suppressed`. In `proposed` autonomy the run is
`pending_approval` until approved via `agents.action.approve`.

---

## Traffic-Fine Triage (`/admin/depots/{id}/traffic-fines`)

Gated by `TRAFFIC_FINE_AGENT_ENABLED` (default off). An operator uploads a fine
document; a Depot Agent workflow extracts the issuing authority, amounts,
early-payment deadline, and IBAN (multimodally), and the platform alerts the
Logistics Manager (via the notifications pipeline) when the early-payment
discount is closing within the configured window (default 48h).

### POST /admin/depots/{depot_id}/traffic-fines

Upload a fine document for triage. **Role:** `customer_admin` or `favonius_admin`
(depot-scoped).

- **Body:** the raw document bytes (PDF or PNG/JPEG/GIF/WEBP). Content-Type is
  informational; the type is confirmed by magic bytes. Unsupported types → 415.
  Bodies above `TRAFFIC_FINE_UPLOAD_MAX_BYTES` (default 10 MiB) → 413.
- **Query:** `file_name` (optional original filename).
- **Response (202):** `{ "id": "<uuid>", "status": "received", "status_url": "/admin/depots/{depot_id}/traffic-fines/{id}" }`.
  Extraction + evaluation run asynchronously; poll the detail endpoint.

### GET /admin/depots/{depot_id}/traffic-fines

List a depot's uploaded fines (most recent first; no raw bytes). Any depot
member. Returns `{ "fines": [ <fine>, … ] }`.

### GET /admin/depots/{depot_id}/traffic-fines/{fine_id}

Fetch one fine. Any depot member. Returns the row including `status`
(`received` | `parsing` | `parsed` | `alerted` | `no_alert` | `parse_failed` |
`unsupported_media`), the extracted fields (`issuing_authority`,
`issuing_country`, `fine_reference`, `currency`, `full_amount`,
`early_payment_amount`, `discount_amount`, `iban`, `early_payment_deadline`),
the evaluation (`evaluation_kind`, `hours_until_deadline`, `within_alert_window`),
and `decision_id` / `alert_id` links. 404 if the fine does not exist for the
depot.

**Alert:** when the discount is closing within the window, an alert of type
`traffic_fine_early_payment` is raised; the title is the verbatim operator
message (`Priority: Early payment discount for Fine #[ID] expires in 2 days.
Automate payment now to save €[Discount Amount]?`). Register the Logistics
Manager as a `notification_recipients` row subscribed to that alert type (or
`*`).

---

## Implementation Notes

- All endpoints use FastAPI framework
- Authentication: JWT tokens (1 hour access, 24 hour refresh) - per PRD Section 10.3
- TLS: Required for all exposed ports in production (PRD Section 10.3)
- Error responses follow consistent format
- Request/response logging included
- OpenAPI schema auto-generated from FastAPI

For product-level direction, see [PRD_Depot_Agent.md](PRD_Depot_Agent.md). The OpenAPI schema auto-generated by FastAPI (`/docs`) is the live machine-readable contract.

