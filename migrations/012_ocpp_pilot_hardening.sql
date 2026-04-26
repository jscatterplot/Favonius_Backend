-- Migration 012: OCPP 1.6 pilot-hardening
--
-- Adds sequences for OCPP 1.6 transactionId and chargingProfileId so the
-- handler does not regenerate colliding IDs across restarts. Adds the
-- vehicles.id_tag index needed by the Authorize lookup. Ensures the
-- station_credentials table exists (it was previously only defined in
-- src/websocket_handler/ocpp_schema.py, which is dead code — the
-- migrations/ runner is the only path that actually applies DDL in
-- production).
--
-- Idempotent: every statement uses IF NOT EXISTS or equivalent.
-- Reference: PRD Section 9.1, /root/.claude/plans/hmm-but-i-want-floating-rabin.md

-- ---------------------------------------------------------------------------
-- 1. Sequences
-- ---------------------------------------------------------------------------

-- OCPP 1.6 spec requires StartTransaction.transactionId to be a positive
-- integer. We give it 64 bits of headroom and start from 1.
CREATE SEQUENCE IF NOT EXISTS ocpp_transaction_id
    AS BIGINT
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO CYCLE;

-- chargingProfileId is an OCPP integer; using a sequence gives every push
-- a unique id so a charger can hold multiple stacked profiles concurrently.
CREATE SEQUENCE IF NOT EXISTS ocpp_charging_profile_id
    AS BIGINT
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO CYCLE;

-- ---------------------------------------------------------------------------
-- 2. vehicles.id_tag index — required by Authorize handler lookup
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS vehicles_id_tag_idx
    ON vehicles (id_tag)
    WHERE id_tag IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. station_credentials — Basic Auth lookup target for OCPPWebSocketServer
-- ---------------------------------------------------------------------------
-- Already referenced by src/websocket_handler/timescale_client.py:1753-1771
-- (validate_basic_auth → bcrypt.checkpw). The table definition lived only
-- in src/websocket_handler/ocpp_schema.py which is never imported, so on
-- a fresh database this table did not exist.

CREATE TABLE IF NOT EXISTS station_credentials (
    id              SERIAL PRIMARY KEY,
    station_id      VARCHAR(255) NOT NULL,
    username        VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used       TIMESTAMPTZ,
    UNIQUE (station_id, username)
);

CREATE INDEX IF NOT EXISTS station_credentials_station_id_idx
    ON station_credentials (station_id);
CREATE INDEX IF NOT EXISTS station_credentials_active_idx
    ON station_credentials (active) WHERE active = TRUE;
