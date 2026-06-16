-- Supabase migration 014: cache per-charger metering ChangeConfiguration state.
--
-- Background
-- ----------
-- After every BootNotification ``OCPP16Session._push_metering_config`` walks
-- a fixed allowlist of OCPP 1.6 ``ChangeConfiguration`` keys
-- (``MeterValuesSampledData``, ``StopTxnSampledData``,
-- ``MeterValueSampleInterval``) so we are guaranteed to see
-- ``Energy.Active.Import.Register`` samples regardless of factory defaults.
-- The list is static, so once a charger accepts every key on a given
-- firmware string there is nothing to re-push on later reconnects.
--
-- Without a per-firmware cache the handler re-runs the full sequence on
-- every WebSocket reconnect. Each ``ChangeConfiguration`` takes ~2 s on
-- ABB Terra AC, and a mid-bootstrap drop (observed at the pilot depot —
-- chargers reconnect every ~60 s) leaves the rest of the keys stranded
-- and the cycle loops indefinitely.
--
-- Columns
-- -------
-- * ``metering_config_applied_firmware``
--     The ``firmware_version`` reported in BootNotification at the time
--     ``_push_metering_config`` last completed without a hard failure
--     (all keys returned ``Accepted`` / ``RebootRequired``). When the
--     next BootNotification reports a different firmware string the
--     cache is treated as stale and the full sequence runs again.
-- * ``metering_config_applied_at``
--     Wall-clock timestamp of that last successful push — operator
--     visibility into "when did we last reconfigure metering on this
--     charger".
--
-- Both columns are nullable; existing rows keep their current behavior
-- (NULL = push on next BootNotification, matching the pre-migration
-- behaviour). Mirrors the pattern from migration 012
-- (``local_list_supported`` / ``local_list_probed_firmware`` /
-- ``local_list_probed_at``).

ALTER TABLE public.charging_stations
    ADD COLUMN IF NOT EXISTS metering_config_applied_firmware TEXT,
    ADD COLUMN IF NOT EXISTS metering_config_applied_at       TIMESTAMPTZ;
