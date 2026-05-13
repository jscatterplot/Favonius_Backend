-- Migration 037: Depot Agent — workflows, per-depot tiers, decision audit log.
--
-- Sprint 1 of the Depot Agent V1 build (docs/PRD_Depot_Agent.md §4.4, §5.2,
-- §5.3, §9, §10.4). Schema only — no runtime, no endpoints. Three tables:
--
--   * workflows         — workflow definition (prompt, allowed tools, params)
--   * workflow_tiers    — per-(workflow, depot) permission tier + graduation
--                         rule. Tier graduation is per workflow per depot
--                         (PRD §9.3).
--   * decisions         — append-only audit record of every agent action,
--                         including human disposition. Hypertable on
--                         `timestamp` with a 90-day retention policy.
--
-- The append-only guarantee on `decisions` is enforced by a BEFORE
-- UPDATE/DELETE trigger that RAISES — PRD §10.4 ("Decision records are
-- append-only. Edits are recorded as new records referencing the original.
-- No deletion."). Edits link to the original via `parent_decision_id` and
-- carry their delta in `diff_if_edited`.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, CREATE OR REPLACE FUNCTION,
-- create_hypertable(if_not_exists => TRUE), add_retention_policy(
-- if_not_exists => TRUE). Safe to re-run.

-- ── workflows ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workflows (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT UNIQUE NOT NULL,
    version         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    prompt          TEXT NOT NULL,
    allowed_tools   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    parameters      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── workflow_tiers ───────────────────────────────────────────────────────
-- Tier + graduation rule are per-(workflow, depot). Depot is NOT a FK
-- here because depot rows live in Supabase (`sites`), not TimescaleDB.
CREATE TABLE IF NOT EXISTS workflow_tiers (
    workflow_id              UUID    NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    depot_id                 UUID    NOT NULL,
    tier                     TEXT    NOT NULL
        CHECK (tier IN ('inform', 'draft_and_wait', 'act_and_notify', 'autonomous')),
    min_decisions            INTEGER NOT NULL DEFAULT 100  CHECK (min_decisions >= 0),
    max_override_rate        NUMERIC NOT NULL DEFAULT 0.05 CHECK (max_override_rate >= 0 AND max_override_rate <= 1),
    max_edit_rate            NUMERIC NOT NULL DEFAULT 0.15 CHECK (max_edit_rate >= 0 AND max_edit_rate <= 1),
    requires_human_signoff   BOOLEAN NOT NULL DEFAULT TRUE,
    next_tier                TEXT
        CHECK (next_tier IS NULL OR next_tier IN ('inform', 'draft_and_wait', 'act_and_notify', 'autonomous')),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workflow_id, depot_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_tiers_depot
    ON workflow_tiers (depot_id);

-- ── decisions (hypertable) ───────────────────────────────────────────────
-- TimescaleDB requires the time partition column to be part of the
-- primary key, so PK is `(id, timestamp)`. Logical decision identity is still
-- globally unique on `id`, enforced by an INSERT trigger (see below).
CREATE TABLE IF NOT EXISTS decisions (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    workflow_id         UUID         NOT NULL REFERENCES workflows(id),
    depot_id            UUID         NOT NULL,
    organization_id     UUID         NOT NULL,
    timestamp           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    inputs_hash         TEXT         NOT NULL,
    tool_calls          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    output              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    rule_applied        TEXT,
    disposition         TEXT         NOT NULL
        CHECK (disposition IN ('pending', 'approved', 'edited', 'rejected', 'auto_executed')),
    human_user_id       UUID,
    diff_if_edited      JSONB,
    parent_decision_id  UUID,
    PRIMARY KEY (id, timestamp)
);


-- Sidecar identity table to guarantee global uniqueness of decisions.id
-- safely under concurrency (hypertable unique constraints must include
-- the partition key, so we cannot enforce UNIQUE(id) directly on decisions).
-- `decision_ts` mirrors `decisions.timestamp` so rows age out on the same
-- 90-day floor as the decisions hypertable (chunk drops do not touch this
-- table; a Timescale user-defined job prunes by time).
CREATE TABLE IF NOT EXISTS decision_identity_keys (
    id           UUID         PRIMARY KEY,
    decision_ts  TIMESTAMPTZ NOT NULL
);

DO $upgrade_decision_identity_keys$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'decision_identity_keys'
          AND column_name = 'decision_ts'
    ) THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'decision_identity_keys'
    ) THEN
        ALTER TABLE decision_identity_keys ADD COLUMN decision_ts TIMESTAMPTZ;
        UPDATE decision_identity_keys k
        SET decision_ts = COALESCE(
            (SELECT MIN(d.timestamp) FROM decisions d WHERE d.id = k.id),
            TIMESTAMPTZ 'epoch'
        );
        ALTER TABLE decision_identity_keys ALTER COLUMN decision_ts SET NOT NULL;
    END IF;
END;
$upgrade_decision_identity_keys$ LANGUAGE plpgsql;

CREATE INDEX IF NOT EXISTS idx_decision_identity_keys_decision_ts
    ON decision_identity_keys (decision_ts);

SELECT create_hypertable(
    'decisions',
    'timestamp',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- 90-day retention (PRD §10.4: decisions are append-only and used for
-- trust, compliance, and graduation; align with the security_audit_log
-- retention floor).
SELECT add_retention_policy(
    'decisions',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_decisions_workflow_depot_time
    ON decisions (workflow_id, depot_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_org_time
    ON decisions (organization_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_id
    ON decisions (id);
CREATE INDEX IF NOT EXISTS idx_decisions_parent
    ON decisions (parent_decision_id)
    WHERE parent_decision_id IS NOT NULL;

-- ── Append-only enforcement (PRD §10.4) ──────────────────────────────────
-- Row-level trigger that blocks every UPDATE and DELETE on `decisions`.
-- Edits to a prior decision must be recorded as a NEW row whose
-- `parent_decision_id` references the original and whose `diff_if_edited`
-- carries the delta. There is no application path that bypasses this.
CREATE OR REPLACE FUNCTION decisions_append_only_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'decisions is append-only (PRD Depot Agent §10.4): % rejected. '
        'Record edits as a new row with parent_decision_id set.',
        TG_OP
    USING ERRCODE = 'check_violation';
END;
$$;



-- Enforce logical decision identity uniqueness on `id` even though the
-- hypertable primary key must include `timestamp`. We reserve IDs in a
-- sidecar table with a real PRIMARY KEY so concurrency is safely handled
-- by PostgreSQL's unique index machinery.
CREATE OR REPLACE FUNCTION decisions_enforce_unique_id()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO decision_identity_keys (id, decision_ts)
    VALUES (NEW.id, NEW.timestamp);

    RETURN NEW;
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION
            'duplicate decisions.id: % already exists',
            NEW.id
        USING ERRCODE = 'unique_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_decisions_unique_id ON decisions;
CREATE TRIGGER trg_decisions_unique_id
    BEFORE INSERT ON decisions
    FOR EACH ROW
    EXECUTE FUNCTION decisions_enforce_unique_id();

DROP TRIGGER IF EXISTS trg_decisions_append_only ON decisions;
CREATE TRIGGER trg_decisions_append_only
    BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW
    EXECUTE FUNCTION decisions_append_only_guard();


DROP TRIGGER IF EXISTS trg_decisions_append_only_truncate ON decisions;
CREATE TRIGGER trg_decisions_append_only_truncate
    BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT
    EXECUTE FUNCTION decisions_append_only_guard();

-- Prune sidecar keys in lockstep with the 90-day decisions retention window.
CREATE OR REPLACE PROCEDURE prune_decision_identity_keys(job_id int, config jsonb)
LANGUAGE plpgsql
AS $prune$
BEGIN
    DELETE FROM decision_identity_keys
    WHERE decision_ts < NOW() - INTERVAL '90 days';
    COMMIT;
END;
$prune$;

DO $register_prune_job$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        IF NOT EXISTS (
            SELECT 1
            FROM timescaledb_information.jobs
            WHERE proc_schema = 'public'
              AND proc_name = 'prune_decision_identity_keys'
        ) THEN
            PERFORM add_job(
                'prune_decision_identity_keys',
                INTERVAL '1 day',
                initial_start => NOW() + INTERVAL '1 minute'
            );
        END IF;
    END IF;
EXCEPTION
    WHEN undefined_function THEN
        RAISE NOTICE 'add_job unavailable: prune_decision_identity_keys not scheduled';
    WHEN undefined_table THEN
        RAISE NOTICE 'add_job skipped: timescaledb_information.jobs missing';
    WHEN OTHERS THEN
        RAISE NOTICE 'add_job skipped: %', SQLERRM;
END;
$register_prune_job$ LANGUAGE plpgsql;
