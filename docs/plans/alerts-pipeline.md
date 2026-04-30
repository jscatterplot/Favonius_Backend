# Alerts Pipeline — Implementation Plan

**Branch:** `claude/alerts-pipeline-migration-7oK0g`
**Status:** plan locked, implementation pending
**Authoritative spec context:** `docs/PRD_v2_7_Building_Integration.md` (re-optimization triggers, Section 5.1; alerts endpoint, Section 10)

This document captures the decisions from the four-section plan review (Architecture / Code Quality / Tests / Performance) so the implementation work has a single source of truth. Every choice below was made interactively; deviations during coding require revisiting this doc.

---

## 1. Goal

Add a depot-level alerts pipeline that:

1. Detects fault conditions at the source (initially: charger faults from `connector_status`; extensible to optimization failures, telemetry stalls, price-feed gaps).
2. Aggregates them into a queryable `notification_alerts` table the API can read.
3. Dispatches email notifications via Resend to a configurable per-org recipient list, with severity gating.
4. Surfaces delivery status (sent / bounced / complained) so the frontend can show "delivery healthy" badges.

This work intentionally **does not** add SMS, Slack, PagerDuty, or in-app push. The `EmailDeliveryClient` interface leaves room for a second channel later without re-architecting.

---

## 2. Locked decisions

| # | Section | Issue | Choice | Rationale |
|---|---|---|---|---|
| 1.1 | Architecture | Producer location | A — fault detection in connector_status writers + a Postgres trigger; dispatcher in WS handler | Keeps detection close to the source of truth; trigger guarantees no missed transitions |
| 1.2 | Architecture | Notification trigger | A — pg_notify + LISTEN with 30s polling fallback | Sub-second fast path; polling backstop covers dispatcher restarts (see 4.4) |
| 1.3 | Architecture | State storage | A — `notification_alerts` aggregator + `notification_deliveries` ledger | Aggregator dedupes; ledger is the audit trail and powers webhook updates |
| 1.4 | Architecture | Recipient model | A — DB-backed `notification_recipients` per org with severity threshold | Self-service via API; per-org severity gating |
| 2.1 | Code quality | Severity mapping | A — single Python module + DB-side generated column from severity string | One source of truth; DB index can use the int |
| 2.2 | Code quality | Dispatcher loop | A — single `_tick()` with explicit phases (claim → fetch recipients → render → send → record) | Easy to test phase-by-phase |
| 2.3 | Code quality | Resend client | A — `EmailDeliveryClient` interface + `ResendEmailClient` impl | Tests use FakeEmailClient; future channels slot in |
| 2.4 | Code quality | Templates | A — Jinja2 templates in `src/notifications/templates/email/` | Standard, readable, testable |
| 3.1 | Tests | Producer tests | A — Postgres trigger tests in tests/integration + adapter unit tests | Covers SQL trigger logic and the Python paths |
| 3.2 | Tests | Dispatcher tests | A — full integration with FakeEmailClient | Validates the full DB → email path without hitting Resend |
| 3.3 | Tests | Frontend feedback | A — Resend webhook + status column on notification_deliveries | Frontend reads delivery health from the same source of truth |
| 3.4 | Tests | Acceptance | A — AT-08 scenarios in tests/e2e covering fault → email → ack → resolve | Mirrors the AT-01..07 PRD pattern |
| 4.1 | Performance | Worker safety | C — in-memory `_currently_sending: set[UUID]` + single-worker assumption | Smallest diff; we run exactly one WS handler today. Trip-wire: TODO + startup assertion if `WEB_CONCURRENCY > 1` |
| 4.2 | Performance | Recipient query | A — per-alert indexed query | ~1ms with `(organization_id, severity_level)` index; new recipients live within one tick |
| 4.3 | Performance | Alerts UNION | A — indexed UNION ALL of connector_status DISTINCT ON + notification_alerts | One round-trip; verify connector_status index from migration 016 still covers it |
| 4.4 | Performance | NOTIFY safety | A — NOTIFY + 30s reconciliation backstop | Survives Railway deploys and PG NOTIFY queue overflow |

---

## 3. Database changes — migration 022

File: `migrations/022_alerts_pipeline.sql`

### 3.1 New tables

