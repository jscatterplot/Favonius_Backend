-- Migration 037: workflow_decisions audit log for depot-agent workflows.
--
-- One row per Decision per the PRD §5.3 schema (the agent's structured
-- audit record):
--
--   Decision(id, workflow_id, depot_id, timestamp, inputs_hash, tool_calls,
--            output, rule_applied, disposition, human_user_id, diff_if_edited)
--
-- The Phase 1 "daily readiness check" workflow (PRD §6.1) writes one row
-- here each time the workflow runs — whether triggered manually via the
-- /agent-workflows REST endpoint or automatically by the WorkflowScheduler
-- ahead of the earliest scheduled departure.
--
-- Distinct from `agent_runs` (per-turn chat trace, mig 025): that table
-- captures one row per user message; this one captures one row per
-- workflow run. The /agent/* endpoints and the /agent-workflows endpoints
-- are deliberately separate trails so audit reviewers can filter on
-- either one.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS workflow_decisions (
    decision_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name    TEXT NOT NULL,                   -- e.g. 'daily_readiness_check'
    workflow_version TEXT NOT NULL DEFAULT 'v1',
    depot_id         UUID NOT NULL,
    organization_id  UUID,                            -- NULL for cross-org admin runs
    triggered_by     TEXT NOT NULL                    -- 'manual' | 'scheduler' | 'event'
        CHECK (triggered_by IN ('manual', 'scheduler', 'event')),
    triggered_by_user_id UUID,                        -- NULL when triggered_by != 'manual'
    inputs_hash      TEXT NOT NULL,                   -- sha256 of canonicalised input bundle
    tool_calls       JSONB NOT NULL DEFAULT '[]'::jsonb,
    output           JSONB NOT NULL,                  -- the §6.1 payload
    rule_applied     TEXT,                            -- graduation rule label (optional)
    disposition      TEXT NOT NULL DEFAULT 'pending'
        CHECK (disposition IN ('pending', 'approved', 'edited', 'rejected', 'auto_executed')),
    permission_tier  TEXT NOT NULL DEFAULT 'inform'
        CHECK (permission_tier IN ('inform', 'draft_and_wait', 'act_and_notify', 'autonomous')),
    human_user_id    UUID,                            -- whoever approved / edited / rejected
    diff_if_edited   TEXT,                            -- JSON Patch text or NULL
    status           TEXT NOT NULL DEFAULT 'success'  -- end-of-run status
        CHECK (status IN ('running', 'success', 'degraded', 'error')),
    duration_ms      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- "Recent decisions for this depot/workflow" — backs the audit log view
-- (PRD §7.3) and the 60-second idempotency cache used by
-- POST /agent-workflows/today/{depot_id}.
CREATE INDEX IF NOT EXISTS idx_workflow_decisions_depot_created
    ON workflow_decisions (workflow_name, depot_id, created_at DESC);

-- "All workflow runs for an organization, newest first" — backs the
-- /agent-workflows/{name}/decisions cross-depot audit view.
CREATE INDEX IF NOT EXISTS idx_workflow_decisions_org_created
    ON workflow_decisions (workflow_name, organization_id, created_at DESC);

COMMENT ON TABLE workflow_decisions IS
    'Per-run decision/audit log for depot-agent workflows (PRD §5.3 + §6.1). '
    'Distinct from agent_runs (per-chat-turn trace, mig 025).';
