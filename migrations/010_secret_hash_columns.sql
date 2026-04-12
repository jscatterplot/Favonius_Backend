-- Add hash columns for authentication secrets.
-- Keeps legacy plaintext columns for backward compatibility while allowing
-- verification against hash-only values.

ALTER TABLE IF EXISTS auth_tokens
ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64);

ALTER TABLE IF EXISTS api_keys
ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64);

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Backfill hashes for existing records (SHA-256 of secret value).
DO $$
BEGIN
    IF to_regclass('public.auth_tokens') IS NOT NULL THEN
        UPDATE auth_tokens
        SET token_hash = encode(digest(token, 'sha256'), 'hex')
        WHERE token_hash IS NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.api_keys') IS NOT NULL THEN
        UPDATE api_keys
        SET api_key_hash = encode(digest(api_key, 'sha256'), 'hex')
        WHERE api_key_hash IS NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_auth_tokens_token_hash ON auth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(api_key_hash);
