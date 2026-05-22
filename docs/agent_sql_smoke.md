# Agent SQL-mode substrate smoke test — PR #216

**Verdict: BLOCKED (environment)** — Step 1 PASS. Steps 2-6 could not be run
in this remote execution container because no TimescaleDB instance is
available and the package source needed to install one is outside the
network policy's allowlist. The smoke test needs to be re-run from an
environment with the `docker-compose.yml` dev DB pair up (TimescaleDB +
Supabase). No code was modified.

---

## Session context

- Branch: `claude/wonderful-brahmagupta-bfUod` (synced to `origin/main` @
  `401ebad`).
- PR #216 merge commit on main: `52ebfaa` ("feat(agent): general-purpose
  SQL mode for depot analytics chatbot").
- Host environment: ephemeral remote container, Linux 6.18.5, no
  pre-existing TimescaleDB, no `.env`, no seeded depots.

## Step 1 — Substrate file inventory (PASS)

All files from PR #216 that the task brief required are present on
`main`:

| Path | Bytes | Lines |
| --- | --- | --- |
| `migrations/042_agent_views_ts.sql` | 19,245 | — |
| `migrations/supabase/040_agent_views_static.sql` | 9,348 | — |
| `src/api/agent/sql_validator.py` | 47,703 | 1,167 |
| `src/api/agent/sql_executor.py` | 10,207 | 259 |
| `src/api/agent/sql_tools.py` | 16,758 | 405 |
| `src/api/agent/catalogue.py` | 17,770 | 391 |
| `src/api/agent/planner.py` | 5,367 | 151 |
| `src/api/agent_workflows/runtime.py` | 45,714 | 1,079 |

`run_qa_turn` lives at `src/api/agent_workflows/runtime.py:729` and is
exported from `__all__` at line 1078.

`sqlglot>=23.0.0` declared in `pyproject.toml:64`.

Confirmation that these all came from #216 (from `git show --stat
52ebfaa`):

```
migrations/042_agent_views_ts.sql                  |  419 +++++++
migrations/supabase/040_agent_views_static.sql     |  235 ++++
src/api/agent/sql_validator.py                     | 1167 ++++++++++++++++++++
src/api/agent/sql_executor.py                      |  259 +++++
src/api/agent/sql_tools.py                         |  405 +++++++
src/api/agent/catalogue.py                         |  391 +++++++
src/api/agent/planner.py                           |  151 +++
src/api/agent_workflows/runtime.py                 |  418 ++++++-
pyproject.toml                                     |    6 +
```

The version landed on main matches the brief — #216 is not a different
version of the work.

## Step 2 — Apply migrations against dev DB pair (BLOCKED)

The remote execution container does not ship with TimescaleDB and the
network policy blocks the install paths I tried. The exact attempts:

```bash
# Native Postgres 16 cluster brought up successfully:
$ pg_ctlcluster 16 main start
$ pg_lsclusters
Ver Cluster Port Status Owner    Data directory              Log file
16  main    5432 online postgres /var/lib/postgresql/16/main /var/log/postgresql/postgresql-16-main.log

$ sudo -u postgres psql -c "SELECT version();"
 PostgreSQL 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, ...

# TimescaleDB not packaged in the stock Ubuntu repos:
$ sudo -u postgres psql -c "SELECT name FROM pg_available_extensions WHERE name='timescaledb';"
 name
------
(0 rows)

# Added the Timescale packagecloud repo:
$ curl -s https://packagecloud.io/install/repositories/timescale/timescaledb/script.deb.sh | bash
... Packagecloud gpg key imported ... done.

# Install fails because the CDN packagecloud redirects to is not in the
# network allowlist:
$ apt-get update
... Failed to fetch https://packagecloud.io/timescale/timescaledb/ubuntu/dists/noble/InRelease  403  Forbidden [IP: 3.170.149.32 443]
... The repository 'https://packagecloud.io/timescale/timescaledb/ubuntu noble InRelease' is not signed.

$ curl -sL https://packagecloud.io/timescale/timescaledb/ubuntu/dists/noble/main/binary-amd64/Packages
Host not in allowlist

