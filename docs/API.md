# API Specifications

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD_v2_7_Building_Integration.md#7-api-specifications](PRD_v2_7_Building_Integration.md#7-api-specifications).

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

For detailed OCPP integration specifications, see [PRD_v2_7_Building_Integration.md#9-1-ocpp-integration](PRD_v2_7_Building_Integration.md#9-1-ocpp-integration).

---

---

## Depot Chat Agent (`/agent/*`)

The agent endpoints expose the depot chat interface that converts plain-English
questions about charging data into SQL-sourced answers.  They are mounted only
when `AGENT_SEARCH_ENABLED=true` (default **on** since sprint B6).  All three
share the global JWT auth middleware and the geo-block middleware; the two turn
endpoints additionally enforce a **10 req/min per user** rate limit (same cadence
as `POST /optimize`).

Full design: `docs/plans/agent_search_architecture_v0.md`.
PRD: `docs/plans/agent_search_prd_v1.md`.

---

### POST /agent/turn

Run one chat turn synchronously.  Blocks until the answer is ready, then returns
the complete `AgentReply`.

**Rate limit:** 10 req/min per authenticated user.

**Request:**
```json
{
    "message": "How much did John charge last month?"
}
```

| Field | Type | Constraints |
|---|---|---|
| `message` | `string` | 1–2000 characters |

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
- 422 — `message` length out of range
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
| `step` | After each pipeline phase completes | `{"name": "<phase>", "summary": "<human-readable summary>"}` |
| `answer` | After the final phase | Full `AgentReply` JSON (same shape as the sync endpoint) |
| `error` | On unrecoverable failure | `{"status": <http_code>, "detail": "<message>"}` |

**Phase names** (emitted in order): `extract_plan` → `resolve_entities` →
`compile` → `execute` → *(answer emitted next)*.  The `compile` and `execute`
steps are skipped when the turn short-circuits at disambiguation or not-found.

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
data: {"name": "extract_plan", "summary": "consumption_by_user, 1 subject(s), last_month"}

event: step
data: {"name": "resolve_entities", "summary": "driver: John Smith (Vilnius)"}

event: step
data: {"name": "compile", "summary": "consumption_by_user SQL prepared"}

event: step
data: {"name": "execute", "summary": "2 rows returned"}

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

## Implementation Notes

- All endpoints use FastAPI framework
- Authentication: JWT tokens (1 hour access, 24 hour refresh) - per PRD Section 10.3
- TLS: Required for all exposed ports in production (PRD Section 10.3)
- Error responses follow consistent format
- Request/response logging included
- OpenAPI schema auto-generated from FastAPI

For complete specifications, see [PRD_v2_7_Building_Integration.md](PRD_v2_7_Building_Integration.md).

