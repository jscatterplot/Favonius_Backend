-- Migration 047: Traffic-fine triage agent — uploaded fines + triage results.
--
-- Backs the Depot Agent "traffic_fine_triage" workflow (see
-- src/api/agent_workflows/traffic_fine.py + src/core/traffic_fines/). An
-- operator uploads a fine document; the workflow extracts the issuing
-- authority, amounts, early-payment deadline, and IBAN (multimodally), and the
-- deterministic evaluator decides whether the early-payment discount is closing
-- within the alert window (<48h) — in which case an alert is raised to the
-- Logistics Manager via the existing notifications pipeline (migration 022).
--
-- Operational table only. depot_id / organization_id are bare UUIDs with no FK
-- to the Supabase static tables (the two-database invariant). The raw document
-- is retained as BYTEA so a parse can be re-run without re-uploading.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, and an
-- ON CONFLICT DO NOTHING seed. Safe to re-run.

CREATE TABLE IF NOT EXISTS traffic_fines (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id                    UUID NOT NULL,
    organization_id             UUID NOT NULL,
    uploaded_by                 UUID,
    status                      TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN (
            'received', 'parsing', 'parsed', 'alerted',
            'no_alert', 'parse_failed', 'unsupported_media'
        )),
    -- Uploaded document.
    content_type                TEXT,
    file_name                   TEXT,
    byte_size                   INTEGER,
    raw_payload                 BYTEA,
    -- LLM-extracted fields (nullable until parsed).
    is_traffic_fine             BOOLEAN,
    issuing_authority           TEXT,
    issuing_country             TEXT,
    fine_reference              TEXT,
    currency                    TEXT,
    full_amount                 NUMERIC(12, 2),
    early_payment_amount        NUMERIC(12, 2),
    discount_amount             NUMERIC(12, 2),
    iban                        TEXT,
    early_payment_deadline      TIMESTAMPTZ,     -- resolved to a UTC instant
    early_payment_deadline_raw  TEXT,            -- as extracted, for display
    -- Deterministic evaluation.
    evaluation_kind             TEXT,            -- within_window|not_yet|expired|no_deadline|not_a_fine
    hours_until_deadline        DOUBLE PRECISION,
    within_alert_window         BOOLEAN,
    -- Links + audit.
    decision_id                 UUID,            -- the workflow Decision row (migration 037)
    alert_id                    UUID,            -- the notification_alerts row (migration 022)
    extraction                  JSONB,           -- full validated extraction payload
    error_detail                TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traffic_fines_depot_created
    ON traffic_fines (depot_id, created_at DESC);

-- Candidates for the deadline re-check sweep: parsed fines with a future
-- deadline that have not yet alerted.
CREATE INDEX IF NOT EXISTS idx_traffic_fines_sweep
    ON traffic_fines (early_payment_deadline)
    WHERE status IN ('parsed', 'no_alert') AND early_payment_deadline IS NOT NULL;

-- FK anchor: decisions.workflow_id REFERENCES workflows(id) (migration 037), so
-- the traffic-fine workflow must exist as a row before its Decisions are
-- written. The authoritative prompt + allowed_tools live in code
-- (src/api/agent_workflows/traffic_fine.py); this row exists only for
-- referential integrity. The fixed UUID matches TRAFFIC_FINE_WORKFLOW_ID there.
INSERT INTO workflows (id, name, version, prompt, allowed_tools)
VALUES (
    '7f1ce0a0-0000-4000-8000-000000000047',
    'traffic_fine_triage',
    '1',
    'Traffic-fine triage workflow. Authoritative definition lives in '
        || 'src/api/agent_workflows/traffic_fine.py; this row is an FK anchor '
        || 'for decisions.workflow_id.',
    ARRAY['record_fine_extraction']::TEXT[]
)
ON CONFLICT DO NOTHING;
