# CLAUDE.md — Favonius Energy Backend

This file provides guidance for AI assistants working in this codebase.

---

## Engineering Preferences

These preferences govern how all work in this repo should be approached. Apply them when reviewing plans, writing code, and making recommendations.

- **When resolving user requests** — Always offer **three options** to address the request (or fewer only if the problem admits fewer). For each option give **pros and cons**. **Recommend one option** and state **why** (e.g. robustness, maintainability, least change, or fit with project preferences). This gives the user a clear choice and a justified default.
- **DRY is important** — flag repetition aggressively. If the same logic appears twice, it should be extracted.
- **Well-tested code is non-negotiable** — err toward more tests, not fewer. Cover happy paths, sad paths, and edge cases.
- **"Engineered enough"** — avoid both under-engineering (fragile, hacky, no error handling) and over-engineering (premature abstraction, unnecessary complexity, features no one asked for).
- **Handle edge cases thoughtfully** — thoughtfulness > speed. Missing edge cases are bugs waiting to happen.
- **Explicit over clever** — readable code beats smart code. Future readers (and AI assistants) should not need to reverse-engineer intent.

---

## Plan Mode Protocol

When in plan mode, always follow this workflow before making any code changes.

### Before starting: ask scope

Always ask the user to choose one of:

1. **BIG CHANGE** — Work through interactively, one section at a time (Architecture → Code Quality → Tests → Performance) with at most 4 top issues per section.
2. **SMALL CHANGE** — Work through interactively with ONE question per review section.

### Review sections

For each section, output the issues with pros/cons AND an opinionated recommendation, then use `AskUserQuestion` before proceeding to the next section.

**1. Architecture review**
- Overall system design and component boundaries
- Dependency graph and coupling concerns
- Data flow patterns and potential bottlenecks
- Scaling characteristics and single points of failure
- Security architecture (auth, data access, API boundaries)

**2. Code quality review**
- Code organization and module structure
- DRY violations — be aggressive
- Error-handling patterns and missing edge cases (call these out explicitly)
- Technical debt hotspots
- Areas that are over-engineered or under-engineered

**3. Test review**
- Test coverage gaps (unit, integration, e2e)
- Test quality and assertion strength
- Missing edge case coverage
- Untested failure modes and error paths

**4. Performance review**
- N+1 queries and database access patterns
- Memory usage concerns
- Caching opportunities
- Slow or high-complexity code paths

### For each issue found

For every specific issue (bug, smell, design concern, risk):
- Describe the problem concretely, with `file_path:line_number` references
- Present 2–3 options, including "do nothing" where reasonable
- For each option: implementation effort, risk, impact on other code, maintenance burden
- Give an opinionated recommendation mapped to the engineering preferences above
- **Number issues** (1, 2, 3…) and **letter options** (A, B, C) so they're unambiguous in `AskUserQuestion`
- Put the recommended option first
- Ask for explicit agreement before proceeding

### Interaction rules
- Do not assume priorities on timeline or scale — ask
- After each review section, pause and ask for feedback before moving on
- Never skip the `AskUserQuestion` step; always wait for direction

---

## Project Overview

**Favonius Energy** is an EV Fleet Depot Optimization Platform. It coordinates EV charging schedules, stationary batteries, and building loads to reduce electricity costs by 30–50% for commercial fleet operators.

**Core capabilities:**
- OCPP 1.6/2.0.1 protocol for charger communication
- MILP-based optimization (Pyomo + Gurobi primary, HiGHS fallback)
- VDV 463 transit operations integration (BMS/ITCS interface)
- ENTSO-E day-ahead electricity price ingestion (European depots)
- Gaussian Process surrogate model for energy consumption prediction
- TimescaleDB for time-series data storage
- Prometheus/Grafana observability

**Authoritative product spec:** `docs/PRD_Depot_Agent.md` — product direction (depot agent: workflows, today view, trust graduation). The substrate (optimiser, OCPP, MILP constraints, schema, regulatory docs) is documented in the in-repo operational docs listed in §15.2 of that PRD; this CLAUDE.md remains the operational reference for working in the codebase.

---

## Repository Structure

```
Favonius_Backend/
├── src/                         # Primary application code (new architecture)
│   ├── api/
│   │   ├── main.py              # FastAPI app, most REST endpoints, OCPP WebSocket mount
│   │   ├── optimization.py      # /depots/{id}/optimization/solver + readiness router
│   │   ├── savings.py           # savings-summary computation
│   │   ├── reports.py           # energy report aggregation + CSV streaming
│   │   ├── report_schedules.py  # scheduled-reports reads + command handlers
│   │   ├── report_schedule_timing.py # DST-aware next-run computation (stdlib)
│   │   ├── report_pdf.py        # PDF rendering (reportlab)
│   │   ├── charger_logs.py      # charger diagnostic-log fetch/upload/compare
│   │   ├── fleet_list.py        # chargers/vehicles list helpers
│   │   ├── liveness_hub.py      # LISTEN/NOTIFY charger-liveness SSE fan-out
│   │   ├── error_codes.py       # structured API error codes
│   │   ├── agent/               # Depot chat agent (Agent Search) — see "Depot Chat Agent" section
│   │   ├── agent_workflows/     # Depot agent runtime + eval harness — see "Depot Agent" sections
│   │   ├── charging_import/     # Charging-session / price import helpers
│   │   └── data_sources/        # Self-serve external integrations router — see "Data Sources"
│   ├── core/
│   │   ├── controller.py        # DepotController — main control loop
│   │   ├── controller_config.py # ControllerConfig dataclass (reads FAVONIUS_* env knobs)
│   │   ├── controller_manager.py# ControllerManager — manages per-depot controllers
│   │   ├── models.py            # Shared Python dataclasses (Depot, Vehicle, etc.)
│   │   ├── version_info.py      # Build/version metadata
│   │   ├── optimizer/
│   │   │   ├── milp_model.py    # Pyomo MILP model construction
│   │   │   ├── solver.py        # Gurobi + HiGHS fallback solver wrapper
│   │   │   ├── allocator.py     # Post-solve schedule allocation
│   │   │   ├── warm_start.py    # Warm-start from prior solutions
│   │   │   ├── pool.py          # SolverPool — ProcessPoolExecutor solve dispatch
│   │   │   └── exceptions.py    # SolverError, InfeasibleModelError, SolverTimeoutError
│   │   ├── billing/             # session_cost.py — per-session cost calculator
│   │   ├── reconciliation/      # charger-log ↔ telemetry reconciliation
│   │   ├── data_sources/        # Provider-agnostic ingestion (base/registry/kempower/ingestion/scheduler)
│   │   ├── scheduling/          # recurring.py — recurring schedule template expansion
│   │   ├── state/
│   │   │   ├── assembler.py     # StateAssembler — assembles depot state from DB
│   │   │   └── triggers.py      # TriggerMonitor, TriggerConfig — re-optimization triggers
│   │   └── surrogate/
│   │       ├── energy_model.py  # Gaussian Process energy consumption model
│   │       └── training.py      # Model training pipeline
│   ├── adapters/
│   │   ├── ocpp/
│   │   │   ├── server.py        # OCPPServer — WebSocket server for chargers
│   │   │   ├── charge_point.py  # FleetChargePoint — per-charger OCPP handler
│   │   │   ├── dispatch.py      # SetChargingProfile command dispatch
│   │   │   ├── mapping.py       # ocpp_id ↔ charger_id lookup
│   │   │   ├── telemetry.py     # MeterValues → telemetry DB writes
│   │   │   └── asgi_adapter.py  # Starlette/FastAPI WebSocket adapter
│   │   ├── vdv463/
│   │   │   ├── handler.py       # VDV 463 WebSocket handler
│   │   │   ├── messages.py      # Message parsing, validation, builders
│   │   │   ├── repository.py    # DB queries for VDV 463 state
│   │   │   ├── depot_state.py   # Depot charging info for VDV responses
│   │   │   ├── vehicle_resolver.py
│   │   │   └── charging_point_resolver.py
│   │   ├── caiso/               # CAISO price ingestion (deprecated — Europe-only feeder; module retained as dead code, see follow-up)
│   │   ├── entsoe/              # ENTSO-E European price ingestion
│   │   ├── kempower/            # Kempower ChargEye one-shot inventory + history import (scripts/onboard_depot_from_kempower.py)
│   │   ├── chargers/            # Charger-side log parsers (abb/log_parser.py) + vendor dispatch
│   │   ├── weather/             # OpenMeteo weather adapter
│   │   └── handoff/
│   │       └── manager.py       # Inter-depot vehicle handoff manager
│   ├── db/
│   │   ├── models.py            # SQLAlchemy ORM models (mirror of SQL schema)
│   │   └── queries.py           # Async DB query helpers
│   ├── monitoring/
│   │   └── metrics.py           # Prometheus metrics definitions
│   ├── notifications/           # Alerts pipeline: dispatcher, renderer, Resend client, webhook, severity
│   └── security/
│       ├── auth.py              # JWT verification (Supabase) + Favonius staff auto-promotion
│       ├── rbac.py              # Role-based access control / Permission checks
│       ├── ocpp_auth.py         # Per-charger Basic Auth verification
│       ├── rate_limiter.py      # Rate limiter (optimize: 10/min, API: 100/min, handoff: 50/hr)
│       ├── validators.py        # UUID and input validators
│       ├── handoff_validator.py # Inter-depot handoff message validation
│       ├── data_freshness.py    # Stale data detection
│       ├── geo_block.py         # Article 73-3 geo-blocking middleware (MaxMind GeoLite2)
│       ├── forwarded_ip.py      # Trusted-proxy client-IP resolution
│       ├── headers.py           # TLS / forwarded header parsing
│       ├── tenant_mirror.py     # JIT tenant mirroring + metadata self-heal
│       ├── admin_audit.py       # audit_log writer (admin actions)
│       ├── audit_log.py         # audit helpers
│       ├── credential_cipher.py # Fernet credential encryption (data sources)
│       ├── incident_response.py # incident-response helpers
│       └── secrets.py           # Secrets management
│
├── src/websocket_handler/       # Legacy OCPP WebSocket service (standalone)
│   ├── main.py                  # Application entry point + orchestrator
│   ├── server.py                # OCPPWebSocketServer (close hook persists Unavailable + last_seen_at)
│   ├── config.py                # Config dataclass from env vars
│   ├── message_handler.py       # OCPP message dispatch
│   ├── connection_manager.py    # Active session tracking
│   ├── timescale_client.py      # TimescaleDB async client (incl. OCPP recovery helpers)
│   ├── ocpp_handler.py          # OCPP 2.0.1 EnhancedOCPPChargePoint
│   ├── ocpp16_adapter.py        # OCPP 1.6 OCPP16Session (wraps FleetChargePoint, reload-on-boot + queue replay)
│   ├── optimization_engine.py   # Heuristic scheduler (legacy)
│   ├── price_feeder.py          # ENTSO-E day-ahead price ingestion
│   ├── analytics_service.py     # Aggregated metrics for REST API
│   ├── security_manager.py      # Auth, TLS, rate limiting
│   └── ...                      # Many additional managers (cache, cert, DER, etc.)
│
├── migrations/                  # TimescaleDB ops-table migrations (idempotent; 001–045+)
│   ├── 001_initial_schema.sql   # Core schema + seed data
│   ├── 012_ocpp_pilot_hardening.sql  # OCPP 1.6 sequences + station_credentials
│   ├── 013_recovery.sql         # charging_command_queue + cross-restart recovery
│   ├── 037_depot_agent_workflows.sql # decisions + workflows + workflow_tiers
│   ├── 040_session_cost_provenance.sql # charging_sessions.cost_total_source
│   ├── 042_charger_log_imports.sql   # charger-log tables (NB: 042_agent_views_ts.sql ALSO exists)
│   ├── 044_report_schedules.sql      # report schedules + runs + deliveries
│   ├── …                        # ~45 files; numbers can repeat across parallel PRs — see "Migrations" note
│   └── supabase/                # Static-schema migrations (Supabase pool), e.g. 040_agent_views_static, 044_agent_sql_mode_org_flag
│
├── tests/
│   ├── unit/                    # Unit tests (mock everything)
│   ├── integration/             # Integration tests (require DB)
│   ├── e2e/                     # End-to-end tests
│   ├── performance/             # Performance benchmarks
│   ├── security/                # Security tests
│   ├── load/                    # Load tests
│   └── chaos/                   # Chaos/resilience tests
│
├── scripts/
│   ├── simulation/              # Depot simulation scripts
│   ├── ocpp_simulator.py        # Standalone OCPP charger simulator
│   └── run_migrations.py        # Migration runner
│
├── config/
│   ├── depot_config.yaml        # Depot configuration template
│   └── tariff_config.yaml       # Tariff/rate configuration
│
├── schemas/vdv463/              # JSON schemas for VDV 463 message validation
├── optimization/                # Julia MILP reference implementation
├── monitoring/                  # Prometheus config
├── docs/                        # Architecture and API documentation
├── docker-compose.yml           # Production service definitions
├── docker-compose.test.yml      # Test environment
├── Dockerfile                   # Main API image
├── Dockerfile.ocpp-simulator    # OCPP simulator image
├── pyproject.toml               # Package config, tool settings, dependencies
├── pytest.ini                   # pytest configuration
├── Makefile                     # Development convenience commands
└── .pre-commit-config.yaml      # Pre-commit hook definitions
```

