-- Snapshot sprint hardening: static-assumption building load, version
-- metadata, recent telemetry, FKs, retention.
--
-- 1. depots.building_load_assumption_kw — depot-level static derate used
--    when no live meter/forecast source is configured. The optimizer
--    treats this as a constant baseline load on the grid, so the site
--    power constraint (max_grid_kw) is still respected without a meter.
-- 2. optimization_input_snapshots gains:
--      - building_load_source CHECK accepts 'static_assumption'
--      - code_version / solver_version / surrogate_model_version columns
--      - recent_telemetry JSONB capturing the last hour of telemetry rows
--        that drove this optimization (replay-friendly)
--      - foreign keys to depots and organizations (when those tables
--        live in the same DB; production runs Supabase + TimescaleDB
--        as separate pools, so FK is conditional)
--      - composite PK (snapshot_id, captured_at) so the table can be
--        promoted to a TimescaleDB hypertable on captured_at
--      - hypertable + 90-day retention policy on captured_at
--
-- HRX onboarding: building_load_assumption_kw is set at depot setup time;
-- the readiness check downgrades runs to 'degraded' when the source is
-- 'static_assumption'.

-- 1. Depot-level static building-load derate
ALTER TABLE depots
    ADD COLUMN IF NOT EXISTS building_load_assumption_kw DOUBLE PRECISION NOT NULL DEFAULT 0.0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'depots_building_load_assumption_nonneg'
    ) THEN
        ALTER TABLE depots
            ADD CONSTRAINT depots_building_load_assumption_nonneg
            CHECK (building_load_assumption_kw >= 0.0);
    END IF;
END$$;

-- 2. Snapshot table — accept the new source name
ALTER TABLE optimization_input_snapshots
    DROP CONSTRAINT IF EXISTS optimization_input_snapshots_building_load_source_check;

ALTER TABLE optimization_input_snapshots
    ADD CONSTRAINT optimization_input_snapshots_building_load_source_check
    CHECK (building_load_source IN ('meter', 'forecast_fallback', 'static_assumption', 'absent'));

-- 3. Version metadata + recent telemetry payload
ALTER TABLE optimization_input_snapshots
    ADD COLUMN IF NOT EXISTS code_version VARCHAR(40),
    ADD COLUMN IF NOT EXISTS solver_version VARCHAR(40),
    ADD COLUMN IF NOT EXISTS surrogate_model_version VARCHAR(40),
    ADD COLUMN IF NOT EXISTS recent_telemetry JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 4. Foreign keys (only add when the referenced table exists in this DB).
-- NOT VALID skips validation of pre-existing rows. Production has orphaned
-- depot_ids in optimization_input_snapshots because the depots/organizations
-- shadow tables were never populated (Supabase owns canonical static data).
-- These FKs are dropped immediately by 028_drop_static_shadow_fks.sql, so
-- the relaxed validation only matters for the moments between 020 and 028.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = 'depots'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_opt_input_snapshots_depot'
    ) THEN
        ALTER TABLE optimization_input_snapshots
            ADD CONSTRAINT fk_opt_input_snapshots_depot
            FOREIGN KEY (depot_id) REFERENCES depots(depot_id) ON DELETE CASCADE
            NOT VALID;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = 'organizations'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_opt_input_snapshots_org'
    ) THEN
        ALTER TABLE optimization_input_snapshots
            ADD CONSTRAINT fk_opt_input_snapshots_org
            FOREIGN KEY (organization_id) REFERENCES organizations(organization_id) ON DELETE SET NULL
            NOT VALID;
    END IF;
END$$;

-- 5. Promote to a TimescaleDB hypertable (only when the extension is
-- present — local dev/test sometimes runs vanilla Postgres). A hypertable
-- requires every UNIQUE constraint to include the partitioning column,
-- so we swap the PK to (snapshot_id, captured_at) before calling
-- create_hypertable. Existing snapshot_id lookups still work via the
-- new btree index added below.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        IF NOT EXISTS (
            SELECT 1 FROM timescaledb_information.hypertables
            WHERE hypertable_name = 'optimization_input_snapshots'
        ) THEN
            ALTER TABLE optimization_input_snapshots
                DROP CONSTRAINT optimization_input_snapshots_pkey;
            ALTER TABLE optimization_input_snapshots
                ADD CONSTRAINT optimization_input_snapshots_pkey
                PRIMARY KEY (snapshot_id, captured_at);

            PERFORM create_hypertable(
                'optimization_input_snapshots',
                'captured_at',
                chunk_time_interval => INTERVAL '7 days',
                if_not_exists => TRUE,
                migrate_data => TRUE
            );
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_retention'
              AND hypertable_name = 'optimization_input_snapshots'
        ) THEN
            PERFORM add_retention_policy(
                'optimization_input_snapshots',
                INTERVAL '90 days',
                if_not_exists => TRUE
            );
        END IF;
    END IF;
END$$;

-- snapshot_id-only index keeps link_snapshot_to_run fast even though
-- the PK is now composite.
CREATE INDEX IF NOT EXISTS idx_opt_input_snapshots_snapshot_id
    ON optimization_input_snapshots (snapshot_id);