**`notification_alerts`** — depot-scoped aggregator
```
id                  uuid PK default gen_random_uuid()
organization_id     uuid NOT NULL REFERENCES organizations(id)
depot_id            uuid REFERENCES depots(id)               -- NULLable for org-scoped alerts
alert_type          text NOT NULL                            -- 'charger_fault' | 'optimization_failed' | ...
severity            text NOT NULL                            -- 'info' | 'warning' | 'critical'
severity_level      smallint GENERATED ALWAYS AS (CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END) STORED
title               text NOT NULL
detail              jsonb NOT NULL DEFAULT '{}'              -- producer-specific payload
dedup_key           text NOT NULL                            -- e.g. 'charger_fault:<station_id>:<connector_id>'
status              text NOT NULL DEFAULT 'active'           -- 'active' | 'acknowledged' | 'resolved'
first_occurrence_at timestamptz NOT NULL DEFAULT now()
last_occurrence_at  timestamptz NOT NULL DEFAULT now()
last_notified_at    timestamptz                              -- NULL until first dispatch
last_notified_count smallint NOT NULL DEFAULT 0              -- monotonic, used as idempotency salt
acknowledged_at     timestamptz
acknowledged_by     uuid                                     -- user_id from JWT sub
resolved_at         timestamptz
created_at          timestamptz NOT NULL DEFAULT now()
updated_at          timestamptz NOT NULL DEFAULT now()
UNIQUE (organization_id, dedup_key, status)                  -- partial unique on status != 'resolved' (see CHECK below)
```
Partial unique index: `CREATE UNIQUE INDEX uq_notification_alerts_active ON notification_alerts (organization_id, dedup_key) WHERE status != 'resolved';` — lets the same dedup_key reappear after resolution.
Indexes:
- `(depot_id, status, severity_level DESC, last_occurrence_at DESC)` — powers `/depots/{id}/alerts`
- `(status, last_notified_at)` — powers dispatcher claim query

**`notification_deliveries`** — append-only ledger
```
id                  uuid PK default gen_random_uuid()
alert_id            uuid NOT NULL REFERENCES notification_alerts(id)
recipient_id        uuid NOT NULL REFERENCES notification_recipients(id)
notified_count      smallint NOT NULL                        -- matches notification_alerts.last_notified_count at send
channel             text NOT NULL DEFAULT 'email'
provider_message_id text                                     -- Resend message ID
status              text NOT NULL DEFAULT 'sent'             -- 'sent' | 'delivered' | 'bounced' | 'complained' | 'failed'
status_detail       jsonb
sent_at             timestamptz NOT NULL DEFAULT now()
provider_updated_at timestamptz                              -- updated by webhook
UNIQUE (alert_id, recipient_id, notified_count)              -- idempotency anchor
```
Index: `(provider_message_id)` for webhook lookups.

**`notification_recipients`** — per-org subscription
```
id                  uuid PK default gen_random_uuid()
organization_id     uuid NOT NULL REFERENCES organizations(id)
email               text NOT NULL
display_name        text
alert_types         text[] NOT NULL DEFAULT '{*}'            -- '{*}' = all types; otherwise subset
min_severity        text NOT NULL DEFAULT 'warning'          -- 'info' | 'warning' | 'critical'
min_severity_level  smallint GENERATED ALWAYS AS (CASE min_severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END) STORED
active              boolean NOT NULL DEFAULT TRUE
created_at          timestamptz NOT NULL DEFAULT now()
updated_at          timestamptz NOT NULL DEFAULT now()
UNIQUE (organization_id, email)
```
Index: `(organization_id, active, min_severity_level) INCLUDE (email, alert_types)`.

### 3.2 Trigger on `connector_status`

