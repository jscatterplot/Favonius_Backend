-- Migration 036: Recompute charging_sessions.import_row_hash with the canonical
-- form that omits energy_delivered_kwh and revenue, so re-uploads with corrected
-- energy or cost merge into the same row via the UPSERT-with-fill-nulls path.
--
-- Companion runtime changes in src/api/main.py:
--   * _compute_import_row_hash() drops energy/revenue from the canonical tuple.
--   * _platform_import_hash_token() drops transaction_type (not persisted) and
--     switches to a length-prefixed canonical form so this SQL can reconstruct
--     the same token from charging_sessions columns alone.
--   * POST /admin/depots/{id}/charging-sessions/import switches from plain
--     INSERT to INSERT ... ON CONFLICT DO UPDATE with fill-nulls semantics.
--
-- This migration:
--   1. Drops the existing partial unique index on (site_id, import_row_hash)
--      so we can mutate hashes without violating uniqueness.
--   2. Computes the new hash for every row where source='import' (both raw-id
--      and platform-initiated variants).
--   3. Finds collision groups under the new hash. Within each group, the
--      earliest row by (start_time, session_id) is the survivor; the others
--      are folded into it using the same fill-nulls rules the runtime UPSERT
--      uses, then deleted. Counts are RAISE NOTICE'd so deploy logs surface
--      any merges.
--   4. Applies the new hashes.
--   5. Recreates the partial unique index and refreshes the column COMMENT.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Step 1: drop the unique index so we can rewrite hashes freely.
DROP INDEX IF EXISTS charging_sessions_import_dedup_idx;

-- Step 2: compute the new hash for every import row.
--
-- For non-platform rows the canonical id_token-for-hash is the raw id_token
-- value as stored. For platform-initiated rows we reconstruct the length-
-- prefixed inner hash from persisted columns (end_time / import_status /
-- import_user_full_name / import_station_owner) — identical bytes to the
-- Python helper.
CREATE TEMPORARY TABLE _new_import_hashes ON COMMIT DROP AS
WITH platform_inner AS (
    SELECT
        cs.session_id,
        encode(
            digest(
                octet_length(
                    COALESCE(to_char(cs.end_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS+00:00'), '')
                )::text || ':' ||
                COALESCE(to_char(cs.end_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS+00:00'), '') ||
                octet_length(COALESCE(cs.import_status, ''))::text || ':' ||
                COALESCE(cs.import_status, '') ||
                octet_length(COALESCE(cs.import_user_full_name, ''))::text || ':' ||
                COALESCE(cs.import_user_full_name, '') ||
                octet_length(COALESCE(cs.import_station_owner, ''))::text || ':' ||
                COALESCE(cs.import_station_owner, ''),
                'sha256'
            ),
            'hex'
        ) AS inner_hash
    FROM charging_sessions cs
    WHERE cs.source = 'import'
      AND cs.id_token = 'platform-start'
)
SELECT
    cs.session_id,
    cs.site_id,
    encode(
        digest(
            cs.site_id::text || '|' ||
            to_char(cs.start_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS+00:00') || '|' ||
            CASE
                WHEN cs.id_token = 'platform-start' THEN
                    'platform-start:' || pi.inner_hash
                ELSE cs.id_token
            END,
            'sha256'
        ),
        'hex'
    ) AS new_hash
FROM charging_sessions cs
LEFT JOIN platform_inner pi ON pi.session_id = cs.session_id
WHERE cs.source = 'import';

-- Step 3: detect collision groups and merge with fill-nulls semantics.
DO $$
DECLARE
    coll RECORD;
    other_id UUID;
    merge_count INTEGER := 0;
    delete_count INTEGER := 0;
BEGIN
    FOR coll IN
        SELECT
            cs.site_id,
            nh.new_hash,
            array_agg(cs.session_id ORDER BY cs.start_time, cs.session_id) AS members
        FROM charging_sessions cs
        JOIN _new_import_hashes nh ON nh.session_id = cs.session_id
        WHERE cs.source = 'import'
        GROUP BY cs.site_id, nh.new_hash
        HAVING COUNT(*) > 1
    LOOP
        merge_count := merge_count + 1;
        -- Fold each non-survivor into the survivor (the first element of the
        -- ordered array), then delete the non-survivor. Order of merges does
        -- not matter for the COALESCE rules but is deterministic.
        FOREACH other_id IN ARRAY coll.members[2:array_length(coll.members, 1)]
        LOOP
            UPDATE charging_sessions surv
            SET
                vehicle_id            = COALESCE(surv.vehicle_id, other.vehicle_id),
                driver_id             = COALESCE(surv.driver_id, other.driver_id),
                card_id               = COALESCE(surv.card_id, other.card_id),
                end_time              = COALESCE(surv.end_time, other.end_time),
                import_user_full_name = COALESCE(surv.import_user_full_name, other.import_user_full_name),
                import_station_owner  = COALESCE(surv.import_station_owner, other.import_station_owner),
                import_status         = COALESCE(surv.import_status, other.import_status),
                energy_delivered_kwh  = CASE
                    WHEN COALESCE(surv.energy_delivered_kwh, 0) > 0 THEN surv.energy_delivered_kwh
                    ELSE other.energy_delivered_kwh
                END,
                cost_total            = CASE
                    WHEN COALESCE(surv.cost_total, 0) > 0 THEN surv.cost_total
                    ELSE other.cost_total
                END
            FROM charging_sessions other
            WHERE surv.session_id = coll.members[1]
              AND other.session_id = other_id;

            DELETE FROM charging_sessions WHERE session_id = other_id;
            delete_count := delete_count + 1;
        END LOOP;
    END LOOP;

    IF merge_count > 0 THEN
        RAISE NOTICE 'Migration 036: merged % collision group(s); deleted % redundant row(s).',
            merge_count, delete_count;
    END IF;
END $$;

-- Step 4: apply the new hashes to every surviving import row.
UPDATE charging_sessions cs
SET import_row_hash = nh.new_hash
FROM _new_import_hashes nh
WHERE cs.session_id = nh.session_id
  AND cs.source = 'import';

-- Step 5: recreate the partial unique index under the new hash formula.
CREATE UNIQUE INDEX IF NOT EXISTS charging_sessions_import_dedup_idx
    ON charging_sessions (site_id, import_row_hash)
    WHERE source = 'import';

COMMENT ON COLUMN charging_sessions.import_row_hash IS
    'SHA-256 hex of (site_id|start_time_utc|id_token_for_hash). The id_token_for_hash is the raw rfid_label/id_tag for identified rows, or a length-prefixed inner hash of (end_time, import_status, import_user_full_name, import_station_owner) for platform-initiated rows. Excludes energy_delivered_kwh and cost_total so re-uploads with corrected metering merge into the same row via the UPSERT-with-fill-nulls path. See migration 036.';

COMMIT;
