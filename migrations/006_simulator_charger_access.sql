-- Migration 006: Seed charger_vehicle_access for development simulation
--
-- Populates charger_vehicle_access so that mapping.py can build the
-- vehicle-to-charger map for Depot A. Without rows here, the MILP
-- optimizer runs but SetChargingProfile is never dispatched (empty map).
--
-- All vehicles in Depot A are granted access to all chargers in Depot A.
-- This is appropriate for simulation; production should reflect actual
-- physical bay layout.

-- Guarded with to_regclass so the migration is a no-op once the static
-- shadow tables are dropped (these tables now live in Supabase; the
-- TimescaleDB shadows are removed by migration 029 on main). Without the
-- guard, re-running migrations against a post-029 DB crashes with
-- `relation "charger_vehicle_access" does not exist`, which puts the
-- migration runner into a restart loop.
DO $$
BEGIN
    IF to_regclass('public.charger_vehicle_access') IS NULL THEN
        RAISE NOTICE 'Skipping migration 006: table charger_vehicle_access does not exist';
        RETURN;
    END IF;

    INSERT INTO charger_vehicle_access (charger_id, vehicle_id, is_accessible)
    SELECT
        c.charger_id,
        v.vehicle_id,
        TRUE
    FROM chargers c
    CROSS JOIN vehicles v
    WHERE c.depot_id = '550e8400-e29b-41d4-a716-446655440001'::uuid
      AND v.depot_id = '550e8400-e29b-41d4-a716-446655440001'::uuid
    ON CONFLICT (charger_id, vehicle_id) DO NOTHING;
END $$;
