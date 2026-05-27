"""Unit tests for the data-source provider registry."""

from __future__ import annotations

import pytest

from src.core.data_sources import registry
from src.core.data_sources.base import (
    CredentialField,
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
    fields_by_key = {f.key: f for f in kempower.credential_fields}
    assert {"refresh_token", "username", "password", "locationId"} <= fields_by_key.keys()
    # Both auth methods are annotated for the frontend's mutual-exclusivity logic.
    assert fields_by_key["refresh_token"].auth_group == "token"
    assert fields_by_key["username"].auth_group == "basic"
    assert fields_by_key["password"].auth_group == "basic"
    # Non-auth fields have no group.
    assert fields_by_key["locationId"].auth_group is None
    # Secrets are masked.
    assert fields_by_key["refresh_token"].secret is True
    assert fields_by_key["password"].secret is True and fields_by_key["password"].type == "password"


def test_credential_field_auth_group_defaults_to_none():
    field = CredentialField(key="username", label="Username")
    assert field.auth_group is None
    assert "auth_group" in field.model_dump()
    assert field.model_dump()["auth_group"] is None


def test_credential_field_auth_group_round_trips():
    field = CredentialField(key="api_key", label="API Key", type="password", auth_group="apikey")
    dumped = field.model_dump()
    assert dumped["auth_group"] == "apikey"


def test_catalogue_entry_serialises_auth_group():
    """auth_group appears in the provider catalogue wire format used by /providers."""
    entry = ProviderCatalogueEntry(
        provider_key="test-provider",
        display_name="Test",
        description="test",
        credential_fields=[
            CredentialField(key="username", label="Username", auth_group="basic"),
            CredentialField(key="password", label="Password", type="password", auth_group="basic"),
            CredentialField(key="api_key", label="API Key", type="password", auth_group="apikey"),
            CredentialField(key="location_id", label="Location ID"),
        ],
    )
    wire = entry.model_dump()
    fields_by_key = {f["key"]: f for f in wire["credential_fields"]}
    assert fields_by_key["username"]["auth_group"] == "basic"
    assert fields_by_key["password"]["auth_group"] == "basic"
    assert fields_by_key["api_key"]["auth_group"] == "apikey"
    assert fields_by_key["location_id"]["auth_group"] is None


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
