# Agent Search — Sprint Prompts

This document holds the verbatim prompts for each coding sprint of the Depot
Chat Agent. Feed each section to a fresh Claude Code session when that
sprint comes up. Sprints are independent enough that the agent does not need
this conversation's history — every prompt is self-contained.

**Companion documents (every prompt references these):**

- `docs/plans/agent_search_prd_v1.md` — the product spec
- `docs/plans/agent_search_architecture_v0.md` — the technical design

---

## Conventions (every sprint follows these)

### Branches and PRs

- Each sprint runs on its own branch off `main`, named per the §13 Status
  table in the PRD (e.g. `agent-search/sprint-b1-foundations`).
- PRs are opened **as drafts**, titled `<type>(agent): <sprint slug> — <one-line summary>`,
  with a body that links back to both companion docs and this prompts file.
- Commit message format: `<type>(agent): <one-line summary>` per the repo's
  `type(scope): description` convention. Use `feat`, `fix`, `test`, `docs`,
  or `chore` as appropriate.
- Do not merge until tests pass, lint passes, coverage on new files is
  ≥ 90%, and the PRD §13 status row is updated.

### PRD update protocol

Before opening the PR, edit `docs/plans/agent_search_prd_v1.md` §13
Implementation Status:

1. Move the sprint's row to `in PR` and fill in the PR URL.
2. If you discovered any clash between the architecture doc and reality,
   add a one-line note in the row's Notes column linking to the section you
   updated in the architecture doc.
3. Update the "Last updated" line at the top of §13.

When the PR merges, the merger updates the row to `merged` with the date.

### Architecture doc update protocol

If implementation reveals that the architecture doc is wrong (a column is
named differently than expected, an existing helper has different
semantics, etc.), **update the architecture doc as part of the same PR**.
Don't carry the contradiction. Call out the change in the PR body under a
heading "Architecture doc deltas".

### Test and quality bar

- Coverage target: ≥ 90% on every new file under `src/api/agent/`.
- All tests must pass: `pytest tests/unit/agent/ tests/integration/agent/`.
- Lint must pass: `make lint`.
- No `# type: ignore` or `# noqa` without a one-line justification comment.

### Out-of-scope guard

Each sprint has an explicit "out of scope" list. Do not exceed it. If a
genuine blocker forces you to touch out-of-scope code, stop, post a comment
on the PR, and wait for direction. Don't ship "while we were at it"
changes — those belong in their own sprint.

---

## Sprint dependency graph

```
              B1 (foundations)
             /  |  \
            B2  B3  B4   (parallel after B1 merges)
             \  |  /
              \ | /
                B5 (router + SSE + integration test)
                |
                B6 (launch: golden tests, metrics, docs)

  F1 (wire existing chat shell to /agent endpoints)
  F1 ──> F2 (reasoning panel, CSV, feedback)
  F1 needs B5 merged on the backend
  F2 needs F1 + B6 (final API contracts)
```

The frontend chat shell already exists. Frontend sprints are integration,
not green-field UI. F1 wires the existing shell to the new endpoints; F2
layers transparency features on top.

The LLM is selected by env var (`AGENT_LLM_MODEL`, default
`claude-sonnet-4-6`). Sprint B4 builds the abstraction so swapping
Sonnet/Haiku/Opus is a redeploy of env, not a code change.

---

# Backend Sprints

---

## Sprint B1 — Foundations: migrations + plan types + auth context

