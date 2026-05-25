"""Integration tests for the Data Sources HTTP endpoints.

Spins a minimal FastAPI app with just the data-sources router, overrides the
auth + pool dependencies, and mocks the repository so the tests exercise routing,
auth scoping, status codes, error mapping, and the no-secrets response contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.data_sources import router as router_mod
from src.api.data_sources.router import get_static_pool, get_ts_pool, router
from src.core.data_sources import registry
from src.core.data_sources import repository as repo
from src.core.data_sources.base import (
    CredentialField,
    IngestionResult,
    ProviderCatalogueEntry,
)
from src.core.data_sources.errors import CredentialValidationError
from src.security.tenant_mirror import ensure_tenant_mirrored

pytestmark = pytest.mark.integration

_ORG = str(uuid4())
_DEPOT = str(uuid4())


class _FakeProvider:
    def __init__(self, raises: bool = False) -> None:
        self._raises = raises

    @property
    def provider_key(self) -> str:
        return "kempower"

    def catalogue_entry(self) -> ProviderCatalogueEntry:
        return ProviderCatalogueEntry(
            provider_key="kempower",
            display_name="Kempower",
            description="d",
            credential_fields=[
                CredentialField(key="locationId", label="Location", type="string"),
            ],
        )

    async def validate_credentials(self, credentials, config) -> None:
        if self._raises:
            raise CredentialValidationError("bad creds")

    async def run_ingestion(self, ctx) -> IngestionResult:
        return IngestionResult(status="succeeded")


def _connection_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": str(uuid4()),
        "organization_id": _ORG,
        "site_id": _DEPOT,
        "provider_key": "kempower",
        "display_name": "Helsinki",
        "status": "active",
        "config": {"locationId": "loc1"},
        "sync_interval_minutes": 1440,
        "scheduled_sync_enabled": True,
        "next_sync_at": datetime(2026, 5, 26, tzinfo=timezone.utc),
        "last_run_at": None,
        "last_status": None,
        "created_by": str(uuid4()),
        "created_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


def _job_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": str(uuid4()),
        "connection_id": str(uuid4()),
        "organization_id": _ORG,
        "site_id": _DEPOT,
        "provider_key": "kempower",
        "trigger": "manual",
        "status": "pending",
        "progress": {},
        "error_detail": None,
        "import_batch_id": None,
        "triggered_by": None,
        "created_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
    }
    row.update(over)
    return row


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: {"sub": str(uuid4())}
    app.dependency_overrides[get_static_pool] = lambda: MagicMock()
    app.dependency_overrides[get_ts_pool] = lambda: MagicMock()

    # Bypass role/depot checks + secret machinery (covered elsewhere).
    monkeypatch.setattr(router_mod, "_require_admin_org", lambda user: _ORG)
    monkeypatch.setattr(router_mod, "verify_depot_access", AsyncMock())
    monkeypatch.setattr(router_mod, "is_data_sources_ready", lambda: True)
    monkeypatch.setattr(router_mod, "encrypt_credentials", lambda payload: (b"tok", 1))
    monkeypatch.setattr(router_mod, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(registry, "get_provider", lambda key: _FakeProvider())
    monkeypatch.setattr(router_mod, "_audit", AsyncMock())
    return TestClient(app)


def test_providers_catalogue(client):
    resp = client.get("/admin/data-sources/providers")
    assert resp.status_code == 200
    keys = {p["provider_key"] for p in resp.json()["providers"]}
    assert "kempower" in keys


def test_create_connection_happy(client, monkeypatch):
    conn = _connection_row()
    monkeypatch.setattr(repo, "insert_connection", AsyncMock(return_value=conn))
    monkeypatch.setattr(repo, "enqueue_job", AsyncMock(return_value=_job_row()))

    resp = client.post(
        "/admin/data-sources/connections",
        json={
            "depotId": _DEPOT,
            "providerKey": "kempower",
            "displayName": "Helsinki",
            "credentials": {"username": "u", "password": "p"},
            "config": {"locationId": "loc1"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["connection"]["id"] == conn["id"]
    assert body["status_url"].endswith(body["job"]["id"])
    # No secrets in the response anywhere.
    blob = resp.text.lower()
    assert "password" not in blob and "encrypted_credentials" not in blob


def test_create_duplicate_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        repo, "insert_connection", AsyncMock(side_effect=asyncpg.UniqueViolationError("dup"))
    )
    resp = client.post(
        "/admin/data-sources/connections",
        json={
            "depotId": _DEPOT,
            "providerKey": "kempower",
            "credentials": {"username": "u", "password": "p"},
            "config": {"locationId": "loc1"},
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONNECTION_ALREADY_EXISTS"


def test_create_bad_credentials_returns_422(client, monkeypatch):
    monkeypatch.setattr(registry, "get_provider", lambda key: _FakeProvider(raises=True))
    resp = client.post(
        "/admin/data-sources/connections",
        json={
            "depotId": _DEPOT,
            "providerKey": "kempower",
            "credentials": {"username": "u", "password": "bad"},
            "config": {"locationId": "loc1"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "CREDENTIAL_VALIDATION_FAILED"


def test_sync_in_progress_returns_409(client, monkeypatch):
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=_connection_row()))
    monkeypatch.setattr(
        repo, "enqueue_job", AsyncMock(side_effect=asyncpg.UniqueViolationError("dup"))
    )
    cid = str(uuid4())
    resp = client.post(f"/admin/data-sources/connections/{cid}/sync")
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONNECTION_SYNC_IN_PROGRESS"


def test_get_connection_not_found(client, monkeypatch):
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=None))
    resp = client.get(f"/admin/data-sources/connections/{uuid4()}")
    assert resp.status_code == 404


def test_get_job_ok(client, monkeypatch):
    job = _job_row(status="running", progress={"stage": "chargers", "chargers_created": 2})
    monkeypatch.setattr(repo, "get_job", AsyncMock(return_value=job))
    resp = client.get(f"/admin/data-sources/jobs/{job['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert resp.json()["progress"]["chargers_created"] == 2


def test_list_connections(client, monkeypatch):
    monkeypatch.setattr(repo, "list_connections", AsyncMock(return_value=[_connection_row()]))
    resp = client.get("/admin/data-sources/connections")
    assert resp.status_code == 200
    assert len(resp.json()["connections"]) == 1


def test_delete_connection(client, monkeypatch):
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=_connection_row()))
    monkeypatch.setattr(repo, "disable_connection", AsyncMock(return_value=True))
    resp = client.delete(f"/admin/data-sources/connections/{uuid4()}")
    assert resp.status_code == 204


def test_malformed_connection_id_returns_400(client):
    resp = client.get("/admin/data-sources/connections/not-a-uuid")
    assert resp.status_code == 400


def test_malformed_job_id_returns_400(client):
    resp = client.get("/admin/data-sources/jobs/not-a-uuid")
    assert resp.status_code == 400


def test_create_malformed_depot_returns_400(client):
    resp = client.post(
        "/admin/data-sources/connections",
        json={
            "depotId": "not-a-uuid",
            "providerKey": "kempower",
            "credentials": {"username": "u", "password": "p"},
            "config": {"locationId": "loc1"},
        },
    )
    assert resp.status_code == 400


def test_sync_advances_next_sync(client, monkeypatch):
    rec = _connection_row()
    advance = AsyncMock()
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=rec))
    monkeypatch.setattr(repo, "enqueue_job", AsyncMock(return_value=_job_row()))
    monkeypatch.setattr(repo, "set_next_sync_now_plus_interval", advance)
    resp = client.post(f"/admin/data-sources/connections/{rec['id']}/sync")
    assert resp.status_code == 202
    advance.assert_awaited_once()


def test_update_rename(client, monkeypatch):
    rec = _connection_row()
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=rec))
    monkeypatch.setattr(
        repo, "update_connection", AsyncMock(return_value=_connection_row(display_name="New"))
    )
    resp = client.patch(f"/admin/data-sources/connections/{rec['id']}", json={"displayName": "New"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New"


def test_update_credentials_persists_merged_config(client, monkeypatch):
    # Empty stored config → the new locationId in credentials fills + persists.
    rec = _connection_row(config={})
    upd = AsyncMock(return_value=_connection_row())
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=rec))
    monkeypatch.setattr(repo, "update_connection", upd)
    resp = client.patch(
        f"/admin/data-sources/connections/{rec['id']}",
        json={"credentials": {"username": "u", "password": "p", "locationId": "loc9"}},
    )
    assert resp.status_code == 200
    assert upd.call_args.kwargs["config"]["locationId"] == "loc9"


def test_update_credentials_not_ready_returns_503(client, monkeypatch):
    rec = _connection_row()
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=rec))
    monkeypatch.setattr(router_mod, "is_data_sources_ready", lambda: False)
    resp = client.patch(
        f"/admin/data-sources/connections/{rec['id']}",
        json={"credentials": {"username": "u", "password": "p"}},
    )
    assert resp.status_code == 503


def test_update_reactivation_conflict_returns_409(client, monkeypatch):
    rec = _connection_row(status="disabled")
    monkeypatch.setattr(repo, "get_connection", AsyncMock(return_value=rec))
    monkeypatch.setattr(
        repo, "update_connection", AsyncMock(side_effect=asyncpg.UniqueViolationError("dup"))
    )
    resp = client.patch(f"/admin/data-sources/connections/{rec['id']}", json={"status": "active"})
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONNECTION_ALREADY_EXISTS"