---

## Architecture

### Two Application Layers

The codebase contains **two application layers** that overlap in responsibility:

1. **`src/` (primary, new architecture)** — FastAPI-based REST + OCPP WebSocket server with the new MILP optimizer. This is the canonical implementation going forward.

2. **`src/websocket_handler/` (legacy)** — A standalone OCPP WebSocket handler with its own application orchestrator. Still used for legacy charger communications. Do not delete without verifying no active use.

### Request Flow (new architecture)

```
HTTP/WebSocket client
        │
        ▼
src/api/main.py (FastAPI, middleware: rate-limiting, logging, CORS)
        │
        ├── JWT auth via src/security/auth.py (Supabase JWT)
        │
        ├── REST: /optimize → ControllerManager → DepotController
        │                          → StateAssembler → MILP solver → OCPP dispatch
        │
        ├── REST: /depots/{id}/state|schedule|alerts → DB queries
        │
        ├── REST: /depots/{id}/vehicles/{id}/handoff → interdepot_messages table
        │
        └── WebSocket: /ocpp/{charge_point_id} → OCPPServer → FleetChargePoint
```

### Depot Chat Agent (Agent Search)

`src/api/agent/` is a self-contained module that adds a plain-English query interface for depot operators. The module is mounted into the FastAPI app behind the `AGENT_SEARCH_ENABLED` feature flag (now default `true` since B6 golden-test gate passed). A user message goes through three server-side stages: (1) **LLM extraction** (`llm.py`) converts the message into a strict `QueryPlan` via Anthropic's tool-use API — the model never sees UUIDs or raw SQL; (2) **entity resolution** (`resolve.py`) maps the plan's subject names to real database UUIDs, scoped to the caller's `visible_depot_ids` from their JWT — this is the auth boundary; and (3) **deterministic compilation** (`intents/consumption_by_user.py`) turns the resolved plan into a parameterised SQL query that executes against TimescaleDB. Every turn is audited in `agent_runs` (full step trace) and `audit_log` (action `agent.query`). Prometheus metrics (`favonius_agent_turns_total`, `favonius_agent_turn_duration_seconds`, `favonius_agent_llm_tokens_total`, `favonius_agent_resolver_misses_total`) are incremented from `router.py`, `llm.py`, and `controller.py`. The golden test suite in `tests/golden/agent_consumption.yaml` (50 Q&A pairs) gates every deploy; AT-18 (`tests/e2e/test_agent_search.py`) is the end-to-end acceptance test. The endpoint reference lives in `docs/API.md`; the original design plan has been retired now that the implementation has shipped — the code in `src/api/agent/` is the source of truth, and this feature is positioned as a precursor to the broader Depot Agent product (`docs/PRD_Depot_Agent.md`).

#### SQL mode — general-purpose analytics (`AGENT_SQL_MODE_ENABLED`)

The chat agent has a second execution path that opens it up to arbitrary depot analytics questions — not just consumption. `src/api/agent/planner.py` runs first on every turn: messages matching the `consumption_by_user` shape stay on the existing fast path; anything else falls through to a **text-to-SQL agent loop** when `AGENT_SQL_MODE_ENABLED=true` AND the caller's organisation has `organizations.agent_sql_mode_enabled = TRUE` — a per-org DB flag (default `TRUE`, from `migrations/supabase/044_agent_sql_mode_org_flag.sql`) checked in `src/api/agent/planner.py`. (The legacy `AGENT_SQL_ORG_ALLOWLIST` env allowlist has been removed.) The SQL loop reuses `WorkflowAgent.run_qa_turn` (`src/api/agent_workflows/runtime.py`) — same Anthropic tool-use mechanics as the workflows, but writes to `agent_runs` instead of `decisions` (Q&A is not a workflow per PRD §13; chat is the "escape hatch from the today view"). Security is defence-in-depth: (S1) the LLM only sees curated `agent_views.*` `SECURITY DEFINER` table-functions — each takes `p_depot_ids uuid[]` filtered server-side, so no WHERE-clause injection can leak across tenants; (S2) every executor call swaps to a read-only role (`agent_reader_ts` / `agent_reader_static`) and asserts `current_user` matches — fail-closed; (S3) `migrations/042_agent_views_ts.sql` puts a restricted-update guard on `agent_runs` — a denylist of immutable columns (`run_id`/`user_id`/`organization_id`/`depot_id`/`user_message`/`created_at`), mirroring 037's pattern on `decisions`; (S4) the validator runs `EXPLAIN (FORMAT TEXT)` only — never `ANALYZE`; sqlglot rejects multi-statement, DML, dangerous functions, and any FROM/JOIN target outside the function allowlist. The eight tools the LLM gets (`list_tables`, `describe_table`, `sample_values`, `run_select_ts`, `run_select_static`, `current_time`, `lookup_entity`, `emit_final_answer`) are registered by `src/api/agent/sql_tools.py:build_sql_agent_tool_registry`; the system prompt with the inlined catalogue lives in `src/api/agent/prompts.py` and is cache-keyed (`cache_control: ephemeral`). New Prometheus metrics: `favonius_agent_sql_validations_total{verdict}`, `favonius_agent_sql_executions_total{outcome,pool}`, `favonius_agent_sql_execution_seconds`, `favonius_agent_sql_rows_returned`, `favonius_agent_sql_tool_turns`. Migrations: `migrations/042_agent_views_ts.sql` (numbered 042 to avoid collision with in-flight PR #214's 040+041) + `migrations/supabase/040_agent_views_static.sql`.

**Module map** (`src/api/agent/`): `router.py` (HTTP routes + metrics), `controller.py` (turn orchestration), `llm.py` (extraction), `planner.py` (fast-path vs SQL-mode routing + the per-org flag check), `resolve.py` (name→UUID, the auth boundary), `intents/` (`base.py` + `consumption_by_user.py` — the fast path now also answers vehicle/fleet and depot-wide consumption questions), `plan.py` (`QueryPlan` Pydantic types), `catalogue.py` (LLM-facing schema catalogue for SQL mode), `sql_tools.py` / `sql_executor.py` / `sql_validator.py` (SQL-mode tool registry, read-only executor, sqlglot validator), `audit.py` (`agent_runs`/`audit_log` writers + `classify_failure`), `auth_context.py`, `stream.py` (SSE helpers), `thinking.py`, `feature_flag.py`.

### Depot Agent — Workflow Runtime (`src/api/agent_workflows/`)

The workflow runtime is the Depot Agent product surface (PRD §4.3, §4.4). Sprint 1 (migration 037 + `models.py` + `repository.py` + `feature_flag.py`) shipped the substrate: append-only `decisions` hypertable, `workflows` + `workflow_tiers` tables, Pydantic types, and the canonical `insert_decision(pool, decision)` writer. Sprint 2 (this section) layers the runtime on top.

`WorkflowAgent.run_turn(workflow, depot_id, auth_context, tool_registry, user_input, *, permission_tier=DRAFT_AND_WAIT)` in `runtime.py` opens one Anthropic Messages tool-use loop: the model sees only the tools named in `workflow.allowed_tools` plus the reserved `emit_decision` structured-output terminator. The runtime validates every tool call against the allow-list **before** dispatching it (`ToolNotAllowedError` if not), records each call as a Sprint-1 `ToolCall(name, arguments, result, ok, error)` in the per-turn `tool_calls` list, runs the `HardConstraintGuard` (`constraints.py`) twice — pre-dispatch on tool inputs and post-emit on the LLM's `proposed_actions` (PRD §10.3: departure SoC ≥ 99%, `max_grid_kw` never exceeded) — and writes exactly one `Decision` row at the end with `disposition=Disposition.PENDING` via `DecisionRepo` (`repo.py` wraps Sprint 1's `insert_decision`). The agent itself **never** writes `auto_executed`; humans, or a later promotion pathway, advance disposition.