**Branch:** `agent-search/sprint-b1-foundations`
**Base:** `main` (rebased after `claude/natural-language-search-chat-aAckE` merges)
**Depends on:** architecture doc + PRD merged
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_prd_v1.md` (full)
2. `docs/plans/agent_search_architecture_v0.md` §§ 1–5
3. `src/security/auth.py:204` (`verify_token`) and surrounding helpers
4. `src/db/queries.py` — find `get_depots_for_organization` and `get_all_depots`
5. `migrations/` — most recent files for style; note `024` is duplicated

### Goal
Land the database changes and the typed primitives that the rest of the
agent depends on. Nothing here calls an LLM, runs a query, or returns a
response.

### What to build

1. **Migration `migrations/supabase/009_drivers_category.sql`** — adds a
   nullable `category TEXT` column to `drivers` with
   `CHECK (category IS NULL OR category IN ('delivery', 'employee'))`.
   Include the `COMMENT ON COLUMN` text from architecture doc §5.3.1
   verbatim.
2. **Migration `migrations/025_agent_runs.sql`** — creates `agent_runs` per
   architecture doc §5.3.2, including both indexes.
3. **Migration `migrations/026_charging_sessions_agent_indexes.sql`** —
   creates `idx_sessions_driver_time` and `idx_sessions_card_time` per
   architecture doc §5.3.3. Use `CREATE INDEX IF NOT EXISTS` (not
   CONCURRENTLY) for first deploy. Add a one-line comment explaining the
   CONCURRENTLY swap is a future cleanup.
4. **`src/api/agent/__init__.py`** — empty module with a docstring naming
   the agent submodules.
5. **`src/api/agent/plan.py`** — Pydantic types from architecture doc §3.1.
   Strict: `model_config = ConfigDict(extra="forbid")` so unexpected fields
   from the LLM fail validation. Use `Literal` for every enumerated field.
6. **`src/api/agent/auth_context.py`** — `AuthContext` BaseModel and
   `build_auth_context(token_payload: dict, static_pool) -> AuthContext`
   per architecture doc §3.3. Re-use existing helpers
   (`get_user_organization_id`, `get_user_favonius_role`) from
   `src/security/auth.py`. For `favonius_admin`, call
   `get_all_depots(static_pool)`; otherwise call
   `get_depots_for_organization(static_pool, org_id)`. Raise `HTTPException(403)`
   if a non-admin caller has no `organization_id`.

### Tests

- **`tests/unit/agent/__init__.py`** (empty)
- **`tests/unit/agent/conftest.py`** — fixtures: a fake static_pool with
  `fetch` returning configurable rows; sample JWT payloads for each role.
- **`tests/unit/agent/test_plan_validation.py`**:
  - `QueryPlan` accepts a minimal valid payload (intent, subjects, time_window).
  - Rejects unknown intents.
  - Rejects extra top-level fields.
  - `EntityMention` rejects unknown `kind`.
  - `TimeWindow(kind="relative")` requires `relative` and rejects when both
    `from_iso` and `to_iso` are also set.
  - `TimeWindow(kind="absolute")` requires `from_iso` and `to_iso`.
- **`tests/unit/agent/test_auth_context.py`**:
  - `favonius_admin` payload → all depots returned.
  - `customer_admin` with org_id → only that org's depots.
  - `customer_operator` with org_id → only that org's depots.
  - Missing `app_metadata.organization_id` for non-admin → 403.
  - Empty depot list for non-admin → still returns successfully with empty
    `visible_depot_ids` (the resolver will produce "not found" downstream).

### Acceptance criteria
- [ ] All three migrations apply cleanly to a freshly-initialized DB
      (run `python scripts/run_migrations.py` against a local Tiger and
      `supabase db reset` against a local Supabase, or document why not).
- [ ] `pytest tests/unit/agent/` passes.
- [ ] `make lint` passes.
- [ ] Coverage on `src/api/agent/plan.py` and
      `src/api/agent/auth_context.py` ≥ 95%.
- [ ] PRD §13 row B1 updated to `in PR` with the PR URL.

### Out of scope
- LLM client, resolvers, compiler, router, SSE, golden tests, metrics.
- Modifying `src/api/main.py`. The agent module is not yet mounted.
- Changing existing migrations or fixing the `024` duplicate (separate PR).

---

## Sprint B2 — Entity resolution + time window

**Branch:** `agent-search/sprint-b2-resolution`
**Base:** `main` (after B1 merges)
**Depends on:** B1 merged
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_prd_v1.md` §§ 4 (user stories), 5
2. `docs/plans/agent_search_architecture_v0.md` §§ 3.4, 3.5
3. The just-merged `src/api/agent/plan.py` and
   `src/api/agent/auth_context.py`
4. Live Supabase schema: `drivers` columns are `id, site_id,
   external_driver_id, display_name, email, phone, status` — there is no
   `organization_id` column on `drivers`. Org scoping JOINs through `sites`.

### Goal
Turn `EntityMention` and `TimeWindow` from the LLM-produced `QueryPlan`
into `ResolvedEntity` and `ResolvedTimeWindow` objects with real UUIDs and
real UTC bounds. This is the single layer where names become IDs.

### What to build

1. **`src/api/agent/resolve.py`** containing:
   - `ResolvedEntity` and `ResolvedTimeWindow` Pydantic models per
     architecture doc §3.2.
   - `async def resolve_entities(mentions, auth, static_pool) -> list[ResolvedEntity]` —
     dispatches by `kind` to per-kind resolvers.
   - `_resolve_driver(text, auth, static_pool)` per architecture doc §3.4,
     including the JOIN through `sites`, the `card_ids` aggregation, and
     `external_driver_id` ILIKE matching.
   - `_resolve_vehicle(text, auth, static_pool)` — analogous, but
     `vehicles` does have `organization_id` natively, so use it directly.
     Match on `display_name`, `vin`, `external_id`, `license_plate`.
   - `_resolve_depot(text, auth, static_pool)` — query `sites` for
     `name ILIKE` and filter by `id = ANY(visible_depot_ids)`.
   - `_resolve_rfid(text, auth, static_pool)` — query `rfid_cards` JOIN
     `sites` for `id_tag` exact match or `label ILIKE`.
   - `def resolve_time_window(window, visible_depot_ids, depot_timezones) -> ResolvedTimeWindow` —
     per architecture doc §3.5, with explicit handling of every `relative`
     literal value and DST boundaries.
   - `async def load_depot_timezones(static_pool, visible_depot_ids) -> dict[UUID, str]` —
     cached helper that pulls `id, timezone` from `sites` for the given
     depot IDs.

