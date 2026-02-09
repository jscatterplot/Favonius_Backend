-- Ensure telemetry has a primary key for ON CONFLICT
-- Idempotent: only adds constraint if missing.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'telemetry_pkey'
          AND conrelid = 'telemetry'::regclass
    ) THEN
        ALTER TABLE telemetry
        ADD CONSTRAINT telemetry_pkey PRIMARY KEY (time, vehicle_id);
    END IF;
END $$;
