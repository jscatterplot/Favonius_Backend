-- Migration 014: Queue-mediated SetChargingProfile dispatch.
--
-- Sessions 1+2 introduced charging_command_queue (migration 013) as the
-- offline buffer replayed on BootNotification. Session 3 makes the queue
-- the *primary* delivery path for SetChargingProfile so the optimizer no
-- longer depends on the in-process OCPPServer (production runs the
-- FastAPI service with OCPP_SERVER_ENABLED=false, so the in-process
-- dispatch silently dropped every schedule).
--
-- Changes:
--   1. Add 'sent' to the status check constraint. The new queue consumer
--      marks rows 'sent' once the WebSocket push to the charger returns
--      Accepted. ('acked' is reserved for an OCPP-conf level ack that we
--      may surface later; both states represent successful delivery.)
--   2. Add a TRIGGER that pg_notify()s 'charging_command_queue' on every
--      INSERT so a LISTEN-based consumer can drain the queue without
--      polling. The polling consumer in
--      websocket_handler/charging_profile_manager.py works without this
--      trigger; it's an opt-in latency win for future LISTEN consumers.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- 1. Status constraint: allow 'sent'
-- ---------------------------------------------------------------------------
ALTER TABLE charging_command_queue
    DROP CONSTRAINT IF EXISTS charging_command_queue_status_chk;

ALTER TABLE charging_command_queue
    ADD CONSTRAINT charging_command_queue_status_chk
    CHECK (status IN ('pending', 'sent', 'acked', 'failed', 'expired'));

-- ---------------------------------------------------------------------------
-- 2. Notify trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION charging_command_queue_notify() RETURNS trigger AS $$
BEGIN
    -- Payload is queue_id + cp_id so a LISTEN-based consumer can immediately
    -- look up the connected charger without re-querying. asyncpg delivers
    -- the payload as a string; consumers parse "<queue_id>:<cp_id>".
    PERFORM pg_notify(
        'charging_command_queue',
        NEW.queue_id::text || ':' || NEW.charge_point_id
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS charging_command_queue_notify_trg
    ON charging_command_queue;

CREATE TRIGGER charging_command_queue_notify_trg
    AFTER INSERT ON charging_command_queue
    FOR EACH ROW
    EXECUTE FUNCTION charging_command_queue_notify();