### Tests

- **`tests/unit/agent/test_resolve_entities.py`**:
  - Driver resolver: single match, multi-match (returns first with
    `candidates` populated), zero match returns `primary_id=None`.
  - Org scoping: a driver in another org's depot is invisible (mocked
    static_pool returns no rows).
  - `card_ids` populated when assignments exist, empty list otherwise.
  - `external_driver_id` matches: `EMP-1042` finds the driver named
    "John Smith" with that external ID.
  - Vehicle, depot, RFID resolvers each have at least one happy-path and
    one empty-result test.
- **`tests/unit/agent/test_resolve_time_window.py`**:
  - Each `relative` literal returns the right UTC range for a fixed
    "now" (use `freezegun` or pass `now` as a parameter to the function).
  - DST boundary: a "last_week" call spanning the spring-forward weekend in
    Europe/Vilnius produces 7×24h - 1h.
  - Multi-TZ: pass two depot timezones, assert the function raises or
    returns a per-TZ dict (decide and document the contract).
  - Absolute window: `from_iso=2026-03-01, to_iso=2026-04-01` →
    `[2026-03-01T00:00 TZ, 2026-04-01T00:00 TZ)` in the depot's TZ.

### Acceptance criteria
- [ ] All resolver SQL has been verified against the live Supabase schema
      (use the Supabase MCP if available; otherwise run a dry SELECT against
      a local copy).
- [ ] `pytest tests/unit/agent/` passes; coverage on `resolve.py` ≥ 90%.
- [ ] `make lint` passes.
- [ ] PRD §13 row B2 updated to `in PR`.
- [ ] If the resolver SQL diverged from architecture doc §3.4, the
      architecture doc is updated in the same PR with a "Architecture doc
      deltas" section in the PR body.

### Out of scope
- LLM extraction or formatting. The `mentions` arrive as already-typed
  `EntityMention` lists.
- The compiler — that's B3.
- Endpoint wiring.

---

## Sprint B3 — Intent compiler + audit writer

**Branch:** `agent-search/sprint-b3-compiler`
**Base:** `main` (after B1 merges; can run parallel with B2 and B4)
**Depends on:** B1 merged
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_architecture_v0.md` §§ 3.6, 5, 8
2. `src/security/admin_audit.py` — the existing audit pattern
3. `src/db/models.py:379` — `optimization_runs` (the shape `agent_runs`
   mirrors)

### Goal
Convert a `QueryPlan` + resolved entities + resolved time window into
parameterized SQL for the `consumption_by_user` intent. Persist the run
trace to `agent_runs` and mirror each executed query into `audit_log`.

### What to build

1. **`src/api/agent/intents/__init__.py`** (empty).
2. **`src/api/agent/intents/base.py`** — `IntentCompiler` Protocol with a
   single method:
   ```python
   def compile(plan: QueryPlan, resolved: list[ResolvedEntity],
               window: ResolvedTimeWindow) -> tuple[str, list]
   ```
3. **`src/api/agent/intents/consumption_by_user.py`** — the compiler from
   architecture doc §3.6, including the `driver_id = ANY(...) OR
   card_id = ANY(...)` branch and the `NULLS LAST` ordering. Raise a
   `ValueError` with a clear message when no driver subjects resolved.
4. **`src/api/agent/audit.py`**:
   - `async def agent_runs_open(ts_pool, auth, message) -> UUID` — inserts
     a row with `status='success'` placeholder and returns the `run_id`.
     (Final status is overwritten on close.)
   - `async def agent_runs_step(ts_pool, run_id, name, payload)` — appends
     a step to `steps_json` via
     `UPDATE agent_runs SET steps_json = steps_json || $1::jsonb WHERE run_id = $2`.
   - `async def agent_runs_close(ts_pool, run_id, status, reply)` — sets
     final status, `duration_ms`, and `final_intent`. Idempotent.
   - `async def write_agent_query_audit(ts_pool, auth, run_id, intent, row_count, depot_id)` —
     thin wrapper around the existing `write_admin_audit_row` that fills in
     `action='agent.query'`, `target_type='charging_sessions'`,
     `target_id=str(run_id)`, `metadata={...}`.

### Tests

- **`tests/unit/agent/test_compile_consumption_by_user.py`**:
  - Golden-file assertion: input `QueryPlan` + `resolved` + `window` →
    expected SQL string and parameter list.
  - `driver_id`-only path (no `card_ids` populated).
  - Mixed path with both `driver_ids` and `card_ids`.
  - Empty driver list raises `ValueError`.
  - Multi-driver: parameter array contains all UUIDs in order.
- **`tests/unit/agent/test_audit.py`** — mock the pool, assert that
  `agent_runs_open` writes one row, `agent_runs_step` updates `steps_json`,
  and `write_agent_query_audit` writes one `audit_log` row with the right
  `action` and `metadata` shape.

### Acceptance criteria
- [ ] `pytest tests/unit/agent/` passes; coverage on `intents/` and
      `audit.py` ≥ 90%.
- [ ] Generated SQL passes `EXPLAIN` against a local TimescaleDB with the
      indexes from B1 applied (document the EXPLAIN output in the PR body).
- [ ] PRD §13 row B3 updated to `in PR`.

### Out of scope
- LLM client (B4), router (B5), additional intents (v1).
- Adding new columns to `agent_runs` or `audit_log` beyond what B1 created.

---

## Sprint B4 — LLM integration

**Branch:** `agent-search/sprint-b4-llm`
**Base:** `main` (after B1 merges; can run parallel with B2 and B3)
**Depends on:** B1 merged
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_architecture_v0.md` §4
2. The `claude-api` skill (invoke `Skill(skill="claude-api")` if available
   in the agent environment) — codifies prompt caching and structured
   output for the Anthropic SDK