```
CREATE FUNCTION fn_alerts_on_connector_status() RETURNS trigger AS $$
DECLARE
  org_id uuid;
  dep_id uuid;
BEGIN
  IF NEW.status NOT IN ('Faulted', 'Unavailable') THEN
    -- transition out → resolve any active alert for this charger
    UPDATE notification_alerts
       SET status = 'resolved', resolved_at = now(), updated_at = now()
     WHERE dedup_key = format('charger_fault:%s:%s', NEW.station_id, NEW.connector_id)
       AND status != 'resolved';
    RETURN NEW;
  END IF;

  SELECT d.organization_id, d.id INTO org_id, dep_id
    FROM chargers c JOIN depots d ON d.id = c.depot_id
   WHERE c.ocpp_id = NEW.station_id LIMIT 1;
  IF org_id IS NULL THEN RETURN NEW; END IF;

  INSERT INTO notification_alerts (
    organization_id, depot_id, alert_type, severity, title, detail, dedup_key
  ) VALUES (
    org_id, dep_id, 'charger_fault',
    CASE NEW.status WHEN 'Faulted' THEN 'critical' ELSE 'warning' END,
    format('Charger %s connector %s: %s', NEW.station_id, NEW.connector_id, NEW.status),
    jsonb_build_object('station_id', NEW.station_id, 'connector_id', NEW.connector_id,
                       'status', NEW.status, 'error_code', NEW.error_code),
    format('charger_fault:%s:%s', NEW.station_id, NEW.connector_id)
  )
  ON CONFLICT (organization_id, dedup_key) WHERE status != 'resolved'
  DO UPDATE SET
    last_occurrence_at = now(),
    severity = EXCLUDED.severity,
    detail = EXCLUDED.detail,
    updated_at = now();

  PERFORM pg_notify('notification_alerts_new',
    jsonb_build_object('organization_id', org_id, 'depot_id', dep_id,
                       'dedup_key', format('charger_fault:%s:%s', NEW.station_id, NEW.connector_id))::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_alerts_on_connector_status
  AFTER INSERT ON connector_status
  FOR EACH ROW EXECUTE FUNCTION fn_alerts_on_connector_status();
```

### 3.3 Index verification

Confirm `connector_status (station_id, connector_id, recorded_at DESC)` exists from migration 016. If not, add it as part of 022.

---

## 4. Code layout

### 4.1 New module: `src/notifications/`

```
src/notifications/
├── __init__.py
├── severity.py              # Severity enum + str→int mapping (mirrors DB generated column)
├── alerts.py                # AlertRepository: create/upsert/ack/resolve helpers (used by webhooks etc.)
├── recipients.py            # RecipientRepository: list_for_org_filtered(org_id, alert_type, severity_level)
├── dispatcher.py            # AlertDispatcher: LISTEN + 30s poll, _tick() with claim/render/send/record phases
├── email_client.py          # EmailDeliveryClient (Protocol) + EmailMessage dataclass
├── resend_client.py         # ResendEmailClient implementing EmailDeliveryClient
├── webhook.py               # Resend webhook handler — updates notification_deliveries.status
└── templates/
    └── email/
        ├── base.html.j2
        ├── charger_fault.html.j2
        ├── charger_fault.txt.j2
        └── _shared.j2
```

### 4.2 Wire-in points

- **`src/websocket_handler/main.py`** — start `AlertDispatcher` as an asyncio task alongside `ChargingCommandQueueConsumer`. Add startup assertion: if `WEB_CONCURRENCY > 1`, log a `CRITICAL` warning that the in-memory dedup set is unsafe. (TODO comment referencing decision 4.1.)
- **`src/api/main.py`**:
  - `/depots/{id}/alerts` — replace current connector_status-only response with UNION ALL over connector_status DISTINCT ON + `notification_alerts WHERE depot_id=$1 AND status IN ('active','acknowledged')`.
  - `POST /depots/{id}/alerts/{alert_id}/acknowledge` — new endpoint; sets `status='acknowledged'`, stamps `acknowledged_at/by`.
  - `POST /webhooks/resend` — public endpoint, signature-verified via `RESEND_WEBHOOK_SECRET`, updates `notification_deliveries.status`.
  - `GET/POST/PATCH/DELETE /admin/organizations/{org_id}/notification_recipients` — CRUD for `notification_recipients` (favonius_admin or matching customer_admin; cross-org reads write `admin.read`).

### 4.3 Environment variables

| Variable | Default | Notes |
|---|---|---|
| `RESEND_API_KEY` | — | Required when `EMAIL_DELIVERY_ENABLED=true` |
| `RESEND_FROM_ADDRESS` | `alerts@favonius.energy` | Default sender |
| `RESEND_WEBHOOK_SECRET` | — | HMAC verification secret |
| `EMAIL_DELIVERY_ENABLED` | `true` | Set to `false` in tests / staging |
| `ALERT_DISPATCHER_POLL_INTERVAL_S` | `30` | Reconciliation cadence |
| `ALERT_DISPATCHER_BATCH_SIZE` | `50` | Max alerts per tick |
| `ALERT_NOTIFY_RESEND_INTERVAL_S` | `3600` | Min interval between re-notifies on a still-active alert |

