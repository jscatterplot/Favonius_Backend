-- Migration 039: Drop driver / RFID shadow tables on TimescaleDB
--
-- Background:
--   Migration 018_fleet_identity.sql creates ``drivers``, ``rfid_cards``,
--   ``rfid_card_vehicle_assignments`` and ``rfid_card_driver_assignments``
--   on TimescaleDB inside a guard ``IF to_regclass('public.depots') IS NULL``.
--   Because mig 018 runs before mig 029 (which drops the ``depots`` shadow),
--   these tables were created on Tiger and then orphaned when 029 dropped
--   ``depots`` CASCADE — the FK constraints were removed but the tables
--   themselves stayed.
--
--   The canonical source of truth for driver/RFID identity is Supabase
--   (see ``migrations/supabase/004_fleet_identity.sql`` and
--   ``migrations/supabase/007_supabase_static_operational_tables.sql``).
--   In production the legacy WS handler's ``lookup_id_tag`` already
--   routes through ``timescale_client._static_pool()`` to the Supabase
--   pool (see ``src/websocket_handler/main.py`` set_supabase_client
--   wiring), so no runtime code reads these tables from Tiger.
--
-- This migration drops the Tiger shadows. Re-runs are idempotent
-- (``DROP TABLE IF EXISTS``); on a fresh init mig 018 will recreate the
-- tables and this migration will then drop them again — wasteful but safe,
-- and avoids editing the historical migration 018.

DROP TABLE IF EXISTS rfid_card_vehicle_assignments CASCADE;
DROP TABLE IF EXISTS rfid_card_driver_assignments CASCADE;
DROP TABLE IF EXISTS rfid_cards                    CASCADE;
DROP TABLE IF EXISTS drivers                       CASCADE;
