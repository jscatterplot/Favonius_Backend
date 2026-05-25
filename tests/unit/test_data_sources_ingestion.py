"""Unit tests for the durable ingestion-job driver (repository mocked)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.core.data_sources import ingestion, registry
from src.core.data_sources import repository as repo
from src.core.data_sources.base import DataSourceProvider, IngestionResult, ProviderCatalogueEntry
from src.security.credential_cipher import CredentialCipherError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeProvider(DataSourceProvider):
    def __init__(self, result=None, raises=None):
        self._result = result or IngestionResult(status="succeeded", chargers_created=1)
        self._raises = raises

    @property
    def provider_key(self) -> str:
        return "faketest"

    def catalogue_entry(self) -> ProviderCatalogueEntry:
        return ProviderCatalogueEntry(provider_key="faketest", display_name="F", description="d")

    async def validate_credentials(self, credentials, config) -> None:
        return None

    async def run_ingestion(self, ctx) -> IngestionResult:
        if self._raises:
            raise self._raises
        return self._result


def _claimed(provider_key="faketest") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "connection_id": str(uuid4()),
        "trigger": "manual",
        "triggered_by": None,
        "organization_id": str(uuid4()),
        "site_id": str(uuid4()),
        "provider_key": provider_key,
    }


def _secret(provider_key="faketest") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "site_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "provider_key": provider_key,
        "config": {"locationId": "loc1"},
        "encrypted_credentials": b"x",
        "encryption_version": 1,
    }


@pytest.fixture
def patched(monkeypatch):
    """Patch repo writers + audit + decrypt; register a fake provider."""
    finalize = AsyncMock()
    touch = AsyncMock()
    monkeypatch.setattr(repo, "finalize_job", finalize)
    monkeypatch.setattr(repo, "touch_connection_after_run", touch)
    monkeypatch.setattr(repo, "merge_job_progress", AsyncMock())
    monkeypatch.setattr(ingestion, "write_admin_audit_row", AsyncMock())
    monkeypatch.setattr(
        ingestion, "decrypt_credentials", lambda payload, version: {"secrets": {"u": "1"}}
    )
    registry.register(_FakeProvider())
    yield {"finalize": finalize, "touch": touch}
    registry._PROVIDERS.pop("faketest", None)


async def test_happy_path_finalizes_succeeded(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed()))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret()))

    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")

    patched["finalize"].assert_awaited_once()
    assert patched["finalize"].await_args.kwargs["status"] == "succeeded"
    patched["touch"].assert_awaited_once()
    assert patched["touch"].await_args.kwargs["last_status"] == "succeeded"


async def test_already_terminal_job_is_skipped(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=None))
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    patched["finalize"].assert_not_awaited()


async def test_startup_recovery_reclaims_running_without_stale_threshold(monkeypatch, patched):
    claim = AsyncMock(return_value=_claimed())
    monkeypatch.setattr(repo, "claim_job", claim)
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret()))

    await ingestion.run_ingestion_job(
        MagicMock(), MagicMock(), job_id="j1", allow_stale_running_claim=True
    )

    assert claim.await_args.kwargs["allow_running_reclaim"] is True
    assert claim.await_args.kwargs["stale_threshold_seconds"] == 120


async def test_missing_connection_fails(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed()))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=None))
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    assert patched["finalize"].await_args.kwargs["status"] == "failed"


async def test_decrypt_failure_fails(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed()))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret()))

    def _boom(payload, version):
        raise CredentialCipherError("bad key")

    monkeypatch.setattr(ingestion, "decrypt_credentials", _boom)
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    kwargs = patched["finalize"].await_args.kwargs
    assert kwargs["status"] == "failed"
    assert "decryption" in kwargs["error_detail"]


async def test_unknown_provider_fails(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed("ghost")))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret("ghost")))
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    assert patched["finalize"].await_args.kwargs["status"] == "failed"


async def test_provider_exception_fails(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed()))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret()))
    registry.register(_FakeProvider(raises=RuntimeError("kaboom")))  # replaces faketest
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    assert patched["finalize"].await_args.kwargs["status"] == "failed"


async def test_partial_status_propagates(monkeypatch, patched):
    monkeypatch.setattr(repo, "claim_job", AsyncMock(return_value=_claimed()))
    monkeypatch.setattr(repo, "get_connection_secret", AsyncMock(return_value=_secret()))
    registry.register(
        _FakeProvider(result=IngestionResult(status="partial", skipped=["x"], chargers_created=2))
    )
    await ingestion.run_ingestion_job(MagicMock(), MagicMock(), job_id="j1")
    kwargs = patched["finalize"].await_args.kwargs
    assert kwargs["status"] == "partial"
    assert kwargs["progress"]["credentials_pending_rotation"] is True
