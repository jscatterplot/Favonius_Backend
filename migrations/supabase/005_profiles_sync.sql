-- Keep public.profiles in sync with auth.users metadata for registration flows.
-- Supports both snake_case and camelCase keys from sign-up forms.

CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    company TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION public.sync_profile_from_auth_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth
AS $$
DECLARE
    metadata JSONB;
BEGIN
    metadata := COALESCE(NEW.raw_user_meta_data, '{}'::jsonb);

    INSERT INTO public.profiles (id, email, first_name, last_name, company, created_at, updated_at)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(metadata->>'first_name', metadata->>'firstName', ''),
        COALESCE(metadata->>'last_name', metadata->>'lastName', ''),
        COALESCE(metadata->>'company', metadata->>'companyName', ''),
        COALESCE(NEW.created_at, NOW()),
        NOW()
    )
    ON CONFLICT (id) DO UPDATE
    SET
        email = EXCLUDED.email,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        company = EXCLUDED.company,
        updated_at = NOW();

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_profile_from_auth_user ON auth.users;
CREATE TRIGGER trg_sync_profile_from_auth_user
AFTER INSERT OR UPDATE OF email, raw_user_meta_data
ON auth.users
FOR EACH ROW
EXECUTE FUNCTION public.sync_profile_from_auth_user();

-- Backfill existing users.
INSERT INTO public.profiles (id, email, first_name, last_name, company, created_at, updated_at)
SELECT
    u.id,
    u.email,
    COALESCE(u.raw_user_meta_data->>'first_name', u.raw_user_meta_data->>'firstName', ''),
    COALESCE(u.raw_user_meta_data->>'last_name', u.raw_user_meta_data->>'lastName', ''),
    COALESCE(u.raw_user_meta_data->>'company', u.raw_user_meta_data->>'companyName', ''),
    COALESCE(u.created_at, NOW()),
    NOW()
FROM auth.users u
ON CONFLICT (id) DO UPDATE
SET
    email = EXCLUDED.email,
    first_name = EXCLUDED.first_name,
    last_name = EXCLUDED.last_name,
    company = EXCLUDED.company,
    updated_at = NOW();

-- Lock down profile data so it is never publicly readable.
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

-- Avoid broad table grants that could expose PII.
REVOKE ALL ON TABLE public.profiles FROM PUBLIC;
REVOKE ALL ON TABLE public.profiles FROM anon;
REVOKE ALL ON TABLE public.profiles FROM authenticated;
GRANT SELECT ON TABLE public.profiles TO authenticated;

-- Authenticated users can only read their own profile row.
DROP POLICY IF EXISTS profiles_select_own ON public.profiles;
CREATE POLICY profiles_select_own
ON public.profiles
FOR SELECT
TO authenticated
USING (auth.uid() = id);
