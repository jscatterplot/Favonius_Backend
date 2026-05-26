-- S4 — Per-org token-budget override storage (PLAN.md §S4 / Open Question 2).
--
-- Adds a general-purpose `metadata` JSONB bag to organizations so per-org
-- config can live on the row without a column-per-knob. First (and so far
-- only) consumer: the SQL-mode chat agent's monthly token budget, read at
-- src/api/agent/budget.py::resolve_org_token_budget as
--     metadata->>'agent_token_budget_monthly'
-- with precedence per-org override > env default
-- (AGENT_SQL_TOKEN_BUDGET_PER_ORG_MONTHLY).
--
-- Mirrors the established static-table JSONB-config precedent
-- (sites.billing_metadata / building_load_source, migration supabase/006):
-- NOT NULL DEFAULT '{}' so existing rows get an empty bag and the backend can
-- read keys without a NULL guard.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. The runner re-executes every file each
-- deploy.

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN organizations.metadata IS
    'General-purpose per-org config bag. Backend-consumed keys: '
    'agent_token_budget_monthly (positive-integer string; SQL-mode chat agent '
    'monthly token ceiling, overrides AGENT_SQL_TOKEN_BUDGET_PER_ORG_MONTHLY).';
