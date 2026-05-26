-- S4 — Per-org monthly token-budget ceiling (PLAN.md §S4).
--
-- Cost-protection counter for the SQL-mode chat agent: one row per
-- (organization, billing month) accumulating the input/output tokens the agent
-- has spent. The in-process counter in src/api/agent/budget.py is authoritative
-- for a turn; this table is the durable, best-effort flush target AND the
-- cold-start hydration source. organization_id is a bare UUID (no FK to the
-- Supabase static `organizations` table) per the two-database invariant — this
-- is TimescaleDB operational data, alongside agent_runs.
--
-- period_yyyymm is the calendar month as 'YYYYMM' (e.g. '202605'), computed in
-- UTC by the writer. The index on it powers the period-scoped cleanup job
-- (bulk-delete months older than the retention window).
--
-- This migration also extends agent_runs.status to allow 'refused' so a budget
-- refusal is a first-class terminal status. The specific cause is carried by
-- failure_reason='budget_exceeded' (migration 045 +
-- src/api/agent/audit.py::classify_failure); 'refused' distinguishes a
-- pre-LLM cost refusal from a genuine 'error'.
--
-- Idempotent: the migration runner is stateless and re-executes every file on
-- each deploy; all DDL uses IF [NOT] EXISTS / DROP … IF EXISTS.

CREATE TABLE IF NOT EXISTS agent_token_usage (
    organization_id  UUID         NOT NULL,
    period_yyyymm    TEXT         NOT NULL,
    input_tokens     BIGINT       NOT NULL DEFAULT 0,
    output_tokens    BIGINT       NOT NULL DEFAULT 0,
    last_updated     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (organization_id, period_yyyymm)
);

COMMENT ON TABLE agent_token_usage IS
    'S4 per-org monthly token spend for the SQL-mode chat agent. One row per '
    '(organization_id, period_yyyymm=''YYYYMM'' UTC). Best-effort durable mirror '
    'of the in-process counter in src/api/agent/budget.py; hydrated on cold '
    'start. Not billing-grade — record_actual reconciles rough estimates.';

-- Cleanup-job index: delete/scan by month without touching organization_id.
CREATE INDEX IF NOT EXISTS idx_agent_token_usage_period
    ON agent_token_usage (period_yyyymm);

-- Allow 'refused' as a terminal agent_runs status (budget refusal). The inline
-- CHECK from migration 025 carries PostgreSQL's default name for a column
-- CHECK (agent_runs_status_check); DROP/ADD keeps this idempotent and
-- re-runnable across the stateless migration runner.
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check;
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('running', 'success', 'disambiguation', 'not_found', 'error', 'refused'));
