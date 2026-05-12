-- Append-only audit table for depot-agent workflow runs.
-- Implements docs/PRD_Depot_Agent.md §5.3 (Decision schema) and §10.4
-- (audit immutability: no UPDATE, no DELETE; edits insert a new row
-- that references the original via edits_decision_id).
--
-- Distinct from agent_runs (depot chat) and audit_log (admin actions):
-- decisions captures one structured workflow output per turn, including
-- the full tool-call trace and the constraint-violation list filtered
-- out by the runtime's hard-constraint guard.
--
-- Idempotent.
CREATE TABLE IF NOT EXISTS decisions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id           TEXT NOT NULL,
    workflow_version      TEXT NOT NULL,
    depot_id              UUID NOT NULL,
    user_id               UUID NOT NULL,
    organization_id       UUID,
    permission_tier       TEXT NOT NULL
        CHECK (permission_tier IN (
            'inform',
            'draft_and_wait',
            'act_and_notify',
            'autonomous'
        )),
    inputs_hash           TEXT NOT NULL
        CHECK (char_length(inputs_hash) = 64),
    tool_calls            JSONB NOT NULL DEFAULT '[]'::jsonb,
    output                JSONB NOT NULL DEFAULT '{}'::jsonb,
    rule_applied          TEXT,
    disposition           TEXT NOT NULL DEFAULT 'pending'
        CHECK (disposition IN (
            'pending',
            'approved',
            'edited',
            'rejected',
            'auto_executed'
        )),
    constraint_violations JSONB NOT NULL DEFAULT '[]'::jsonb,
    edits_decision_id     UUID REFERENCES decisions(id),
    model_id              TEXT NOT NULL,
    latency_ms            INTEGER NOT NULL CHECK (latency_ms >= 0),
    input_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens         INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    stop_reason           TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decisions_workflow_depot_created
    ON decisions (workflow_id, depot_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_org_created
    ON decisions (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_user_created
    ON decisions (user_id, created_at DESC);

-- Append-only enforcement: block UPDATE and DELETE outright. Edits to a
-- decision must INSERT a new row whose edits_decision_id points at the
-- original (PRD §10.4).
CREATE OR REPLACE FUNCTION decisions_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = 'decisions is append-only (PRD §10.4); insert a new row with edits_decision_id set to the original instead of mutating',
        ERRCODE = 'P0001';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_block_update ON decisions;
CREATE TRIGGER decisions_block_update
    BEFORE UPDATE ON decisions
    FOR EACH ROW
    EXECUTE FUNCTION decisions_block_mutation();

DROP TRIGGER IF EXISTS decisions_block_delete ON decisions;
CREATE TRIGGER decisions_block_delete
    BEFORE DELETE ON decisions
    FOR EACH ROW
    EXECUTE FUNCTION decisions_block_mutation();
