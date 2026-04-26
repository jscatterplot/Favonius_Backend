"""Regression tests for auth secret hash migration."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_secret_hash_migration_creates_auth_tables_before_indexing() -> None:
    """Migration 010 must be runnable on a fresh production database."""
    sql = (REPO_ROOT / "migrations" / "010_secret_hash_columns.sql").read_text()

    auth_table_pos = sql.index("CREATE TABLE IF NOT EXISTS auth_tokens")
    api_key_table_pos = sql.index("CREATE TABLE IF NOT EXISTS api_keys")
    auth_index_pos = sql.index("CREATE INDEX IF NOT EXISTS idx_auth_tokens_token_hash")
    api_key_index_pos = sql.index("CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash")

    assert auth_table_pos < auth_index_pos
    assert api_key_table_pos < api_key_index_pos


def test_secret_hash_migration_owns_tables_used_by_timescale_client() -> None:
    """The migration runner is the production DDL path for these tables."""
    sql = (REPO_ROOT / "migrations" / "010_secret_hash_columns.sql").read_text()

    assert "UNIQUE(station_id, token_type)" in sql
    assert "UNIQUE(api_key)" in sql
    assert "usage_count INTEGER NOT NULL DEFAULT 0" in sql
