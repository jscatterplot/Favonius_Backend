-- Rename organization_users -> user_organizations to match the canonical
-- Supabase project schema (auth lifecycle owns the table name; this backend
-- holds a write-side cache that should mirror the upstream name).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'organization_users'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'user_organizations'
    )
    THEN
        ALTER TABLE organization_users RENAME TO user_organizations;
    END IF;
END $$;

COMMENT ON TABLE user_organizations IS
    'At most one organization per user (enforced by UNIQUE user_id). '
    'Cache mirrored JIT from JWT app_metadata; canonical source is Supabase.';