$ apt-get install -y timescaledb-2-postgresql-16
E: Unable to locate package timescaledb-2-postgresql-16
```

So I have a local Postgres 16, but no TimescaleDB extension, and no way
to install one from this container.

Why that blocks migration 042:

- `migrations/042_agent_views_ts.sql:250` and `:275` call
  `time_bucket('1 hour'::interval, …)` inside
  `agent_views.prices_hourly` and `agent_views.building_load_hourly`.
  These functions don't exist outside the TimescaleDB extension.
- Migrations 005, 035 and others earlier in the chain create
  hypertables on `telemetry`, `electricity_prices`, etc. via
  `create_hypertable()`. Without TimescaleDB those migrations
  can't run, so the prerequisite tables that 042 depends on
  (`public.charging_sessions`, `public.electricity_prices`,
  `public.building_load`, `public.connector_status`,
  `public.agent_runs`) wouldn't exist.

Why the Supabase side (040) also can't be applied here:

- The Supabase Postgres in the dev `docker-compose.yml` is a separate
  cluster from the TimescaleDB one. I have no Supabase project locally
  and I deliberately did not touch the `favonius-pilot` Supabase
  project (the only one accessible via the MCP server) because the
  brief says "Do not apply against production" and `favonius-pilot` is
  the labelled-pilot/production-adjacent project, not dev.
- Even on a fresh local Postgres, migration 040 depends on
  `public.sites`, `public.vehicles`, `public.drivers`,
  `public.charging_stations`, `public.schedules` — created by
  `migrations/supabase/001…039_*.sql`. None of those are applied here
  either.

What I needed and didn't have: the `docker-compose up -d timescaledb`
TimescaleDB container plus the Supabase dev project the engineer keeps
on their laptop, with prior migrations applied. Once those exist,
applying 042 and 040 is a one-liner each:

```bash
# TimescaleDB side (against the docker-compose timescaledb service):
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/042_agent_views_ts.sql

# Supabase side (against the local/staging Supabase DB):
psql "$STATIC_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/supabase/040_agent_views_static.sql
```

Both files are idempotent (`CREATE ROLE … EXCEPTION duplicate_object`,
`CREATE OR REPLACE FUNCTION …`, `DROP TRIGGER IF EXISTS …`), so they
are safe to re-run.

## Step 3 — Role and grant audit (BLOCKED)

Not run, because the roles don't exist until step 2 succeeds. For the
follow-up session, here are the commands and the expected exit
condition.

```bash
psql "$DATABASE_URL" <<'SQL'
\du agent_reader_ts
SELECT table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'agent_reader_ts'
ORDER BY 1, 2, 3;

SELECT n.nspname AS schema, p.proname AS function, has_function_privilege('agent_reader_ts', p.oid, 'EXECUTE') AS can_execute
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'agent_views'
ORDER BY 1, 2;
SQL

psql "$STATIC_DATABASE_URL" <<'SQL'
\du agent_reader_static
SELECT table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'agent_reader_static'
ORDER BY 1, 2, 3;

