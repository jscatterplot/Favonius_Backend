import pytest
from prometheus_client import REGISTRY


@pytest.fixture(autouse=True)
def clear_prometheus_registry():
    """Clear the Prometheus default registry before each test to avoid conflicts."""
    # Create a new registry and set it as the default for the duration of the test
    # This prevents "Duplicated timeseries in CollectorRegistry" errors

    # Unregister all existing collectors from the default registry
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)

    # Re-register the default metrics that are usually present
    # This might need to be adjusted based on what default metrics your application uses
    # For simplicity, we'll just ensure the registry is clean.

    # If you have specific metrics that are initialized globally and cause issues,
    # you might need to mock them or ensure they are created within test scope.

    yield

    # After the test, clear the registry again to ensure isolation
    collectors = list(REGISTRY._collector_to_names.keys())
    for collector in collectors:
        REGISTRY.unregister(collector)
