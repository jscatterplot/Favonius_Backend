-- Migration 033: Reports and Agent Actions tables
--
-- reports     — persisted report rows (drafts through to approved exports)
-- agent_actions — proposed / shadow / executed agent-generated actions
--
-- Both tables live in the TimescaleDB (operational) schema.  All statements
-- are additive and idempotent so re-running on an existing DB is a no-op.

-- ── reports ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reports (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id      UUID        NOT NULL,
    title         TEXT        NOT NULL,
    kind          TEXT        NOT NULL
                  CHECK (kind IN (
                      'weekly_ops', 'monthly_savings', 'monthly_consumption',
                      'incident', 'compliance'
                  )),
    status        TEXT        NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'pending', 'approved')),
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at   TIMESTAMPTZ,
    approved_by   TEXT,
    export_url    TEXT,
    group_by      TEXT
                  CHECK (group_by IN ('card', 'vehicle')),
    -- Aggregated payload for monthly_consumption reports so the export
    -- endpoint can stream CSV without re-running the aggregation query.
    data          JSONB
);

CREATE INDEX IF NOT EXISTS idx_reports_depot_created
    ON reports (depot_id, created_at DESC);

-- ── agent_actions ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_actions (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id      UUID        NOT NULL,
    agent_type    TEXT        NOT NULL,
    action_class  TEXT        NOT NULL,
    mode          TEXT        NOT NULL
                  CHECK (mode IN ('shadow', 'proposed', 'auto_notify', 'auto_silent')),
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN (
                      'pending', 'executed', 'rejected',
                      'rolled_back', 'failed', 'shadow'
                  )),
    summary       TEXT        NOT NULL,
    entity_type   TEXT
                  CHECK (entity_type IN ('charger', 'vehicle', 'site', 'session')),
    entity_id     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ,
    payload       JSONB
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_depot_created
    ON agent_actions (depot_id, created_at DESC);

-- Idempotency for monthly report_draft scheduler emissions:
-- at most one action per (depot_id, periodStart) for report_draft actions,
-- regardless of status.  This lets ON CONFLICT DO NOTHING prevent duplicates
-- while still allowing a new action when the previous one was rejected
-- (which can only happen for a different period_start in the next month).
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_actions_report_draft_period
    ON agent_actions (depot_id, (payload->>'periodStart'))
    WHERE action_class = 'report_draft';
