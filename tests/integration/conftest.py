"""
Pytest configuration for integration tests.
Handles Prometheus metrics registry conflicts.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
from prometheus_client import REGISTRY, CollectorRegistry

from src.db.postgres_url import prepare_asyncpg_url_and_ssl

_DEFAULT_TEST_DB_URL = "postgresql://postgres:postgres@localhost:5432/favonius_test"


async def create_integration_pool() -> asyncpg.Pool:
    """asyncpg pool for integration tests (local or TigerCloud via TEST_DATABASE_URL)."""
    url = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL).strip()
    clean_url, ssl_config = prepare_asyncpg_url_and_ssl(url)
    connect_kw: dict = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config
    return await asyncpg.create_pool(clean_url, min_size=1, max_size=5, **connect_kw)


@pytest.fixture(scope="session", autouse=True)
def reset_prometheus_registry():
    """Reset Prometheus registry before each test session."""
    # Clear the default registry
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
    yield
    # Clean up after tests
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()


@pytest.fixture(scope="function", autouse=True)
def isolate_prometheus_registry():
    """Isolate Prometheus registry for each test."""
    # Create a new registry for each test
    test_registry = CollectorRegistry()
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
    yield test_registry
    # Restore original registry
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
