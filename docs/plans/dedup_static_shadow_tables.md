# Plan — retire the TimescaleDB static-shadow tables

## Problem

The backend runs two Postgres databases (`CLAUDE.md` §"Database Schema"):

- **TimescaleDB** (`db_pools.ts`) — time-series + operational data.
- **Supabase** (`db_pools.static`) — the static reference tables (`sites`,
  `vehicles`, `charging_stations`, `drivers`, `schedules`, `organizations`, …).

But the TimescaleDB migration set **also creates shadow copies of the static
reference tables**, then drops them again several migrations later. They are
never populated in production — the state assembler reads static data from
Supabase, and `tenant_mirror` writes orgs/memberships to Supabase. The shadows
are pure churn, and they actively cause problems:

- **Migration churn / confusion** — a fresh TimescaleDB applies `001…043` and
  ends up creating ~12 tables only to drop them. Readers can't tell which
  `vehicles`/`drivers`/`depots` is authoritative.
- **Test-harness collision** — the two migration sets both create
  `vehicles`/`drivers`/`schedules`/`depots`/`organizations` with *different*
  schemas, so they cannot be replayed into a single database. The agent-SQL
  golden harness had to reconstruct the Supabase base separately
  (`tests/golden/agent_sql/supabase_bootstrap.sql`) to dodge this.
- **Historical FK breakage** — operational tables (`optimization_runs.depot_id`,
  `notification_alerts.organization_id`, …) carried FKs to the shadows;
  background writers hit `foreign_key_violation` until migration 028 dropped
  those FKs. The header of `migrations/028_drop_static_shadow_fks.sql` already
  records the intent: *"A follow-up will retire the shadows once test
  infrastructure is restructured."* This plan is that follow-up.

## Inventory (TimescaleDB set, `migrations/*.sql`)

Shadow tables and where they're touched today:

| Shadow table | Created by | FK'd-to by (operational → shadow) | Dropped by |
|---|---|---|---|
| `depots` | 001 | 001, 003, 015, 020, 022 | 029 |
| `vehicles` | 001 (+cols 018) | 001, 003 | 029 |
| `chargers` | 001 | 001, 003 | 029 |
| `schedules` | 001 | 001 | 029 |
| `battery_storage` | 001 | 001 | 029 |
| `charger_vehicle_access` | 001 | 001 | 029 |
| `organizations` | 015 | 015, 020, 022 | 029 |
| `user_organizations` | 015 | 015 | 029 |
| `drivers` | 018 | 018 | 039 |
| `rfid_cards` | 018 | 018 | 039 |
| `rfid_card_vehicle_assignments` | 018 | 018 | 039 |
| `rfid_card_driver_assignments` | 018 | 018 | 039 |

FK constraints from operational tables to the shadows are dropped by
`028_drop_static_shadow_fks.sql`; the tables themselves by
`029_remove_static_shadows.sql` and `039_drop_static_shadow_drivers_rfid.sql`.

**Migration runner is stateless.** `scripts/run_migrations.py` re-runs *every*
file on each deploy and tolerates "already exists" — there is no `applied`
tracking table. So editing an early migration changes the fresh-install journey
**and** re-applies harmlessly to existing DBs (a `CREATE TABLE IF NOT EXISTS`
on an already-present table is a no-op; a removed `CREATE` leaves an
already-dropped table dropped). The end state converges either way — which is
what makes the surgical option below safe to verify.

## Options

### Option A — Surgical removal from the early migrations (recommended)
Stop creating the shadows where they're born, and inline the FK-free shape that
028/029/039 already converge to:
- In `001` (and `015`, `018`): delete the shadow `CREATE TABLE`s and any seed
  `INSERT`s into them.
- In `001`/`003`/`015`/`020`/`022`: change operational columns that referenced
  a shadow (`… REFERENCES depots(depot_id)`, etc.) to plain
  `uuid`/`uuid NOT NULL` columns — exactly the post-028 shape.
- Turn `028`/`029`/`039` into idempotent safety nets (`DROP … IF EXISTS`,
  `DROP CONSTRAINT IF EXISTS`) so legacy DBs that still carry shadows get them
  cleaned, while fresh DBs simply find nothing to drop.
- **Pros:** single linear history; the churn is gone for fresh installs; the
  TS/static separation becomes a real invariant; safe for existing DBs given
  the stateless runner. **Cons:** edits span ~7 migrations and must be verified
  by schema equivalence (below); touches sensitive files, so it needs `/freeze`
  discipline and a careful review.

### Option B — Gate shadow DDL on `favonius.migration_mode`
`run_migrations.py` already sets a `favonius.migration_mode` GUC
(`ts_dedicated`/`ts_shared`/…). Wrap every shadow `CREATE`/FK/seed in
`IF current_setting('favonius.migration_mode') <> 'ts_dedicated'`.
- **Pros:** no destination change for `ts_shared`; opt-in. **Cons:** conditional
  DDL across many files is ugly and error-prone; the create-then-drop churn
  still happens in shared mode; FKs and tables must be guarded in lockstep or a
  half-built schema results. Net: more complexity than Option A for less gain.

### Option C — Do nothing structural; document the end state
The shadows are already gone by `039`; the destination is correct today. Add a
note to `CLAUDE.md` that the shadows are legacy churn and leave the history.
- **Pros:** zero risk. **Cons:** the churn, the reader confusion, and the
  test-collision all remain; the 028 "follow-up" is never closed.

