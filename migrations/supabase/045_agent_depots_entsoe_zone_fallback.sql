-- Supabase migration 045: agent_views.depots() — resolve entsoe_zone like the rest of the system.
--
-- Migration 040 exposed `entsoe_zone` from the explicit operator override
-- (sites.tariff_config->>'entsoe_zone') ONLY, deliberately skipping the
-- timezone fallback. That left the chat agent as the lone component whose
-- notion of a depot's bidding zone disagrees with billing / savings /
-- optimizer, all of which resolve it via src/db/queries.py:resolve_bidding_zone
-- → src/adapters/entsoe/mappings.py:get_bidding_zone. With tariff_config
-- unset (the common case), the agent saw entsoe_zone = NULL and could not
-- cross-link agent_views.prices_hourly($1) — every price/cost question
-- silently returned no rows or the wrong zone.
--
-- This migration replaces depots() so entsoe_zone mirrors resolve_bidding_zone's
-- cascade: explicit override → timezone-derived (single-zone countries) → NULL.
-- The timezone → EIC map below is a verbatim mirror of
-- src/adapters/entsoe/mappings.py:TIMEZONE_TO_BIDDING_ZONE (canonical source).
-- A unit test (tests/unit/test_agent_depots_entsoe_zone_map.py) asserts the two
-- stay in sync, so this duplication cannot silently drift.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS agent_views;

DROP FUNCTION IF EXISTS agent_views.depots(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.depots(p_depot_ids uuid[])
RETURNS TABLE (
    depot_id        uuid,
    name            text,
    timezone        text,
    currency        text,
    max_grid_kw     double precision,
    address         text,
    latitude        double precision,
    longitude       double precision,
    entsoe_zone     text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT s.id            AS depot_id,
           s.name::text,
           s.timezone::text,
           s.currency::text,
           s.max_grid_kw,
           s.address::text,
           s.latitude,
           s.longitude,
           COALESCE(
               NULLIF((s.tariff_config ->> 'entsoe_zone')::text, ''),
               (
                   SELECT z.zone
                   FROM (VALUES
                       -- >>> ENTSOE_TZ_ZONE_MAP (mirror of src/adapters/entsoe/mappings.py:TIMEZONE_TO_BIDDING_ZONE) >>>
                       ('Europe/Tirane',     '10YAL-KESH-----5'),
                       ('Europe/Vienna',     '10YAT-APG------L'),
                       ('Europe/Brussels',   '10YBE----------2'),
                       ('Europe/Sarajevo',   '10YBA-JPCC-----D'),
                       ('Europe/Sofia',      '10YCA-BULGARIA-R'),
                       ('Europe/Zagreb',     '10YHR-HEP------M'),
                       ('Europe/Prague',     '10YCZ-CEPS-----N'),
                       ('Europe/Copenhagen', '10YDK-1--------W'),
                       ('Europe/Tallinn',    '10Y1001A1001A39I'),
                       ('Europe/Helsinki',   '10YFI-1--------U'),
                       ('Europe/Paris',      '10YFR-RTE------C'),
                       ('Europe/Berlin',     '10Y1001A1001A82H'),
                       ('Europe/London',     '10YGB----------A'),
                       ('Europe/Athens',     '10YGR-HTSO-----Y'),
                       ('Europe/Budapest',   '10YHU-MAVIR----U'),
                       ('Europe/Dublin',     '10Y1001A1001A59C'),
                       ('Europe/Rome',       '10Y1001A1001A73I'),
                       ('Europe/Milan',      '10Y1001A1001A73I'),
                       ('Europe/Riga',       '10YLV-1001A00074'),
                       ('Europe/Vilnius',    '10YLT-1001A0008Q'),
                       ('Europe/Luxembourg', '10YLU-CEGEDEL-NQ'),
                       ('Europe/Malta',      '10Y1001A1001A93C'),
                       ('Europe/Podgorica',  '10YCS-CG-TSO---S'),
                       ('Europe/Amsterdam',  '10YNL----------L'),
                       ('Europe/Skopje',     '10YMK-MEPSO----8'),
                       ('Europe/Oslo',       '10YNO-1--------2'),
                       ('Europe/Warsaw',     '10YPL-AREA-----S'),
                       ('Europe/Lisbon',     '10YPT-REN------W'),
                       ('Europe/Bucharest',  '10YRO-TEL------P'),
                       ('Europe/Belgrade',   '10YCS-SERBIATSOV'),
                       ('Europe/Bratislava', '10YSK-SEPS-----K'),
                       ('Europe/Ljubljana',  '10YSI-ELES-----O'),
                       ('Europe/Madrid',     '10YES-REE------0'),
                       ('Europe/Stockholm',  '10Y1001A1001A46L'),
                       ('Europe/Zurich',     '10YCH-SWISSGRIDZ'),
                       ('Europe/Munich',     '10Y1001A1001A82H'),
                       ('Europe/Frankfurt',  '10Y1001A1001A82H'),
                       ('Europe/Hamburg',    '10Y1001A1001A82H'),
                       ('Europe/Dusseldorf', '10Y1001A1001A82H'),
                       ('Europe/Cologne',    '10Y1001A1001A82H'),
                       ('Europe/Marseille',  '10YFR-RTE------C'),
                       ('Europe/Lyon',       '10YFR-RTE------C'),
                       ('Europe/Barcelona',  '10YES-REE------0')
                       -- <<< ENTSOE_TZ_ZONE_MAP <<<
                   ) AS z(tz, zone)
                   WHERE z.tz = s.timezone
               )
           ) AS entsoe_zone
    FROM public.sites s
    WHERE s.id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.depots(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.depots(uuid[]) TO agent_reader_static;
