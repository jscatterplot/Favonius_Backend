-- Migration 028: Drop FK constraints from operational tables to static shadow tables.
--
-- Background: Static reference data (depots/sites, vehicles, organizations,
-- chargers) lives in Supabase (`pools.static`); this database (TimescaleDB,
-- `pools.ts`) holds operational and time-series data. Earlier migrations
-- (001, 015, 020, 022) created shadow copies of the static tables here and
-- added FK constraints from operational tables to them.
--
-- In production the shadows are never populated — the assembler reads static
-- data from Supabase, and tenant_mirror writes orgs/memberships to Supabase.
-- That makes every FK insert from a background path (controller snapshot
-- persist, readiness alert emission) fail with foreign_key_violation.
--
-- Scope of this migration: drop only the FKs that are actively breaking
-- production. The shadow tables themselves and the connector_status alerts
-- trigger remain in place because tests and migration 006 still depend on
-- them. A follow-up will retire the shadows once test infrastructure is
-- restructured.
--
-- After this migration, depot_id / organization_id columns on the affected
-- operational tables are unvalidated UUID references; tenancy is enforced
-- by application code (JWT vs. sites.organization_id).

ALTER TABLE optimization_input_snapshots
    DROP CONSTRAINT IF EXISTS fk_opt_input_snapshots_depot;

ALTER TABLE optimization_input_snapshots
    DROP CONSTRAINT IF EXISTS fk_opt_input_snapshots_org;

ALTER TABLE notification_alerts
    DROP CONSTRAINT IF EXISTS notification_alerts_organization_id_fkey;

ALTER TABLE notification_alerts
    DROP CONSTRAINT IF EXISTS notification_alerts_depot_id_fkey;
