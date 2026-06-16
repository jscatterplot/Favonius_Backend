"""
Pytest configuration for integration tests.
Handles Prometheus metrics registry conflicts and dual-pool DB fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

import asyncpg
import pytest
import pytest_asyncio
from prometheus_client import REGISTRY, CollectorRegistry

from src.db.postgres_url import prepare_asyncpg_url_and_ssl

_DEFAULT_TEST_DB_URL = "postgresql://postgres:postgres@localhost:5432/favonius_test"
PILOT_SITE_ID = "a1b2c3d4-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class IntegrationPools:
    """TimescaleDB + Supabase static pools for split deployments."""

    ts_pool: asyncpg.Pool
    static_pool: asyncpg.Pool
    can_seed_sites: bool
    is_split: bool
    static_sites_readable: bool


def resolve_integration_ts_url() -> str:
    """URL for TimescaleDB (sessions, telemetry, electricity_prices)."""
    return (
        os.getenv("TEST_DATABASE_URL")
        or os.getenv("TIMESCALE_SERVICE_URL")
        or os.getenv("DATABASE_URL")
        or _DEFAULT_TEST_DB_URL
    ).strip()


def _build_supabase_url_from_env() -> str | None:
    """Build postgres URL from SUPABASE_DB_* parts (Railway / local env)."""
    host = os.getenv("SUPABASE_DB_HOST")
    password = os.getenv("SUPABASE_DB_PASSWORD")
    if not host or not password:
        return None
    port = os.getenv("SUPABASE_DB_PORT", "5432")
    name = os.getenv("SUPABASE_DB_NAME", "postgres")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


def resolve_integration_static_url(ts_url: str) -> str:
    """URL for Supabase static schema (sites). Falls back to ts_url when unset."""
    explicit = os.getenv("STATIC_DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if explicit:
        return explicit.strip()
    built = _build_supabase_url_from_env()
    if built:
        return built
    return ts_url


def urls_same(a: str, b: str) -> bool:
    """Compare DB endpoints ignoring credentials and query params."""
    pa, pb = urlparse(a), urlparse(b)
    path_a = (pa.path or "/").lstrip("/") or "postgres"
    path_b = (pb.path or "/").lstrip("/") or "postgres"
    return (pa.hostname, pa.port, path_a) == (pb.hostname, pb.port, path_b)


async def create_pool_from_url(url: str) -> asyncpg.Pool:
    clean_url, ssl_config = prepare_asyncpg_url_and_ssl(url)
    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config
    return await asyncpg.create_pool(clean_url, min_size=1, max_size=5, **connect_kw)


async def create_integration_pool() -> asyncpg.Pool:
    """asyncpg pool for integration tests (local or TigerCloud via TEST_DATABASE_URL)."""
    return await create_pool_from_url(resolve_integration_ts_url())


async def _static_schema_available(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                  SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'public' AND table_name = 'sites'
                )
                """
            )
        )


@pytest_asyncio.fixture
async def db_pools():
    """Timescale + static pools; ``can_seed_sites`` only when both URLs are the same DB."""
    ts_url = resolve_integration_ts_url()
    static_url = resolve_integration_static_url(ts_url)
    is_split = not urls_same(ts_url, static_url)
    ts_pool = await create_pool_from_url(ts_url)
    static_pool = (
        await create_pool_from_url(static_url) if is_split else ts_pool
    )
    static_sites_readable = await _static_schema_available(static_pool)
    can_seed = not is_split and static_sites_readable

    pools = IntegrationPools(
        ts_pool=ts_pool,
        static_pool=static_pool,
        can_seed_sites=can_seed,
        is_split=is_split,
        static_sites_readable=static_sites_readable,
    )
    yield pools
    if is_split:
        await static_pool.close()
    await ts_pool.close()


@pytest_asyncio.fixture
async def pool(db_pools: IntegrationPools):
    """TimescaleDB pool (backward-compatible alias)."""
    yield db_pools.ts_pool


@pytest.fixture
def integration_db_urls() -> tuple[str, str]:
    """(timescale_url, static_url) for backfill script env monkeypatch."""
    ts = resolve_integration_ts_url()
    static = resolve_integration_static_url(ts)
    return ts, static


@pytest.fixture(scope="session", autouse=True)
def reset_prometheus_registry():
    """Reset Prometheus registry before each test session."""
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
    yield
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()


@pytest.fixture(scope="function", autouse=True)
def isolate_prometheus_registry():
    """Isolate Prometheus registry for each test."""
    test_registry = CollectorRegistry()
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
    yield test_registry
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
