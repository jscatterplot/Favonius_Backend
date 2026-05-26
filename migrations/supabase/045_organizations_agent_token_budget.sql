-- S4 — Per-org monthly token budget for the SQL-mode chat agent (PLAN.md §S4).
--
-- The agent's monthly token ceiling is a per-company commercial attribute
-- (different customers buy different amounts), so it lives as a first-class
-- column on organizations alongside subscription_tier — queryable and editable
-- directly (e.g. in the Supabase table editor), not buried in a JSONB blob or a
-- process env var.
--
-- Read at src/api/agent/budget.py::resolve_org_token_budget: a non-NULL,
-- positive value here is the org's ceiling; NULL (or no value) falls back to the
-- hard-coded DEFAULT_TOKEN_BUDGET_MONTHLY (10,000,000) in code — the platform
-- default for orgs that haven't negotiated a specific amount.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + a guarded named CHECK. The runner
-- re-executes every file each deploy.

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS agent_token_budget_monthly BIGINT;

-- Named CHECK (PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS); guard against
-- re-runs via pg_constraint. NULL is allowed (means "use the platform default").
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'organizations_agent_token_budget_monthly_check'
          AND conrelid = 'organizations'::regclass
          AND contype = 'c'
    ) THEN
        ALTER TABLE organizations
            ADD CONSTRAINT organizations_agent_token_budget_monthly_check
            CHECK (agent_token_budget_monthly IS NULL OR agent_token_budget_monthly > 0);
    END IF;
END $$;

COMMENT ON COLUMN organizations.agent_token_budget_monthly IS
    'SQL-mode chat agent monthly token ceiling for this org. NULL = use the '
    'platform default (DEFAULT_TOKEN_BUDGET_MONTHLY in src/api/agent/budget.py). '
    'Positive integer; enforced by src/api/agent/budget.py.';
