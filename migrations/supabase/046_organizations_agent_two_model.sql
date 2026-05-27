-- Migration 046 (Supabase): Per-org two-model split flag on organizations.
--
-- PLAN.md §S5b (build-only spike). When TRUE, the depot chat agent's
-- consumption fast path routes its "explore" extraction call to
-- claude-haiku-4-5 while keeping the user-facing formatter on
-- claude-sonnet-4-6 (src/api/agent/llm_router.py::pick_model, resolved per org
-- by resolve_org_two_model_enabled). Default FALSE: the split is rolled out one
-- org at a time, so enabling an org is the exceptional path. Mirrors the
-- per-org knobs in 044 (agent_sql_mode_enabled) and 045
-- (agent_token_budget_monthly); there is intentionally no env var.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. The migration runner is stateless and
-- re-runs every file on each deploy.

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS agent_two_model_enabled BOOLEAN NOT NULL DEFAULT FALSE;
