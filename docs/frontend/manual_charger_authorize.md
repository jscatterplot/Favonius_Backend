# Manual Charger Authorize — Frontend Brief

A `customer_admin` can press a "Manual Authorize" button on the charger detail
view to start a charging session at a specific connector without needing the
driver to scan an RFID card. Backed by the operator-override mechanism in
`migrations/031_operator_authorization_overrides.sql` + `RFIDAuthorizationService`.

## API surface

### `POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize`

Mints a one-shot synthetic idTag and dispatches `RemoteStartTransaction` to the
charger via the queue. Auth: `customer_admin` (or `favonius_admin`) with tenant
access to the depot.

```http
POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize HTTP/1.1
Authorization: Bearer <jwt>
Content-Type: application/json
Idempotency-Key: <uuid v4 generated client-side>

{
  "connector_id": 1,
  "expires_in_seconds": 60,
  "reason": "Driver forgot card"
}
```

**Body:**
- `connector_id` (int, **required**) — 1-based connector to authorize.
- `expires_in_seconds` (int, optional, default `60`, max `300`) — how long the
  synthetic tag stays valid.
- `reason` (string, optional, max 200 chars) — operator note for the audit trail.

**Idempotency:** The `Idempotency-Key` header is accepted and recorded in the
audit metadata. The natural dedupe is the per-connector cooldown — a second
POST within 60 s returns `409 RECENT_OVERRIDE_EXISTS` regardless of the key.

**Responses:**
| Status | Shape |
|---|---|
| `200 OK` | `{ "status": "Accepted", "override_id": "<uuid>", "expires_at": "<iso8601>", "queue_id": <int>, "transaction_started": false }` |
| `409` | `{ "error_code": "RECENT_OVERRIDE_EXISTS", "message": "...", "retry_after_seconds": <int>, "active_override_id": "<uuid>", "expires_at": "<iso8601>" }` |
| `404` | `{ "error_code": "CHARGER_NOT_FOUND", "message": "..." }` |
| `403` | `{ "error_code": "FORBIDDEN_ROLE" \| "FORBIDDEN_DEPOT", ... }` |
| `422` | `{ "error_code": "VALIDATION_ERROR", "message": "..." }` |

Note: `transaction_started` is always `false` in the immediate response —
the charger has only been told to start, not yet sent `StartTransaction.req`
back to us. Use polling (below) to detect the actual transition.

### `GET /admin/depots/{depot_id}/chargers/{charger_id}/last_manual_override`

Returns the most recent manual authorization for this charger, used by the
admin "Last manual override" panel. Auth: `customer_admin`, `customer_operator`,
or `favonius_admin`.

```json
{
  "id": "<uuid>",
  "connector_id": 1,
  "created_at": "<iso8601>",
  "created_by": "<user uuid>",
  "reason": "Driver forgot card",
  "expires_at": "<iso8601>",
  "consumed_at": "<iso8601 | null>"   // null while the charger hasn't
                                      //   sent Authorize/StartTransaction yet
}
```

`404 NO_MANUAL_OVERRIDE` when the charger has never had a manual auth.

## UI specification

### Button placement
- Charger detail page, in the connector status panel (one button per connector).
- Visible only to roles: `customer_admin`, `favonius_admin`. Hide entirely for
  `customer_operator` and below — operators get the read-only "Last manual
  override" indicator but not the action button.

### Enabled state
**Enabled** when ALL of:
- Charger is online (`charger.connection_state == "online"`).
- Connector status ∈ `{"Preparing", "Available"}` — Preparing = vehicle plugged
  + awaiting auth; Available also accepted because some firmwares delay the
  Preparing transition until the charger sees a card-read attempt.
- No active transaction on this connector.
- No successful manual auth within the last 60 s (client-side cooldown is a
  UX nicety; backend enforces too).

**Disabled** otherwise. Show a tooltip explaining why:
- "Charger offline"
- "Already in use" (active transaction)
- "Cooldown — wait Ns" (with countdown)
- "Charger needs a vehicle plugged in" (status `Available` with no recent
  Preparing transition).

### Confirmation modal
On click, open a modal:

