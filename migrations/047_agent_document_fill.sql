-- Migration 047: Agent document-fill (collaborative DOCX/PDF templates).
--
-- Backs the depot chat agent's document-fill path: an operator uploads a
-- DOCX or PDF — either a blank template OR a finished old report — and the
-- agent collaborates over several turns to produce a filled copy, pulling
-- the real figures from the SQL-mode agent_views.* data tools. See
-- "Collaborative document-fill" in the agent module + the plan at
-- /root/.claude/plans/i-want-to-set-distributed-lerdorf.md.
--
-- Wire-up:
--   1. POST /agent/documents  (multipart) → stores a template row AND opens a
--      fill session (status='gathering'); returns {session_id, document_id, ...}.
--   2. POST /agent/turn { message, session_id } → _run_document_fill_turn drives
--      the multi-turn loop. mode='ask' saves a partial draft + open questions
--      (session→'awaiting_input', renders a preview output); mode='finalize'
--      renders the final output (session→'finalized').
--   3. GET /agent/documents/{output_id}/download → the rendered bytes.
--   4. GET /agent/documents/sessions/{session_id} → working state for the UI.
--
-- Three tables, all on TimescaleDB (db_pools.ts) alongside agent_runs. Per the
-- two-database invariant, organization_id / depot_id / *_by are BARE UUIDs
-- (no FK to the Supabase static tables). Idempotent (safe to re-run).

-- ---------------------------------------------------------------------------
-- 1. agent_document_templates — the uploaded source document (blob + metadata).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_document_templates (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID,                    -- NULL only for favonius_admin uploads
    depot_id          UUID,                    -- optional scope hint; NULL spans visible depots
    uploaded_by       UUID NOT NULL,           -- the caller's user_id (sub claim)
    kind              TEXT NOT NULL CHECK (kind IN ('docx', 'pdf')),
    -- For PDFs: 'acroform' = fillable form (layout preserved on fill);
    -- 'flat' = no form fields (best-effort text re-render, layout NOT
    -- preserved). NULL for docx.
    pdf_form_type     TEXT CHECK (pdf_form_type IN ('acroform', 'flat')),
    file_name         TEXT,
    file_size_bytes   BIGINT,
    content_sha256    TEXT,
    raw_payload       BYTEA,
    -- Fields/placeholders detected at upload: [{name, source}] where source is
    -- 'jinja' | 'placeholder' | 'acroform'. Empty for a finished old report
    -- (no blanks) — the agent proposes targeted replacements instead.
    detected_fields   JSONB NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT NOT NULL DEFAULT 'stored'
        CHECK (status IN ('stored', 'expired', 'failed')),
    idempotency_key   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_agent_document_templates_idempotency
    ON agent_document_templates (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_document_templates_org
    ON agent_document_templates (organization_id, created_at DESC);

COMMENT ON TABLE agent_document_templates IS
    'Uploaded source documents for the agent document-fill path. raw_payload may be nulled by retention after AGENT_DOC_FILL_RETENTION_DAYS; metadata is kept.';

-- ---------------------------------------------------------------------------
-- 2. agent_document_fill_sessions — the multi-turn collaboration state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_document_fill_sessions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id       UUID NOT NULL REFERENCES agent_document_templates(id) ON DELETE CASCADE,
    organization_id   UUID,
    depot_id          UUID,
    user_id           UUID NOT NULL,           -- owner; access is owner-only (or favonius_admin)
    status            TEXT NOT NULL DEFAULT 'gathering'
        CHECK (status IN ('gathering', 'awaiting_input', 'finalized', 'abandoned')),
    -- {field_values, replacements, open_questions, confirmations, notes}.
    draft             JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Trimmed [{role, text, run_id, ts}] of prior text turns (cap ~20),
    -- replayed into run_qa_turn(history=...) for conversation memory.
    message_log       JSONB NOT NULL DEFAULT '[]'::jsonb,
    latest_output_id  UUID,                    -- newest rendered preview/final
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_document_fill_sessions_user
    ON agent_document_fill_sessions (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_document_fill_sessions_org
    ON agent_document_fill_sessions (organization_id, updated_at DESC);

COMMENT ON TABLE agent_document_fill_sessions IS
    'Per-document collaborative fill session. status carries the awaiting-input lifecycle (agent_runs stays per-turn).';

-- ---------------------------------------------------------------------------
-- 3. agent_document_outputs — rendered filled documents (previews + final).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_document_outputs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id       UUID NOT NULL REFERENCES agent_document_templates(id) ON DELETE CASCADE,
    session_id        UUID REFERENCES agent_document_fill_sessions(id) ON DELETE CASCADE,
    run_id            UUID,                    -- the agent_runs turn that produced it
    organization_id   UUID,
    depot_id          UUID,
    kind              TEXT NOT NULL CHECK (kind IN ('docx', 'pdf')),
    output_kind       TEXT NOT NULL DEFAULT 'preview'
        CHECK (output_kind IN ('preview', 'final')),
    -- 'preserved' = original layout kept (docx / AcroForm fill); 'degraded' =
    -- re-rendered (flat PDF) so layout is NOT preserved.
    fidelity          TEXT NOT NULL DEFAULT 'preserved'
        CHECK (fidelity IN ('preserved', 'degraded')),
    file_name         TEXT,
    file_size_bytes   BIGINT,
    content_sha256    TEXT,
    raw_payload       BYTEA,
    field_values      JSONB NOT NULL DEFAULT '{}'::jsonb,
    replacements      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_document_outputs_session
    ON agent_document_outputs (session_id, created_at DESC);

COMMENT ON TABLE agent_document_outputs IS
    'Rendered filled documents (previews emitted each turn + the final). raw_payload may be nulled by retention after AGENT_DOC_FILL_RETENTION_DAYS.';
