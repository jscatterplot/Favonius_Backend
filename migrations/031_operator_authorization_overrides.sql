-- Migration 031: Operator-initiated charging session authorization.
--
-- Lets a customer_admin authorize a charging session at a charger from the
-- platform UI without the driver having to scan an RFID card. Use cases:
--   * RFID reader hardware/firmware issue
--   * Driver doesn't have a card on hand
--   * Card scan returns Invalid and operator wants to override
--
-- Flow:
--   1. POST /admin/depots/{id}/chargers/{cid}/manual_authorize creates a row
--      here with a synthetic id_tag (e.g. "OP-<uuid>"), an expiry (default
--      60 s, max 300 s), and the calling operator's user_id.
--   2. Same endpoint enqueues a RemoteStartTransaction in
--      charging_command_queue; the WS handler dispatches it to the charger.
--   3. Charger sends Authorize and/or StartTransaction with the synthetic tag.
--   4. RFIDAuthorizationService.authorize finds the row here (after the
--      regular lookup_id_tag returns None), atomically marks it consumed,
--      and returns Accepted. Subsequent reuse of the same tag is rejected.
--
-- Audit: created_by + reason captured here; the audit_log table also gets a
-- charger.manual_authorize row from the API endpoint for cross-queries.

CREATE TABLE IF NOT EXISTS operator_authorization_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id      VARCHAR(255) NOT NULL,
    id_tag          VARCHAR(255) NOT NULL,
    connector_id    INTEGER NOT NULL,
    organization_id UUID NOT NULL,
    depot_id        UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    consumed_at     TIMESTAMPTZ,
    created_by      UUID NOT NULL,
    reason          TEXT,
    audit_metadata  JSONB
);

-- Authorization hot path: at most one unconsumed override for a given
-- (station, tag). The auth service finds and atomically consumes it via
-- UPDATE ... RETURNING *.
CREATE UNIQUE INDEX IF NOT EXISTS operator_overrides_active_uniq
    ON operator_authorization_overrides (station_id, id_tag)
    WHERE consumed_at IS NULL;

-- Cooldown query: did this connector get an unconsumed override in the last N
-- seconds? Backs the API's per-connector rate limit.
CREATE INDEX IF NOT EXISTS operator_overrides_recent_idx
    ON operator_authorization_overrides (station_id, connector_id, created_at DESC);

-- Admin display: "last manual override" panel on the charger detail page.
CREATE INDEX IF NOT EXISTS operator_overrides_org_recent_idx
    ON operator_authorization_overrides (organization_id, created_at DESC);

COMMENT ON TABLE operator_authorization_overrides IS
    'One-shot synthetic idTags minted by customer_admin to authorize a charging session without RFID. Consumed by RFIDAuthorizationService.';
COMMENT ON COLUMN operator_authorization_overrides.id_tag IS
    'Synthetic OCPP idTag, opaque to drivers. Format: OP-<uuid>.';
COMMENT ON COLUMN operator_authorization_overrides.consumed_at IS
    'Set when RFIDAuthorizationService accepts the tag. Subsequent reuse is rejected.';
COMMENT ON COLUMN operator_authorization_overrides.expires_at IS
    'Override is rejected after this timestamp even if unconsumed; the charger may still be processing the RemoteStartTransaction.';
