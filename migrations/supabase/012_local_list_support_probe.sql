-- Supabase migration 012: cache per-charger LocalAuthorizationList support.
--
-- Extends migration 011's charger local-auth-list state with three columns
-- that record the outcome of an OCPP 1.6 ``GetConfiguration`` probe issued
-- before ``SendLocalList``.
--
-- Some firmware (notably ABB Terra AC ``CDT_TACW11`` V1.8.x) accepts the
-- ``LocalAuthListEnabled`` / ``LocalPreAuthorize`` / ``AuthorizationCacheEnabled``
-- config keys but returns ``NotSupported`` for ``SendLocalList`` itself.
-- Without these columns we re-attempt the (failing) push on every reconnect
-- and the operator-facing ``local_list_last_status`` churns between empty
-- and ``NotSupported`` forever.
--
-- Columns
-- -------
-- * ``local_list_supported``
--     NULL  → never probed (or stale firmware; see below).
--     FALSE → probe negative OR ``SendLocalList`` returned ``NotSupported``
--             for this firmware. Sync is short-circuited until firmware
--             changes.
--     TRUE  → probe positive. Sync proceeds normally.
-- * ``local_list_probed_firmware``
--     The ``firmware_version`` reported in BootNotification at the time the
--     probe outcome above was recorded. When the next BootNotification
--     reports a different firmware string, the cached negative is treated
--     as stale and a fresh probe runs.
-- * ``local_list_probed_at``
--     Timestamp of the recorded probe outcome — operator-visible signal
--     of "when did we last actually ask this charger".
--
-- All columns are nullable; existing rows keep their current behavior
-- (NULL = probe on next BootNotification).

ALTER TABLE public.charging_stations
    ADD COLUMN IF NOT EXISTS local_list_supported       BOOLEAN,
    ADD COLUMN IF NOT EXISTS local_list_probed_firmware TEXT,
    ADD COLUMN IF NOT EXISTS local_list_probed_at       TIMESTAMPTZ;
