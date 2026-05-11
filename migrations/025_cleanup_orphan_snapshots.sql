-- One-shot cleanup for orphan rows in optimization_input_snapshots.
--
-- Migration 020 originally added the snapshots → depots/organizations FKs
-- without NOT VALID and crash-looped when historical rows referenced rows
-- that had already been deleted (e.g. cascade fallout from an organization
-- delete). The companion fix on 020 added NOT VALID so the migration could
-- complete, but it leaves the orphans in place and the constraints in an
-- unvalidated state. This migration deletes the orphans and validates the
-- constraints so the table is consistent and new writes are FK-checked
-- against the same rules used for historical rows.
--
-- Idempotent: DELETE of non-existent rows is a no-op, and VALIDATE on an
-- already-validated constraint is a no-op.

DO $$
BEGIN
    -- Nothing to clean if the table doesn't exist yet.
    IF to_regclass('public.optimization_input_snapshots') IS NULL THEN
        RAISE NOTICE 'Skipping 025: optimization_input_snapshots does not exist';
        RETURN;
    END IF;

    -- depot_id FK is ON DELETE CASCADE; orphans only exist when the
    -- constraint wasn't in place at the time of the depot delete.
    IF to_regclass('public.depots') IS NOT NULL THEN
        DELETE FROM optimization_input_snapshots
        WHERE depot_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM depots d WHERE d.depot_id = optimization_input_snapshots.depot_id
          );
    END IF;

    -- organization_id FK is ON DELETE SET NULL; mirror that for any
    -- historical rows that predated the constraint.
    IF to_regclass('public.organizations') IS NOT NULL THEN
        UPDATE optimization_input_snapshots
           SET organization_id = NULL
         WHERE organization_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM organizations o
                WHERE o.organization_id = optimization_input_snapshots.organization_id
           );
    END IF;

    -- Validate the constraints now that the table is consistent.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_opt_input_snapshots_depot'
           AND NOT convalidated
    ) THEN
        ALTER TABLE optimization_input_snapshots
            VALIDATE CONSTRAINT fk_opt_input_snapshots_depot;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_opt_input_snapshots_org'
           AND NOT convalidated
    ) THEN
        ALTER TABLE optimization_input_snapshots
            VALIDATE CONSTRAINT fk_opt_input_snapshots_org;
    END IF;
END $$;
