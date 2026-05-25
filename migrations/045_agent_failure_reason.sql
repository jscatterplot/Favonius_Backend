-- S3.5 — Failure taxonomy for the depot chat agent (PLAN.md §S3.5).
--
-- Adds agent_runs.failure_reason: a nullable, CHECK-constrained text column
-- that makes turn failures countable. NULL on success and on graceful
-- non-failures (disambiguation / refusal); otherwise one of seven fixed
-- categories. The set is deliberately frozen — do NOT add to it without
-- revisiting PLAN.md Open Question 6 ("the seven categories are a guess;
-- revisit after a month of production data") AND the single mapping function
-- src/api/agent/audit.py::classify_failure, which is the ONLY place the
-- reason string literals are allowed to live.
--
-- The column is written by agent_runs_close() via the same UPDATE that stamps
-- `status`. The migration-042 restricted-update guard already permits it:
-- that guard is a DENYLIST (it rejects changes to run_id / user_id /
-- organization_id / depot_id / user_message / created_at) and failure_reason
-- is not in the denylist, so no trigger change is required. We only refresh
-- the guard's COMMENT, whose text from 042 still claims that "only status,
-- steps_json, duration_ms, final_intent" are mutable. The RAISE message inside
-- the function body is left untouched: it fires only when an *immutable*
-- column is mutated, never for failure_reason, so it cannot mislead at runtime.
--
-- Index note: PLAN.md §S3.5 specifies (organization_id, started_at,
-- failure_reason). agent_runs has no `started_at` column — `created_at` (set
-- at agent_runs_open, i.e. the start of the turn) is the equivalent, so the
-- index uses created_at.
--
-- Idempotent: the migration runner is stateless and re-executes every file on
-- each deploy.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS failure_reason text;

-- Named CHECK constraint (PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS;
-- guard against re-runs via pg_constraint). Naming it makes the eventual
-- Open-Question-6 revision a one-line DROP/ADD.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agent_runs_failure_reason_check'
    ) THEN
        ALTER TABLE agent_runs
            ADD CONSTRAINT agent_runs_failure_reason_check
            CHECK (
                failure_reason IS NULL
                OR failure_reason IN (
                    'validator_rejected',
                    'executor_timeout',
                    'empty_result',
                    'budget_exceeded',
                    'tool_error',
                    'llm_error',
                    'other'
                )
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agent_runs_org_started_failure
    ON agent_runs (organization_id, created_at, failure_reason);

COMMENT ON COLUMN agent_runs.failure_reason IS
    'S3.5 failure taxonomy (PLAN.md §S3.5). NULL on success / graceful '
    'non-failures; otherwise one of validator_rejected, executor_timeout, '
    'empty_result, budget_exceeded, tool_error, llm_error, other. Written '
    'exclusively via src/api/agent/audit.py::classify_failure.';

-- Refresh the 042 guard COMMENT so it no longer omits failure_reason from the
-- mutable set. Function body intentionally unchanged (see header note).
COMMENT ON FUNCTION agent_runs_restricted_update_guard() IS
    'PRD Depot Agent §10.4 + agent SQL S3/S3.5: only the orchestrator-mutable '
    'columns (status, steps_json, duration_ms, final_intent, failure_reason) '
    'may be updated; steps_json is append-only.';
