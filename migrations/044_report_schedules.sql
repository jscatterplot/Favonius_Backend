-- Migration 044: Scheduled reports
--
-- Backs the configurable report-scheduling feature (frontend PR #124):
--   report_schedules            — one row per configured schedule
--   report_schedule_recipients  — ordered recipient list per schedule
--   schedule_runs               — one row per fired (or attempted) slot
--   schedule_run_deliveries     — append-only per-recipient delivery ledger
--
-- Numbered 044 to avoid colliding with in-flight PR #233's 043. Per-depot
-- autonomy overrides are NOT created here: the worker reads PR #233's
-- `agent_autonomy_settings(depot_id, action_class, level)` for action_class
-- 'report_draft' (graceful fallback to the schedule's own autonomy_mode when
-- that table is absent), so there is a single autonomy matrix.
--
-- All tables live in the TimescaleDB (operational) schema alongside `reports`
-- and `agent_actions` (migration 033).  Like 033, depot_id / created_by are
-- bare UUID columns with NO foreign key to the Supabase `sites` / `auth.users`
-- tables — those live in a separate database.  Intra-database FKs (to reports
-- and between the new tables) are used normally.
--
-- All statements are additive and idempotent so re-running is a no-op.

-- ── report_schedules ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report_schedules (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID        NOT NULL,
    name            TEXT        NOT NULL,
    kind            TEXT        NOT NULL
                    CHECK (kind IN (
                        'weekly_ops', 'monthly_savings', 'monthly_consumption',
                        'incident', 'compliance'
                    )),
    group_by        TEXT        CHECK (group_by IN ('card', 'vehicle')),
    frequency       TEXT        NOT NULL
                    CHECK (frequency IN ('weekly', 'monthly', 'quarterly')),
    day_of_month    SMALLINT    CHECK (day_of_month BETWEEN 1 AND 28),
    day_of_week     SMALLINT    CHECK (day_of_week BETWEEN 0 AND 6),
    time_of_day     TIME        NOT NULL,
    autonomy_mode   TEXT        NOT NULL
                    CHECK (autonomy_mode IN (
                        'shadow', 'proposed', 'auto_notify', 'auto_silent'
                    )),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    next_run_at     TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,
    last_run_status TEXT        CHECK (last_run_status IN (
                        'succeeded', 'failed', 'skipped', 'pending_approval'
                    )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT report_schedules_cadence_ck CHECK (
        (frequency = 'weekly'
            AND day_of_week IS NOT NULL AND day_of_month IS NULL)
        OR
        (frequency IN ('monthly', 'quarterly')
            AND day_of_month IS NOT NULL AND day_of_week IS NULL)
    )
);

-- Worker scan path: only active, due schedules.
CREATE INDEX IF NOT EXISTS report_schedules_due_idx
    ON report_schedules (next_run_at) WHERE is_active;

-- List endpoint: schedules for a depot.
CREATE INDEX IF NOT EXISTS report_schedules_depot_idx
    ON report_schedules (depot_id);

-- ── report_schedule_recipients ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report_schedule_recipients (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id   UUID NOT NULL REFERENCES report_schedules(id) ON DELETE CASCADE,
    email_address TEXT NOT NULL,
    format        TEXT NOT NULL CHECK (format IN ('pdf', 'csv')),
    position      INT  NOT NULL,
    UNIQUE (schedule_id, email_address, format)
);

CREATE INDEX IF NOT EXISTS report_schedule_recipients_schedule_idx
    ON report_schedule_recipients (schedule_id, position);

-- ── schedule_runs ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedule_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id     UUID        NOT NULL REFERENCES report_schedules(id) ON DELETE CASCADE,
    report_id       UUID        REFERENCES reports(id) ON DELETE SET NULL,
    scheduled_for   TIMESTAMPTZ NOT NULL,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT        NOT NULL CHECK (status IN (
                        'succeeded', 'failed', 'skipped', 'pending_approval'
                    )),
    error_message   TEXT,
    -- Idempotency anchor for the cron tick: at most one run per fired slot.
    UNIQUE (schedule_id, scheduled_for)
);

-- Runs endpoint: most-recent first per schedule.
CREATE INDEX IF NOT EXISTS schedule_runs_schedule_recent_idx
    ON schedule_runs (schedule_id, triggered_at DESC);

-- ── schedule_run_deliveries ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schedule_run_deliveries (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID        NOT NULL REFERENCES schedule_runs(id) ON DELETE CASCADE,
    recipient_id        UUID        NOT NULL REFERENCES report_schedule_recipients(id) ON DELETE CASCADE,
    email_address       TEXT        NOT NULL,
    format              TEXT        NOT NULL CHECK (format IN ('pdf', 'csv')),
    status              TEXT        NOT NULL CHECK (status IN (
                            'sent', 'failed', 'bounced', 'suppressed'
                        )),
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider_message_id TEXT,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS schedule_run_deliveries_run_idx
    ON schedule_run_deliveries (run_id);

-- lastDelivery lookup (latest row per recipient) + webhook append by message id.
CREATE INDEX IF NOT EXISTS schedule_run_deliveries_recipient_recent_idx
    ON schedule_run_deliveries (recipient_id, attempted_at DESC);
CREATE INDEX IF NOT EXISTS schedule_run_deliveries_provider_msg_idx
    ON schedule_run_deliveries (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

-- ── agent_actions report_draft uniqueness (schedule-aware) ────────────────────
-- Migration 033 keyed report_draft idempotency on (depot_id, periodStart),
-- assuming a single hardcoded monthly scheduler.  The configurable feature
-- allows multiple schedules per depot to each emit a draft for the same
-- period, so the key gains a scheduleId discriminator.  Pre-existing rows from
-- the retired legacy scheduler carry no scheduleId and collapse to '_legacy',
-- preserving their original one-per-(depot, period) guarantee.

DROP INDEX IF EXISTS idx_agent_actions_report_draft_period;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_actions_report_draft_schedule_period
    ON agent_actions (
        depot_id,
        COALESCE(payload->>'scheduleId', '_legacy'),
        (payload->>'periodStart')
    )
    WHERE action_class = 'report_draft';
