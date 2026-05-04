-- Per-turn audit trail for the natural-language depot agent.
-- Mirrors the optimization_runs shape: domain-specific run record with a
-- JSONB step trace and outcome status. Complements (does NOT replace)
-- audit_log; agent reads also write a row to audit_log via
-- write_admin_audit_row() so they appear in the existing admin audit feed.
--
-- Idempotent.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL,
    organization_id  UUID,                    -- NULL for cross-org admin runs
    depot_id         UUID,                    -- NULL when query spans depots
    user_message     TEXT NOT NULL,
    final_intent     TEXT,
    steps_json       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status           TEXT NOT NULL
        CHECK (status IN ('running', 'success', 'disambiguation', 'not_found', 'error')),
    duration_ms      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_user_created
    ON agent_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_org_created
    ON agent_runs (organization_id, created_at DESC);
