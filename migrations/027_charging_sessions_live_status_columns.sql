-- Add live-session fields read by GET /depots/{id}/chargers.
--
-- The charger list treats charging_sessions as optional runtime enrichment,
-- but the query contract should still match the table schema.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS current_power_kw DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS current_soc DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS target_soc DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS estimated_end_time TIMESTAMPTZ;

COMMENT ON COLUMN charging_sessions.current_power_kw IS
    'Latest observed charging power for an open session, in kW.';
COMMENT ON COLUMN charging_sessions.current_soc IS
    'Latest observed vehicle SoC for an open session, normalized 0.0 to 1.0 when known.';
COMMENT ON COLUMN charging_sessions.target_soc IS
    'Target vehicle SoC for an open session, normalized 0.0 to 1.0 when known.';
COMMENT ON COLUMN charging_sessions.estimated_end_time IS
    'Estimated completion timestamp for the current open charging session.';
