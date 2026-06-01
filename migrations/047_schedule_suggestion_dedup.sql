-- Migration 047: schedule_suggestion agent_action dedup index
--
-- The chat agent proactively proposes a scheduled report when it detects a
-- recurring consumption query in a user's recent history (see
-- src/api/agent/automation_suggestions.py). Each proposal is surfaced as an
-- agent_actions row with action_class='schedule_suggestion' (table created in
-- migration 033) and is approved/rejected via agents.action.approve/reject.
--
-- This adds a partial UNIQUE index so at most one *pending* suggestion exists
-- per (depot, signature). The emit helper relies on ON CONFLICT DO NOTHING for
-- race-safe single-flight dedup under concurrent turns — mirroring the
-- report_draft idempotency index from migrations 033/044.
--
-- The discriminator is a top-level `signatureKey` mirror written onto the
-- payload by emit_schedule_suggestion_action (kept top-level so the index
-- expression stays a simple ->> path, exactly like the report_draft index keys
-- on payload->>'periodStart'). Because the predicate is scoped to
-- status='pending', a previously rejected or executed suggestion does not block
-- a fresh proposal once the application-level cooldown elapses.
--
-- Additive + idempotent: agent_actions already exists (migration 033), so this
-- re-runs as a no-op on every deploy.

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_actions_schedule_suggestion_sig
    ON agent_actions (depot_id, (payload->>'signatureKey'))
    WHERE action_class = 'schedule_suggestion' AND status = 'pending';
