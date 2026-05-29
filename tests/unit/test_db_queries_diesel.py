"""Unit tests for the diesel/fuel query helpers in src/db/queries.py (fakes)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.db.queries import (
    DEFAULT_FUEL_BASELINE_FLEET,
    FUEL_BASELINE_DEFAULT_KEY,
    fetch_or_pull_diesel_price,
    resolve_country_code,
    resolve_fuel_baselines,
)

_WHEN = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


class _FakeDB:
    """Minimal asyncpg-like stub: scripted fetchrow / fetch."""

    def __init__(self, *, fetchrow_result=None, fetch_result=None, fetch_raises=None):
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result if fetch_result is not None else []
        self._fetch_raises = fetch_raises
        self.fetchrow_calls = 0
        self.fetch_calls = 0

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls += 1
        if callable(self._fetchrow_result):
            return self._fetchrow_result(sql, *args)
        return self._fetchrow_result

    async def fetch(self, sql, *args):
        self.fetch_calls += 1
        if self._fetch_raises is not None:
            raise self._fetch_raises
        return self._fetch_result


# ── resolve_country_code ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_country_code_explicit_override():
    db = _FakeDB(
        fetchrow_result={
            "timezone": "Europe/Vilnius",
            "tariff_config": {"diesel_country": "de"},
        }
    )
    assert await resolve_country_code(db, uuid4()) == "DE"  # override wins, uppercased


@pytest.mark.asyncio
async def test_resolve_country_code_from_timezone():
    db = _FakeDB(fetchrow_result={"timezone": "Europe/Vilnius", "tariff_config": None})
    assert await resolve_country_code(db, uuid4()) == "LT"


@pytest.mark.asyncio
async def test_resolve_country_code_tariff_is_json_string():
    db = _FakeDB(
        fetchrow_result={
            "timezone": "Europe/Berlin",
            "tariff_config": json.dumps({"diesel_country": "FR"}),
        }
    )
    assert await resolve_country_code(db, uuid4()) == "FR"


@pytest.mark.asyncio
async def test_resolve_country_code_unknown_tz_returns_none():
    db = _FakeDB(fetchrow_result={"timezone": "Mars/Olympus", "tariff_config": None})
    assert await resolve_country_code(db, uuid4()) is None


@pytest.mark.asyncio
async def test_resolve_country_code_missing_row():
    db = _FakeDB(fetchrow_result=None)
    assert await resolve_country_code(db, uuid4()) is None


# ── fetch_or_pull_diesel_price ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_diesel_price_cache_hit_no_fetch(monkeypatch):
    monkeypatch.delenv("DIESEL_PRICE_API_BASE_URL", raising=False)
    db = _FakeDB(fetchrow_result={"price_eur_per_l": 0.95})
    price = await fetch_or_pull_diesel_price(db, "DE", _WHEN, source="eu_oil_bulletin")
    assert price == pytest.approx(0.95)
    assert db.fetchrow_calls == 1  # read-through only, no second read


@pytest.mark.asyncio
async def test_fetch_diesel_price_miss_no_base_url_returns_none(monkeypatch):
    monkeypatch.delenv("DIESEL_PRICE_API_BASE_URL", raising=False)
    db = _FakeDB(fetchrow_result=None)
    price = await fetch_or_pull_diesel_price(db, "DE", _WHEN, source="eu_oil_bulletin")
    assert price is None


@pytest.mark.asyncio
async def test_fetch_diesel_price_miss_then_fetch(monkeypatch):
    monkeypatch.setenv("DIESEL_PRICE_API_BASE_URL", "https://example.invalid/api")

    # First read misses, second read (post-fetch) hits.
    results = [None, {"price_eur_per_l": 1.12}]

    class _DB(_FakeDB):
        async def fetchrow(self, sql, *args):
            self.fetchrow_calls += 1
            return results[min(self.fetchrow_calls - 1, len(results) - 1)]

    db = _DB()

    # Patch the adapter so no real HTTP happens.
    import src.adapters.diesel_prices.adapter as adapter_mod

    class _FakeAdapter:
        def __init__(self, *a, **k):
            pass

        async def get_current_prices(self, regions):
            return ["sentinel"]  # non-empty → triggers store

        async def store_prices_to_db(self, prices):
            return 1

        async def aclose(self):
            pass

    monkeypatch.setattr(adapter_mod, "DieselPriceAdapter", _FakeAdapter)
    price = await fetch_or_pull_diesel_price(db, "DE", _WHEN, source="eu_oil_bulletin")
    assert price == pytest.approx(1.12)
    assert db.fetchrow_calls == 2  # miss, then hit after fetch


@pytest.mark.asyncio
async def test_fetch_diesel_price_fetch_failure_returns_none(monkeypatch):
    monkeypatch.setenv("DIESEL_PRICE_API_BASE_URL", "https://example.invalid/api")
    db = _FakeDB(fetchrow_result=None)

    import src.adapters.diesel_prices.adapter as adapter_mod

    class _BoomAdapter:
        def __init__(self, *a, **k):
            pass

        async def get_current_prices(self, regions):
            raise RuntimeError("source down")

        async def aclose(self):
            pass

    monkeypatch.setattr(adapter_mod, "DieselPriceAdapter", _BoomAdapter)
    assert await fetch_or_pull_diesel_price(db, "DE", _WHEN) is None


# ── resolve_fuel_baselines ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_fuel_baselines_defaults_when_empty():
    db = _FakeDB(fetch_result=[])
    out = await resolve_fuel_baselines(db, depot_id=uuid4(), organization_id=uuid4())
    assert out["bus_large"] == pytest.approx(35.0)
    assert out[FUEL_BASELINE_DEFAULT_KEY] == pytest.approx(DEFAULT_FUEL_BASELINE_FLEET)


@pytest.mark.asyncio
async def test_resolve_fuel_baselines_precedence():
    site = uuid4()
    rows = [
        {"site_id": None, "vehicle_type": "bus", "diesel_l_per_100km": 31.0},  # org+type
        {"site_id": site, "vehicle_type": "bus", "diesel_l_per_100km": 29.0},  # site+type wins
        {"site_id": None, "vehicle_type": None, "diesel_l_per_100km": 13.0},  # org default
        {"site_id": site, "vehicle_type": None, "diesel_l_per_100km": 14.0},  # site default wins
    ]
    db = _FakeDB(fetch_result=rows)
    out = await resolve_fuel_baselines(db, depot_id=site, organization_id=uuid4())
    assert out["bus"] == pytest.approx(29.0)  # site+type beats org+type
    assert out[FUEL_BASELINE_DEFAULT_KEY] == pytest.approx(14.0)  # site default beats org default


@pytest.mark.asyncio
async def test_resolve_fuel_baselines_fails_open_on_missing_table():
    db = _FakeDB(fetch_raises=RuntimeError('relation "vehicle_type_fuel_baselines" does not exist'))
    out = await resolve_fuel_baselines(db, depot_id=uuid4(), organization_id=uuid4())
    # Falls back to code defaults rather than raising.
    assert out["van"] == pytest.approx(11.0)
    assert out[FUEL_BASELINE_DEFAULT_KEY] == pytest.approx(DEFAULT_FUEL_BASELINE_FLEET)


@pytest.mark.asyncio
async def test_resolve_fuel_baselines_ignores_nonpositive():
    rows = [{"site_id": None, "vehicle_type": "bus", "diesel_l_per_100km": 0.0}]
    db = _FakeDB(fetch_result=rows)
    out = await resolve_fuel_baselines(db, depot_id=uuid4(), organization_id=uuid4())
    assert out["bus"] == pytest.approx(30.0)  # invalid override ignored → code default
