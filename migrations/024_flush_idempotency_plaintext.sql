-- Security (H5): flush any in-flight charger onboarding idempotency rows
-- whose response_json may still embed a one-time plaintext credential from
-- the previous version of the onboarding endpoint.
--
-- Safe by construction: rows in this table expire after 30 minutes anyway,
-- and the worst-case effect is that any retry of an Idempotency-Key issued
-- in the rollout window will get a fresh credential. Operators can use
-- POST /admin/depots/{id}/chargers/{charger_id}/rotate_credentials to obtain
-- new credentials if needed.

DELETE FROM charger_onboarding_idempotency;

COMMENT ON COLUMN charger_onboarding_idempotency.response_json IS
    'Replay receipt for charger onboarding; never contains plaintext credentials (H5).';
