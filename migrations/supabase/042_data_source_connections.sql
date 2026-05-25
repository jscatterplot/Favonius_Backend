-- Migration 042 (Supabase): External data-source connections (Data Sources page).
--
-- A connection ties one external system (provider_key, e.g. 'kempower') to one
-- depot. The user creates it from the website by pasting credentials; the
-- backend stores the secret part Fernet-encrypted (encrypted_credentials) and
-- the non-secret part in plain JSONB (config). The ingestion runtime decrypts
-- on demand to connect and import the provider's inventory + history into the
-- existing chargers / vehicles / charging_sessions tables.
--
-- Wire-up:
--   1. POST /admin/data-sources/connections → insert a row (creds encrypted).
--   2. The scheduler (src/core/data_sources/scheduler.py) enqueues a job per
--      due connection; manual "Sync now" inserts one directly.
--   3. src/core/data_sources/ingestion.py decrypts and runs the provider.
--
-- Lives on the Supabase static pool so it can FK public.sites /
-- public.organizations (the depot's commercial/regulatory context). The data
-- the sync produces still lands in charging_stations / vehicles (static) and
-- charging_sessions (TimescaleDB) via the existing import path.
--
-- Idempotent (safe to re-run).

CREATE TABLE IF NOT EXISTS public.data_source_connections (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id         UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    site_id                 UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    provider_key            TEXT NOT NULL,
    display_name            TEXT,
    status                  TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'error', 'disabled')),
    -- Non-secret connection config (locationId, backfillSince, baseUrl, ...).
    config                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Fernet token over the secrets JSON. NEVER plaintext; NEVER returned by the API.
    encrypted_credentials   BYTEA NOT NULL,
    encryption_version      INTEGER NOT NULL DEFAULT 1,
    sync_interval_minutes   INTEGER NOT NULL DEFAULT 1440 CHECK (sync_interval_minutes >= 15),
    scheduled_sync_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    next_sync_at            TIMESTAMPTZ,
    last_run_at             TIMESTAMPTZ,
    last_status             TEXT,
    created_by              UUID,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dsc_org  ON public.data_source_connections (organization_id);
CREATE INDEX IF NOT EXISTS idx_dsc_site ON public.data_source_connections (site_id);

-- Scheduler hot path: connections due for a scheduled sync.
CREATE INDEX IF NOT EXISTS idx_dsc_due
    ON public.data_source_connections (next_sync_at)
    WHERE scheduled_sync_enabled AND status = 'active';

-- At most one live connection per (depot, provider). A soft-deleted ('disabled')
-- row frees the slot so the operator can reconnect.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dsc_site_provider_active
    ON public.data_source_connections (site_id, provider_key)
    WHERE status <> 'disabled';

-- Backend connects as the service role; follows the existing static-table convention.
ALTER TABLE public.data_source_connections DISABLE ROW LEVEL SECURITY;
