-- Migration 009: vehicles.external_id is required and unique per depot.
--
-- Background: external_id is the customer's fleet number (e.g. "BUS-217").
-- The backend already requires it on POST /admin/depots/{id}/vehicles via the
-- VehicleIdentityBase Pydantic model. Migration 006 added the column as
-- nullable with a GLOBAL partial unique index — this is wrong for two
-- reasons:
--   1. The same fleet number is reusable across depots (Customer A's
--      "BUS-1" and Customer B's "BUS-1" should not collide).
--   2. GET /depots/{id}/vehicles returns external_id as a non-nullable
--      string in the response contract; nullable rows would force ugly
--      fallback chains in the frontend.
--
-- This migration:
--   1. Aborts if any existing rows have NULL external_id (defensive — at
--      time of writing the table is empty, but a misordered run elsewhere
--      could surface NULLs and we want a loud failure rather than silent
--      placeholder backfill).
--   2. Sets external_id NOT NULL.
--   3. Drops the global partial unique index from migration 006.
--   4. Creates a per-(site_id, external_id) unique index.

BEGIN;

DO $$
DECLARE
    _null_count INTEGER;
BEGIN
    SELECT count(*) INTO _null_count
      FROM public.vehicles
     WHERE external_id IS NULL;

    IF _null_count > 0 THEN
        RAISE EXCEPTION
            'Cannot apply migration 009: % vehicles have NULL external_id. '
            'Backfill them with the customer''s fleet number before re-running.',
            _null_count;
    END IF;
END$$;

ALTER TABLE public.vehicles
    ALTER COLUMN external_id SET NOT NULL;

DROP INDEX IF EXISTS public.vehicles_external_id_unique_idx;

CREATE UNIQUE INDEX IF NOT EXISTS vehicles_external_id_per_site_unique_idx
    ON public.vehicles (site_id, external_id);

COMMIT;