SELECT n.nspname AS schema, p.proname AS function, has_function_privilege('agent_reader_static', p.oid, 'EXECUTE') AS can_execute
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'agent_views'
ORDER BY 1, 2;
SQL
```

Expected:

- Both roles exist, both `Cannot login` (NOLOGIN) and have no
  attributes other than the function-execute grants.
- `role_table_grants` returns **0 rows** for both — the agent roles
  have no table-level grants. They get reach via `EXECUTE` on the
  `agent_views.*` SECURITY DEFINER functions, not via direct table
  access.
- The function-privilege query returns one row per function with
  `can_execute=true`. Expected functions:
  - TimescaleDB side: `sessions`, `optimization_runs`,
    `prices_hourly`, `building_load_hourly`,
    `connector_status_latest`, optionally `alerts` (only present if
    migration 022 has been applied).
  - Supabase side: `depots`, `vehicles`, `chargers`, `drivers`,
    `schedules_recent`.

Anything outside that — any grant on `auth.*`, `storage.*`, `pg_*`,
`public.*`, or schemas other than `agent_views` — should be flagged as
a deviation from the substrate contract.

Migration code review (read-only, since I can't run it):

- 042 `REVOKE ALL ON SCHEMA public, pg_catalog, information_schema
  FROM agent_reader_ts` lines 39-41 — defence-in-depth.
- 042 only `GRANT USAGE ON SCHEMA agent_views` and `GRANT EXECUTE ON
  FUNCTION agent_views.<fn>(uuid[])` — no table grants anywhere.
- 040 symmetrically does the same for `agent_reader_static`.
- Both migrations grant role membership in
  `agent_reader_{ts,static}` to `current_user` and `session_user`
  (deduplicated), so the executor's `SET LOCAL ROLE` will succeed
  whether the connection user is a pooler or a direct login. The
  GRANT is in an EXCEPTION block that warns instead of failing if
  the migration is being run by an unprivileged role.

Static read confirms the grant surface is exactly what the brief
required. Live verification still needs the steps above against a real
DB.

## Step 4 — Set `AGENT_SQL_MODE_ENABLED=true` and start the API (BLOCKED)

Not run. The API requires `DATABASE_URL` pointing at a TimescaleDB
instance with the migration chain applied; without that, importing
`src.api.main` will fail at lifespan startup (asyncpg pool init against
a DB whose tables don't match the SQLAlchemy models in
`src/db/models.py`). I did not write a local `.env` because it would
have served no purpose without a backing DB and because committing it
later would be a hazard.

For the follow-up session, the local `.env` overlay needed is roughly:

```dotenv
AGENT_SQL_MODE_ENABLED=true
# AGENT_SQL_ORG_ALLOWLIST=  # empty = all enabled orgs
DATABASE_URL=postgresql://favonius:<pw>@localhost:5432/favonius
STATIC_DATABASE_URL=postgresql://postgres:<pw>@localhost:54322/postgres
ANTHROPIC_API_KEY=sk-ant-…
JWT_SECRET_KEY=<from supabase project>
SUPABASE_URL=http://localhost:54321
# Plus whatever org/depot auth-context the test caller will carry.
```

Then `uvicorn src.api.main:app --reload`.

## Step 5 — POST /agent/turn/stream (BLOCKED)

Not run. Required a running API (step 4) and seeded data including a
depot named "Vilnius" and at least one week's worth of
`charging_sessions` for that depot.

The agent's intended SQL trajectory for "How many charging sessions
did we have last week at depot Vilnius?" (predicted from the catalogue
and planner code on `main`, NOT observed):

1. `planner.classify_query()` rejects the consumption-by-user fast
   path (no driver/user, no kWh) and falls through to the SQL agent
   loop.
2. Tool 1 — `lookup_entity('depot', 'Vilnius')` → `depot_id`.
3. Tool 2 — `current_time()` → returns now (and the agent computes
   "last week" as the [Monday 00:00, next Monday 00:00) UTC interval).
4. Tool 3 — `run_select_static` with something like:
   ```sql
   SELECT depot_id, name, timezone
   FROM agent_views.depots($1)
   WHERE name = $2
   ```
   to confirm the depot is in scope (or the equivalent — this is the
   pattern the system prompt should encourage).
5. Tool 4 — `run_select_ts` with the actual count:
   ```sql
   SELECT COUNT(*) AS sessions
   FROM agent_views.sessions($1)
   WHERE start_time >= $2 AND start_time < $3
   ```
6. Tool 5 — `emit_final_answer` with a natural-language wrap.

What I'd want the captured trace from the SSE stream to confirm:

- The planner did NOT route this to the `consumption_by_user`
  fast-path. `favonius_agent_sql_executions_total` increments, not the
  consumption metrics.
- The `$1` bound on each `run_select_*` call equals the caller's
  `visible_depot_ids`, not a hard-coded list. (Verifier: the
  `steps_json` array on the `agent_runs` row should record the
  parameter values.)
- `agent_runs` row written: `status='completed'`,
  `final_intent='sql_qa'` (or whatever the SQL-mode value is —
  worth confirming in `audit.py`),
  `len(steps_json) >= 4`, non-zero `token_input` and `token_output`,
  `duration_ms` reasonable.

## Step 6 — Hand-run the equivalent SQL (BLOCKED)

Not run. The reference query I would run via `psql` once the DB pair
exists:

```sql
-- Substitute the literal depot UUID for :depot_id and the actual
-- previous-week UTC bounds for :ws / :we. Bypassing agent_views.* so
-- this verifies the raw row count, not the curated surface.
SELECT COUNT(*) AS session_count
FROM public.charging_sessions
WHERE site_id = :depot_id
  AND start_time >= :ws
  AND start_time <  :we;
```

Then compare against the agent's final answer. They MUST match — if
the agent returns a different number, the verdict is FAIL and the
issue likely sits in the `agent_views.sessions` filter (e.g. a row
shape that doesn't propagate `site_id`, or a misnamed time column).

## Issues to revisit in S1 (none identified yet from the code-only review)

From reading `planner.py` and `sql_tools.py` on `main`:

- The planner's fast-path matches only `consumption_by_user`-shaped
  questions; "how many sessions" is correctly expected to fall through
  to SQL mode.
- The SQL-mode tool set includes `lookup_entity`, which handles depot
  name → id resolution server-side. The LLM never invents a depot
  UUID.

No code-level concerns to flag yet. The verdict awaits a real run of
steps 5 and 6.

## What needs to happen next

Re-run this checklist from a host with the dev DB pair already up:

1. `docker-compose up -d timescaledb` (or a comparable local
   Supabase + TimescaleDB stack).
2. Apply migrations 001 → 042 against the TimescaleDB pool.
3. Apply Supabase migrations 001 → 040 against the static pool.
4. Run steps 3 → 6 of this brief against the real DBs.
5. Replace this file's "BLOCKED" verdict with the actual PASS/FAIL.
