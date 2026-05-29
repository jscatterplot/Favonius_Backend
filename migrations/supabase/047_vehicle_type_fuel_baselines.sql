-- Migration 047 (Supabase): per-vehicle-type diesel fuel-consumption baselines.
--
-- The EV-vs-diesel "price per kilometre" comparison report needs the litres/100km
-- an equivalent DIESEL vehicle would burn, per vehicle type. This is reference/
-- config data (like vehicles themselves) so it lives on the Supabase static pool,
-- honouring the two-DB invariant. src/db/queries.py::resolve_fuel_baselines reads
-- it, falling back to hard-coded DEFAULT_FUEL_BASELINES so the report never blocks
-- on missing config.
--
-- Scoping (resolve_fuel_baselines precedence, most specific wins):
--   site_id + vehicle_type  → that type at that depot
--   org   + vehicle_type    → that type org-wide
--   site_id, vehicle_type NULL → that depot's fleet default
--   org,    vehicle_type NULL → the org's fleet default
-- A NULL vehicle_type row is the "fleet default" for its scope.
--
-- RLS: backend tables on this project run with RLS disabled (see
-- migrations/supabase/010_disable_rls_backend_tables.sql); no policy needed.
--
-- Idempotent (safe to re-run).

CREATE TABLE IF NOT EXISTS public.vehicle_type_fuel_baselines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    site_id             UUID REFERENCES public.sites(id) ON DELETE CASCADE,  -- NULL = org-wide
    vehicle_type        VARCHAR(50),                                          -- NULL = fleet default
    diesel_l_per_100km  DOUBLE PRECISION NOT NULL CHECK (diesel_l_per_100km > 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One baseline per (org, site-or-org-wide, type-or-fleet-default). COALESCE
    -- the nullables to sentinels so NULL site/type still participates in the
    -- uniqueness (NULLs are otherwise distinct in a UNIQUE constraint).
    UNIQUE (
        organization_id,
        COALESCE(site_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(vehicle_type, '')
    )
);

CREATE INDEX IF NOT EXISTS idx_vt_fuel_baselines_org
    ON public.vehicle_type_fuel_baselines (organization_id);
