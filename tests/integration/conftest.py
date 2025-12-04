"""
Pytest configuration for integration tests.
Handles Prometheus metrics registry conflicts.
"""

import pytest
from prometheus_client import REGISTRY, CollectorRegistry


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
    original_registry = REGISTRY
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
    yield test_registry
    # Restore original registry
    REGISTRY._names_to_collectors.clear()
    REGISTRY._collector_to_names.clear()
