-- Disable RLS on tables that are accessed only through the backend REST API.
--
-- Root cause: the backend's asyncpg pool connects to Supabase via the
-- connection pooler (Supavisor), which does not set the JWT session context
-- that auth.uid() reads from. Every auth.uid()-gated SELECT policy therefore
-- evaluated to NULL and silently filtered every row, causing GET /me/depots
-- to return depots:[] for all users regardless of their organization_id.
--
-- These tables are not queried directly by the Supabase JS SDK; all frontend
-- access goes through the backend REST API, which enforces organization_id
-- scoping at the Python layer via verified JWT claims. The RLS policies below
-- were adding no additional security while actively breaking backend queries.
--
-- If direct frontend SDK access to these tables is ever introduced, RLS
-- policies should be re-enabled alongside a mechanism for the backend to
-- bypass them (e.g., a dedicated service role granted BYPASSRLS, or switching
-- to the direct Supabase PostgreSQL URL which lands as the postgres superuser
-- and bypasses RLS automatically when relforcerowsecurity = false).

ALTER TABLE public.sites               DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.charging_stations   DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.organizations       DISABLE ROW LEVEL SECURITY;
-- The table was renamed organization_users → user_organizations by migration 016.
-- On a fresh install migration 010 runs before 016, so we must handle both names.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'user_organizations'
    ) THEN
        ALTER TABLE public.user_organizations DISABLE ROW LEVEL SECURITY;
    ELSIF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'organization_users'
    ) THEN
        ALTER TABLE public.organization_users DISABLE ROW LEVEL SECURITY;
    END IF;
END $$;
ALTER TABLE public.vehicles            DISABLE ROW LEVEL SECURITY;