```
Title: "Manually authorize a charging session?"

Body:
"This will start a charging session on [Charger Display Name],
connector [N], without requiring an RFID scan. Use this only when
the RFID reader has failed or no card is available.

Your name and the timestamp will be recorded in the audit log."

Optional textarea:
"Reason (optional)" — bound to body.reason

Confirmation:
"Type the connector number to confirm" — small text input that must
equal connector_id before [Authorize] is enabled. Defends against
misclicks and wrong-connector errors.

Actions: [Cancel]   [Authorize]
```

### Submission flow
1. On `[Authorize]` click:
   - Disable button + show spinner.
   - Generate a fresh `Idempotency-Key` (UUID v4).
   - POST.
2. On `200`:
   - Toast: "Authorized — charging should start within a few seconds."
   - Start accelerated polling of `GET /depots/{depot_id}/state` at **1 s**
     intervals for **30 s**, then back to the default 5 s.
3. On `409`:
   - Toast: "A manual auth is already pending — wait Ns and try again."
4. On `403`:
   - Toast: "You don't have permission to authorize this charger."
5. On `404` or `422`:
   - Toast with the server's error message.
6. On `5xx` or network failure:
   - Toast: "Couldn't reach the platform. Retry in a moment." Re-enable the
     button after the spinner clears (no cooldown applied — the request may
     not have landed).

### Detecting the transition
- The charger detail page already polls `/depots/{depot_id}/state` every 5 s.
- After a successful `manual_authorize`, accelerate that polling to **1 s for
  30 s**, then revert.
- Watch the connector's `status` field. Expected progression:
  `Preparing → Charging` within 2-5 s.
- If after 30 s the status hasn't moved past `Preparing`, show a sticky
  warning: "Charger accepted the override but didn't start. Check the
  charger or unplug/replug the cable." Surface `last_heartbeat_at` and
  the most recent `SecurityEventNotification` if available so the operator
  can diagnose.

### Last-override indicator (visible to all admin/operator roles)
When `GET .../last_manual_override` returns a row:

```
Last manual override: <created_by display name> @ <relative timestamp>
[Reason: "<reason>"]                                    [show/hide details]
```

Click "show/hide details" to expose `id`, `expires_at`, `consumed_at`. This
helps operators avoid duplicate manual auths on the same connector and
provides quick context when something goes wrong.

## Behavior summary for the user

1. Driver plugs in a vehicle but the RFID scan fails (or no card).
2. Operator opens charger detail page, sees connector in `Preparing` state.
3. Clicks "Manual Authorize", confirms in the modal.
4. Backend creates a 60 s synthetic auth + tells the charger to start.
5. Within 1-3 s the connector status flips to `Charging`. Frontend shows
   green "Charging" indicator.
6. The synthetic tag is consumed exactly once — a second click during the
   60 s window returns `409`. After 60 s, an unconsumed override is
   inert (the `expires_at` check blocks reuse).

## Backend behavior reference (for context, no action required)

- The endpoint inserts one row into `operator_authorization_overrides`
  (TimescaleDB) and one into `charging_command_queue`.
- The WS handler's `ChargingCommandQueueConsumer` drains the queue every
  ~2 s (faster via `pg_notify`) and dispatches `RemoteStartTransaction` via
  `OCPP16Session.send_remote_start_transaction`.
- When the charger sends `Authorize` and/or `StartTransaction.req` with the
  synthetic tag, `RFIDAuthorizationService.authorize` finds and atomically
  consumes the override — returns `Accepted`.
- A row in `audit_log` (action `charger.manual_authorize`) records the actor,
  depot, charger, connector, override_id, queue_id, and reason.

## Out of scope for this feature

- **Cancelling a pending override.** If an override is minted and the operator
  changes their mind, currently they have to wait for the 60 s expiry.
  Adding `DELETE /admin/depots/.../manual_authorize/{override_id}` would be
  a small follow-up but isn't in the initial PR.
- **Showing the synthetic id_tag.** It's an internal implementation detail;
  the UI never needs to reveal `OP-<uuid>` to operators.
- **OCPP 2.0.1 chargers.** This brief covers OCPP 1.6 (the pilot's chargers).
  The OCPP 2.0.1 path will need parallel wiring through
  `EnhancedOCPPChargePoint.send_remote_start_transaction` — separate PR when
  the first OCPP 2.0.1 charger lands.
