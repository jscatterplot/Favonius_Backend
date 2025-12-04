import pytest
from prometheus_client import REGISTRY, CollectorRegistry

def pytest_configure(config):
    """Clear Prometheus registry before any tests are collected."""
    # Clear registry before collection
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)

@pytest.fixture(autouse=True, scope="session")
def clear_prometheus_registry_session():
    """Clear the Prometheus default registry at session start to avoid conflicts."""
    # Clear registry at session start
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)
    
    yield
    
    # Clear registry at session end
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)

@pytest.fixture(autouse=True)
def clear_prometheus_registry():
    """Clear the Prometheus default registry before each test to avoid conflicts."""
    # Clear registry before each test
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)
    
    yield
    
    # Clear registry after each test
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)
