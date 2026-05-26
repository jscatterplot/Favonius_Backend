-- Migration 043 (Supabase): Durable ingestion jobs for data-source connections.
--
-- One row per sync run. Created 'pending' by the trigger endpoint, the
-- scheduler, or the startup recovery sweep; advanced to 'running' when a worker
-- claims it; terminalised 'succeeded' | 'partial' | 'failed'. progress holds
-- live counters the status-poll endpoint surfaces.
--
-- Restart-safety: a worker stamps heartbeat_at periodically. On startup,
-- recover_orphaned_data_source_jobs (src/core/data_sources/scheduler.py)
-- re-kicks pending/running rows whose heartbeat is stale. Re-running is safe —
-- the import path dedups (ON CONFLICT DO NOTHING / DO UPDATE).
--
-- Lives on the Supabase static pool so it can FK public.data_source_connections
-- (migration 042) and public.sites / public.organizations. The charging_sessions
-- it produces live in TimescaleDB — no cross-DB FK; linked advisorily by
-- import_batch_id.
--
-- Idempotent (safe to re-run).

CREATE TABLE IF NOT EXISTS public.data_source_ingestion_jobs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id    UUID NOT NULL REFERENCES public.data_source_connections(id) ON DELETE CASCADE,
    organization_id  UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    site_id          UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    provider_key     TEXT NOT NULL,
    trigger          TEXT NOT NULL DEFAULT 'manual'
        CHECK (trigger IN ('manual', 'scheduled', 'recovery')),
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed')),
    progress         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_detail     TEXT,
    -- == charging_sessions.import_batch_id (TimescaleDB). Cross-DB link; advisory.
    import_batch_id  UUID,
    triggered_by     UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    heartbeat_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dsij_conn ON public.data_source_ingestion_jobs (connection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dsij_org  ON public.data_source_ingestion_jobs (organization_id, created_at DESC);

-- Recovery sweep: find stale non-terminal jobs by heartbeat.
CREATE INDEX IF NOT EXISTS idx_dsij_active
    ON public.data_source_ingestion_jobs (status, heartbeat_at)
    WHERE status IN ('pending', 'running');

-- Overlap guard: at most one non-terminal job per connection. The manual /sync
-- endpoint and the scheduler both race to insert; the loser gets a
-- UniqueViolation → 409 (endpoint) / silent skip (scheduler). DB-enforced, so
-- it holds even under WEB_CONCURRENCY > 1.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dsij_one_active_per_conn
    ON public.data_source_ingestion_jobs (connection_id)
    WHERE status IN ('pending', 'running');

ALTER TABLE public.data_source_ingestion_jobs DISABLE ROW LEVEL SECURITY;
