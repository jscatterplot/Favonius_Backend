-- Mirror organizations tenancy for Supabase static schema (see migrations/015_organizations.sql).

CREATE TABLE IF NOT EXISTS organizations (
    organization_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS organization_users (
    user_id         UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations (organization_id) ON DELETE CASCADE,
    role            VARCHAR(50) NOT NULL DEFAULT 'customer_operator',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO organizations (organization_id, name)
VALUES (
    '00000000-0000-4000-8000-000000000001'::uuid,
    'Default Development Organization'
)
ON CONFLICT (organization_id) DO NOTHING;

ALTER TABLE depots
    ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations (organization_id);

UPDATE depots
SET organization_id = '00000000-0000-4000-8000-000000000001'::uuid
WHERE organization_id IS NULL;

ALTER TABLE depots
    ALTER COLUMN organization_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_depots_organization_id ON depots (organization_id);
