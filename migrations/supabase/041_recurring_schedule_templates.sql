-- Migration 041 (Supabase): Recurring vehicle schedule templates.
--
-- A template describes a daily-repeating route pattern for one vehicle. The
-- optimizer materialises one concrete trip per matching day in the horizon
-- (see src/core/scheduling/recurring.py). One-off rows in public.schedules
-- continue to flow through unchanged and override matching recurring trips
-- when their created_at is more recent than the template's.
--
-- Wire-up:
--   1. POST /admin/depots/{depot_id}/schedule/recurring → insert template row
--   2. POST .../occurrences/{yyyy-mm-dd}/cancel → insert cancellation row
--      keyed by (template_id, occurrence_date). Single-date skips only —
--      bulk / holiday-calendar logic is explicitly out of scope.
--   3. StateAssembler._get_schedules joins templates + cancellations and
--      expands into the assembled schedules list before VDV463 merge.
--
-- Tables live on the Supabase static pool alongside public.schedules,
-- public.sites, public.vehicles — same pool the existing manual schedule
-- endpoints write to.
--
-- Idempotent (safe to re-run).

-- ---------------------------------------------------------------------------
-- 1. Recurring template — the daily pattern definition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.recurring_schedule_template (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id                 UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    vehicle_id               UUID NOT NULL REFERENCES public.vehicles(id) ON DELETE CASCADE,
    route_id                 VARCHAR(100) NOT NULL,
    departure_time_of_day    TIME NOT NULL,
    return_time_of_day       TIME NOT NULL,
    days_of_week             TEXT[] NOT NULL,
    start_date               DATE NOT NULL,
    end_date                 DATE,
    required_soc             DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    energy_kwh               DOUBLE PRECISION,
    active                   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT recurring_schedule_template_required_soc_range
        CHECK (required_soc >= 0.99 AND required_soc <= 1.0),
    CONSTRAINT recurring_schedule_template_energy_kwh_nonneg
        CHECK (energy_kwh IS NULL OR energy_kwh >= 0),
    CONSTRAINT recurring_schedule_template_date_range
        CHECK (end_date IS NULL OR end_date >= start_date),
    CONSTRAINT recurring_schedule_template_distinct_times
        CHECK (departure_time_of_day <> return_time_of_day),
    CONSTRAINT recurring_schedule_template_days_nonempty
        CHECK (array_length(days_of_week, 1) IS NOT NULL),
    CONSTRAINT recurring_schedule_template_days_valid
        CHECK (
            days_of_week <@ ARRAY['mon','tue','wed','thu','fri','sat','sun']::TEXT[]
        )
);

CREATE INDEX IF NOT EXISTS idx_recurring_schedule_template_depot
    ON public.recurring_schedule_template (depot_id, active);

CREATE INDEX IF NOT EXISTS idx_recurring_schedule_template_vehicle
    ON public.recurring_schedule_template (vehicle_id);

-- ---------------------------------------------------------------------------
-- 2. Single-day cancellation — operator override of one occurrence.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.recurring_schedule_cancellation (
    template_id              UUID NOT NULL
        REFERENCES public.recurring_schedule_template(id) ON DELETE CASCADE,
    occurrence_date          DATE NOT NULL,
    cancelled_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cancelled_by_user_id     UUID,
    reason                   VARCHAR(280),
    PRIMARY KEY (template_id, occurrence_date)
);

CREATE INDEX IF NOT EXISTS idx_recurring_schedule_cancellation_date
    ON public.recurring_schedule_cancellation (occurrence_date);

-- ---------------------------------------------------------------------------
-- 3. Keep updated_at fresh on every UPDATE (matches the convention used by
--    drivers / rfid_cards tables in migration 007).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_recurring_schedule_template_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_recurring_schedule_template_updated_at
    ON public.recurring_schedule_template;

CREATE TRIGGER trg_touch_recurring_schedule_template_updated_at
    BEFORE UPDATE ON public.recurring_schedule_template
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_recurring_schedule_template_updated_at();