3. `src/security/secrets.py:42` — the `get_secrets_manager()` rotation
   pattern

### Goal
Wire the `anthropic` SDK so the agent can extract a `QueryPlan` from a user
message and format a natural-language reply from the SQL result. The model
must be selectable per environment via `AGENT_LLM_MODEL` (default
`claude-sonnet-4-6`) so we can swap Sonnet/Haiku/Opus without code changes.
No endpoint wiring yet.

### What to build

1. **Add `anthropic >= 0.40.0` to `pyproject.toml`** under runtime
   dependencies. Run `pip install -e .` and verify the import.
2. **`src/api/agent/llm.py`**:
   - `class LLMConfig(BaseModel)` — frozen settings object loaded once at
     module import:
     - `model: str` — read from `AGENT_LLM_MODEL` env var, default
       `"claude-sonnet-4-6"`.
     - `extract_max_tokens: int` — read from `AGENT_LLM_EXTRACT_MAX_TOKENS`,
       default `400`.
     - `format_max_tokens: int` — read from `AGENT_LLM_FORMAT_MAX_TOKENS`,
       default `800`.
     - `temperature: float` — `AGENT_LLM_TEMPERATURE`, default `0.0` for
       extraction; format step uses a separate `0.3` constant.
     - `request_timeout_s: int` — `AGENT_LLM_TIMEOUT_S`, default `30`.
     - Validate at load time that `model` is one of a known-good list
       (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`). Reject
       unknown values with a `ValueError` at startup, not at first request.
     - Log the active model on import so ops can grep it from container
       logs.
   - `_get_client()` — singleton wrapper around `anthropic.AsyncAnthropic`.
     Pulls API key via `get_secrets_manager().get_secret("ANTHROPIC_API_KEY")`.
     Honors `_PREVIOUS` rotation per the existing JWT pattern. Constructs
     the client with `timeout=LLMConfig.request_timeout_s`.
   - **All `messages.create` calls take `model` from `LLMConfig`** — never
     hardcode a model id. This is the key abstraction this sprint is
     responsible for.
   - `EXTRACT_PLAN_SYSTEM_PROMPT` — a constant string. Includes:
     - The intent catalog (only `consumption_by_user` for v0).
     - The entity kinds (`driver`, `vehicle`, `depot`, `rfid`).
     - The relative time vocabulary (`last_month`, `this_month`, etc.).
     - 3–5 few-shot examples covering happy path, ambiguity (multiple
       names mentioned), and explicit refusal cases.
     - Instructions: "produce only JSON matching the QueryPlan schema; if
       the question is out of scope (writes, multi-intent, non-English),
       return `subjects=[]` so the server returns a refusal."
   - `async def extract_plan(message: str, *, model: str | None = None) -> QueryPlan` —
     calls `messages.create` with
     `system=[{"type":"text","text":SYS, "cache_control":{"type":"ephemeral"}}]`
     for prompt caching, with `tool_choice` configured for structured
     output bound to the `QueryPlan` JSON schema. The optional `model`
     parameter lets callers override the env default per call (used by the
     side-by-side eval script and by tests). Validates result through
     Pydantic. Single retry on validation failure with a "your last output
     was malformed, here's the schema again" follow-up. Hard-fails on
     second failure.
   - `FORMAT_ANSWER_SYSTEM_PROMPT` — separate constant. Style: friendly,
     concise, never reveal raw IDs.
   - `async def format_answer(plan, resolved, window, rows, *, model: str | None = None) -> str` —
     non-streaming for v0. The compiled SQL output shape is small (one
     row per driver-day); pass a serialized summary plus the user's
     original intent to the model. Same per-call `model` override.
3. **`src/api/agent/schema_graph.yaml`** — hand-curated schema map covering
   the tables this intent touches (`drivers`, `sites`,
   `rfid_card_driver_assignments`, `charging_sessions`). Each table lists
   primary key, FKs, and a one-line "what this means" comment. Loaded by
   the system prompt at startup so the model has consistent vocabulary.
4. **`scripts/agent_eval.py`** — CLI that runs the same prompt across two
   models and prints a side-by-side diff. Usage:
   `python scripts/agent_eval.py --message "how much did John charge last month" --models claude-sonnet-4-6,claude-haiku-4-5`.
   This is the tool that lets product/engineering decide the long-term
   default per PRD Q4.
5. **`.env.example`** — add the new env vars with comments:
   ```
   # Model used by the depot chat agent. Must be one of the known-good
   # Anthropic model IDs. Switching models is a redeploy of this env var,
   # not a code change. Default: claude-sonnet-4-6.
   AGENT_LLM_MODEL=claude-sonnet-4-6
   AGENT_LLM_EXTRACT_MAX_TOKENS=400
   AGENT_LLM_FORMAT_MAX_TOKENS=800
   AGENT_LLM_TEMPERATURE=0.0
   AGENT_LLM_TIMEOUT_S=30
   ANTHROPIC_API_KEY=
   # ANTHROPIC_API_KEY_PREVIOUS is honored during rotation windows.
   ```

### Tests

- **`tests/unit/agent/test_llm.py`**:
  - `LLMConfig` reads `AGENT_LLM_MODEL` from env (use `monkeypatch`).
  - `LLMConfig` rejects an unknown model id with `ValueError` at load.
  - Patch `anthropic.AsyncAnthropic.messages.create` to capture the
    `model` argument. Assert that with `AGENT_LLM_MODEL=claude-haiku-4-5`,
    the call uses Haiku; with default env, Sonnet 4.6.
  - Per-call `model="claude-opus-4-7"` override beats env default.
  - Patch to return canned structured outputs; assert `extract_plan`
    round-trips through Pydantic.
  - Validation failure → second call attempt → success on retry.
  - Validation failure twice → raises a typed error.
  - System prompt contains the cache_control marker (assert by string
    inspection on the call args).
- **`tests/unit/agent/snapshot/extract_plan.yaml`** — fixture set of 10
  English messages mapped to expected `QueryPlan`. Run with
  `pytest --runsnapshot` (gated marker, not on every CI run; spec a
  nightly job in §B6) — and the snapshot suite runs against
  `AGENT_LLM_MODEL` from env so we can compare quality across models.

### Acceptance criteria
- [ ] `pytest tests/unit/agent/` passes (excluding snapshot marker).
- [ ] Manual smoke test: a developer runs a Python REPL,
      `extract_plan("how much did John charge last month")`, and gets back
      a valid `QueryPlan`. Repeat with `AGENT_LLM_MODEL=claude-haiku-4-5`
      and capture the output. Document both runs in the PR body.
- [ ] `scripts/agent_eval.py` runs and produces a diff.
- [ ] `make lint` passes.
- [ ] No `ANTHROPIC_API_KEY` value committed; `.env.example` updated with
      all new env vars and a comment pointing at `secrets.py`.
- [ ] PRD §13 row B4 updated to `in PR`.

### Out of scope
- Endpoint wiring, SSE, integration tests against real LLM in CI.
- Multi-provider support (OpenAI, etc.) — Anthropic-only for v0. The
  abstraction makes it possible to add a provider later, but don't ship
  unused provider code.
- Caching beyond the system prompt cache marker.

---

## Sprint B5 — Router + SSE + integration test

**Branch:** `agent-search/sprint-b5-router`
**Base:** `main` (after B1, B2, B3, B4 all merge)
**Depends on:** B1, B2, B3, B4 merged
**Estimated agent runs:** 1–2 (this is the largest sprint)

### Required reading
1. `docs/plans/agent_search_architecture_v0.md` §6, §7
2. `src/api/main.py:248` (lifespan) and the existing `StreamingResponse`
   usage at `src/api/main.py:4608`
3. `src/security/rate_limiter.py` — `check_optimize_limit` is the tier
   we're using (PRD §5.2)
4. The geo-block + tenant-mirror middleware order in `src/api/main.py`

### Goal
Expose the agent over HTTP. Three endpoints behind JWT, geo-block, and
rate limit. SSE for the streaming variant. End-to-end integration test
that proves the full pipeline works against real DB pools and a fake LLM
client.

### What to build

1. **`src/api/agent/stream.py`** — SSE helpers:
   - `format_sse_event(name: str, data: dict) -> bytes` — produces the
     `event: <name>\ndata: <json>\n\n` byte string.
   - `class SSEEventStream` — async generator wrapper that yields step
     events as they happen and finally the answer event.
2. **`src/api/agent/router.py`**:
   - `router = APIRouter(prefix="/agent", tags=["agent"])`
   - `POST /agent/turn` — synchronous, returns `AgentReply` JSON.
   - `POST /agent/turn/stream` — `StreamingResponse(media_type="text/event-stream")`
     with the no-cache + `X-Accel-Buffering: no` headers.
   - `GET /agent/runs/{run_id}` — returns the stored `agent_runs` row,
     gated on the run belonging to the calling user.
   - All three: `Depends(verify_token)`, `Depends(check_optimize_limit)`.
3. **`async def run_turn(message, token_payload, static_pool, ts_pool, llm_client)`**
   in `router.py` (or factor into its own `controller.py` if cleaner) —
   the orchestrator from architecture doc §3.7.
4. **Mount in `src/api/main.py`** behind a feature flag:
   ```python
   if os.environ.get("AGENT_SEARCH_ENABLED", "false").lower() == "true":
       app.include_router(agent_router)
   ```
   Default off. Document the flag in `.env.example`.
5. **`tests/integration/agent/conftest.py`** — fixtures:
   - Two seeded depots in different orgs, each with 2 drivers, 1 RFID per
     driver, ~50 `charging_sessions` rows spanning a month.
   - `FakeLLMClient` that returns canned `QueryPlan` and formatted
     answers given an input message.
6. **`tests/integration/agent/test_run_turn.py`**:
   - Happy path: "how much did John charge last month" →
     resolves to one driver, executes, returns reply, writes one
     `agent_runs` row and one `audit_log` row.
   - Disambiguation: two Johns → reply contains candidates, `agent_runs`
     row has `status='disambiguation'`.
   - Not found: "how much did Zorblax charge last month" →
     `status='not_found'`, helpful suggestion in reply.
   - Cross-org isolation: user from Org A asks about a driver in Org B →
     "not found" (proves the `visible_depot_ids` filter holds).
   - SSE: connect to `/agent/turn/stream`, assert event order
     (`extract_plan`, `resolve_entities`, `compile`, `execute`, `answer`).
   - Error path: LLM client raises → `agent_runs.status='error'`, response
     is a 502 with a generic message (no internal error leak).

### Acceptance criteria
- [ ] `pytest tests/integration/agent/` passes against a Docker Compose
      DB stack.
- [ ] `pytest tests/unit/agent/` still passes (no regressions).
- [ ] `make lint` passes.
- [ ] Manual smoke test against a running API:
      `curl -N -H "Authorization: Bearer ..." -X POST localhost:8000/agent/turn/stream -d '{"message":"..."}'`
      returns SSE events. Capture output in the PR body.
- [ ] PRD §13 row B5 updated to `in PR`.
- [ ] `AGENT_SEARCH_ENABLED` defaults to `false`. The flag flips to
      default-on only in B6 after golden tests pass.

### Out of scope
- Golden test suite (B6).
- Prometheus metrics (B6).
- Acceptance test AT-18 (B6).
- Frontend.

---

## Sprint B6 — Golden tests + observability + acceptance + docs polish

**Branch:** `agent-search/sprint-b6-launch`
**Base:** `main` (after B5 merges)
**Depends on:** B5 merged
**Estimated agent runs:** 1–2

### Required reading
1. `docs/plans/agent_search_prd_v1.md` §7 (success metrics) and §8 (risks)
2. `src/monitoring/metrics.py` — existing Prometheus pattern
3. CLAUDE.md AT-table at §11 of CLAUDE.md (acceptance tests)
4. `docs/API.md` for the existing endpoint reference style

### Goal
Take the agent from "works for the happy path" to "ready for the pilot."
Build the correctness suite, wire metrics, write the AT-18 acceptance
test, and update the docs that humans read.

### What to build

1. **`tests/golden/agent_consumption.yaml`** — 50 Q&A pairs covering:
   - Happy paths (10): one driver, multiple drivers, single depot, multi-depot.
   - Ambiguity (10): two drivers same first name; driver name overlapping
     with vehicle name.
   - Not found (10): unknown name, typo, non-existent depot, driver in
     another org.
   - Edge cases (15): NULL `driver_id` sessions, sessions spanning month
     boundary, DST transition weekend, empty result set, single-row
     result, January (year boundary).
   - Refusals (5): write request, multi-intent, non-English, voice,
     forecasting.

   Each entry has: `id`, `message`, `expected_intent`, `expected_subjects`,
   `expected_status` (`success`/`disambiguation`/`not_found`/`refused`),
   and optional `expected_row_count` for happy paths.

2. **`tests/golden/test_agent_golden.py`** — pytest runner that:
   - Loads the YAML.
   - For each entry, calls `extract_plan` against the real LLM (gated
     behind a marker `--golden` so it's opt-in).
   - Asserts the extracted plan matches `expected_intent` and the right
     number/kind of subjects.
   - For `success` and `disambiguation` entries, also runs the resolver
     against seeded test fixtures and asserts `expected_status`.

3. **CI wiring** — add a GitHub Actions job (or update the existing one)
   that runs `pytest --golden tests/golden/` on every PR touching
   `src/api/agent/` and nightly on `main`. Fails the build on regression.

4. **`src/monitoring/metrics.py`** — add the four metrics from
   architecture doc §8.3:
   - `agent_turns_total{status, intent}`
   - `agent_turn_duration_seconds{intent}`
   - `agent_llm_tokens_total{model, direction}`
   - `agent_resolver_misses_total{kind}`
   Increment them from the right places in `src/api/agent/router.py`.

5. **AT-18 acceptance test** — `tests/e2e/test_agent_search.py`:
   Implements the AT-18 spec from architecture doc §9.4. Marker: `acceptance`.

6. **CLAUDE.md updates**:
   - Add three new endpoints to the REST API table.
   - Add `AT-18` to the acceptance test table at §11.
   - Add a one-paragraph blurb on the agent module under "Architecture".

7. **`docs/API.md` updates** — full endpoint reference for `/agent/turn`,
   `/agent/turn/stream`, `/agent/runs/{run_id}`. Include request/response
   examples and a section describing the SSE event contract.

8. **Flip `AGENT_SEARCH_ENABLED` default to `true`** in `.env.example` and
   in the `if` check in `src/api/main.py` (still respect the env var, but
   default to on now that golden tests are the gate). Add a comment
   pointing at this PR.

### Acceptance criteria
- [ ] `pytest --golden tests/golden/` passes ≥ 95% of entries (per PRD §5.2).
- [ ] `pytest -m acceptance tests/e2e/test_agent_search.py` passes.
- [ ] All four Prometheus metrics appear in `/metrics` after one turn.
- [ ] CLAUDE.md and `docs/API.md` updated.
- [ ] PRD §13 row B6 updated to `in PR`. After merge, the row is updated
      to `merged` and §13 is updated with the agent's go-live date.

### Out of scope
- Frontend (F1–F3).
- v1 intents, Lithuanian, on-device models — those are post-pilot.

---

# Frontend Sprints

The frontend repo and stack are not in this backend's tree. Each frontend
sprint runs against the frontend repo (typically Next.js + Supabase JS
client). Adapt the file paths to whatever that repo's conventions look
like.

**Important context:** The chat shell (input box, message list, layout,
auth context, theme) **already exists** in the frontend. These sprints are
integration with the new `/agent/*` backend, not green-field UI. Before
starting either sprint, locate the existing chat component(s) and
understand:

- Where messages are stored (Zustand / Redux / local state).
- How the existing send-message flow currently works (does it hit a
  different backend? a Supabase edge function? a stub?).
- How JWT/auth is already plumbed (likely the Supabase session token).
- Whether there's any existing SSE or streaming handling, or whether this
  is the first SSE consumer.

The goal is to **replace the existing send-message backend call** with
`POST /agent/turn/stream`, and **extend the existing message-rendering
pipeline** to handle the new reply shapes (disambiguation, not-found,
refusal). Do not duplicate the chat shell.

---

## Sprint F1 — Wire existing chat shell to `/agent/*` endpoints

**Branch:** `agent-search/sprint-f1-integration`
**Base:** main of the frontend repo
**Depends on:** B5 merged on the backend
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_prd_v1.md` §4 (all stories), §5 (requirements)
2. `docs/plans/agent_search_architecture_v0.md` §6 (REST + streaming) and
   §3 (reply shapes — `success`, `disambiguation`, `not_found`, refusal)
3. The existing chat shell components in the frontend repo. Read them
   first; do not write code until you can describe how the current send
   flow works.

### Goal
Replace whatever the existing chat shell currently calls with the new
`/agent/*` backend. Implement the SSE consumer, render all four reply
shapes (success, disambiguation, not-found, refusal), and pipe per-step
status updates into whatever loading-state UI already exists.

### What to build

1. **API client module** (e.g. `lib/agent/client.ts`):
   - `streamAgentTurn(message: string, opts: { signal?: AbortSignal })` —
     opens an SSE connection to `POST /agent/turn/stream`. Yields `step`
     events as they arrive and resolves with the final `answer` event
     payload. Pulls JWT from the existing auth provider; attaches
     `Authorization: Bearer ...` header.
   - `fetchAgentRun(runId: string)` — `GET /agent/runs/{run_id}`. Used by
     F2 for the reasoning panel, but ship the function in F1 so the API
     surface is complete.
   - **Do not invent a mock backend.** B5 must be live in staging before
     F1 starts; develop against staging or a local backend.
2. **Hook the API client into the existing send-message flow.** Find the
   existing function (likely named `sendMessage`, `handleSubmit`, or
   similar) and replace its backend call with `streamAgentTurn`. Keep the
   existing optimistic-add-user-message behaviour. Append agent step
   updates to the in-flight assistant message instead of creating new
   messages — this keeps the existing UI shape.
3. **Reply shape discriminator** — extend the existing message renderer to
   route by the `status` field on the final `answer` payload:
   - `status: "success"` — render the markdown body in the existing reply
     style (no UI change beyond passing through the markdown).
   - `status: "disambiguation"` — render a candidates list. Each
     candidate is a clickable chip; clicking sends a follow-up message
     with the chosen display name.
   - `status: "not_found"` — render the suggestions list, also as
     clickable chips.
   - `status: "refused"` — distinct visual treatment (muted background,
     info icon). Render the agent's suggested next action.
4. **Per-step status indicator** — the existing chat shell almost certainly
   has a "thinking…" indicator. Replace its static text with the most
   recent SSE step name (e.g., "Resolving driver…", "Querying sessions…").
   Reset to idle when the answer event arrives.
5. **Markdown renderer check** — confirm the existing renderer handles
   tables. If not, swap in a renderer that does (e.g.
   `react-markdown` + `remark-gfm`); the formatter on the backend can emit
   tabular bodies.
6. **Error handling** — if the SSE connection drops or the server returns
   a non-2xx, render an inline error styled like the refusal component
   ("the agent is temporarily unavailable; please try again or use the
   dashboard"). Do not crash the chat shell.

### Tests

- Component test: the discriminator routes each of the four `status`
  values to the right component variant.
- API client test: SSE event parsing handles partial chunks correctly.
- Auth test: the request header matches the active Supabase session.
- Manual smoke against staging:
  - "How much did John charge last month?" → see step indicator update,
    see markdown reply with table.
  - "How much did John consume?" (ambiguous) → see disambiguation chips,
    click one, see follow-up.
  - "How much did Zorblax charge?" → see not-found suggestions.
  - "Schedule John tonight." → see refusal styling.

### Acceptance criteria
- [ ] All four reply shapes render correctly against staging without
      console errors.
- [ ] Existing chat shell visual style is preserved (this is integration,
      not a redesign).
- [ ] PRD §13 row F1 updated to `in PR`.

### Out of scope
- Reasoning panel expansion (F2).
- CSV export (F2).
- Feedback buttons (F2).
- Redesigning the chat shell layout, theming, or animations.
- Building a new mock backend — depends on staging being live.

---

## Sprint F2 — Reasoning panel + CSV export + feedback

**Branch:** `agent-search/sprint-f2-panel`
**Base:** main of the frontend repo (after F1 merges)
**Depends on:** F1 merged, B6 merged on the backend
**Estimated agent runs:** 1

### Required reading
1. `docs/plans/agent_search_prd_v1.md` §4 story 1, §5 F9–F14
2. `docs/plans/agent_search_architecture_v0.md` §6
   (`GET /agent/runs/{run_id}`)

### Goal
The transparency and exportability story. Users can see how the agent got
its answer, export the data to CSV for downstream tools, and signal
satisfaction with each reply.

### What to build

1. **Reasoning panel:**
   - "Show reasoning" toggle on each agent reply.
   - When expanded, calls `fetchAgentRun(runId)` (the function shipped in
     F1) and renders each entry from `steps_json` as a labelled row with
     the step name, summary, and timing.
   - Role gate: for `favonius_admin` users only, also shows raw SQL and
     parameter values. Pull the role from the existing JWT decoder; do
     not call a new endpoint just to learn the role.
2. **CSV export:**
   - "Export CSV" button on replies whose payload includes tabular
     `result` data.
   - Client-side conversion of the structured `result` to CSV.
   - Filename: `agent-<intent>-<YYYYMMDD-HHMM>.csv`.
3. **Feedback buttons:**
   - 👍/👎 on every successful reply.
   - On 👎: open a one-line "what was wrong?" inline form.
   - Until the backend feedback endpoint exists (separate issue, link
     from the PR), store feedback locally and emit it as a Sentry
     breadcrumb so we capture early signal.
4. **Optional, time permitting:** a "my recent questions" sidebar that
   lists the user's last 10 turns. Requires a new backend endpoint
   (`GET /agent/runs?user=me`); if that's not ready, defer this to a
   later sprint and surface it as an issue.

### Tests

- Component tests for the reasoning panel (collapsed, expanded,
  role-gated SQL view).
- CSV export unit test (correct quoting, headers, filename).
- Feedback button test: 👍 logs, 👎 opens form, form submit fires the
  Sentry breadcrumb.
- Manual against staging:
  - Expand reasoning panel → see real steps from a successful run.
  - Export CSV → file opens cleanly in Excel/Sheets with correct headers.
  - 👎 a reply → form appears, submit captures feedback.
  - As `favonius_admin`, raw SQL is visible. As `customer_operator`, it
    is not.

### Acceptance criteria
- [ ] All three features (reasoning panel, CSV export, feedback) work
      end-to-end against staging.
- [ ] Role gate on the SQL view verified with two test accounts.
- [ ] PRD §13 row F2 updated to `in PR`. After merge, the agent search v0
      is feature-complete; trigger the launch retrospective described in
      "After v0 ships" below.

### Out of scope
- Backend feedback endpoint (separate issue).
- "My recent questions" if it stretches the sprint.
- v1 intents / Lithuanian / writes — all post-launch.

---

## After v0 ships

Convene a retrospective:
- What questions did users ask that v0 didn't cover? Sort by frequency.
- Where did the LLM extraction fail? Add the failures to the golden suite.
- Was Sonnet 4.6 the right model, or should v1 try Haiku for cost?
- Did the trust boundary hold? Any near-misses? Add them as adversarial
  test cases.

The output of the retro is the v1 sprint plan, which gets its own PRD
revision.