**Recommendation: Option A.** It actually removes the churn and makes
"no static reference table is ever created in the TS set" a verifiable
invariant, and the stateless idempotent runner makes it safe — provided the
refactor is gated on a strict **schema-dump equivalence test** (the destination
must be byte-identical before and after).

## Sequenced steps (Option A)

1. **Capture the baseline.** Apply the current `migrations/*.sql` to a fresh
   TimescaleDB; `pg_dump --schema-only --no-owner --no-privileges` → `before.sql`.
2. **Edit the creators.** Remove shadow `CREATE TABLE`s + shadow seed `INSERT`s
   from `001`, `015`, `018`. Keep all time-series/operational tables.
3. **De-FK the operational tables.** In `001`/`003`/`020`/`022`, rewrite columns
   that referenced a shadow as plain `uuid` columns (preserve `NOT NULL`/index
   exactly as the post-028 state has them).
4. **Neutralize the droppers.** Confirm `028`/`029`/`039` are pure
   `IF EXISTS`/`IF EXISTS`-guarded so they're no-ops on a fresh DB and still
   clean a legacy DB.
5. **Verify equivalence.** Apply the edited set to a fresh DB; `pg_dump` →
   `after.sql`; `diff before.sql after.sql` MUST be empty (ignoring only the
   now-absent intermediate churn, which a schema-only end-state dump won't
   show). This is the gate: the destination is unchanged.
6. **Re-run the suites against the refactored fresh DB:** the workflow-golden
   gate, the agent-SQL golden gate (`pytest -m agent_sql_golden`), and the
   unit suite. All must stay green.
7. **Update docs.** `CLAUDE.md` §"Database Schema" — state the invariant that the
   TS set never creates static reference tables, and that Supabase owns them.

## Verification / done-when

- `diff before.sql after.sql` is empty (end-state schema identical).
- A fresh TimescaleDB built from the edited set contains **none** of the 12
  shadow tables at any point (grep the apply log for their `CREATE TABLE`).
- `pytest -m workflow_golden` and `pytest -m agent_sql_golden` green on the
  refactored fresh DB.
- `make lint` clean.

## Constraints / notes for the executor

- This touches `migrations/` — run under `/freeze` discipline and treat every
  edit as reviewable. Do **not** renumber existing migrations.
- The runner has no `applied` table; **never** assume a migration "already ran".
  Idempotency (`IF [NOT] EXISTS`, `DROP CONSTRAINT IF EXISTS`) is the contract.
- Supabase migrations (`migrations/supabase/*.sql`) are **out of scope** — they
  already own the static tables. The agent-SQL test bootstrap
  (`tests/golden/agent_sql/supabase_bootstrap.sql`) is unaffected.
- The duplicate `"… 2.sql"` files in both migration dirs are a separate hygiene
  issue; flag but don't fix here unless asked.

---

## Ready-to-run session prompt

> Reference: `docs/plans/dedup_static_shadow_tables.md`.
>
> Goal: retire the TimescaleDB static-shadow tables so the TS migration set
> never creates static reference tables (`depots`, `vehicles`, `chargers`,
> `schedules`, `battery_storage`, `charger_vehicle_access`, `organizations`,
> `user_organizations`, `drivers`, `rfid_cards`, `rfid_card_*_assignments`).
> Execute **Option A** from the plan.
>
> Read first (do not edit yet): the plan above; `migrations/001_initial_schema.sql`,
> `015_organizations.sql`, `018_fleet_identity.sql`, `028_drop_static_shadow_fks.sql`,
> `029_remove_static_shadows.sql`, `039_drop_static_shadow_drivers_rfid.sql`, and
> the shadow-referencing parts of `003`, `020`, `022`; `scripts/run_migrations.py`.
>
> Then, with a STOP between each:
>
> STEP 1 — Baseline. Stand up a fresh TimescaleDB
> (`timescale/timescaledb:latest-pg16`), apply the current `migrations/*.sql`,
> and save `pg_dump --schema-only --no-owner --no-privileges` as
> `/tmp/before.sql`. Also grep the apply log and record which shadow
> `CREATE TABLE`s fire. STOP and show the baseline shadow list.
>
> STEP 2 — Edit. Apply Option A steps 2–4: remove shadow creates/seeds from
> `001`/`015`/`018`; convert operational FK columns to plain `uuid` in
> `001`/`003`/`020`/`022`; confirm `028`/`029`/`039` are `IF EXISTS`-guarded.
> Do NOT renumber. STOP and show a diff of every migration touched.
>
> STEP 3 — Prove equivalence + green suites. Apply the edited set to a fresh DB,
> `pg_dump` → `/tmp/after.sql`, and show `diff /tmp/before.sql /tmp/after.sql`
> is empty. Confirm no shadow `CREATE TABLE` fires in the apply log. Run
> `pytest -m workflow_golden` and `pytest -m agent_sql_golden` (set up the
> agent-SQL pair via `tests/golden/agent_sql/setup_test_dbs.sh`) and the unit
> suite — all green. Update `CLAUDE.md` with the invariant. `make lint` clean.
> Commit + push + open a draft PR.
>
> Constraints: no migration renumbering; idempotency (`IF [NOT] EXISTS`) is the
> contract — the runner is stateless; Supabase migrations and the
> `"… 2.sql"` duplicate-file hygiene issue are out of scope; the end-state
> schema dump MUST be byte-identical before vs after (that is the gate).
