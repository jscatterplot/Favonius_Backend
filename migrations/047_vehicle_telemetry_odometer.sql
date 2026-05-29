-- Migration 047: add CAN-bus odometer to vehicle_telemetry.
--
-- The telematics feed (src/adapters/navirec) now ingests each vehicle's CAN-bus
-- odometer alongside SoC/position. Weekly per-vehicle DISTANCE — the denominator
-- of the EV "price per kilometre" comparison report (ev_vs_diesel_tco) — is
-- computed from deltas of this column by src/core/billing/distance.py.
--
-- Design notes:
--   * Nullable: a single Navirec record may carry SoC, odometer, or both
--     (src/adapters/navirec/mapping.py::parse_navirec_point keeps a record with
--     either). An odometer-only row has soc = NULL; that is safe because
--     StateAssembler._get_vehicle_socs filters `soc IS NOT NULL` on both UNION
--     arms, so such rows never enter the optimizer's freshest-SoC merge.
--   * Unbounded growth: an odometer only ever increases (barring ECU resets,
--     which distance.py handles), so the CHECK is a simple non-negative floor —
--     no upper clamp here (garbage values are rejected at ingest by
--     mapping._coerce_odometer's _MAX_ODOMETER_KM guard).
--
-- Idempotent (safe to re-run): ADD COLUMN / CREATE INDEX IF NOT EXISTS.

ALTER TABLE vehicle_telemetry
    ADD COLUMN IF NOT EXISTS odometer_km DOUBLE PRECISION
        CHECK (odometer_km IS NULL OR odometer_km >= 0);

-- Partial index for the first/last-in-window odometer lookups in
-- compute_distances_for_depot (only odometer-bearing rows are relevant).
CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_odometer
    ON vehicle_telemetry (vehicle_id, time)
    WHERE odometer_km IS NOT NULL;
