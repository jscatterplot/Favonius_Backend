-- Migration 044 (Supabase): Per-org SQL-mode flag on organizations.
--
-- Replaces the AGENT_SQL_ORG_ALLOWLIST env-var with a column so access can
-- be toggled per org at runtime without a redeploy.  Default TRUE means every
-- existing org gets SQL mode; disabling an org is the exceptional path.

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS agent_sql_mode_enabled BOOLEAN NOT NULL DEFAULT TRUE;