The runtime never queries `workflow_tiers` itself — callers resolve the per-(workflow, depot) tier via Sprint 1's `get_tier(pool, workflow_id, depot_id)` and pass it through. `auth_context.organization_id` is required (it's NOT NULL on the `decisions` row) — turns with no org abort with `WorkflowRuntimeError` before the LLM is called. Prompt caching matches the `src/api/agent/llm.py` pattern: the workflow's system block carries `cache_control={"type": "ephemeral"}` so repeat turns of the same workflow hit Anthropic's prefix cache; the per-turn user message (depot id, parameters, user input) is kept out of the cached block. Prometheus metrics: `favonius_workflow_turns_total{workflow,depot,status}`, `favonius_workflow_turn_duration_seconds`, `favonius_workflow_llm_tokens_total`. **No HTTP endpoint yet** — the runtime is exercised via `tests/unit/test_agent_workflows_runtime.py` with fake tools and a fake Anthropic client. The agent is gated by `DEPOT_AGENT_ENABLED` (default off; Sprint 1's feature flag). A sibling method `WorkflowAgent.run_qa_turn(...)` reuses the same tool-use loop but writes to `agent_runs` instead of `decisions` — it backs the chat agent's SQL mode (see the Agent Search SQL-mode section), not a workflow. There is still **no workflow HTTP endpoint**.

### Depot Agent — Eval Harness (`src/api/agent_workflows/eval/`)

Sprint 3 lands the eval harness ahead of any workflow definition (PRD principle 6: "the evaluation harness ships before the workflow"). The harness in `src/api/agent_workflows/eval/runner.py` drives Sprint 2's `WorkflowAgent.run_turn` against a frozen `graph_snapshot` loaded into a transactional savepoint on the **real test DB** (no mocked DDL — we want to catch schema drift). Scenarios live as YAML under `tests/golden/workflows/` against the JSON-schema-style reference in `_schema.yaml`. Each scenario carries (1) a `workflow` block (name, version, prompt, allowed_tools), (2) a `graph_snapshot` (depot, vehicles, chargers, schedules, drivers, telemetry, prices, building_load), (3) an `llm_trace` — a deterministic sequence of Anthropic tool_use turns the harness replays through a fake `AnthropicClient` so the gate doesn't need a real LLM, and (4) an `expected` block that asserts against `Decision.output` + `Decision.tool_calls`. Determinism: `Decision.id`, `timestamp`, and `inputs_hash` are excluded from assertions; everything the test pins is reproducible across runs. Tools the workflow needs in the harness are registered in a per-scenario `ToolRegistry` whose callables read from the savepoint via a thin asyncpg-like adapter. The pytest plugin in `tests/golden/workflows/conftest.py` registers the `workflow_golden` marker; `test_workflow_golden.py` parametrises across every `*.yaml` directly under the gated dir (excluding `_schema.yaml`). Three placeholder scenarios under `_examples/` (`readiness-all-clear`, `readiness-charger-fault`, `readiness-constraint-violation`) prove the harness end-to-end. CI gate: `.github/workflows/workflow-golden.yml` spins up TimescaleDB, applies migrations, runs the harness, and enforces a 90% runner coverage floor. Sprint 5 lands the first 10 readiness scenarios under the gated path.
### Depot Agent — Sprint 4 readiness tools (`src/api/agent_workflows/readiness_tools.py`)

Sprint 4 ships the five tools the daily-readiness workflow needs (PRD §6.1) on top of the Sprint-2 runtime. `build_readiness_tool_registry(static_pool, ts_pool, auth, *, depot_id, now=None)` returns a populated `ToolRegistry` whose callables take `**kwargs` matching their JSON input schema (UUIDs come in as strings from the LLM). Auth, pools, and the caller's visible-depot scope are captured in the closure at registry-build time so the runtime can keep dispatching as `await fn(**input)` without threading auth through Anthropic. Every tool filters by `auth.visible_depot_ids` against `sites.organization_id` — the same auth boundary as `src/api/agent/resolve.py` — and uses `src.security.data_freshness.MAX_TELEMETRY_AGE` (15 min) for the staleness threshold, matching `StateAssembler`. Tools never raise on empty data; they return `[]` or `None`-bearing dicts with explicit freshness flags so the runtime can reason about gaps without aborting the turn.

Tools registered: `get_scheduled_departures(depot_id, window_start, window_end)`, `get_vehicle_state(vehicle_id)`, `get_charger_state(charger_id)`, `get_charging_plan(vehicle_id)`, `get_driver_assignment(route_id)`. The data plumbing this depends on: Supabase migration 013 (`schedules.driver_id`) and migration 014 (`routes` view aliasing `schedules` to the PRD §5.1 `Route` shape).

### Charger-side log extraction (`src/adapters/chargers/` + `src/api/charger_logs.py`)

Operators can pull a charger's own session log on demand and compare it against `charging_sessions`/`telemetry`. Wire-up — all in TimescaleDB (`db_pools.ts`) alongside `charging_sessions`:

- **Trigger** — `POST /admin/depots/{depot_id}/chargers/{charger_id}/sessions/{session_id}/fetch_logs` (customer_admin/favonius_admin) calls `dispatch_get_diagnostics()` (`src/adapters/ocpp/dispatch.py`). One transaction inserts a `charger_log_imports` row in `status='requested'` and a `charging_command_queue` row with `command_type='get_diagnostics'` carrying the signed upload URL in `payload['location']`. Writes a `charger.logs.fetched` admin-audit row.
- **WS handler dispatch** — `ChargingCommandQueueConsumer._handle_get_diagnostics` (`src/websocket_handler/charging_profile_manager.py`) drains the queue and calls `FleetChargePoint.get_diagnostics(...)`. The charger asynchronously uploads to the URL.
- **Upload** — public route `POST /internal/charger_logs/upload?token=<HMAC>` (no JWT — chargers don't carry one). Token is `<import_id>.<expiry_unix>.<hmac_hex>`, signed with `CHARGER_LOG_UPLOAD_SIGNING_KEY`. `MaxBodySizeMiddleware` enforces the per-endpoint cap (`CHARGER_LOG_UPLOAD_MAX_BYTES`, default 50 MiB) instead of the global 1 MiB cap. Stores body in `charger_log_imports.raw_payload BYTEA`, flips status to `'received'`, schedules parse+reconcile as a post-commit `asyncio.create_task` (same pattern as session-cost calculator).
- **Parser dispatch** — `src/adapters/chargers/__init__.py::get_parser_for_vendor` normalises the vendor string (matching `_is_abb_vendor`) and returns the per-vendor parser. ABB Terra AC archives (`.tar.gz` containing CSV) → `src/adapters/chargers/abb/log_parser.py`. Unknown vendor → raw blob retained, no entries; a future parser can backfill without re-fetching.
- **Normalized entries** — written to the `charger_session_log_entries` hypertable. Schema mirrors `telemetry` so reconciliation joins are symmetric (`(station_id, transaction_id, time)`). Vendor-unknown columns land in `raw_fields JSONB`.
- **Reconciler** — `src/core/reconciliation/session_log_reconciliation.py::reconcile_session_log` integrates `telemetry.charging_kw` over the session window (trapezoidal, same SQL shape as `session_cost.py`) and compares against the cumulative meter delta from `charger_session_log_entries`. Writes one `session_log_reconciliations` row per `(session_id, log_import_id)` with a `source` enum: `reconciled` | `partial` | `no_log_entries` | `no_session` | `parse_failed`. Idempotent via `ON CONFLICT (session_id, log_import_id) DO UPDATE`.
- **Read** — `GET /admin/depots/{depot_id}/sessions/{session_id}/log_comparison` returns `{session, import, charger_entries, reconciliation}` for the UI. 200 with `reconciliation=null` while parse/reconcile is in flight; 404 when no import exists yet.

Vendor metadata is persisted on `BootNotification` via `OCPP16Session._persist_station_vendor` (UPDATEs `charging_stations.vendor` / `firmware_version`). The Supabase mig-006 vendor column was previously empty; new boots fill it in.

`migrations/042_charger_log_imports.sql` adds the three tables and extends `charging_command_queue.command_type` to allow `'get_diagnostics'` and `'get_log'` (placeholder for future OCPP 2.0.1 chargers — same queue, same upload endpoint, new dispatch helper). Note there is a **second, unrelated** migration also numbered 042 — `042_agent_views_ts.sql` (agent SQL-mode views); duplicate numbers are expected here (see the "Migrations" note).

### Scheduled reports (`src/api/report_schedules.py` + `report_schedule_timing.py` + `report_pdf.py`)

Configurable per-depot report schedules (frontend PR #124). A depot admin defines a schedule (e.g. "Monthly electricity consumption", `kind=monthly_consumption`, `frequency=monthly`, `dayOfMonth=1`, `timeOfDay=06:00`, `autonomyMode=auto_silent`) with PDF/CSV recipients; a per-minute worker fires due schedules, generates the report via the existing `reports.generate` pipeline, renders a PDF/CSV, and emails it.

- **Schema (migration 044, TimescaleDB):** `report_schedules`, `report_schedule_recipients` (ordered, stable ids, `UNIQUE(schedule_id, email, format)`), `schedule_runs` (`UNIQUE(schedule_id, scheduled_for)` = cron idempotency anchor), `schedule_run_deliveries` (append-only delivery ledger). Like migration 033, `depot_id`/`created_by` are bare UUIDs (no cross-DB FK to Supabase `sites`/`auth.users`). 044 also rekeys the `report_draft` agent-action uniqueness index to `(depot_id, COALESCE(scheduleId,'_legacy'), periodStart)` so multiple schedules per depot can each emit a draft. Numbered 044 to avoid colliding with in-flight PR #233's 043. **The legacy hardcoded `monthly_scheduler` is removed** — this feature replaces it.
- **Reads:** `GET /depots/{id}/report-schedules`, `/{schedule_id}`, `/{schedule_id}/runs` — any depot member. Responses are plain camelCase dicts with all nullable fields present (`nextRunAt`/`lastRunAt`/`lastRunStatus`/`recipients[].lastDelivery`) for the frontend's strict Zod.
- **Writes:** via `POST /commands/execute` — `reports.schedule.{create,update,delete,run_now}`, gated by `Permission.ADMIN_CONFIG` (customer_admin+). Handlers return the domain object as `CommandResponse.result`. `run_now` executes inline (scheduled_for = now truncated to the second → double-click safe) and returns a `ScheduleRun`.
- **Autonomy gating** (resolved by `resolve_autonomy_mode`, which reads PR #233's shared `agent_autonomy_settings.level` for `report_draft` — gracefully falling back when that table isn't present yet — then falls back to the schedule's `autonomy_mode`): `shadow`→`skipped` (no report/delivery); `proposed`→generate + emit a pending `report_draft` agent_action (payload carries `scheduleId`/`runId`/`reportId`), run `pending_approval` (approving via `agents.action.approve` delivers + flips run to `succeeded`; rejecting flips to `skipped`); `auto_notify`→deliver + informational action; `auto_silent`→deliver, no action.
- **Scheduling/DST:** `compute_next_run_at` (in `report_schedule_timing.py`, pure stdlib) computes the next wall-clock occurrence in the depot tz then converts to UTC via `zoneinfo`, so a 06:00 schedule stays 06:00 local across DST (the UTC instant shifts). spec weekday is 0=Sun..6=Sat. Quarterly = calendar quarters (Jan/Apr/Jul/Oct).
- **Delivery:** PDF via `reportlab` (`report_pdf.py`), CSV reuses `reports.stream_rows_as_csv`. `EmailMessage.attachments` (new) carries them; `ResendEmailClient` base64-encodes. The API process builds its own email client at startup (`report_email_client`: Resend when `RESEND_API_KEY` set, else `FakeEmailClient`). Provider webhook callbacks (`POST /webhooks/resend`) **append** a new `schedule_run_deliveries` row with the mapped status (`delivered→sent`, `complained→suppressed`), so `lastDelivery` reflects the latest provider state. Env vars reuse the alerts pipeline's `RESEND_API_KEY`/`RESEND_FROM_ADDRESS`/`EMAIL_DELIVERY_ENABLED`/`RESEND_WEBHOOK_SECRET`.

### Per-session billing (`src/core/billing/session_cost.py`)

`compute_session_cost(ts_pool, session_row)` returns a `SessionCostResult` (`cost`, `source`, diagnostics). Two strategies, automatic selection: **granular** integrates `charging_kw × Δt × price(t)` via TimescaleDB `time_bucket('1 hour', telemetry.time)` (trapezoidal between consecutive samples) joined to `electricity_prices`, **fallback_average** uses `energy_delivered_kwh × avg(price over [start,end])`. The granular path is gated: telemetry timestamps must cover ≥80% of the session AND telemetry-implied energy must reconcile to within ±10% of `energy_delivered_kwh`. The chosen strategy is written to `charging_sessions.cost_total_source` (migration 040). Missing prices → `'unpriceable'`, `cost_total` stays NULL; billing never fabricates a price.

Price lookup goes through `src/db/queries.py::fetch_or_pull_prices_by_zone` — a read-through cache around `electricity_prices` (keyed by ENTSO-E `node_id`, EUR/MWh stored, EUR/kWh returned). On cache miss the helper calls `ENTSOEAdapter.get_day_ahead_prices(zone, start, end)` directly against the Transparency Platform API, persists the result back to `electricity_prices`, and re-reads through the standard forward-fill path. This is what keeps billing working when the WS handler's price feeder is misconfigured or hasn't populated the table yet. Requires `EUROPEAN_ELECTRICITY_API`; without it the helper returns whatever the cache has (possibly empty) and the calculator lands `'unpriceable'`. Each call site resolves the bidding zone for a depot via `src/db/queries.py::resolve_bidding_zone(static_pool, site_id)` once, with a cascade: `sites.tariff_config['entsoe_zone']` → `get_bidding_zone(sites.timezone)` → `None`. Callers (cost calculator + `StateAssembler._get_prices`) inject the resolved zone; the calculator returns `'unpriceable'` and the optimizer falls back to $0.15/kWh when no zone resolves. The legacy `prices` table (per-depot tariff) is unused on the current ENTSO-E deployment — `ENTSOEAdapter.store_prices_to_db` and `_get_cached_prices` write/read `electricity_prices` keyed by bidding zone too, so all ENTSO-E ingestion paths (this adapter + the WS-handler feeder + the read-through cache) land in one canonical hypertable.

The OCPP close path (`TimescaleClient.close_open_session`, `recover_orphaned_sessions`) schedules the calc as a post-commit `asyncio.create_task`; if it fails, the row stays NULL and `scripts/backfill_session_cost.py` sweeps it on the next run (predicate `WHERE cost_total IS NULL OR cost_total = 0`). The backfill script takes two URLs (`--database-url` for TimescaleDB, `--static-database-url` for Supabase; fall through to `DATABASE_URL` / `STATIC_DATABASE_URL` / `SUPABASE_DB_URL` env). It builds a `{site_id → zone}` cache at startup so the resolver hits Supabase once per depot, not per row. Chunked (default 500 rows) with `SELECT FOR UPDATE SKIP LOCKED` and is safely re-runnable. Metrics: `favonius_session_cost_computed_total{source}`, `favonius_session_cost_compute_failures_total{reason}`, `favonius_session_cost_duration_seconds`.

### Kempower ChargEye onboarding (`src/adapters/kempower/` + `scripts/onboard_depot_from_kempower.py`)

Two-step onboarding for Kempower customers. The operator first creates the Favonius depot via the existing `POST /admin/depots` flow (so commercial/regulatory context — utility, tariff, currency, timezone, building-load source, stationary battery — is set correctly by the human who actually knows it). Then `python scripts/onboard_depot_from_kempower.py --depot-id <uuid> --kempower-location-id <kempower_loc> --backfill-since <date> --execute` attaches Kempower's inventory and history to that depot: chargers (dedup on `(site_id, station_id)`), vehicles (dedup on `(site_id, external_id)` with `kempower:<vehicleId>` namespacing to avoid global-UNIQUE collisions across tenants), all-to-all `charger_vehicle_access`, and historical `charging_sessions` rows with `source='import'` matching migration 030's `import_row_hash` contract (`sha256(depot_id|start_time_utc|id_tag)`). Idempotent across days — re-running adds zero new rows when nothing has changed in Kempower. The adapter (`KempowerClient`) is a small httpx wrapper with JWT bearer auth (cached, refreshed on 401 or T-5min expiry), pagination, 429 back-off, and 5xx retry; mappers in `mapping.py` produce locally-validated Pydantic payloads (mirrors of `ChargerCreateRequest` / `VehicleIdentityBase` — duplicated rather than imported so the CLI stays standalone). The script bypasses `next_charger_ocpp_id` and uses the Kempower `stationId` verbatim so the physical charger keeps its existing identity when OCPP is later cut over to Favonius. Per-charger Basic Auth credentials are minted once and emitted to stderr; rotate via `POST /admin/depots/{id}/chargers/{cid}/rotate_credentials`. Non-CCS connectors are skipped per-station with a warning (PRD §3.2 MVP constraint). `--dry-run` (the default) plans the writes; `--execute` commits. `--apply-site-suggestions` adds an interactive per-field PATCH against the depot row from Kempower's Location + root Power Group data (name, address, lat/lng, max_grid_kw) — read-only diff without the flag. Env vars: `KEMPOWER_API_BASE_URL`, `KEMPOWER_USERNAME`, `KEMPOWER_PASSWORD`. **No new migrations** — the existing `charging_stations.station_id` UNIQUE, `vehicles.external_id` UNIQUE, and `charging_sessions_import_dedup_idx` (migration 030) carry the dedup load.

### Data Sources — self-serve external integrations (`src/core/data_sources/` + `src/api/data_sources/`)

The website "Data Sources" page lets a customer admin connect an external system (Kempower ChargEye first) by pasting credentials; the backend stores them encrypted, connects, and ingests inventory + history into the depot on a schedule. Provider-agnostic by design: `src/core/data_sources/base.py` defines `DataSourceProvider` (`provider_key`, `catalogue_entry()` → the credential-field schema the UI renders dynamically, `validate_credentials()`, `run_ingestion()`); `registry.py` is the `provider_key → provider` lookup; `kempower_provider.py` is the only concrete provider and drives the **same** `src/adapters/kempower/onboarding.py` stages the CLI uses (one code path — the CLI was refactored to import them). Adding a second provider is a drop-in.

Credentials are Fernet-encrypted at rest via `src/security/credential_cipher.py` (`DATA_SOURCES_ENCRYPTION_KEY`, `encryption_version` column for rotation) — replayable, unlike bcrypt; never logged, never returned by the API. Two Supabase-static tables (so they FK `sites`/`organizations` and each other): `data_source_connections` (mig `supabase/042`) and `data_source_ingestion_jobs` (mig `supabase/043`). Jobs are durable: `ingestion.py::run_ingestion_job` claims a row (`pending→running`), decrypts, runs the provider, writes a terminal `succeeded|partial|failed` with progress counts, and is invoked from three triggers — the manual `/sync` endpoint, the scheduler, and a startup recovery sweep (mirrors `recover_orphaned_sessions`). The overlap guard is a partial unique index `uq_dsij_one_active_per_conn` (one non-terminal job per connection) → manual sync races return 409, the scheduler silently skips; this holds even under `WEB_CONCURRENCY>1` though a single web worker is assumed (`scheduler.py::check_single_worker` logs CRITICAL otherwise). `scheduler.py::run_data_source_scheduler` enqueues due connections (`next_sync_at <= NOW()`) each `DATA_SOURCES_SCHEDULER_INTERVAL_S`. The runtime is gated by `DATA_SOURCES_ENABLED` (default on; fails closed if enabled without an encryption key). Freshly-minted OCPP charger passwords from a first import are **not** surfaced (no plaintext through the API) — the job flags `credentials_pending_rotation` and the operator sets them via the existing rotate-credentials endpoint. Endpoints (all `customer_admin` + depot-scoped, snake_case responses): `GET/POST /admin/data-sources/connections`, `GET/PATCH/DELETE …/connections/{id}`, `POST …/connections/{id}/sync` (202 + `status_url`), `GET …/connections/{id}/jobs`, `GET …/jobs/{id}` (poll), `GET …/providers` (catalogue). No new read endpoints — imported chargers/vehicles/sessions surface in the existing depot views.

### Optimization Control Loop

```
TriggerMonitor (SoC deviation / price spike / return delay / scheduled)
        │
        ▼
StateAssembler.get_current_state()
  ├── vehicle SoCs (telemetry table, max age 15 min)
  ├── prices (prices table, max age 24 hr)
  ├── schedules (departures/returns)
  ├── building_load (required, max age 1 hr)
  └── battery SoC
        │
        ▼
optimizer/milp_model.py → Pyomo ConcreteModel
        │
        ▼
optimizer/solver.py → Gurobi (primary) → HiGHS (fallback)
        │
        ▼
optimizer/allocator.py → per-vehicle charging schedules
        │
        ▼
adapters/ocpp/dispatch.py → SetChargingProfile to each charger
        │
        ▼
DB: optimization_runs, charging_commands tables
```

---

## Hard Constraints (MUST NEVER RELAX)

These come directly from the PRD and are non-negotiable:

| Constraint | Value | PRD Ref |
|---|---|---|
| Vehicle departure SoC | ≥ 99% | Section 8.1 |
| Optimization solve time | < 60 seconds | Section 8.3 |
| Primary OCPP protocol | OCPP 1.6 (2.0.1 future-ready) | Section 9.1 |
| Site grid power constraint (`max_grid_kw`) | NEVER violated | Section 9.4 |
| MVP connector type | CCS only | Section 3.2 |

**Building load is OPTIONAL for initial customer onboarding (e.g. HRX pilot)** — deferred until a meter/BMS integration is delivered. When no live source is configured, the optimizer runs in `degraded` mode and applies a depot-level static `building_load_assumption_kw` as a derate on `max_grid_kw` so the site-power constraint is still respected. The run's `status='degraded'` and the snapshot records the assumption used. Re-introduce as required once meter/BMS lands. (PRD Section 9.4)

---

## Re-optimization Triggers

| Trigger | Threshold | PRD Ref |
|---|---|---|
| Price spike | > 25% OR > $25/MWh (OR logic) | Section 5.1 |
| SoC deviation | > 5% | Section 5.1 |
| Return time deviation | > 15 minutes late | Section 5.1 |
| Inter-depot handoff | On message receipt | Section 5.4 |
| Scheduled | Hourly 24/7 | Section 5.1 |

---

## Database Schema (TimescaleDB / PostgreSQL 16)

> **Two-database invariant:** Static reference tables (`sites`/depots, `vehicles`, `charging_stations`/chargers, `organizations`, `schedules`, `battery_storage`, `charger_vehicle_access`, `drivers`, `rfid_cards`) live **exclusively in Supabase** (`pools.static`). The TimescaleDB migration set (`migrations/*.sql`) NEVER creates these tables. Depot/vehicle/charger identity columns on TimescaleDB operational tables are plain `UUID` columns — no FK constraints pointing at static data. This invariant was established by migrations 028–029 and 039 and is now enforced from the very first migration so that a fresh TimescaleDB install contains zero shadow copies.

### Reference (static) tables — Supabase project `favonius-pilot`

> **Naming convention:** Supabase owns the canonical naming for the static
> tables, so the actual table names in Postgres differ from the Favonius
> internal vocabulary. The backend continues to call them "depots" and
> "chargers" in Python (`depot_id`, `charger_id`, `Depot` class, etc.) and
> aliases the Supabase column names back at the SQL boundary
> (`SELECT id AS depot_id FROM sites …`). Supabase additions live in
> `migrations/supabase/006_align_static_schema_to_supabase.sql` and
> `migrations/supabase/007_supabase_static_operational_tables.sql`.

| Backend concept | Supabase table | PK column (alias) | Key columns / aliases |
|---|---|---|---|
| Depot | `sites` | `id` (`AS depot_id`) | `organization_id`, `max_grid_kw`, `demand_charge_rate_kw`, `demand_charge_billing_period`, `timezone`, `currency`, `utility_id`, `address`, `billing_metadata`, `building_load_source`, `building_load_assumption_kw`, `access_mode`, `charger_vehicle_access_default`, `tariff_config`, `latitude`, `longitude` |
| Charger | `charging_stations` | `id` (`AS charger_id`) | `site_id` (`AS depot_id`), `station_id` (`AS ocpp_id`), `max_power_kw` (`AS rated_kw`), `efficiency`, `auth_required`, `connector_type`, `display_name`, `vendor`, `connector_count`, `connector_ids` |
| Vehicle | `vehicles` | `id` (`AS vehicle_id`) | `organization_id` (Supabase native), `site_id` (`AS depot_id`, added by mig 006), `vin` (Supabase native, UNIQUE), `external_id` (mig 006), `vehicle_type` (mig 006), `id_tag` (mig 006), `battery_capacity_kwh` (`AS battery_kwh`), `max_charge_rate_kw` (`AS max_charge_kw`), `max_discharge_rate_kw`, `v2g_capable`, `license_plate`, `driver_id`, `status` |
| Organization | `organizations` | `id` (`AS organization_id`) | `name`, `type`, `billing_address`, `primary_contact`, `subscription_tier`, `is_active`, `agent_sql_mode_enabled` (BOOLEAN, default TRUE; gates chat SQL mode per org — mig `supabase/044`) |
| Org membership | `user_organizations` | `(user_id, organization_id)` | `role` (Supabase vocab: `owner|admin|operator|viewer`, enforced by CHECK constraint). The backend's tenant mirror translates Favonius vocab → Supabase vocab at the write boundary (`customer_admin → owner`, `customer_operator → operator`, `favonius_admin → admin`, unknown → `viewer`). Authorization never reads this column — see `_supabase_role_for` in `src/security/tenant_mirror.py`. |
| Charger ↔ Vehicle access | `charger_vehicle_access` | `(charging_station_id, vehicle_id)` | `is_accessible`, `notes`. Note the FK column name is `charging_station_id`, not `charger_id`. |
| Battery | `battery_storage` | `id` (`AS battery_id`) | `site_id` (`AS depot_id`), `capacity_kwh`, `max_power_kw`, `efficiency`, `soc_min`, `soc_max` |
| Per-day route | `schedules` | `id` (`AS schedule_id`) | `vehicle_id`, `route_id`, `departure_time`, `return_time`, `actual_return_time`, `energy_kwh`, `required_soc`, `dest_site_id` (`AS dest_depot_id`). Distinct from Supabase's recurring `vehicle_schedules` table. |
| Recurring route template | `recurring_schedule_template` (+ a cancellations table) | `id` | `depot_id`, `vehicle_id`, `route_id`, `departure_time_of_day`, `return_time_of_day`, `days_of_week TEXT[]`, `start_date`, `end_date`, `required_soc` (0.99–1.0), `energy_kwh`, `active` (mig `supabase/041`). Materialised into per-day trips by `src/core/scheduling/recurring.py`; cancellations keyed by `(template_id, occurrence_date)`. |
| Driver | `drivers` | `id` (`AS driver_id`) | `site_id`, `external_driver_id`, `display_name`, `email`, `phone`, `status` |
| RFID card | `rfid_cards` | `id` (`AS card_id`) | `site_id`, `id_tag` (UNIQUE), `label`, `status` |
| RFID assignments | `rfid_card_vehicle_assignments`, `rfid_card_driver_assignments` | composite | `card_id`, `vehicle_id` / `driver_id` (FK column names retained on join tables) |
| Per-charger Basic Auth | `station_credentials` | `id` (SERIAL) | `station_id` (the OCPP id; lookup key — NOT the UUID PK), `username`, `password_hash`, `active`, `last_rotated_at` |
| Charger onboarding cache | `charger_onboarding_idempotency` | `id` | `organization_id`, `endpoint`, `idempotency_key`, `request_hash`, `response_json`, `expires_at` |

Frontend-owned Supabase tables not consumed by this backend: `profiles`, `waitlist`, `faqs`, `glossary_items`, `vehicle_schedules` (recurring patterns), `charging_schedules_config`, `charging_sessions_active`, `charging_sessions_summary`, `vehicle_realtime_state`, `api_usage`.

### Time-series hypertables
- `telemetry` — Vehicle SoC, charging_kw, is_plugged (from OCPP MeterValues)
- `prices` — $/kWh by depot and time (utility TOU; new architecture)
- `electricity_prices` — Per-bidding-zone day-ahead prices written by the WS handler price feeder (migration 034). Schema-compatible with the historical CAISO LMP shape but populated from ENTSO-E in the current deployment.
- `weather_forecasts` — Temperature, precipitation, solar radiation
- `building_load` — Non-EV site power draw (**required** for grid calc)

### Tenant mirroring (JIT)
- On each authenticated API request, `src/security/tenant_mirror.py` best-effort **UPSERT**s `organizations` and `user_organizations` from the verified JWT payload (`sub`, `app_metadata.organization_id`, `app_metadata.organization_name`, `app_metadata.favonius_role`). The org row uses `organizations.id` as the PK column. If `organization_name` is absent, a deterministic placeholder (`org-<org_uuid_prefix>`) is used for bootstrap rows. **Skips** `favonius_admin` and users without `organization_id`. Failures are logged and do not block the request (depot access still uses JWT vs `sites.organization_id`).
- In-process TTL cache: `TENANT_MIRROR_TTL_S` (default `300`) seconds per `sub` to limit DB writes.
- **Reverse-direction self-heal:** `repair_user_tenant_metadata` runs first in `ensure_tenant_mirrored`. Handles two cases:
  - **Case A** — JWT lacks `app_metadata.organization_id`: when the user has exactly one `user_organizations` row whose role is `owner` or `operator`, the repair `PUT`s the full triple (`favonius_role`, `organization_id`, `organization_name`) to `{SUPABASE_URL}/auth/v1/admin/users/{user_id}` using `SUPABASE_SERVICE_KEY`.
  - **Case B** — JWT has `organization_id` but lacks `favonius_role` (the Gustas/HRX pattern): looks up the role for that specific `(user, org)` pair in `user_organizations` and pushes just `favonius_role` (plus `organization_name` if also absent). `organization_id` is not overwritten.
  In both cases the current request still proceeds with the unpatched token — the user's *next* token refresh sees the corrected claims. Skipped silently if `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are unset, no repairable membership exists, or the membership role is `admin`/`viewer` (the `admin` exclusion prevents a corrupted DB row from escalating to `favonius_admin`). Successful repairs log `tenant_metadata_repair: backfilled app_metadata` at WARNING level so high counts surface a frontend-signup regression.
- Workspace **invitations** are managed in Supabase only; there is no `invitations` table in this backend.

### Favonius staff auto-promotion
- `get_user_role` in `src/security/auth.py` resolves any verified JWT whose `email` ends with `@favoniusenergy.com` to `favonius_admin` regardless of `app_metadata.favonius_role`. This grants platform-wide access (all depots, all tenants, cross-org reads) and skips tenant mirroring as a side effect of the existing `favonius_admin` skip.
- Domain comparison is exact (no subdomain matching) and case-insensitive. A token whose `user_metadata.email_verified` is explicitly `False` is **not** promoted — that defends against unverified-signup spoofing in projects that disabled email confirm.
- Configure additional / alternate domains via `FAVONIUS_ADMIN_EMAIL_DOMAINS` (comma-separated). When set, it **replaces** the default — include `favoniusenergy.com` explicitly if you still want it.
- Promotion overrides any explicit `app_metadata.favonius_role`, so a stale Supabase metadata value cannot demote a Favonius employee. To exclude a specific Favonius email (e.g. a contractor on a `@favoniusenergy.com` address), do not issue them an `@favoniusenergy.com` JWT email — there is no per-user opt-out hook.

### Operational tables
- `optimization_runs` — Solver results, schedule JSON, status, solver_used
- `charging_commands` — OCPP SetChargingProfile records and acknowledgment status (per-run audit; `charger_id` and `vehicle_id` are plain UUID references — no FK to Supabase static tables)
- `charging_command_queue` — Durable buffer for SetChargingProfile pushes that arrived while a charger was offline; replayed by the legacy WS handler on next BootNotification (migration 013)
- `charging_sessions` — OCPP 1.6 transaction lifecycle. `transaction_id` (BIGINT, from `ocpp_transaction_id` sequence), `last_seen_at` stamped by the WS close hook
- `interdepot_messages` — Cross-depot vehicle handoff messages
- `trigger_log` — Audit trail of re-optimization triggers
- `audit_log` — Application-level admin audit trail (cross-org reads, credential rotations). Distinct from `security_audit_log` (NKSC hypertable). Columns: `id, occurred_at, actor_user_id, actor_role, organization_id, depot_id, action, target_type, target_id, metadata` (JSONB). Common `action` values: `admin.read`, `charger.credentials.rotated`, `agent.query`. Written by `src/security/admin_audit.py::write_admin_audit_row` via the **TimescaleDB pool** (`db_pools.ts`, migration 021).
- `security_events` — Failed-auth events emitted by the legacy WS handler (migration 017). Written by `src/websocket_handler/timescale_client.py::store_security_event` via the TimescaleDB pool. Columns: `id (SERIAL), station_id, event_type, timestamp, tech_info, additional_info, created_at`.
- `connector_status` — OCPP StatusNotification records per connector. Append-only; the latest row per `(station_id, connector_id)` is the current state. The legacy WS handler appends an `Unavailable`/`ConnectionLost` row when the WebSocket drops. Migration 022 attaches the `fn_alerts_on_connector_status` trigger that produces `notification_alerts` rows on Faulted/Unavailable transitions and PERFORMs `pg_notify('notification_alerts_new', …)`.
- `notification_alerts` — Depot/org-scoped alert aggregator (migration 022). Partial unique index `(organization_id, dedup_key) WHERE status != 'resolved'` keeps one active row per fault; resolved rows let new occurrences in. `severity_level` is a generated SMALLINT (1=info, 2=warning, 3=critical). Status: `'active' | 'acknowledged' | 'resolved'`.
- `notification_recipients` — Per-org email subscribers (migration 022). `alert_types` is `TEXT[]` where `'{*}'` matches all types; `min_severity` (with generated `min_severity_level`) gates which alerts the recipient receives.
- `notification_deliveries` — Append-only delivery ledger (migration 022). `UNIQUE (alert_id, recipient_id, notified_count)` is the idempotency anchor. Status: `'sent' | 'delivered' | 'bounced' | 'complained' | 'failed'`. Updated by the `POST /webhooks/resend` handler from Resend events.
- `agent_runs` — Per-turn audit trace for the chat agent (migration 025). `failure_reason` (migration `045_agent_failure_reason.sql`) is a nullable, CHECK-constrained taxonomy column (seven fixed categories) written by `agent_runs_close()` and classified solely in `src/api/agent/audit.py::classify_failure`. Restricted-update guard added by `042_agent_views_ts.sql`.
- `decisions` / `workflows` / `workflow_tiers` — Depot Agent workflow substrate (migration 037). Append-only `decisions` hypertable; `workflow_tiers` holds per-(workflow, depot) trust-graduation rows. Written via `insert_decision` (`src/api/agent_workflows/repository.py`).
- `agent_autonomy_settings` — Per-(depot_id, action_class) autonomy level (migration 043). PK `(depot_id, action_class)`; `level ∈ {shadow, proposed, auto_notify, auto_silent}`. Backs `GET /depots/{id}/autonomy-settings` and the `agents.autonomy.set` command. Distinct from `workflow_tiers` (which is per-workflow).
- `charger_log_imports` / `charger_session_log_entries` / `session_log_reconciliations` — Charger-side diagnostic-log extraction (migration `042_charger_log_imports.sql`). See "Charger-side log extraction".
- `report_schedules` / `report_schedule_recipients` / `schedule_runs` / `schedule_run_deliveries` — Scheduled reports (migration 044). See "Scheduled reports".

### Key columns
- All UUIDs use `gen_random_uuid()` as default
- `solver_used` values: `'gurobi'` | `'highs'`
- `optimization_runs.status` values: `'optimal'` | `'feasible'` | `'degraded'` | `'infeasible'` | `'timeout'`
- `charging_command_queue.status` values: `'pending'` | `'sent'` | `'acked'` | `'failed'` | `'expired'`
- `ocpp_transaction_id` / `ocpp_charging_profile_id` sequences (migration 012) provide restart-safe OCPP 1.6 integer IDs
- `charging_sessions.cost_total_source` values (migration 040): `'granular'` | `'fallback_average'` | `'unpriceable'` | `'no_energy'` | `'no_depot'` | `'manual'`. Written by `src/core/billing/session_cost.py`; the calculator never overwrites `'manual'` or any non-zero externally-sourced cost.

### Migrations
Migrations in `migrations/` run automatically on `docker-compose up` (mounted to `/docker-entrypoint-initdb.d`). To run manually: `python scripts/run_migrations.py`.

The runner is **stateless** (no `applied` tracking table) — every file re-executes on each deploy, and all DDL uses `IF [NOT] EXISTS` / `DROP … IF EXISTS` for idempotency. Migrations that formerly altered shadow tables (011, 016, 017, 018, 020, 021) are guarded with `to_regclass('public.<shadow_table>') IS NULL` checks so they silently skip on fresh databases. The `schedules` Supabase table is listed under §"Reference (static) tables" above; there is no `schedules` table in TimescaleDB.

**Numbering note — duplicate migration numbers are expected.** Parallel PRs each grab the "next" number, so several numbers have 2–3 files (e.g. TimescaleDB `014`, `021`×3, `024`×3, `025`, `028`, `036`, `042`×2, `045`×3; Supabase `005`, `008`, `009`, `013`, `014`). The runner applies `sorted(glob("*.sql"))` (`scripts/run_migrations.py`), so same-number files run in **alphabetical filename order** by their suffix (e.g. `042_agent_views_ts.sql` before `042_charger_log_imports.sql`). Because every file is idempotent and re-runs on each deploy, this is currently benign, but it makes apply order depend on naming — keep new migrations independent of any same-number sibling. Supabase numbering also jumps `015 → 040` (the static set was renumbered into the 040+ band to track the TimescaleDB set). The directory itself is the source of truth for the full list.

---

## REST API Endpoints

All non-health endpoints require JWT in `Authorization: Bearer <token>` header. Paths below use `{id}` as shorthand for the depot UUID (`{depot_id}` in the code). The table covers the primary endpoints; the API surface is larger (~95 routes across `main.py` + the `optimization`, `data_sources`, and `agent` routers) — **`docs/API.md` is the exhaustive reference**, and the grouped subsections below cover the rest.

| Method | Path | Description |
|---|---|---|
| `POST` | `/optimize` | Trigger depot MILP optimization |
| `GET` | `/depots/{id}/state` | Current SoCs, battery state, peak demand, price |
| `GET` | `/depots/{id}/savings-summary` | Month-to-date charging cost vs flat-rate baseline (current_month_eur, baseline_month_eur, saved_eur, saved_pct, period_start, period_end, as_of). Computed from `charging_sessions.cost_total` + `electricity_prices` average over the period. Missing-data paths (no sessions, no zone, no prices) return zeros instead of 500 so the UI shows '—'. |
| `GET` | `/depots/{id}/schedule` | Latest charging schedule |
| `GET` | `/depots/{id}/alerts` | Charger faults + last optimization + notification_alerts (alerts pipeline) |
| `POST` | `/depots/{id}/alerts/{alert_id}/acknowledge` | Mark a notification alert as acknowledged (alerts pipeline) |
| `POST` | `/depots/{id}/vehicles/{vid}/handoff` | Send inter-depot handoff |
| `POST` | `/depots/{id}/handoff/receive` | Receive inter-depot handoff |
| `GET` | `/depots/{id}/agent-actions` | List depot agent-proposed actions ordered by `created_at` desc (camelCase wire format; `AgentActionSchema`). Polled by the today view. |
| `GET` | `/depots/{id}/autonomy-settings` | Per-depot autonomy matrix: `{rows: [{actionClass, level}], asOf}`. Returns defaults for the five known classes (`charger_restart`, `session_reassign`, `price_reoptimize`, `soc_guardrail`, `report_draft`) plus any persisted rows from `agent_autonomy_settings` (mig 043). |
| `POST` | `/commands/execute` | Unified command dispatcher. Agent commands (all `depot:manage`): `agents.action.approve` / `agents.action.reject` / `agents.action.rollback` (`{actionId}`), `agents.autonomy.set` (`{actionClass, level}`). |
| `GET` | `/health` | Component health (DB, OCPP server, Gurobi license) |
| `GET` | `/metrics` | Prometheus metrics (text format) |
| `GET` | `/admin/controllers` | List active depot controllers |
| `GET` | `/admin/controllers/{id}/health` | Controller health |
| `POST` | `/admin/depots` | Create a tenant-scoped depot in the caller's organization. Safe to call repeatedly; `Idempotency-Key` header required (customer_admin, JWT `app_metadata.organization_id` required) |
| `POST` | `/admin/first-depot-setup` | Backward-compatible alias for `POST /admin/depots`. Same handler; same `Idempotency-Key` requirement |
| `PATCH` | `/admin/depots/{id}` | Update tenant-scoped depot setup (customer_admin + depot access required) |
| `GET` | `/admin/organizations` | List all organizations (favonius_admin only; writes `admin.read` audit row) |
| `GET` | `/admin/organizations/{org_id}/depots` | List depots for an organization (favonius_admin or matching customer_admin; cross-org reads write `admin.read`; mismatched customer_admin → 403, NOT 404) |
| `GET` | `/admin/depots/{id}/chargers/{charger_id}/credentials_status` | `{configured, created_at, last_rotated_at}` only — never plaintext or password_hash (favonius_admin or tenant member; cross-org reads write `admin.read`) |
| `POST` | `/admin/depots/{id}/chargers/{charger_id}/rotate_credentials` | Generate new Basic Auth credential, replace `station_credentials.password_hash`, return plaintext exactly once (favonius_admin or matching customer_admin; writes `charger.credentials.rotated`) |
| `GET` | `/admin/ocpp/{cp_id}/state` | (Legacy WS handler, port 8080) Per-charger debug dump: connection state, vendor/model, last_boot_at, last_heartbeat_at, latest connector_status, open transactions, charging_command_queue rollup. Owner role required. |
| `GET` | `/admin/organizations/{org_id}/notification_recipients` | List alert recipients (favonius_admin or matching customer_admin; cross-org reads write `admin.read`) |
| `POST` | `/admin/organizations/{org_id}/notification_recipients` | Create a recipient (alerts pipeline). 409 on duplicate `(org, email)`. |
| `PATCH` | `/admin/organizations/{org_id}/notification_recipients/{id}` | Patch a recipient |
| `DELETE` | `/admin/organizations/{org_id}/notification_recipients/{id}` | Hard-delete a recipient (cascades deliveries). |
| `POST` | `/webhooks/resend` | Public, signature-verified Resend webhook for delivery status updates (alerts pipeline) |
| `GET` | `/admin/depots/{id}/chargers/{cid}/sessions` | List completed charging sessions for a single charger (keyset pagination, `from`/`to` filters). Feeds the charger-logs admin UI: operator picks a session, then triggers `fetch_logs`. Same role gate as `fetch_logs` (customer_admin or favonius_admin); same response shape as `GET /depots/{id}/sessions`. |
| `POST` | `/admin/depots/{id}/chargers/{cid}/sessions/{sid}/fetch_logs` | Trigger OCPP `GetDiagnostics` on a session's charger and store the upload alongside the session. customer_admin or favonius_admin; writes `charger.logs.fetched` audit. |
| `GET` | `/admin/depots/{id}/sessions/{sid}/log_comparison` | Return `{session, import, charger_entries, reconciliation}` for side-by-side comparison. 404 until an import exists. |
| `POST` | `/internal/charger_logs/upload` | Public charger-uploaded diagnostic archive. Auth via HMAC token in query string (`CHARGER_LOG_UPLOAD_SIGNING_KEY`). |
| `POST` | `/agent/turn` | Depot chat agent — synchronous turn; returns `AgentReply` (10 req/min; requires `AGENT_SEARCH_ENABLED=true`) |
| `POST` | `/agent/turn/stream` | Depot chat agent — SSE streaming turn; emits `step` events then `answer` (10 req/min; same gate) |
| `GET` | `/agent/runs/{run_id}` | Fetch stored agent run trace (ownership-gated; `favonius_admin` may access any run) |
| `GET` | `/depots/{id}/report-schedules` | List report schedules (any depot member; `[]` when none) |
| `GET` | `/depots/{id}/report-schedules/{schedule_id}` | Get one report schedule |
| `GET` | `/depots/{id}/report-schedules/{schedule_id}/runs` | List a schedule's runs (most-recent first) |

Report-schedule mutations flow through `POST /commands/execute` (customer_admin+, `ADMIN_CONFIG`): `reports.schedule.create` (`params.input`), `reports.schedule.update` (`params.scheduleId`+`patch`), `reports.schedule.delete` (`params.scheduleId`), `reports.schedule.run_now` (`params.scheduleId` → returns `ScheduleRun`).

### Fleet & identity management
CRUD over the Supabase static tables. Admin paths are `customer_admin`+ and depot-scoped; the `/depots/...` reads are any depot member.

| Method | Path | Description |
|---|---|---|
| `GET`/`POST` | `/admin/depots/{id}/chargers` | List / create chargers |
| `GET`/`POST`/`PATCH` | `/admin/depots/{id}/vehicles` (+ `/{vid}`, `/{vid}/primary-id-tag`) | Vehicle CRUD + primary id-tag |
| `GET`/`POST`/`PATCH` | `/admin/depots/{id}/drivers` (+ `/{driver_id}`) | Driver CRUD |
| `GET`/`POST`/`PATCH` | `/admin/depots/{id}/rfid-cards` (+ `/{card_id}`) | RFID card CRUD |
| `POST` | `/admin/depots/{id}/charger-vehicle-access` | Set charger↔vehicle access |
| `GET` | `/admin/depots/{id}/identity` | Depot identity / setup metadata |
| `POST` | `/admin/depots/{id}/charging-sessions/import` | Bulk-import historical sessions (`source='import'`) |
| `GET` | `/depots/{id}/chargers` | List chargers (depot member) |
| `GET` | `/depots/{id}/vehicles`, `/depots/{id}/vehicles/state` | Vehicle list + live SoC / plugged state |
| `GET` | `/me/depots` | Depots visible to the caller |

### Charger operations (admin)
| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/depots/{id}/chargers/{cid}/manual_authorize` | Operator override — manually authorize a charge |
| `POST` | `/admin/depots/{id}/chargers/{cid}/local_auth/reset` | Reset OCPP local auth list |
| `GET` | `/admin/depots/{id}/chargers/{cid}/last_manual_override` | Last manual-override record |

### Schedule management (admin)
| Method | Path | Description |
|---|---|---|
| `GET`/`POST` | `/admin/depots/{id}/schedule/manual` (+ `/{schedule_id}`) | One-off route CRUD |
| `GET`/`POST` | `/admin/depots/{id}/schedule/recurring` (+ `/{template_id}`) | Recurring template CRUD (mig `supabase/041`) |
| `POST` | `…/schedule/recurring/{template_id}/pause` · `/resume` | Pause / resume a template |
| `POST` | `…/schedule/recurring/{template_id}/occurrences/{date}/cancel` | Skip a single occurrence |
| `GET` | `/admin/depots/{id}/schedule/readiness` | Schedule readiness summary |

### Reports & analytics
| Method | Path | Description |
|---|---|---|
| `GET`/`POST` | `/depots/{id}/reports` (+ `/{report_id}`, `/{report_id}/export`) | Generate / fetch / export a report |
| `GET` | `/reports/depots/{id}/energy/monthly` (+ `.csv`, `/sessions`, `/transactions`) | Energy report rollups (`src/api/reports.py`) |
| `GET` | `/depots/{id}/sessions`, `/depots/{id}/sessions/active` | Charging sessions (history / live) |

### Optimization & liveness
| Method | Path | Description |
|---|---|---|
| `GET` | `/depots/{id}/optimization/readiness` | Inputs-ready check before a solve |
| `GET` | `/depots/{id}/optimization/solver` | Latest solver-run metadata (`src/api/optimization.py`; structured 503 on solver failure) |
| `GET` | `/depots/{id}/liveness/stream` | SSE charger-liveness stream (`LivenessHub`) |

### Data Sources (admin; `customer_admin`, depot-scoped)
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/data-sources/providers` | Provider catalogue (UI-rendered credential-field schema) |
| `GET`/`POST` | `/admin/data-sources/connections` | List / create connections |
| `GET`/`PATCH`/`DELETE` | `/admin/data-sources/connections/{id}` | Manage one connection |
| `POST` | `/admin/data-sources/connections/{id}/sync` | Trigger a sync (202 + `status_url`) |
| `GET` | `/admin/data-sources/connections/{id}/jobs` · `/admin/data-sources/jobs/{id}` | Ingestion job history / poll |

### Internal
| Method | Path | Description |
|---|---|---|
| `POST` | `/internal/ocpp-event` | OCPP-event ingress from the WS handler; internal-only, gated by `INTERNAL_API_TOKEN` (required in production) |

### WebSocket endpoints
- `ws://host:9000/ocpp/{charge_point_id}` — OCPP 1.6 (dedicated port)
- `ws://host:8000/ocpp/{charge_point_id}` — OCPP via REST port (when `OCPP_USE_SAME_PORT=true`)
- `wss://host/vdv463/{presystem_id}` — VDV 463 transit operations
- `wss://host/bacnet/{device_id}` — BACnet/SC (future)

### Rate limits (per PRD Section 10.4)
- `POST /optimize`: 10 req/min
- `/depots/*/handoff`: 50 msg/hr per depot pair
- All other endpoints: 100 req/min

---

## OCPP Implementation

### Supported operations (OCPP 1.6 primary)
Full coverage of all 28 OCPP 1.6 actions including: BootNotification, Heartbeat, Authorize, StartTransaction, StopTransaction, MeterValues, StatusNotification, ChangeAvailability, ChangeConfiguration, ClearCache, DataTransfer, GetConfiguration, RemoteStartTransaction, RemoteStopTransaction, Reset, SetChargingProfile, ClearChargingProfile, GetCompositeSchedule, UnlockConnector, GetDiagnostics, UpdateFirmware, and more.

### Key OCPP data flows
- `idTag` in Authorize/StartTransaction → looked up against `vehicles.id_tag`; unknown tags get `Invalid`
- `MeterValues` → `telemetry` table (SoC, charging_kw, max_charge_kw updated)
- `StatusNotification` → `connector_status` table (both the new adapter and the legacy `OCPP16Session` write here)
- `StartTransaction` → `transactionId` from `ocpp_transaction_id` sequence; an open `charging_sessions` row is inserted so a handler restart can rehydrate it
- `StopTransaction` → closes the `charging_sessions` row (`end_time`)
- `SetChargingProfile` → after each optimization run the FastAPI service writes one row per scheduled vehicle to `charging_command_queue` (it does NOT push in-process — production runs with `OCPP_SERVER_ENABLED=false`). The legacy WS handler's `ChargingCommandQueueConsumer` (`src/websocket_handler/charging_profile_manager.py::ChargingCommandQueueConsumer`) drains the queue every ~2 s (or on `pg_notify` from migration 014), pushes via the in-memory `OCPP16Session`, and marks rows `sent`/`failed`. Rows whose charger is offline stay `pending`; the BootNotification replay path flushes them on reconnect.

### Cross-restart recovery (legacy handler, migrations 012 + 013 + 014)
- BootNotification: `OCPP16Session._on_boot` reloads open sessions into `FleetChargePoint.transactions` and triggers `replay_queued_commands` so any pending profiles are pushed within ~1s
- WebSocket close: `OCPPWebSocketServer._cleanup_connection` appends an `Unavailable`/`ConnectionLost` row to `connector_status` and stamps `last_seen_at` on every still-open session at the station

### Connector path routing
```
/ocpp/{charge_point_id}    → OCPP 1.6
/vdv463/{presystem_id}     → VDV 463 transit
/bacnet/{device_id}        → BACnet/SC (future)
```

---

## VDV 463 Integration

VDV 463 is the German transit industry standard for BMS (Battery Management System) / ITCS (Intermodal Transport Control System) communication. Favonius acts as the CMS (Charging Management System).

**Validated against schemas in `schemas/vdv463/`:**
- `MessageStructure.json`
- `ProvideChargingRequestsRequest.json`
- `ProvideChargingInformationRequest.json`

**Key behaviors:**
- On validation or semantic errors, return `MessageType 3` (VDV 463 Error message)
- Log structured error details for `InvalidVehicleId` / `InvalidChargingPointId`
- See `.cursor/rules/` for detailed protocol patterns

---

## Surrogate Model (Energy Consumption)

Located in `src/core/surrogate/`. Gaussian Process Regressor (sklearn) following the Stanford CarbonFree approach.

**Input features:** bus_size, route_id, temp_avg_f, temp_max_f, temp_min_f, rain_inches, solar_radiation, is_school_day

**Output:** predicted kWh energy consumption for a trip

**Coverage targets:** ≥ 90% unit test coverage (PRD Section 11.2)

---

## Development Setup

### Install dependencies
```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

### Run locally (Docker for DB)
```bash
# Start TimescaleDB only
docker-compose up -d timescaledb

# Run API server with hot reload
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Verify
curl http://localhost:8000/health
```

### Full stack with Docker
```bash
# Basic (TimescaleDB + API)
docker-compose up -d

# With OCPP simulator
docker-compose --profile simulation up -d

# With monitoring (Prometheus + Grafana)
docker-compose --profile monitoring up -d

# All services
docker-compose --profile simulation --profile monitoring up -d
```

### Service URLs
- REST API: http://localhost:8000
- API docs: http://localhost:8000/docs
- OCPP WebSocket: ws://localhost:9000/ocpp
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin/admin123)

---

## gstack Skills

gstack is installed globally at `~/.claude/skills/gstack` with individual skills symlinked into `~/.claude/skills/`. Use these slash commands at the appropriate stage of development. Browser-based skills (`/qa`, `/browse`, `/benchmark`, `/canary`, `/setup-browser-cookies`, design-only skills) are excluded — not applicable to this Python backend.

To upgrade: `/gstack-upgrade`. Source: `~/.claude/skills/gstack/`.

### Planning

| Command | When to use |
|---|---|
| `/office-hours` | **Before starting any new feature** — six forcing questions that challenge premises, reframe scope, and surface alternatives before committing to an approach |
| `/plan-ceo-review` | Strategic scope decision (4 modes: expand / selective expand / hold / reduce). Use when debating feature scope with yourself or stakeholders |
| `/plan-eng-review` | **Before coding any non-trivial change** — sequential review: architecture → code quality → tests → performance, one issue at a time with pros/cons. Integrates with the Plan Mode Protocol above |
| `/autoplan` | Hands-off automated CEO + Eng review pipeline. Surfaces only "taste decisions" to the user; auto-decides everything else using completeness, DRY, and pragmatism principles |

> **Integration with Plan Mode Protocol:** `/plan-eng-review` is the preferred execution vehicle for the 4-section review defined in the Plan Mode Protocol above. The BIG CHANGE / SMALL CHANGE scope choice maps directly to gstack's interactive vs. autoplan modes.

### Development & Debugging

| Command | When to use |
|---|---|
| `/review` | Pre-PR staff-engineer code review — two-pass: critical (SQL safety, race conditions, TimescaleDB query patterns, JWT/OCPP trust boundaries, enum completeness) then informational (dead code, test gaps, perf). Auto-fixes what it can, batches ASK findings |
| `/investigate` | **Bug fixing — no fix without root cause first.** 4-phase: symptoms → pattern analysis → hypothesis testing → fix + regression test. Use for solver failures, OCPP connection bugs, DB anomalies, state assembler errors. Scope-locked: won't touch unrelated files |

### Security

| Command | When to use |
|---|---|
| `/cso` | Full 14-phase audit: OWASP Top 10, STRIDE threat modeling, secrets archaeology, supply chain, CI/CD pipeline, LLM/AI security, data classification. Run before releases |
| `/cso --owasp` | OWASP Top 10 only — run after any JWT auth, OCPP handler, or API boundary change |
| `/cso --infra` | Infrastructure only (Phases 0–6) — after Docker, Railway, or DB config changes |
| `/cso --code` | Code-only scan (Phases 0–1, 7, 9–11) — after rate limiter, validator, or auth changes |
| `/cso --comprehensive` | Monthly deep scan with 2/10 confidence gate — surfaces tentative findings |
| `/cso --diff` | Branch-diff only — combinable with any scope flag for PR-scoped audits |

### Shipping & Deployment

| Command | When to use |
|---|---|
| `/ship` | Full PR creation workflow: merge base branch, run test suite, coverage audit (traces all code paths), pre-landing review, version bump, auto-CHANGELOG, PR creation with full evidence body |
| `/land-and-deploy` | Post-PR: waits for CI, merges, detects Railway deploy, polls until live, verifies `/health` endpoint, offers revert commit on failure |
| `/document-release` | After shipping: syncs `docs/API.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md` to reflect what actually shipped. Run after any endpoint, schema, or config change |
| `/setup-deploy` | One-time Railway deploy configuration detection — run when first setting up CI/CD |
| `/gstack-upgrade` | Update gstack to latest version |

### Safety & Guardrails

| Command | When to use |
|---|---|
| `/careful` | Activate at session start for production-adjacent work — intercepts `rm -rf`, `DROP TABLE`, `TRUNCATE`, `git push --force`, `git reset --hard`, `docker system prune` with a warning before execution. Allows `__pycache__` / `.pytest_cache` deletions without warning |
| `/freeze migrations/` | Lock the `migrations/` directory from edits — use when doing work unrelated to schema changes to prevent accidental migration edits |
| `/freeze config/` | Lock `config/` (depot_config.yaml, tariff_config.yaml) during non-config sessions |
| `/unfreeze [path]` | Remove a freeze restriction |
| `/guard` | Combined `/careful` + `/freeze` — activate for high-risk sessions (running migrations, production deploys, dependency upgrades) |

### Retrospective

| Command | When to use |
|---|---|
| `/retro` | Weekly engineering retrospective — commit velocity, per-contributor breakdowns, hotspot files, fix-to-feature ratio, session patterns |
| `/retro compare` | Side-by-side: current week vs prior week |
| `/retro global` | Cross-project retrospective across all AI-assisted coding sessions |

### Excluded Skills (not applicable to this backend)

The following gstack skills are **not symlinked** because they require Chromium or are UI-specific:
`/browse`, `/qa`, `/qa-only`, `/benchmark`, `/canary`, `/setup-browser-cookies`, `/plan-design-review`, `/design-consultation`, `/design-review`.
`/codex` is excluded because its adversarial cross-model review is already embedded inside `/review` (medium/large diffs) and `/ship`.

---

## Testing

### Run tests
```bash
# All tests
pytest

# Unit tests only (no DB required)
pytest tests/unit -v

# With coverage
pytest --cov=src --cov-report=html --cov-report=term

# Specific markers
pytest -m unit
pytest -m integration
pytest -m "not slow"
```

### Via Makefile
```bash
make test              # All tests
make test-unit         # Unit only
make test-integration  # Integration only
make test-coverage     # With HTML coverage report
```

### Coverage requirements
- Overall: ≥ 80% (`fail_under = 80` in pytest.ini)
- Optimizer: ≥ 90% (PRD Section 11.2)
- Surrogate model: ≥ 90% (PRD Section 11.2)

### Test markers
`unit`, `integration`, `e2e`, `load`, `slow`, `docker`, `compliance`, `critical`, `edge_case`, `security`, `acceptance`, `performance`, `database`

### Key test files
- `tests/unit/test_optimizer.py` — MILP model and solver tests
- `tests/unit/test_controller.py` — Control loop tests
- `tests/unit/test_api_main.py` — REST endpoint tests
- `tests/unit/test_ocpp_server_full.py` — Full OCPP coverage tests
- `tests/unit/conftest.py` — Shared fixtures
- `tests/unit/test_agent_workflows_runtime.py` / `tests/unit/agent_workflows/test_workflow_eval_runner.py` — Depot Agent runtime + eval harness
- `tests/unit/test_agent_qa_runtime.py`, `tests/unit/test_api_agent_actions.py`, `tests/unit/test_api_readiness.py`, `tests/unit/test_api_reports.py` — agent Q&A loop, agent-actions/autonomy, readiness tools, report schedules
- `tests/golden/agent_consumption.yaml` + `tests/golden/test_agent_golden.py` / `test_agent_sql_golden.py` — chat-agent golden gates (fast path + SQL mode); `tests/golden/workflows/` — workflow golden gate
- `tests/integration/test_charging_session_import_flow.py` — data-sources / import integration
- `tests/e2e/test_agent_search.py` (AT-18), `tests/e2e/test_alerts_pipeline_e2e.py` (AT-17); `tests/live/test_agent_sql_live.py` — nightly live-LLM shadow

`pytest` auto-discovers everything under `tests/`; the list above is just the high-traffic suites.

---

## Code Quality

### Tools
| Tool | Purpose | Config |
|---|---|---|
| `black` | Code formatting | `pyproject.toml`, line-length=100 |
| `isort` | Import sorting | `pyproject.toml`, profile=black |
| `ruff` | Linting + fast formatting | `pyproject.toml` |
| `mypy` | Type checking | `pyproject.toml`, ignore_missing_imports=true |
| `bandit` | Security scanning | `-r src/ -ll` |
| `pydocstyle` | Docstring style | `--convention=google` |

### Run checks
```bash
make lint           # ruff + black --check + isort --check + mypy
make format         # black + isort (auto-fix)
make type-check     # mypy only
pre-commit run --all-files   # All hooks
```

### Standards
- **Type hints**: Required on all functions
- **Docstrings**: Google format
- **Line length**: 100 characters max
- **Python version**: 3.12+
- **Async**: Use `async`/`await` for all I/O operations; use `asyncpg` for DB

---

## Pre-commit Hooks

Configured in `.pre-commit-config.yaml`:
1. trailing-whitespace, end-of-file-fixer, check-yaml, check-json, check-toml
2. check-added-large-files (max 1000KB), check-merge-conflict, mixed-line-ending
3. `black` (formatting)
4. `isort` (import sorting)
5. `ruff` (linting + auto-fix)
6. `mypy` (type checking, excludes tests/ and scripts/)
7. `bandit` (security, excludes tests/)
8. `pydocstyle` (Google convention, excludes tests/ scripts/ migrations/)

---

## Git Workflow

### Commit message format (enforced)
```
type(scope): description
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

**Scope examples:** `optimizer`, `ocpp`, `api`, `db`, `vdv463`, `surrogate`

**Examples:**
```
feat(optimizer): add warm-start support for MILP solver
fix(ocpp): handle duplicate BootNotification without crashing
test(api): add coverage for handoff rate limiting
```

---

## Environment Variables

### Core (required in production)
| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (TimescaleDB) |
| `JWT_SECRET_KEY` | Supabase legacy HS256 secret. Required for projects that still sign tokens with HS256 (Dashboard → Settings → API → JWT Secret). |
| `JWT_SECRET_KEY_PREVIOUS` | Optional previous HS256 secret; honoured during a rotation window so both old and new tokens verify. |
| `SUPABASE_URL` | Project URL, e.g. `https://<ref>.supabase.co`. Required when the project signs tokens with the new asymmetric JWT Signing Keys (ES256/RS256/EdDSA). The backend derives the JWKS URL `<url>/auth/v1/.well-known/jwks.json` automatically. New Supabase projects (created with CLI ≥ 2.71.1) default to ES256 — `verify_token` selects the verification path from the token's own `alg` header. |
| `SUPABASE_JWKS_URL` | Optional explicit JWKS URL override. |
| `JWT_JWKS_CACHE_LIFESPAN_S` | Optional `PyJWKClient` cache TTL (default `3600`). |
| `JWT_ISSUER` | Optional. If set, the JWT `iss` claim must match (e.g. `https://<ref>.supabase.co/auth/v1`). |
| `FAVONIUS_ADMIN_EMAIL_DOMAINS` | Optional, comma-separated. Email domains whose verified JWT subjects are auto-promoted to `favonius_admin` (default `favoniusenergy.com`). Setting this **replaces** the default — include the original entry explicitly to keep it. |
| `ENVIRONMENT` | `development` / `staging` / `production` |
| `DB_POOL_MAX_SIZE` | asyncpg pool max connections (default `25`). |
| `INTERNAL_API_TOKEN` | Shared secret authenticating `POST /internal/ocpp-event` from the WS handler. **Required in production** (startup fails closed if unset). |

### Agent (chat + workflows)
| Variable | Default | Description |
|---|---|---|
| `AGENT_SEARCH_ENABLED` | `true` | Mounts the chat agent router (`/agent/*`). |
| `AGENT_SQL_MODE_ENABLED` | `false` | Enables the general-purpose text-to-SQL path. Per-org gating is the `organizations.agent_sql_mode_enabled` DB flag (NOT an env allowlist). |
| `DEPOT_AGENT_ENABLED` | `false` | Gates the Depot Agent workflow runtime. |
| `AGENT_LLM_EFFORT` | `high` | Anthropic reasoning-effort knob for agent LLM calls (`src/api/agent/llm.py`). |

### Tenant mirroring (optional)
| Variable | Default | Description |
|---|---|---|
| `TENANT_MIRROR_TTL_S` | `300` | Seconds to cache successful mirror per `sub` (reduces static-DB UPSERTs) |

### OCPP server
| Variable | Default | Description |
|---|---|---|
| `OCPP_SERVER_ENABLED` | `false` | Enable OCPP WebSocket server |
| `OCPP_SERVER_HOST` | `0.0.0.0` | OCPP server bind address |
| `OCPP_SERVER_PORT` | `9000` | OCPP server port |
| `OCPP_USE_SAME_PORT` | `false` | Serve OCPP on same port as REST (for Railway) |
| `WEBSOCKET_PORT` | `9000` | WebSocket server port (legacy handler) |
| `MAX_CONNECTIONS` | `100` | Max concurrent charger sessions |
| `HEARTBEAT_INTERVAL` | `30` | Heartbeat interval in seconds |

### Optimization
| Variable | Default | Description |
|---|---|---|
| `OPTIMIZATION_ENABLED` | `true` | Enable optimization |
| `OPTIMIZATION_TIMEOUT` | `60` | Gurobi solve time limit (seconds) |
| `OPTIMIZATION_MIP_GAP` | `0.01` | MIP optimality gap (1%) |
| `OPTIMIZATION_HORIZON_HOURS` | `4` | Rolling horizon length |
| `OPTIMIZATION_TIMESTEP_MINUTES` | `60` | Decision interval |
| `OPTIMIZATION_SOC_MIN` | `0.2` | Minimum allowed SoC |
| `OPTIMIZATION_SOC_TARGET` | `0.8` | Target SoC before departure |
| `GUROBI_LICENSE_FILE` | — | Path to `gurobi.lic` |
| `GUROBI_LIC_CONTENT` | — | License file contents (for Railway) |
| `SOLVER_PROCESS_POOL_SIZE` | `2` | Worker processes for MILP solves. Solves run out of the API process so the asyncio loop stays responsive. |
| `SOLVER_WORKER_AS_LIMIT_MB` | `1500` | Per-worker `RLIMIT_AS` ceiling. A runaway solve crashes the worker, not the API container. |
| `SOLVER_PROCESS_POOL_DISABLED` | `false` | Set `true` to fall back to running solves in a thread inside the API process (loop will block briefly). Debug only. |

> **Two config sources:** the `OPTIMIZATION_*` variables above are read by the **legacy** `websocket_handler` config (`src/websocket_handler/config.py`). The new-architecture `DepotController` reads its own `FAVONIUS_*` knobs (`src/core/controller_config.py`) — note the defaults differ (e.g. horizon `24` vs `4`).

### Optimization control loop (new-architecture DepotController)
| Variable | Default | Description |
|---|---|---|
| `FAVONIUS_OPTIMIZATION_HORIZON_HOURS` | `24` | Rolling horizon length for the new controller. |
| `FAVONIUS_OPTIMIZATION_TIMEOUT` | `60.0` | Solve time limit (seconds). |
| `FAVONIUS_HOURLY_OPT_START` / `FAVONIUS_HOURLY_OPT_END` | `7` / `23` | Hours bounding the scheduled hourly re-optimization window. |
| `FAVONIUS_TRIGGER_COOLDOWN_MIN` | `5` | Minimum minutes between trigger-driven re-optimizations. |
| `FAVONIUS_MAX_OPT_FAILURES` | `3` | Consecutive solve failures before the controller backs off. |
| `FAVONIUS_DISPATCH_RETRIES` | `3` | OCPP dispatch retry attempts. |
| `FAVONIUS_DISPATCH_RETRY_DELAY` | `2.0` | Seconds between dispatch retries. |
| `FAVONIUS_SHUTDOWN_TIMEOUT` | `30.0` | Graceful controller-shutdown timeout (seconds). |
| `DEPOT_SOLVE_COOLDOWN_SECONDS` | `120` | Per-depot cooldown between `POST /optimize` solves (`src/api/main.py`). |
| `DEFAULT_DEPOT_ENDPOINT` | (empty) | Optional default depot endpoint hint. |

### Price feeder
| Variable | Default | Description |
|---|---|---|
| `PRICE_FEEDER_ENABLED` | `true` | Enable ENTSO-E day-ahead price ingestion |
| `PRICE_FEEDER_FETCH_INTERVAL` | `900` | Fetch interval (seconds) |
| `PRICE_FEEDER_LOOKAHEAD_HOURS` | `24` | Price horizon |
| `PRICE_FEEDER_ENTSOE_ZONES` | — | Comma-separated ENTSO-E EIC bidding-zone codes (e.g. `10YLT-1001A0008Q` for Lithuania, `10Y1001A1001A82H` for DE-LU) |
| `EUROPEAN_ELECTRICITY_API` | — | ENTSO-E Transparency Platform API security token |

### Supabase (legacy websocket_handler)
| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Public anon key |
| `SUPABASE_SERVICE_KEY` | Service role key |
| `SUPABASE_DB_HOST` | Direct DB host |

### Legacy WS handler (misc)
| Variable | Default | Description |
|---|---|---|
| `ALLOWED_FIRMWARE_HOSTS` | (empty) | Comma-separated allowlist of hosts the `UpdateFirmware` download URL may target — SSRF guard (`src/websocket_handler/diagnostics_firmware.py`). |
| `OCPP_SYNTHESIZED_DELTA_CAP_KWH` | (unset) | Caps the synthesized meter-delta when a charger reports a suspicious energy jump (`src/websocket_handler/timescale_client.py`). |
| `AUTH_SECRET_PEPPER` | (empty) | Pepper mixed into credential hashing in the WS handler. |

### Observability
| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logging level |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |

### Geo-blocking (Article 73-3 compliance)
| Variable | Default | Description |
|---|---|---|
| `GEO_BLOCK_ENABLED` | `true` | Enable geo-blocking middleware |
| `GEO_BLOCK_COUNTRIES` | `RU,CN,BY` | ISO-3166 alpha-2 country codes to block |
| `GEO_BLOCK_ALLOWLIST` | — | Comma-separated IPs/CIDRs that bypass geo-blocking |
| `GEO_BLOCK_FAIL_CLOSED` | `true` | Block requests when GeoIP resolution fails |
| `GEO_BLOCK_TRUST_PROXY_HEADERS` | `true` | When the TCP peer is private/loopback/link-local/CGNAT (RFC 6598 `100.64.0.0/10`), honour `Forwarded` / `X-Forwarded-For` / `X-Real-IP` and geo-check the real client IP. Required for Railway, Render, Fly.io, and similar PaaS providers whose edge proxy reaches the container over the CGNAT internal network. |
| `GEO_BLOCK_TRUSTED_PROXY_RANGES` | — | Additional comma-separated CIDRs whose forwarded headers should be trusted (e.g. an on-prem load balancer with a public IP). Public-internet peers are NEVER implicitly trusted, so a spoofed `X-Forwarded-For` from the open internet is ignored. |
| `GEOIP_DB_PATH` | `/app/data/GeoLite2-Country.mmdb` | MaxMind DB path |
| `MAXMIND_ACCOUNT_ID` | — | MaxMind account ID. Required since MaxMind's 2024 policy change — paired with `MAXMIND_LICENSE_KEY` in HTTP Basic Auth (account ID = username, license key = password) against `https://download.maxmind.com/geoip/databases/GeoLite2-Country/download`. Set as a **runtime** (Service) variable only. Never a Docker build arg — that would leak the credential into image history and build logs. Without it the download is skipped and the app fails closed. |
| `MAXMIND_LICENSE_KEY` | — | MaxMind license. Set as a **runtime** (Service) variable only. `src/security/geo_block.py::_download_geoip_db` downloads `GeoLite2-Country.mmdb` on startup with retries. Requires `MAXMIND_ACCOUNT_ID`; without either set, the app fails closed. |

### Charger log extraction
| Variable | Default | Description |
|---|---|---|
| `CHARGER_LOG_UPLOAD_BASE_URL` | — | Public URL chargers reach to upload diagnostic archives (the `location` in OCPP `GetDiagnostics`). Must resolve to `POST /internal/charger_logs/upload` on this API service. Without it, the trigger endpoint returns 503. |
| `CHARGER_LOG_UPLOAD_SIGNING_KEY` | — | HMAC secret for signing per-import upload tokens. Rotating invalidates every in-flight URL. |
| `CHARGER_LOG_UPLOAD_TOKEN_TTL_S` | `3600` | Upload-token lifetime, seconds. Floor of 60 s. |
| `CHARGER_LOG_UPLOAD_MAX_BYTES` | `52428800` (50 MiB) | Per-upload size cap. `MaxBodySizeMiddleware` reads this for the upload path so the global 1 MiB cap doesn't apply. |

### Kempower ChargEye onboarding
Consumed only by `scripts/onboard_depot_from_kempower.py` — the running API does not call ChargEye.
| Variable | Default | Description |
|---|---|---|
| `KEMPOWER_API_BASE_URL` | `https://api.chargeye.com` | ChargEye REST API base URL. Override for sandbox / on-prem deployments. |
| `KEMPOWER_USERNAME` | — | ChargEye account login. The CLI exchanges this + password for a JWT cached for the documented 8 h TTL. |
| `KEMPOWER_PASSWORD` | — | ChargEye account password. Runtime-only secret (never bake into a Docker build arg). If absent and a username is set, the CLI prompts on stdin. |

### Data Sources (self-serve external integrations)
Gates the `/admin/data-sources/*` router + ingestion scheduler. See the "Data Sources" architecture section.
| Variable | Default | Description |
|---|---|---|
| `DATA_SOURCES_ENABLED` | `true` | Master flag. Mounts the router and starts the scheduler + startup recovery. Set to `false` to disable entirely. |
| `DATA_SOURCES_ENCRYPTION_KEY` | — | **Required when enabled** (fails closed otherwise). Fernet key encrypting stored provider credentials. Rotation: comma-separated `version:key` pairs (newest encrypts). Runtime-only secret. |
| `DATA_SOURCES_SCHEDULER_INTERVAL_S` | `300` | Cadence for enqueuing due connection syncs. |
| `DATA_SOURCES_ORPHAN_THRESHOLD_S` | `1800` | Heartbeat age after which a pending/running job is re-kicked by the startup recovery sweep. |
| `DATA_SOURCES_MAX_BACKFILL_DAYS` | `730` | Soft cap on a first-run historical backfill window. |
| `DATA_SOURCES_SCHEDULER_BATCH` | `50` | Max due connections enqueued per scheduler tick (`src/core/data_sources/scheduler.py`). |

### Alerts pipeline (notifications)
The pipeline has shipped; the implementation in `src/api/main.py` (alert endpoints, Resend webhook), `src/websocket_handler/` (AlertDispatcher), and migration 022 is the source of truth. The PR-era design plan has been retired.

| Variable | Default | Description |
|---|---|---|
| `EMAIL_DELIVERY_ENABLED` | `true` | Master switch for the AlertDispatcher in the WS handler. False disables both LISTEN and the polling backstop. |
| `RESEND_API_KEY` | — | Resend API bearer token. Without this, the dispatcher runs with `FakeEmailClient` and logs a warning (no real emails). |
| `RESEND_FROM_ADDRESS` | `alerts@favonius.energy` | Default sender address. |
| `RESEND_WEBHOOK_SECRET` | — | HMAC secret for `POST /webhooks/resend` signature verification. Without this every webhook call returns 401. |
| `ALERT_DISPATCHER_POLL_INTERVAL_S` | `30` | Reconciliation cadence; safety net for dropped pg_notify events (decision 4.4). |
| `ALERT_NOTIFY_RESEND_INTERVAL_S` | `3600` | Minimum interval between re-notifications for a still-active alert. |
| `ALERT_DISPATCHER_BATCH_SIZE` | `50` | Maximum alerts processed per dispatcher tick. |
| `WEB_CONCURRENCY` | (unset) | The dispatcher relies on a single-worker assumption (decision 4.1). If this is set above 1, startup logs CRITICAL and double-emails are likely. |

The tables above are the high-traffic knobs; the code reads ~160 environment variables in total. **`.env.example` is the canonical exhaustive reference** (with inline comments) — consult it before adding a new variable.

---

## Dependency Injection & Lifecycle

The FastAPI app uses `@asynccontextmanager` lifespan (`src/api/main.py`):

1. Initialize `asyncpg.Pool` (DB connection pool, min=2, max=10)
2. Optionally start `OCPPServer` (background task or same-port ASGI)
3. Initialize `ControllerManager` → starts per-depot `DepotController` loops

On shutdown: stop controllers → stop OCPP server → close DB pool.

---

## Solver Configuration

**Gurobi (primary, PRD Section 8.2):**
- `TimeLimit=60` seconds
- `MIPGap=0.01` (1%)
- Requires valid license (`gurobi.lic` or `GUROBI_LIC_CONTENT`)

**HiGHS (fallback):**
- Used automatically when Gurobi is unavailable or license fails
- `solver_used` field in `OptimizationResult` records which solver ran

**Process-pool execution (`src/core/optimizer/pool.py`):**
- Each MILP solve runs in a child process via `ProcessPoolExecutor` (spawn mode), so the FastAPI event loop stays responsive during a 60 s solve. Default 2 workers (`SOLVER_PROCESS_POOL_SIZE`); per-worker virtual-memory ceiling via `RLIMIT_AS` (`SOLVER_WORKER_AS_LIMIT_MB`, default 1500 MiB).
- `SolverPool` catches `BrokenProcessPool` and parent-side `asyncio.TimeoutError`, recreates the executor, and retries once. Second failure raises `SolverError` / `SolverTimeoutError`.
- Wired in `src/api/main.py` lifespan, exposed via `core.optimizer.pool.get_solver_pool()`. `src/core/controller.py` dispatches through the pool when one is installed; falls back to `asyncio.to_thread(optimize, …)` when no pool is set (unit tests and emergency disable).
- Disable for local debugging only: `SOLVER_PROCESS_POOL_DISABLED=true`.

**Julia reference implementation:** `optimization/mip_solver.jl` (not used in production; for algorithm validation only)

---

## Key Data Flows to Remember

### Vehicle → Charger association
`OCPP Authorize(idTag)` → lookup `vehicles.id_tag` → resolve `vehicle_id` for session

### max_charge_kw discovery
`OCPP MeterValues` with `Max.Current.Offered` measurand → update `vehicles.max_charge_kw` and `telemetry.max_charge_kw`

### Grid power balance constraint
```
P_grid[t] = sum(P_vehicle[v,t] for v) + P_batt_charge[t] - P_batt_discharge_effective[t] + P_building[t]
P_grid[t] ≤ max_grid_kw  (hard constraint)
```
Building load (`building_load` table) is **mandatory** in this equation.

### Depot config caching
`_get_depot_config()` in `src/api/main.py` caches `DepotConfig` for 5 minutes (TTL=300s) to reduce DB queries. Invalidate by restarting the API or waiting for TTL expiry.

---

## Acceptance Criteria (PRD Section 11)

Before marking any feature complete, verify:

| ID | Test |
|---|---|
| AT-01 | End-to-End Optimization — schedule generated, applied to chargers |
| AT-02 | Demand Charge Reduction — 30–50% reduction demonstrated |
| AT-03 | Price Spike Re-optimization — triggered within 60s of >25% price jump |
| AT-04 | SoC Deviation Handling — triggered on >5% SoC deviation |
| AT-05 | Return Time Deviation — triggered on >15 min late return |
| AT-06 | Inter-Depot Handoff — vehicle seamlessly handed off between depots |
| AT-07 | Building Load Integration — grid power calc includes building load |
| AT-17 | Alerts Pipeline End-to-End — Faulted → trigger → dispatcher email → Resend webhook → ack via API → recovery → resolve. See `tests/e2e/test_alerts_pipeline_e2e.py`. |
| AT-18 | Agent Search End-to-End — Authenticated user submits "How much did John charge last month?" via `POST /agent/turn/stream`; agent resolves driver, computes UTC bounds, executes aggregation, writes `agent_runs` + `audit_log` rows, returns natural-language reply; cross-org user gets `not_found`. See `tests/e2e/test_agent_search.py`. |

---

## Deployment

### Railway (cloud)
Two separate services:
1. **API service** — `src/api/main.py` via uvicorn, with `OCPP_SERVER_ENABLED=false` and `OCPP_USE_SAME_PORT=false`
2. **WebSocket Handler service** — `src/websocket_handler/main.py`, with `WEBSOCKET_PORT=$PORT`

Both share the same TimescaleDB instance. See `.env.example` Railway section for required env vars.

### Deployment verification
```bash
./scripts/deploy/verify_deployment.sh
# or
make docker-verify
```

### Health endpoints
- `GET /health` — component status (DB, OCPP, Gurobi license)
- `GET /readiness` — readiness probe (legacy websocket_handler)
- `GET /liveness` — liveness probe (legacy websocket_handler)

---

## Docs Reference

| File | Contents |
|---|---|
| `docs/PRD_Depot_Agent.md` | **Authoritative product spec** (depot agent direction) |
| `docs/ARCHITECTURE.md` | System architecture |
| `docs/API.md` | API reference |
| `docs/TESTING.md` | Testing guide |
| `docs/DEPLOYMENT.md` | Deployment guide |
| `docs/RAILWAY_ENV_VARIABLES.md` | Railway env-var reference |
| `docs/SIMULATION.md` | Simulation guide |
| `docs/CONTROL_LOOP.md` | Control loop details |
| `docs/DATA_ANALYST_GUIDE.md` | Data access guide |
| `docs/PILOT_RUNBOOK.md` | OCPP pilot ops runbook |
| `docs/EVEREST_TESTING.md` | EVerest smoke test |
| `docs/COMPLIANCE_GAP_ANALYSIS.md` | Lithuanian Art. 73-3 / NIS2 / IEC 62443 gap analysis |
| `docs/SECURITY_NETWORK_ARCHITECTURE.md` | IEC 62443 security zones |
| `docs/SECURITY_DECLARATION_ESO.md` | ESO security declaration template |
| `docs/VULNERABILITY_DISCLOSURE_POLICY.md` | NIS2 Art. 21(2)(e) policy |
| `docs/WIRELESS_PROHIBITION_POLICY.md` | Art. 73-3 wireless-module policy |
| `docs/plans/ocpp_local_auth_list_roadmap.md` | OCPP local auth list staged roadmap |
| `docs/frontend/manual_charger_authorize.md` | Manual charger authorize (frontend) |
| `.cursor/rules/optimization.mdc` | MILP patterns |
| `.cursor/rules/ocpp.mdc` | OCPP patterns |
| `.cursor/rules/timescale.mdc` | TimescaleDB patterns |
| `.cursor/rules/favonius-rules.mdc` | General dev rules |
| `~/.claude/skills/gstack/` | gstack skill source (17 skills symlinked to `~/.claude/skills/`) |
| `~/.claude/skills/gstack/ETHOS.md` | Boil the Lake / builder philosophy |
