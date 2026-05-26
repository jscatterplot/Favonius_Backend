"""Unit tests for the data-source provider registry."""

from __future__ import annotations

import pytest

from src.core.data_sources import registry
from src.core.data_sources.base import (
    DataSourceProvider,
    IngestionContext,
    IngestionResult,
    ProviderCatalogueEntry,
)
from src.core.data_sources.errors import ProviderNotFound
from src.core.data_sources.kempower_provider import KempowerProvider

pytestmark = pytest.mark.unit


def test_get_kempower_provider():
    assert isinstance(registry.get_provider("kempower"), KempowerProvider)


def test_get_unknown_provider_raises():
    with pytest.raises(ProviderNotFound):
        registry.get_provider("does-not-exist")


def test_catalogue_includes_kempower():
    entries = registry.iter_catalogue()
    kempower = next(e for e in entries if e.provider_key == "kempower")
    field_keys = {f.key for f in kempower.credential_fields}
    assert {"username", "password", "locationId"} <= field_keys
    # The password field is marked secret so the UI masks it.
    pw = next(f for f in kempower.credential_fields if f.key == "password")
    assert pw.secret is True and pw.type == "password"


def test_second_provider_is_drop_in():
    class _DummyProvider(DataSourceProvider):
        @property
        def provider_key(self) -> str:
            return "dummy-test"

        def catalogue_entry(self) -> ProviderCatalogueEntry:
            return ProviderCatalogueEntry(
                provider_key="dummy-test",
                display_name="Dummy",
                description="test",
            )

        async def validate_credentials(self, credentials, config) -> None:
            return None

        async def run_ingestion(self, ctx: IngestionContext) -> IngestionResult:
            return IngestionResult(status="succeeded")

    registry.register(_DummyProvider())
    try:
        assert registry.get_provider("dummy-test").provider_key == "dummy-test"
        keys = {e.provider_key for e in registry.iter_catalogue()}
        assert "dummy-test" in keys and "kempower" in keys
    finally:
        registry._PROVIDERS.pop("dummy-test", None)
