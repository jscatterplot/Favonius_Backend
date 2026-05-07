-- Migration 034: track local-authorization-list state per charger.
--
-- Backs the offline RFID access feature: the WS handler pushes the approved
-- idTag list to each charger via OCPP 1.6 SendLocalList on BootNotification.
-- These columns are the backend's record of what version is currently on the
-- charger, when it last accepted a push, and the last response status — so a
-- restart can reconcile without forcing a full resync.
--
-- All columns are nullable / defaulted; existing rows are unaffected.

ALTER TABLE public.charging_stations
    ADD COLUMN IF NOT EXISTS local_list_version     INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS local_list_synced_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS local_list_last_status TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'charging_stations_local_list_version_nonneg'
    ) THEN
        ALTER TABLE public.charging_stations
            ADD CONSTRAINT charging_stations_local_list_version_nonneg
            CHECK (local_list_version >= 0);
    END IF;
END$$;
