"""Unit tests for the KempowerProvider (onboarding stages mocked)."""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.adapters.kempower import KempowerClientError
from src.core.data_sources import kempower_provider as kp
from src.core.data_sources.base import IngestionContext
from src.core.data_sources.errors import CredentialValidationError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeClientCM:
    """Async-context-manager stand-in for KempowerClient."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _ctx(
    config: dict[str, Any],
    credentials: Optional[dict[str, Any]] = None,
) -> IngestionContext:
    return IngestionContext(
        connection_id=str(uuid4()),
        job_id=str(uuid4()),
        depot_id=str(uuid4()),
        organization_id=str(uuid4()),
        credentials=credentials if credentials is not None else {"username": "u", "password": "p"},
        config=config,
        static_pool=MagicMock(),
        ts_pool=MagicMock(),
        progress=AsyncMock(),
        batch_id=uuid4(),
    )


async def test_validate_credentials_probes_location(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(return_value={"id": "loc1"})
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))

    await provider.validate_credentials({"username": "u", "password": "p"}, {"locationId": "loc1"})
    client.get_location.assert_awaited_once_with("loc1")


async def test_validate_credentials_missing_location():
    provider = kp.KempowerProvider()
    with pytest.raises(CredentialValidationError):
        await provider.validate_credentials({"username": "u", "password": "p"}, {})


async def test_validate_credentials_rejects_bad_backfill(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(return_value={"id": "loc1"})
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))
    with pytest.raises(CredentialValidationError):
        await provider.validate_credentials(
            {"username": "u", "password": "p"},
            {"locationId": "loc1", "backfillSince": "not-a-date"},
        )
    # Rejected before any network probe.
    client.get_location.assert_not_awaited()


async def test_validate_credentials_maps_client_error(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(side_effect=KempowerClientError("bad creds"))
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))
    with pytest.raises(CredentialValidationError):
        await provider.validate_credentials(
            {"username": "u", "password": "p"}, {"locationId": "loc1"}
        )


async def test_validate_credentials_with_refresh_token(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(return_value={"id": "loc1"})
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))
    await provider.validate_credentials({"refresh_token": "tok"}, {"locationId": "loc1"})
    client.get_location.assert_awaited_once_with("loc1")


async def test_validate_credentials_missing_all_auth():
    provider = kp.KempowerProvider()
    with pytest.raises(CredentialValidationError, match="refresh token or both"):
        await provider.validate_credentials({}, {"locationId": "loc1"})


async def test_validate_credentials_both_groups_rejected():
    provider = kp.KempowerProvider()
    with pytest.raises(CredentialValidationError, match="not both"):
        await provider.validate_credentials(
            {"refresh_token": "tok", "username": "u", "password": "p"},
            {"locationId": "loc1"},
        )


async def test_validate_credentials_partial_basic_auth_rejected():
    # username without password is not enough.
    provider = kp.KempowerProvider()
    with pytest.raises(CredentialValidationError, match="refresh token or both"):
        await provider.validate_credentials({"username": "u"}, {"locationId": "loc1"})


async def test_run_ingestion_happy_path(monkeypatch):
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(MagicMock()))

    async def fake_chargers(pool, client, *, depot_id, kempower_location_id, dry_run, counts):
        counts.chargers_created += 2
        return {"S1": "c1", "S2": "c2"}

    async def fake_vehicles(
        pool, client, *, depot_id, organization_id, kempower_location_id, dry_run, counts
    ):
        counts.vehicles_created += 3
        return {"V1": "v1"}

    async def fake_access(pool, *, depot_id, station_map, vehicle_map, dry_run, counts):
        counts.access_rows_upserted += 2

    async def fake_sessions(
        pool,
        client,
        *,
        depot_id,
        station_map,
        vehicle_map,
        backfill_since,
        batch_id,
        dry_run,
        counts,
    ):
        counts.sessions_inserted += 10

    monkeypatch.setattr(kp, "import_chargers", fake_chargers)
    monkeypatch.setattr(kp, "import_vehicles", fake_vehicles)
    monkeypatch.setattr(kp, "upsert_access_matrix", fake_access)
    monkeypatch.setattr(kp, "backfill_sessions", fake_sessions)

    ctx = _ctx({"locationId": "loc1", "backfillSince": "2025-01-01"})
    result = await provider.run_ingestion(ctx)

    assert result.status == "succeeded"
    assert result.chargers_created == 2
    assert result.vehicles_created == 3
    assert result.sessions_inserted == 10
    assert result.access_rows_upserted == 2
    # Progress reported for each of the four stages.
    assert ctx.progress.update.await_count == 4


async def test_run_ingestion_skips_yield_partial(monkeypatch):
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(MagicMock()))

    async def fake_chargers(pool, client, *, depot_id, kempower_location_id, dry_run, counts):
        counts.chargers_skipped.append("S9: non-CCS connector")
        return {}

    async def noop_vehicles(pool, client, **kw):
        return {}

    async def noop_access(pool, **kw):
        return None

    monkeypatch.setattr(kp, "import_chargers", fake_chargers)
    monkeypatch.setattr(kp, "import_vehicles", noop_vehicles)
    monkeypatch.setattr(kp, "upsert_access_matrix", noop_access)

    ctx = _ctx({"locationId": "loc1"})  # no backfill
    result = await provider.run_ingestion(ctx)
    assert result.status == "partial"
    assert any("non-CCS" in s for s in result.skipped)


async def test_run_ingestion_client_error_after_rows_is_partial(monkeypatch):
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(MagicMock()))

    async def fake_chargers(pool, client, *, depot_id, kempower_location_id, dry_run, counts):
        counts.chargers_created += 1
        return {"S1": "c1"}

    async def boom_vehicles(pool, client, **kw):
        raise KempowerClientError("429 forever")

    monkeypatch.setattr(kp, "import_chargers", fake_chargers)
    monkeypatch.setattr(kp, "import_vehicles", boom_vehicles)

    ctx = _ctx({"locationId": "loc1"})
    result = await provider.run_ingestion(ctx)
    assert result.status == "partial"
    assert result.error_detail and "Kempower API error" in result.error_detail


async def test_run_ingestion_missing_location_fails():
    provider = kp.KempowerProvider()
    ctx = _ctx({})
    result = await provider.run_ingestion(ctx)
    assert result.status == "failed"


# ── snake_case key tolerance ───────────────────────────────────────────────
# A BFF/proxy that decamelizes request bodies delivers the nested config keys
# as snake_case (location_id / backfill_since / base_url) even though the
# catalogue advertises camelCase. The resolver must accept either form.


async def test_validate_credentials_accepts_snake_case_location(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(return_value={"id": "loc1"})
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))

    await provider.validate_credentials({"refresh_token": "tok"}, {"location_id": "loc1"})
    client.get_location.assert_awaited_once_with("loc1")


async def test_validate_credentials_accepts_snake_case_backfill(monkeypatch):
    client = MagicMock()
    client.get_location = AsyncMock(return_value={"id": "loc1"})
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(client))

    # snake_case backfill_since must parse (not raise) just like backfillSince.
    await provider.validate_credentials(
        {"refresh_token": "tok"},
        {"location_id": "loc1", "backfill_since": "2025-01-01"},
    )
    client.get_location.assert_awaited_once_with("loc1")


async def test_run_ingestion_accepts_snake_case_location(monkeypatch):
    provider = kp.KempowerProvider()
    monkeypatch.setattr(provider, "_build_client", lambda c, cfg: _FakeClientCM(MagicMock()))

    captured: dict[str, Any] = {}

    async def fake_chargers(pool, client, *, depot_id, kempower_location_id, dry_run, counts):
        captured["location_id"] = kempower_location_id
        return {}

    async def noop_vehicles(pool, client, **kw):
        return {}

    async def noop_access(pool, **kw):
        return None

    monkeypatch.setattr(kp, "import_chargers", fake_chargers)
    monkeypatch.setattr(kp, "import_vehicles", noop_vehicles)
    monkeypatch.setattr(kp, "upsert_access_matrix", noop_access)

    ctx = _ctx({"location_id": "loc-snake"})
    result = await provider.run_ingestion(ctx)

    assert result.status == "succeeded"
    assert captured["location_id"] == "loc-snake"


def test_config_value_prefers_config_and_camelcase():
    # config wins over credentials; first matching key in the given order wins.
    assert kp._config_value({"locationId": "a"}, {"location_id": "b"}, "locationId", "location_id") == "a"
    assert kp._config_value({}, {"location_id": "b"}, "locationId", "location_id") == "b"
    assert kp._config_value({"locationId": ""}, {"location_id": "b"}, "locationId", "location_id") == "b"
    assert kp._config_value({}, {}, "locationId", "location_id") is None
