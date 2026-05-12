-- Supabase migration 013: schedules.driver_id (sprint 4 readiness tools)
--
-- Sprint 4 of the Depot Agent product (see docs/PRD_Depot_Agent.md §6.1)
-- introduces the daily readiness workflow. That workflow needs to answer
-- "which driver is assigned to this route?" deterministically. Until a
-- customer-side route system lands, the existing ``schedules`` row is the
-- per-day route representation, so the driver assignment hangs off it.
--
-- Backfill stays NULL: assignments arrive from an external system later in
-- the sprint sequence; the readiness tool reports `driver_id=None` and
-- `shift_valid_for_route=true` (no evidence to invalidate) when the row has
-- no driver, matching the "tools never raise on empty data" contract.
--
-- The index supports the readiness tool's primary access pattern
-- (`WHERE driver_id = $1 AND departure_time BETWEEN ...`) so the driver
-- lookup never table-scans even as schedules grows.

ALTER TABLE public.schedules
    ADD COLUMN IF NOT EXISTS driver_id UUID
        REFERENCES public.drivers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_schedules_driver_depart
    ON public.schedules (driver_id, departure_time)
    WHERE driver_id IS NOT NULL;

COMMENT ON COLUMN public.schedules.driver_id IS
    'Driver assigned to the route on this schedule row. Nullable: per-day '
    'driver assignments are populated by an external route system in a '
    'later sprint. ON DELETE SET NULL preserves historical schedules when '
    'a driver row is removed.';
