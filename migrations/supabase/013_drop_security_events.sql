-- Migration 013 (Supabase): Drop ``security_events`` from Supabase.
--
-- Background:
--   ``security_events`` is operational telemetry (failed-auth events
--   emitted by the OCPP WebSocket handler). The only writer in the
--   codebase is ``src/websocket_handler/timescale_client.py::store_security_event``
--   which writes via the TimescaleDB pool. The table was historically
--   provisioned on Supabase as well (mig supabase/003_charger_onboarding.sql
--   then re-asserted in supabase/007_supabase_static_operational_tables.sql),
--   but nothing reads or writes it from the Supabase side — the row count
--   on Supabase is 0 at the time of writing.
--
--   Canonical location is TimescaleDB (migrations/017_charger_onboarding.sql).
--   This migration removes the dead Supabase copy so the schema reflects
--   actual ownership.
--
-- Safe: zero rows on Supabase to date; no code in src/ reads this from the
-- static pool; idempotent via DROP TABLE IF EXISTS.

DROP TABLE IF EXISTS public.security_events CASCADE;
