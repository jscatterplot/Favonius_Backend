-- Vehicle / fleet consumption support for the depot chat agent.
--
-- The agent's consumption_by_user intent gained a vehicle-subject path:
-- "how much power did the renault vans consume last night" expands one
-- mention to every matching vehicle and sums charging_sessions filtered
-- by `vehicle_id`. charging_sessions is a regular table (NOT a
-- TimescaleDB hypertable), so there is no chunk pruning on start_time;
-- without this index the fleet WHERE-clause (vehicle_id = ANY(...) plus
-- a time range) is a sequential scan once a customer has more than a few
-- weeks of data. This mirrors migration 026's driver/card indexes.
--
-- charging_sessions.vehicle_id is VARCHAR (it holds the vehicle UUID as
-- text on both the live OCPP and imported-session write paths), so the
-- index column type matches the `vehicle_id = ANY($1::text[])` predicate.
--
-- First-deploy form: plain CREATE INDEX IF NOT EXISTS. The migration
-- runner wraps each file in an implicit transaction, and CONCURRENTLY
-- cannot run inside one. For environments under traffic, the follow-up
-- cleanup is to swap to CREATE INDEX CONCURRENTLY in a separate
-- per-statement migration.
CREATE INDEX IF NOT EXISTS idx_sessions_vehicle_time
    ON charging_sessions (vehicle_id, start_time DESC)
    WHERE vehicle_id IS NOT NULL;
