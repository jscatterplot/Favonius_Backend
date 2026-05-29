-- Migration 047: Context-aware support summaries ("This isn't right" reports)
--
-- Purpose: store the bundle produced when a depot user clicks the frontend
-- "This isn't right" button — a UI screenshot, the last few minutes of API
-- logs (already PII-redacted by the app), a system-health snapshot
-- (Tiger Cloud / TimescaleDB + WebSocket server), and a pre-rendered
-- engineering summary sentence. Written by
-- ``src/observability/support_summary.py::persist_support_summary`` via the
-- TimescaleDB pool (``db_pools.ts``) — same pool as audit_log /
-- charger_log_imports. Row volume is low and access is by id / user_id, so a
-- plain Postgres table on Tiger is fine (NOT a hypertable).
--
-- GDPR notes (see src/observability/support_summary.py for the controls):
--   * Lawful basis: legitimate interest — user-initiated diagnostic support.
--   * Data minimisation: only the reporter's Supabase user_id (UUID) is
--     stored — never email / name / IP. user_note and logs_excerpt are passed
--     through redact_log_line() before insert.
--   * Retention: expires_at (default now() + 90 days) is hard-deleted by the
--     purge sweep (run_support_summary_purge_loop). Right-to-erasure is a hard
--     DELETE by id (admin) or by user_id (erase_support_summaries_for_user).
--   * No cross-DB FK: depot_id / user_id / organization_id are plain UUIDs
--     (the static reference tables live in Supabase, a separate database).
--
-- Idempotent: every statement uses IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS support_summaries (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Reporter's Supabase auth user id. Plain UUID, no FK. NOT the email.
    user_id                 UUID        NOT NULL,
    organization_id         UUID        NOT NULL,
    depot_id                UUID,                                  -- nullable: not all pages are depot-scoped
    page                    TEXT        NOT NULL,                  -- frontend route/page name (length-capped in app)
    user_note               TEXT,                                  -- optional free text (length-capped + redacted in app)
    -- Uploaded UI screenshot (binary). Same BYTEA approach as charger_log_imports.raw_payload.
    screenshot              BYTEA,
    screenshot_content_type TEXT,
    screenshot_size_bytes   INTEGER,
    screenshot_sha256       TEXT,
    -- Captured ~5-minute log excerpt, already PII-redacted before insert.
    logs_excerpt            TEXT,
    -- {"tiger_cloud": "...", "websocket": "...", "websocket_source": "...", "captured_at": "..."}
    health_snapshot         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- The exact engineering summary sentence, pre-rendered.
    summary_text            TEXT        NOT NULL,
    -- Delivery bookkeeping (kept inline; one row per report).
    delivery_status         TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (delivery_status IN ('pending', 'sent', 'failed', 'skipped')),
    delivery_detail         JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- GDPR retention horizon; the purge sweep hard-deletes rows past this.
    expires_at              TIMESTAMPTZ NOT NULL
);

-- Most-recent-first listing for engineering triage.
CREATE INDEX IF NOT EXISTS idx_support_summaries_created_at
    ON support_summaries (created_at DESC);

-- Erasure / lookups by reporter (right-to-erasure by user_id).
CREATE INDEX IF NOT EXISTS idx_support_summaries_user
    ON support_summaries (user_id, created_at DESC);

-- Per-org listing.
CREATE INDEX IF NOT EXISTS idx_support_summaries_org
    ON support_summaries (organization_id, created_at DESC);

-- Retention purge sweep driver.
CREATE INDEX IF NOT EXISTS idx_support_summaries_expires_at
    ON support_summaries (expires_at);

COMMENT ON TABLE support_summaries IS
    'Context-aware support reports ("This isn''t right"): screenshot + redacted '
    'log excerpt + health snapshot + engineering summary. GDPR-bounded via '
    'expires_at; minimised to user_id (no email). Distinct from audit_log.';

COMMENT ON COLUMN support_summaries.user_id IS
    'Reporter Supabase user id (UUID). Personal data — never stored alongside email/name/IP.';

COMMENT ON COLUMN support_summaries.expires_at IS
    'Retention horizon. run_support_summary_purge_loop hard-deletes rows where expires_at < now().';