---

## 5. Test plan

### 5.1 Unit tests (`tests/unit/notifications/`)
- `test_severity.py` — Python ↔ DB mapping is symmetric for all three levels
- `test_alerts_repository.py` — create / upsert dedup behavior / ack / resolve transitions
- `test_recipients_repository.py` — alert_type filter (`{*}` vs subset), severity threshold, active flag
- `test_dispatcher_tick.py` — phase-by-phase with mocked DB and FakeEmailClient
- `test_resend_client.py` — request shape, error handling, retry on 5xx, no retry on 4xx
- `test_email_templates.py` — Jinja2 renders for charger_fault with sample payloads (snapshot tests)
- `test_webhook_signature.py` — HMAC verification accepts/rejects correctly

### 5.2 Integration tests (`tests/integration/`)
- `test_connector_status_trigger.py` — INSERT Faulted → row appears in `notification_alerts`; INSERT Available → status flips to `resolved`; pg_notify channel receives the payload
- `test_alerts_dispatcher_loop.py` — full loop with a real DB and FakeEmailClient: insert connector_status row, dispatcher picks it up via NOTIFY, recipients filtered correctly, deliveries ledger written
- `test_alerts_endpoint.py` — UNION ALL returns both live faults and notification_alerts rows
- `test_recipients_admin_endpoints.py` — CRUD authz tests including cross-org write attempts (403)

### 5.3 E2E (`tests/e2e/`)
- `test_at_08_alert_pipeline.py` — full PRD-style acceptance: charger faults → alert created → email dispatched → webhook updates delivery status → user acks via API → connector recovers → alert auto-resolves

### 5.4 Coverage targets
- `src/notifications/`: ≥ 90% (matches optimizer / surrogate bar)
- Overall: existing `fail_under = 80` must still pass

---

## 6. Rollout order

1. **Migration 022** + trigger tests (integration only, no app code yet) — verifies SQL works
2. **`src/notifications/severity.py`** + unit tests — pure module, no deps
3. **`AlertRepository` + `RecipientRepository`** + their unit + integration tests
4. **`EmailDeliveryClient` interface + `ResendEmailClient`** + `FakeEmailClient` for tests
5. **Email templates** + render tests
6. **`AlertDispatcher`** + unit (mocked) + integration (real DB) tests
7. **WS handler wire-in** (startup assertion, dispatcher task) — manual smoke at this point
8. **API endpoints** (acknowledge, recipients CRUD, webhook, /alerts UNION) + unit tests
9. **E2E AT-08** — gates the final commit
10. **Docs sync** — update `docs/API.md` (new endpoints), `docs/ARCHITECTURE.md` (alerts pipeline diagram), `CLAUDE.md` (env vars + acceptance criteria table)
11. **PR** as draft on `claude/alerts-pipeline-migration-7oK0g`

Each step is a separate commit with `feat(alerts): …` / `test(alerts): …` / `docs(alerts): …` per the repo's commit-message format.

---

## 7. Out of scope (deliberately deferred)

- SMS / Slack / PagerDuty / in-app push channels (interface is forward-compatible)
- Multi-worker dispatcher safety — locked to single-worker; revisit when `WEB_CONCURRENCY > 1`
- Producers other than `connector_status` (optimization failure, telemetry gap, price feed gap) — schema and dispatcher are generic, but each new producer is its own follow-up
- Recipient self-service UI — handled by frontend; backend exposes the CRUD endpoints
- Alert digest / batching — every alert sends immediately; if volume becomes a problem, add `min_interval_between_notifies` per recipient

---

## 8. Open questions to resolve during coding

These are details deferred to implementation, not blockers:

- Exact severity for `Unavailable` vs `Faulted` — spec says both produce alerts; stub maps Faulted=critical, Unavailable=warning. Confirm with product before sending the first prod email.
- Resend webhook payload schema — pin the version in `webhook.py` and add a contract test against a recorded fixture.
- Email subject line template — current placeholder `[Favonius] {severity}: {title}`; product may want different formatting.
- Whether `notification_alerts.depot_id` should be NOT NULL — currently nullable to support future org-scoped alerts; reconsider if no use case materializes.
