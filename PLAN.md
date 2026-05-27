# Plan — Data-search agent on top of PR #216

## Context

You want a data-search agent (Anthropic SDK tool-use loop, **not** the Agent SDK) that answers depot-operations questions over the existing structured databases. Three tools: `list_sources` (catalog), `query_static`, `query_timeseries`. Phase-1 surface is read-only; no knowledge graph, no email, no proactive triggers.

The investigation surfaced one decisive fact: **PR #216 ("feat(agent): general-purpose SQL mode for depot analytics chatbot", open and active)** already implements this with the exact tool shape, plus hardening you have not yet scoped (sqlglot AST validator, SECURITY DEFINER `agent_views.*` table-functions, role-swap executor, EXPLAIN preflight, append-only audit triggers). Your stated tools map directly:

| Your Phase-1 tool | PR #216 tool |
|---|---|
| `list_sources` | `list_tables` + `describe_table` + inline catalogue in cached system prompt |
| `query_static` | `run_select_static` (Supabase reader role) |
| `query_timeseries` | `run_select_ts` (TimescaleDB reader role) |
| (loop terminator) | `emit_final_answer` |

You picked **"build on top once #216 merges"**, surface = FastAPI HTTP endpoint, audience = depot operators (in-app chat, same auth model as today's Agent Search), eval coverage = energy+cost rollups + operations status + pricing & market context. Schedule adherence is out of scope for v1.

The plan below is the gap between what #216 ships and what a depot-operator-grade agent needs.

---

## Inventory — what already exists

### Existing agent / LLM substrate (`src/api/agent/` and `src/api/agent_workflows/`)
- **Tool-use loop**: `WorkflowAgent.run_turn` (`src/api/agent_workflows/runtime.py:227-485`) — 8-iter bounded loop, allow-list enforced before dispatch, prompt caching via `cache_control: ephemeral`, token metrics, terminator capture, HardConstraintGuard. PR #216 adds `run_qa_turn` (no Decision row, no constraint guard, on-step callback for `agent_runs.steps_json`).
- **Tool registry**: `ToolRegistry` (`src/api/agent_workflows/tools.py:60-161`) — typed `register(name, description, input_schema, fn)`, `anthropic_schemas(allowed)`, `dispatch(name, input)`.
- **Auth context**: `AuthContext` (`src/api/agent/auth_context.py:32-144`) — `user_id`, `organization_id`, `role` (favonius_admin/customer_admin/customer_operator), `visible_depot_ids`. Built from JWT once per turn.
- **Entity resolver**: `src/api/agent/resolve.py` — driver/vehicle/depot/RFID name → UUID, scoped to `visible_depot_ids`. Multi-TZ guard for time windows.
- **LLM wrapper**: `src/api/agent/llm.py` — `anthropic.AsyncAnthropic` singleton, rotation-aware API key via `get_rotation_secrets("ANTHROPIC_API_KEY")`, `claude-sonnet-4-6` default (`AGENT_LLM_MODEL`), tool-use with forced `tool_choice`, retry-on-validation-failure pattern.
- **Audit**: `agent_runs` (TimescaleDB, migration 025) + `audit_log` action `agent.query` (migration 021). Writers in `src/api/agent/audit.py`.
- **HTTP surface**: `POST /agent/turn` (sync) + `POST /agent/turn/stream` (SSE) + `GET /agent/runs/{id}` (`src/api/agent/router.py`). Rate limit 10/min via `check_agent_limit` bucket.
- **Feature flags**: `AGENT_SEARCH_ENABLED` (default `true`), `AGENT_SQL_MODE_ENABLED` (default `false` — gated by PR #216). Per-org gating is the `organizations.agent_sql_mode_enabled` DB flag — the originally-planned `AGENT_SQL_ORG_ALLOWLIST` env allowlist was removed.
- **Eval harness**: `tests/golden/workflows/_schema.yaml` + `tests/golden/workflows/test_workflow_golden.py` + `FakeAnthropicClient` (deterministic LLM-trace replay against real TimescaleDB savepoint).
- **CI gate**: `.github/workflows/workflow-golden.yml` — spins up `timescale/timescaledb:latest-pg16`, applies migrations, runs `tests/golden/workflows/` + unit coverage floor (90% on `src/api/agent_workflows/eval`).

### Database surface
- **Two pools**: `db_pools.static` (Supabase, `DATABASE_URL`) + `db_pools.ts` (TimescaleDB, `TIMESCALE_SERVICE_URL`). Both `asyncpg.Pool`. Created in `src/api/main.py:329-330`.
- **9 hypertables**: `telemetry`, `prices`, `weather_forecasts`, `building_load`, `electricity_prices`, `vdv463_charging_requests`, `telemetry_samples`, `security_audit_log`, `decisions`.
- **Static (Supabase)**: `sites` (depots), `charging_stations` (chargers), `vehicles`, `drivers`, `rfid_cards`, `schedules`, `organizations`, `user_organizations`, `charger_vehicle_access`, `battery_storage`, `station_credentials`.
- **TS operational**: `charging_sessions` (rich, billing-grade), `optimization_runs`, `charging_commands`, `connector_status`, `notification_alerts`, `agent_runs`, `audit_log`, `trigger_log`, `interdepot_messages`, `workflows`, `workflow_tiers`.
- **No standalone catalog module today** — closest is `src/api/agent/schema_graph.yaml` (6 tables, baked into one intent's extraction prompt).
- **No RAG / embeddings / vector store** anywhere — confirmed.

### What PR #216 brings to main
- `migrations/042_agent_views_ts.sql` — TimescaleDB views (`agent_views.sessions`, `telemetry_hourly`, `optimization_runs`, `alerts`, `prices_hourly`, `building_load_hourly`, `connector_status_latest`) + `agent_reader_ts` role + append-only triggers on `agent_runs`.
- `migrations/supabase/040_agent_views_static.sql` — Supabase views (`agent_views.depots`, `vehicles`, `chargers`, `drivers`, `schedules_recent`) + `agent_reader_static` role.
- `src/api/agent/sql_validator.py` (sqlglot AST, ~1.2k lines), `sql_executor.py` (role-swap + `current_user` assertion, statement_timeout, EXPLAIN preflight, per-cell byte cap), `sql_tools.py` (8-tool registry closed over `AuthContext`), `catalogue.py`, `prompts.py`, `planner.py`, `controller.py` extension.
- `src/api/agent_workflows/runtime.py::run_qa_turn` — Q&A variant that writes `agent_runs.steps_json` and skips Decision-row + constraint-guard.
- `pyproject.toml`: `sqlglot>=23.0.0`.
- New metrics: `favonius_agent_sql_*`.

### Tooling
- Python ≥3.12 (`pyproject.toml:10`), `pip install -e ".[dev]"` (`Makefile:26-27`).
- `anthropic>=0.40.0` (locked at 0.97.0 in `uv.lock`).
- `pydantic>=2.5.0` (v2 only), `asyncpg>=0.29.0`, `sqlalchemy>=2.0.0` (decorative — backend uses raw SQL).
- pytest with `asyncio_mode = auto`; markers include `acceptance`, `golden`, `workflow_golden`.
- Pre-commit: black/isort/ruff/mypy (non-strict)/bandit/pydocstyle (Google).

---

## Gaps after PR #216 lands

What `#216` explicitly defers as follow-ups (per its own description):
1. **Golden YAML suite for the SQL path** (`tests/golden/agent_sql.yaml`, 15–20 Q&A pairs). Today only the validator/planner/runtime have unit coverage.
2. **Integration test against a real DB pair** using the savepoint pattern from `tests/golden/workflows/conftest.py`.
3. **e2e extension to AT-18** (`tests/e2e/test_agent_search.py`) covering an off-script question.
4. **Per-org monthly token-budget ceiling** (the "P4" from #216's pre-build review).
5. **Two-model split**: Haiku explorer + Sonnet final formatter, drop-in once goldens settle.

What's missing for your stated v1 eval coverage:
- **Operations-status coverage in the catalogue**: `#216`'s views include `optimization_runs`, `alerts`, `connector_status_latest`. Good for the user-selected ops category. `trigger_log` is **not** in `agent_views.*` yet — needed for "when did price spikes trigger reoptimization?" (overlaps with `optimization_runs.trigger_reason`, so possibly redundant — see Open Question 2).
- **Pricing-context coverage**: `prices_hourly` (TS view) + `electricity_prices` underlying. Good.
- **Planner anti-pattern coverage**: `#216`'s `_CONSUMPTION_ANTIPATTERNS` already includes `\bfault`, `\bopt(imization|imisation) (run|trigger)`. Ops-status questions correctly fall through to SQL mode. Pricing questions also fall through (no fast path for them).
- **Operator-grade answers**: `#216`'s system prompt already says "Be honest about empty results" and "Refuse out-of-scope requests" — good. Audience-specific tone tweaks (e.g. don't leak SQL in the final answer, surface UUIDs only as names) need verification in the eval suite.

---

## Sequenced sessions

Each session ≤ ~2 hours. Numbered S0 onward. #216 has merged — S0 can start. Each session runs in its own Claude Code conversation; reference PLAN.md by path, do not paste it into the prompt.

### S0 — Pre-flight: rebase on #216, smoke-test SQL mode
- **Goal**: confirm `#216` is the substrate the rest of the sessions build on, and that a live question round-trips end-to-end.
- **Files touched**: none (read + run only). Set `AGENT_SQL_MODE_ENABLED=true` in a local `.env`.
- **Steps**:
  1. Verify `#216` has merged to `main`; rebase the working branch.
  2. Apply `migrations/042_agent_views_ts.sql` + `migrations/supabase/040_agent_views_static.sql` against the dev DB pair.
  3. Confirm `agent_reader_ts` and `agent_reader_static` roles exist and have only the expected grants (no `pg_*`, no `auth`, no `storage`).
  4. Hit `POST /agent/turn` (or `/stream`) with: *"How many charging sessions did we have last week at depot X?"* Verify the trace shows `planner→list_tables(maybe)→run_select_ts→emit_final_answer` and the answer matches a hand-run query.
- **Deliverable**: a short markdown note under `docs/agent_sql_smoke.md` capturing the trace and DB-state assertions.
- **Done when**: one operator-style question returns a correct, plain-English answer via `/agent/turn/stream`, and the `agent_runs` row records the trace.

### S1 — Catalogue + planner polish for the three eval categories
- **Goal**: make sure `list_tables`/`describe_table` cover everything the 20-query eval needs, and that the planner routes those questions correctly.
- **Files touched**:
  - `src/api/agent/catalogue.py` — add/tighten column notes for `optimization_runs` (trigger_reason vocab), `alerts` (severity vocab), `prices_hourly` (currency/units), `connector_status_latest` (status enum). Verify `purpose` strings answer "what would I aggregate from this?".
  - `src/api/agent/planner.py` — add anti-patterns if any eval question wrongly hits the consumption fast path (e.g. *"how much energy did vehicle X consume last month"* should still hit the fast path; *"top 5 vehicles by consumption"* should NOT). Tighten the existing `_CONSUMPTION_TRIGGERS` against eval scenarios.
  - `tests/unit/agent/test_catalogue.py` and `tests/unit/agent/test_planner.py` — extend.
- **Deliverable**: unit-test green on the planner classification for every question in the new eval suite.
- **Done when**: every eval question routes to the correct path (fast path vs `sql_general`) according to its expected category.

### S2 — Golden eval suite: 20 real questions
- **Goal**: ship `tests/golden/agent_sql.yaml` covering the three user-selected categories (7 energy+cost / 7 ops status / 6 pricing+market) plus the harness that runs them.
- **Files touched**:
  - `tests/golden/agent_sql.yaml` — 20 scenarios.
  - `tests/golden/agent_sql/_schema.yaml` — JSON-Schema-style reference (copy + adapt `tests/golden/workflows/_schema.yaml`).
  - `tests/golden/test_agent_sql_golden.py` — parametrised harness that replays `llm_trace` via `FakeAnthropicClient` against a transactional savepoint on a real TimescaleDB container, asserting on the `final_answer` payload and the SQL targets (which `agent_views.*` function was called).
  - `tests/golden/conftest.py` — register `agent_sql_golden` marker.
  - `.github/workflows/workflow-golden.yml` — extend to run the new harness (or add a sibling workflow file).
- **Deliverable**: 20/20 scenarios green in CI; a 95% pass-rate gate matches the existing 50-question `agent_consumption.yaml` threshold.
- **Done when**: `pytest -m agent_sql_golden` is green locally AND in CI on a fresh checkout.

### S3 — Real-DB integration + AT-18 e2e extension
- **Goal**: close two of `#216`'s explicit follow-ups in one session.
- **Files touched**:
  - `tests/integration/agent_sql/test_sql_mode_real_db.py` — savepoint-style integration test using the pattern from `tests/golden/workflows/conftest.py`. One question per category. Real DB, real validator, real executor.
  - `tests/e2e/test_agent_search.py` — add an off-script SQL-path question to AT-18 (one happy path, one cross-org isolation assertion: identical question from a different org returns either a scoped answer or `not_found`).
- **Deliverable**: green integration + e2e on TimescaleDB + Supabase pair.
- **Done when**: AT-18 includes one SQL-mode happy path + one cross-org scoping case, and the integration test exercises each of the three categories at least once.

### S3.5 — Failure taxonomy + nightly shadow suite
- **Goal**: make turn failures countable and catch model/prompt drift the canned-trace suite can't see.
- **Files touched**:
  - `migrations/045_agent_failure_reason.sql` — add `agent_runs.failure_reason text` (nullable) and a CHECK against an enum-as-text set: `validator_rejected | executor_timeout | empty_result | budget_exceeded | tool_error | llm_error | other`. Index on `(organization_id, started_at, failure_reason)`. _(Shipped as 045, not the planned 043.)_
  - `src/api/agent/audit.py` — set `failure_reason` at the same write site as `status`. One mapping function, not scattered string literals.
  - `src/api/agent/controller.py` + `runtime.py::run_qa_turn` — propagate the reason from the existing exception paths.
  - `tests/golden/agent_sql_live.yaml` — 5 questions copied verbatim from the 20-question suite, one per row, no `llm_trace`, no `expected.agent_views_used`. Keep `expected.final_answer_must_include` and `must_not_include`.
  - `tests/live/test_agent_sql_live.py` — calls the real `/agent/turn` against real Sonnet, real DB savepoint. Reads `ANTHROPIC_API_KEY` from env, skips if absent (so local pytest doesn't burn tokens).
  - `.github/workflows/agent-sql-shadow.yml` — nightly cron (`0 6 * * *` UTC), runs `tests/live/`, posts a one-line summary to Slack via existing webhook. Does not gate PRs. Failure threshold: <4/5 green for 2 nights in a row → page.
- **Deliverable**: `failure_reason` column populated for every non-success row going forward; nightly shadow run visible in Slack.
- **Done when**: a deliberate validator rejection writes `failure_reason='validator_rejected'`, and one nightly shadow run completes green end-to-end.

### S4 — Per-org monthly token-budget ceiling
- **Goal**: prevent a runaway org from burning the Anthropic bill.
- **Files touched**:
  - `migrations/046_agent_token_budget.sql` — small table `agent_token_usage(organization_id uuid, period_yyyymm text, input_tokens bigint, output_tokens bigint, last_updated timestamptz, PRIMARY KEY(organization_id, period_yyyymm))`. Append-style upsert. _(Shipped as 046, not the planned 043 — parallel PRs took the lower numbers.)_
  - `src/api/agent/budget.py` — `check_and_reserve(org, est_tokens) -> Reservation`, `record_actual(reservation, actual_tokens)`. In-process counter with periodic flush; on cold-start, hydrate from the table.
  - `src/api/agent/controller.py` — call `check_and_reserve` before `run_qa_turn`, `record_actual` after.
  - Budget config (**as shipped — no env var**): the platform default is the hard-coded constant `DEFAULT_TOKEN_BUDGET_MONTHLY = 10_000_000` (`src/api/agent/budget.py`); the per-org override is the first-class `organizations.agent_token_budget_monthly` column (`migrations/supabase/045_organizations_agent_token_budget.sql`), read fail-open via `to_jsonb(o)->>'agent_token_budget_monthly'`. The originally-planned `AGENT_SQL_TOKEN_BUDGET_PER_ORG_MONTHLY` env var was intentionally dropped — the budget is a per-company commercial attribute, so it lives in the DB (a dedicated column, not a `metadata` JSONB key), not an env knob.
  - `tests/unit/agent/test_budget.py` — accept/reject/edge cases.
- **Deliverable**: a synthetic test that fakes a high-token response exceeds the ceiling and the next turn returns a friendly refusal (`{"status": "refused", "reason": "monthly_budget_exceeded"}`) via `emit_final_answer`.
- **Done when**: the refusal path is observable in `agent_runs.status` and the Prometheus metric `favonius_agent_sql_budget_refused_total` increments.

### S5a — Dashboard + CLAUDE.md refresh
- **Goal**: ship operator-facing observability and update docs. Ships in ~1 hour, zero runtime risk.
- **Files touched**:
  - `monitoring/grafana/agent_sql_dashboard.json` — turn duration histogram, validation failures by `error_kind`, role-swap failures, failures by `failure_reason` (the S3.5 column), tokens by model, refusals by reason, shadow-suite pass rate.
  - `CLAUDE.md` — point "Depot Chat Agent" sub-section at `agent_sql.yaml` as the v1 SQL-mode gate; document the budget knob and the `failure_reason` taxonomy.
- **Done when**: dashboard renders against real metrics; CLAUDE.md mentions every env var introduced in S0–S4.

### S5b — Two-model split (Haiku explorer + Sonnet formatter)
- **Gating rule**: don't start unless either (a) `agent_token_usage` shows ≥1 org at >50% of monthly ceiling, or (b) one week of telemetry shows median turn cost ≥ a threshold you set when reviewing S5a's dashboard. Otherwise defer.
- **Files touched**:
  - `src/api/agent/llm_router.py` (new) — route to `claude-haiku-4-5` while no `run_select_*` tool result is in history; switch to `claude-sonnet-4-6` for the turn that calls `emit_final_answer`. Behind feature flag `AGENT_SQL_TWO_MODEL_ENABLED` (default `false`).
  - `src/api/agent/llm.py` — delegate model choice to `llm_router` when the flag is on.
  - `tests/golden/test_agent_sql_golden.py` — parametrize over `[single_model, two_model]`; both must hit ≥19/20.
- **Deliverable**: side-by-side golden run shows two-model passes ≥19/20 with measurably fewer tokens. Roll out behind the flag, one org at a time.
- **Done when**: flag enabled in production for one pilot org, one week clean, before broadening.

---

## Eval spec — 20 real questions, where they come from, grading format

Source: derived from PRD §6 use cases (daily readiness, charger fault triage, monthly consumption), the existing `agent_consumption.yaml` 50-question pattern, and the three categories you selected.

**Format** (mirrors `tests/golden/workflows/_schema.yaml`):
```yaml
id: "en_01"
category: "energy_cost"        # energy_cost | ops_status | pricing_market
question: "How much energy did vehicle bus_101 consume last month?"
graph_snapshot:                 # fixtures the savepoint executor loads
  sites: [...]
  vehicles: [...]
  charging_sessions: [...]
  electricity_prices: [...]
llm_trace:                      # canned Anthropic tool_use sequence
  - tool_use:
      name: run_select_ts
      input:
        sql: "SELECT SUM(energy_kwh) FROM agent_views.sessions($1) WHERE vehicle_id = '...' AND start_time >= ..."
  - tool_use:
      name: emit_final_answer
      input:
        text: "Bus_101 consumed 432.5 kWh in April 2026 across 18 charging sessions."
expected:
  status: success
  agent_views_used: ["sessions"]
  sql_validator_rejected: false
  final_answer_must_include: ["432.5 kWh", "18 sessions"]   # substring match
  final_answer_must_not_include: ["SELECT", "UUID", "agent_views"]   # tone gate
```

**Grading**:
- Per-question pass/fail on (a) the right `agent_views.*` function(s) were referenced, (b) no validator rejection, (c) `final_answer` includes/excludes the required substrings, (d) `agent_runs.status='success'`.
- Aggregate gate: 19/20 (95%) green to pass CI for any PR touching `src/api/agent/`.
- Diagnostic mode (`pytest -m agent_sql_golden -v --diag`) prints the LLM trace + executed SQL for failed cases.

### The 20 questions

**Energy + cost rollups (7)**
1. *"How much energy did vehicle bus_101 consume last month?"* → `agent_views.sessions`, SUM(energy_kwh), single-vehicle filter.
2. *"What was the total electricity cost at depot Vilnius last week?"* → resolve depot by name, SUM(cost_total), exclude `cost_total_source='unpriceable'`.
3. *"Which depot had the highest energy consumption in April 2026?"* → GROUP BY depot_id, ORDER BY SUM DESC LIMIT 1, cross-pool join to `agent_views.depots` for the name.
4. *"How many kWh did driver John Smith use this month?"* → `lookup_entity` for driver, SUM(energy_kwh) WHERE driver_id.
5. *"Show me the top 5 vehicles by total cost in the last 30 days."* → GROUP BY vehicle_id, two-pool join for vehicle names.
6. *"What's the average energy per charging session at depot Vilnius this month?"* → AVG(energy_kwh) with depot scope.
7. *"Did any session this week cost more than €100?"* → WHERE cost_total > 100, returns boolean-ish answer.

**Operations status (7)**
8. *"Which chargers were faulted yesterday?"* → `agent_views.alerts` WHERE alert_type='charger_fault' AND first_occurrence_at::date = current_date - 1.
9. *"How many optimization runs went infeasible last week?"* → `agent_views.optimization_runs` WHERE status='infeasible'.
10. *"What triggered the last 5 reoptimizations at depot Vilnius?"* → SELECT trigger_reason, run_time ORDER BY run_time DESC LIMIT 5.
11. *"Which alerts are still active right now?"* → WHERE status='active'.
12. *"Show me chargers that have been unavailable for more than 24 hours."* → `connector_status_latest` WHERE status='Unavailable' AND timestamp < now() - interval '24 hours'.
13. *"How many charging sessions did we have yesterday, total and per depot?"* → COUNT(*) and GROUP BY depot_id with cross-pool depot-name resolution.
14. *"Are any depots running in degraded optimization mode?"* → latest `optimization_runs` per depot WHERE status='degraded'.

**Pricing & market context (6)**
15. *"What were the 5 highest electricity prices last week?"* → `prices_hourly` ORDER BY lmp_price DESC LIMIT 5.
16. *"How many times did a price spike trigger reoptimization last month?"* → `optimization_runs` WHERE trigger_reason='price_spike'.
17. *"What was the average price during morning peak (07–09 local) last week?"* → AVG with TZ-aware time filter.
18. *"Compare today's day-ahead prices vs last Friday's at depot Vilnius."* → two windowed selects, agent narrates the diff.
19. *"Did the electricity price ever go negative in April?"* → WHERE lmp_price < 0.
20. *"When was the most expensive hour in the past 7 days?"* → ORDER BY lmp_price DESC LIMIT 1, render the local datetime.

Source-of-truth references for the schemas these queries hit: `migrations/001_initial_schema.sql`, `migrations/022_alerts_pipeline.sql`, `migrations/034_create_electricity_prices.sql`, `migrations/042_agent_views_ts.sql` (PR #216), `migrations/supabase/040_agent_views_static.sql` (PR #216), `CLAUDE.md` §"Database Schema".

---

## Eval evolution recipe

When an operator-reported turn fails in production:

1. Pull the `agent_runs` row by id. Copy the original question, `failure_reason`, and `steps_json`.
2. If the bug is in the SQL/validator/executor → add a row to `tests/golden/agent_sql.yaml` with a hand-written `llm_trace` reproducing the failure mode. This row should fail before the fix, pass after.
3. If the bug is in tool selection or prompt → add a row to `tests/golden/agent_sql_live.yaml` (the shadow suite). Canned traces can't catch this class.
4. Cap the shadow suite at ~15 questions long-term. Rotate older rows out as patterns become well-tested.

Treat the eval suite as a living artifact, not a one-time deliverable. Every fixed bug leaves a row behind.

---

## Verification

After all sessions complete:

1. **Local smoke** — set `AGENT_SQL_MODE_ENABLED=true`, `AGENT_LLM_MODEL=claude-sonnet-4-6`. Start the API with `uvicorn src.api.main:app --reload`. Hit `/agent/turn/stream` with five hand-picked questions, one per (category × auth role) cell. Verify SSE step events and final natural-language answer.
2. **Golden gate** — `pytest -m agent_sql_golden` (≥19/20).
3. **AT-18 e2e** — `pytest tests/e2e/test_agent_search.py -m acceptance`.
4. **Cross-org isolation** — manual: mint a JWT for org A and one for org B, ask the same depot-scoped question, confirm B sees `not_found` or empty.
5. **Budget refusal** — set `organizations.agent_token_budget_monthly = 1000` for the test org (see `tests/integration/agent_sql/test_budget_refusal_real_db.py`), run any non-trivial question, expect a refusal.
6. **CI** — extended `workflow-golden.yml` gates the new harness; `make lint` clean.

---

## Open questions

1. **Trigger-log granularity** — decided: skip the `agent_views.trigger_log` view. If Q16 fails on `optimization_runs.trigger_reason`, that's an eval signal, not a missing-view signal.
2. **Per-org budget storage** — **resolved (as built)**: no env var. The platform default is the hard-coded constant `DEFAULT_TOKEN_BUDGET_MONTHLY = 10_000_000` and the per-org override is a dedicated first-class `organizations.agent_token_budget_monthly` column (`migrations/supabase/045`), not an `organizations.metadata` JSONB key.
3. **Two-model split priority** — Haiku-then-Sonnet (S5) is a cost optimisation, not a correctness one. If the golden suite already runs comfortably under budget on Sonnet-only, S5 can defer. Recommendation: gate S5 on observed token-cost telemetry from a one-week shadow run.
4. **PR #216 review feedback** — if `#216` lands with material design changes (e.g. the planner gets replaced with a Haiku classifier), some of this plan needs to rebase. Recommendation: re-read this plan against the merged commit before starting S1.
5. **Schedule-adherence coverage** — you excluded it from v1. The catalogue can still expose `agent_views.schedules_recent`; the gap is only the eval suite. Confirm we leave that gap open for v2.
6. **Failure-reason vocabulary** — the seven categories in S3.5 are a guess. Revisit after one month of production data; merge or split categories based on what's actually populated.
