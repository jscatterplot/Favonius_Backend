-- Migration 030: Add provenance + dedup support for historical charging session imports.
--
-- The Reports → Energy accounting flow lets customer admins backfill historical
-- charging transactions from an exported XLSX (one row per session). Imported
-- rows live in the same charging_sessions table as live OCPP rows, distinguished
-- by `source = 'import'`.
--
-- All columns are additive and nullable (or default-backfilled), so existing live
-- rows keep working without rewrites.
--
-- The partial unique index makes re-uploading the same file idempotent:
-- duplicate rows hit the index and the API maps the violation to a stable
-- DUPLICATE_SESSION error code for the bulk-import dialog.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS site_id UUID,
    ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS import_batch_id UUID,
    ADD COLUMN IF NOT EXISTS import_row_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS import_user_full_name TEXT,
    ADD COLUMN IF NOT EXISTS import_station_owner TEXT,
    ADD COLUMN IF NOT EXISTS import_status TEXT;

COMMENT ON COLUMN charging_sessions.source IS
    'Origin of the row: ''live'' = OCPP runtime ingestion, ''import'' = backfilled via XLSX upload.';
COMMENT ON COLUMN charging_sessions.import_batch_id IS
    'UUID generated client-side once per uploaded file; groups all rows from a single import.';
COMMENT ON COLUMN charging_sessions.import_row_hash IS
    'SHA-256 hex of (site_id|start_time_utc|id_tag|energy_delivered_kwh|revenue) for re-upload dedup.';
COMMENT ON COLUMN charging_sessions.import_user_full_name IS
    'Free-text "User FullName" from the source export (traceability only, not resolved).';
COMMENT ON COLUMN charging_sessions.import_station_owner IS
    'Free-text "StationOwner FullName" from the source export (traceability only, not resolved).';
COMMENT ON COLUMN charging_sessions.import_status IS
    'Raw "Charge Status" cell from the source export (e.g. ''Charging'', ''Finished'').';

-- Idempotent re-uploads: the same file POSTed twice yields zero new rows and
-- N DUPLICATE_SESSION error_codes from the bulk-import dialog's perspective.
CREATE UNIQUE INDEX IF NOT EXISTS charging_sessions_import_dedup_idx
    ON charging_sessions (site_id, import_row_hash)
    WHERE source = 'import';

-- Speeds up the (site_id, start_time) filter used by /reports/depots/.../energy/*
-- once the WHERE clause is extended to include cs.site_id = $depot_id for imported
-- rows that have no matching charging_stations row.
CREATE INDEX IF NOT EXISTS charging_sessions_site_start_idx
    ON charging_sessions (site_id, start_time DESC)
    WHERE site_id IS NOT NULL;
