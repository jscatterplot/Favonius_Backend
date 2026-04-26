-- Add hash columns for authentication secrets.
-- Keeps legacy plaintext columns for backward compatibility while allowing
-- verification against hash-only values.

CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    token TEXT NOT NULL,
    token_hash VARCHAR(64),
    token_type VARCHAR(50) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used TIMESTAMPTZ,
    usage_count INTEGER NOT NULL DEFAULT 0,
    revoked_at TIMESTAMPTZ,
    UNIQUE(station_id, token_type)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    api_key VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(64),
    description TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    last_used TIMESTAMPTZ,
    usage_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(api_key)
);

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

CREATE INDEX IF NOT EXISTS idx_auth_tokens_station_id ON auth_tokens(station_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_token_hash ON auth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_station_id ON api_keys(station_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(api_key_hash);
