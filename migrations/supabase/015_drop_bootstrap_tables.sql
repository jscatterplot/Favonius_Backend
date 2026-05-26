-- Drop stale bootstrap objects created by database_schema.py (not the migration runner).
-- All tables were verified empty on 2026-05-25 except charging_sessions_summary (178 rows;
-- frontend reads confirmed migrated to FastAPI endpoints that query TimescaleDB directly).
-- PostGIS extension is retained; only the two application-level geography columns are removed.

-- Views first (depend on tables below)
DROP VIEW IF EXISTS daily_energy_summary;
DROP VIEW IF EXISTS fleet_overview;

-- Function that referenced charging_sessions_summary
DROP FUNCTION IF EXISTS calculate_savings(uuid, date, date);

-- Stale tables (triggers and indexes cascade automatically)
DROP TABLE IF EXISTS charging_sessions_summary CASCADE;
DROP TABLE IF EXISTS charging_sessions_active CASCADE;
DROP TABLE IF EXISTS vehicle_realtime_state CASCADE;
DROP TABLE IF EXISTS vehicle_schedules CASCADE;
DROP TABLE IF EXISTS charging_schedules_config CASCADE;
DROP TABLE IF EXISTS api_usage CASCADE;

-- Dead columns on sites (2 live rows; none referenced in src/)
ALTER TABLE sites DROP COLUMN IF EXISTS location;                 -- GEOGRAPHY type; latitude/longitude float8 are canonical
ALTER TABLE sites DROP COLUMN IF EXISTS caiso_node_id;           -- CAISO adapter deprecated
ALTER TABLE sites DROP COLUMN IF EXISTS utility_account_number;  -- bootstrap leftover
ALTER TABLE sites DROP COLUMN IF EXISTS rate_schedule;           -- tariff_config JSONB is canonical
