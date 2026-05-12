-- Supabase migration 014: routes view (sprint 4 readiness tools)
--
-- The PRD (docs/PRD_Depot_Agent.md §5.1) sketches the operational graph
-- with a ``Route`` dataclass:
--
--     @dataclass
--     class Route:
--         id: str
--         energy_estimate_kwh: float
--         start_time: datetime
--         end_time: datetime
--         contract_id: Optional[str]
--
-- We don't yet have a customer-side routes system; ``schedules`` rows are
-- the per-day route representation in V1. Each ``schedules.route_id`` may
-- repeat across days, so the view exposes one row per (route_id) by
-- folding the per-day departure/return into the earliest start and latest
-- end seen for that route, with the most-recent ``energy_kwh`` as the
-- estimate. This gives the agent a stable Route shape it can join against
-- without having to know which sprint added the underlying physical
-- table.
--
-- ``contract_id`` is exposed as NULL — no upstream system populates it
-- yet, and surfacing the field keeps the agent compiler stable when the
-- contract table arrives.
--
-- Note: the view also surfaces ``site_id`` (depot scope), ``vehicle_id``,
-- and ``driver_id`` so the readiness tool can filter on the caller's
-- visible depots and join driver assignments without a second query.
-- ``route_id`` is the natural key from ``schedules``; rows where
-- ``route_id IS NULL`` are skipped because they have no stable Route
-- identity (they exist when a vehicle is scheduled out without an
-- assigned route, e.g. for maintenance trips — those flow through the
-- ``schedules`` table directly, not the routes view).

CREATE OR REPLACE VIEW public.routes AS
SELECT
    s.route_id                              AS id,
    MAX(s.energy_kwh)                       AS energy_estimate_kwh,
    MIN(s.departure_time)                   AS start_time,
    MAX(s.return_time)                      AS end_time,
    NULL::text                              AS contract_id,
    -- The fields below are not part of the PRD Route dataclass; they let
    -- the readiness tool scope by depot / vehicle / driver without a
    -- second join against schedules.
    MAX(v.site_id)                          AS site_id,
    MAX(s.vehicle_id)                       AS vehicle_id,
    MAX(s.driver_id)                        AS driver_id,
    MAX(s.required_soc)                     AS required_soc
FROM public.schedules s
JOIN public.vehicles  v ON v.id = s.vehicle_id
WHERE s.route_id IS NOT NULL
GROUP BY s.route_id;

COMMENT ON VIEW public.routes IS
    'Stable Route shape for the depot agent, aliased over the schedules '
    'table until a customer-side routes system ships. One row per '
    'route_id; energy_estimate_kwh is the most recent estimate, '
    'start_time/end_time bound the union of all per-day departures and '
    'returns for that route. See docs/PRD_Depot_Agent.md §5.1.';
