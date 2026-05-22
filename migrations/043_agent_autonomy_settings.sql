-- Migration 043: Per-(depot, action_class) agent autonomy levels.
--
-- Backs:
--   * GET /depots/{depot_id}/autonomy-settings (matrix of action_class → level)
--   * `agents.autonomy.set` command (POST /commands/execute)
--
-- Distinct from migration 037's `workflow_tiers`, which is per-(workflow, depot)
-- and tracks trust-graduation rules for the Depot Agent's workflow runtime.
-- This table is per-(depot, action_class) and stores the operator-visible
-- autonomy level for proposed actions surfaced on the today view's agent
-- matrix.  Allowed levels mirror `agent_actions.mode`:
-- shadow | proposed | auto_notify | auto_silent.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS; safe to re-run.

CREATE TABLE IF NOT EXISTS agent_autonomy_settings (
    depot_id      UUID        NOT NULL,
    action_class  TEXT        NOT NULL,
    level         TEXT        NOT NULL
                  CHECK (level IN ('shadow', 'proposed', 'auto_notify', 'auto_silent')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by    UUID,
    PRIMARY KEY (depot_id, action_class)
);

CREATE INDEX IF NOT EXISTS idx_agent_autonomy_settings_depot
    ON agent_autonomy_settings (depot_id);

COMMENT ON TABLE agent_autonomy_settings IS
    'Per-(depot, action_class) autonomy level for agent-proposed actions. Backs GET /depots/{id}/autonomy-settings and the agents.autonomy.set command. Distinct from workflow_tiers (mig 037), which is per-(workflow, depot) and tracks Depot Agent trust graduation.';
