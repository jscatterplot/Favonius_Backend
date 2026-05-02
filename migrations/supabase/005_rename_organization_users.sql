-- Mirror of migrations/024_rename_organization_users.sql for Supabase static schema.

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
