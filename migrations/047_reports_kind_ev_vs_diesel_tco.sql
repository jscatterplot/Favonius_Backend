-- Migration 047: allow the 'ev_vs_diesel_tco' report kind.
--
-- Adds the new EV-vs-diesel comparison report kind to the kind CHECK on both
-- `reports` (migration 033) and `report_schedules` (migration 044). Those CHECKs
-- are INLINE column constraints, so Postgres auto-named them (typically
-- reports_kind_check / report_schedules_kind_check) — but we do NOT rely on the
-- name: each DO block looks the actual constraint up from pg_constraint by its
-- definition (the only CHECK on that table mentioning 'monthly_consumption'),
-- drops it, and re-adds the widened list. This is robust to a differently-named
-- constraint and fully idempotent (re-running drops+re-adds the same widened
-- constraint).
--
-- Does NOT edit the historical 033/044 files (they re-run unchanged on deploy).

-- ── reports.kind ──────────────────────────────────────────────────────────────
DO $$
DECLARE
    conname text;
BEGIN
    IF to_regclass('public.reports') IS NULL THEN
        RAISE NOTICE 'reports table absent; skipping kind CHECK widen';
        RETURN;
    END IF;
    SELECT c.conname INTO conname
    FROM pg_constraint c
    WHERE c.conrelid = 'public.reports'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) LIKE '%monthly_consumption%';
    IF conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.reports DROP CONSTRAINT %I', conname);
    END IF;
    ALTER TABLE public.reports
        ADD CONSTRAINT reports_kind_check CHECK (kind IN (
            'weekly_ops', 'monthly_savings', 'monthly_consumption',
            'incident', 'compliance', 'ev_vs_diesel_tco'
        ));
EXCEPTION WHEN duplicate_object THEN
    -- Re-run after a prior apply already added reports_kind_check: leave it.
    RAISE NOTICE 'reports_kind_check already present; skipping';
END$$;

-- ── report_schedules.kind ─────────────────────────────────────────────────────
DO $$
DECLARE
    conname text;
BEGIN
    IF to_regclass('public.report_schedules') IS NULL THEN
        RAISE NOTICE 'report_schedules table absent; skipping kind CHECK widen';
        RETURN;
    END IF;
    SELECT c.conname INTO conname
    FROM pg_constraint c
    WHERE c.conrelid = 'public.report_schedules'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) LIKE '%monthly_consumption%';
    IF conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.report_schedules DROP CONSTRAINT %I', conname);
    END IF;
    ALTER TABLE public.report_schedules
        ADD CONSTRAINT report_schedules_kind_check CHECK (kind IN (
            'weekly_ops', 'monthly_savings', 'monthly_consumption',
            'incident', 'compliance', 'ev_vs_diesel_tco'
        ));
EXCEPTION WHEN duplicate_object THEN
    RAISE NOTICE 'report_schedules_kind_check already present; skipping';
END$$;
