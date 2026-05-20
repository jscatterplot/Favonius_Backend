-- Migration 040: Per-session electricity cost provenance.
--
-- Background: charging_sessions.cost_total exists (migration 032) but nothing
-- in the live OCPP path computes it. Historical imports landed cost_total=0.
-- This migration adds the provenance column the new cost calculator
-- (src/core/billing/session_cost.py) writes alongside cost_total, plus two
-- indexes the calculator and the optimizer's _get_prices both benefit from.
--
-- Provenance values (Literal in Python):
--   * granular         — telemetry covers ≥80% of session and reconciles
--                        within ±10% of energy_delivered_kwh; cost is the
--                        time_bucket('1 hour', ...) integral × prices.
--   * fallback_average — telemetry was missing or failed the gate;
--                        cost = energy_delivered_kwh × avg(price over window).
--   * unpriceable      — prices missing for >1h within the session window;
--                        cost_total stays NULL.
--   * pending_close    — end_time IS NULL; we never write this to the row
--                        (kept as a return value only).
--   * no_energy        — energy_delivered_kwh IS NULL/≤0 and no telemetry.
--   * no_depot         — site_id IS NULL on an imported row.
--   * manual           — sentinel for rows whose cost_total was set by a
--                        non-NULL, non-zero external source. The calculator
--                        treats this as immutable.
--
-- The partial index on (end_time) WHERE (cost_total IS NULL OR cost_total = 0)
-- is the backfill candidate scan. It also filters out the 'manual' sentinel so
-- the backfill never tries to re-price an externally-set cost.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS cost_total_source VARCHAR(32);

COMMENT ON COLUMN charging_sessions.cost_total_source IS
    'Provenance of cost_total. One of: granular | fallback_average | unpriceable | no_energy | no_depot | manual. Set by src/core/billing/session_cost.py.';

-- (Earlier drafts of this migration also added idx_prices_depot_time on the
-- legacy ``prices`` table. The new architecture reads from
-- ``electricity_prices`` keyed by ENTSO-E ``node_id`` instead — see
-- src/db/queries.py::fetch_prices_by_zone and the existing
-- idx_electricity_prices_node_time created in migration 034. No new
-- index is needed here.)

-- Partial index supporting the backfill candidate scan.
-- The WHERE clause matches the predicate in scripts/backfill_session_cost.py
-- so the planner can read the candidates from the index directly.
CREATE INDEX IF NOT EXISTS idx_charging_sessions_cost_missing
    ON charging_sessions (end_time)
    WHERE end_time IS NOT NULL
      AND (cost_total IS NULL OR cost_total = 0)
      AND cost_total_source IS DISTINCT FROM 'manual';
