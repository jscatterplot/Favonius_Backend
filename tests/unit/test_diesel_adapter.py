"""Unit tests for the diesel-price adapter (mapping + store, fakes only)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.adapters.diesel_prices.adapter import DieselPriceAdapter
from src.adapters.diesel_prices.mapping import (
    DieselPrice,
    _coerce_price_eur_per_l,
    normalize_country,
    parse_diesel_record,
)

_SRC = "eu_oil_bulletin"


# ── normalize_country ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("de", "DE"), ("DE ", "DE"), ("lt", "LT"), (None, ""), ("", "")],
)
def test_normalize_country(raw, expected):
    assert normalize_country(raw) == expected


# ── _coerce_price_eur_per_l ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.504, pytest.approx(1.504)),  # already EUR/L
        ("1.504", pytest.approx(1.504)),
        (150.4, pytest.approx(1.504)),  # ct/L → EUR/L
        (1504.0, pytest.approx(1.504)),  # EUR/1000L → EUR/L
        (0, None),  # non-positive
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),  # bool must not read as 1.0
        ("n/a", None),
        (0.01, None),  # below plausible floor
        (50.0, None),  # 50 EUR/L is absurd, and /100 = 0.5 is plausible... see note
    ],
)
def test_coerce_price(raw, expected):
    # Note: 50.0 > _MAX_PLAUSIBLE (10) so it's divided by 100 → 0.5, which IS
    # plausible. Assert the documented behaviour explicitly below instead.
    if raw == 50.0:
        assert _coerce_price_eur_per_l(raw) == pytest.approx(0.5)
        return
    assert _coerce_price_eur_per_l(raw) == expected


# ── parse_diesel_record ───────────────────────────────────────────────────────


def test_parse_prefers_ex_tax_as_wholesale():
    rec = parse_diesel_record(
        {
            "countryCode": "DE",
            "priceExclTaxes": 0.95,
            "priceInclTaxes": 1.75,
            "date": "2026-05-20",
        },
        source=_SRC,
    )
    assert rec is not None
    assert rec.region == "DE"
    assert rec.price_eur_per_l == pytest.approx(0.95)  # ex-tax is the stored basis
    assert rec.price_incl_tax_eur_per_l == pytest.approx(1.75)
    assert rec.time == datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    assert rec.source == _SRC


def test_parse_falls_back_to_inc_tax_when_no_ex_tax():
    rec = parse_diesel_record({"country": "LT", "price": 1.60, "date": "2026-05-20"}, source=_SRC)
    assert rec is not None
    assert rec.price_eur_per_l == pytest.approx(1.60)


def test_parse_skips_missing_country():
    assert parse_diesel_record({"priceExclTaxes": 1.0, "date": "2026-05-20"}, source=_SRC) is None


def test_parse_skips_missing_price():
    assert parse_diesel_record({"country": "DE", "date": "2026-05-20"}, source=_SRC) is None


def test_parse_skips_missing_date():
    assert parse_diesel_record({"country": "DE", "priceExclTaxes": 1.0}, source=_SRC) is None


def test_parse_scales_ct_per_litre():
    rec = parse_diesel_record(
        {"country": "FR", "priceExclTaxes": 98.5, "date": "2026-05-20"}, source=_SRC
    )
    assert rec is not None
    assert rec.price_eur_per_l == pytest.approx(0.985)


# ── store_prices_to_db (fake pool) ────────────────────────────────────────────


class _FakeConn:
    def __init__(self, *, inserted_rows):
        self.inserted_rows = inserted_rows
        self.captured_args = None

    def transaction(self):
        conn = self

        class _Txn:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Txn()

    async def fetch(self, sql, *args):
        self.captured_args = args
        return self.inserted_rows


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _price(region="DE", t=None):
    return DieselPrice(
        time=t or datetime(2026, 5, 20, tzinfo=timezone.utc),
        region=region,
        price_eur_per_l=0.95,
        source=_SRC,
        price_incl_tax_eur_per_l=1.75,
    )


@pytest.mark.asyncio
async def test_store_counts_true_inserts():
    # Two prices, fake returns one RETURNING row → reports 1 actual insert.
    conn = _FakeConn(inserted_rows=[1])
    adapter = DieselPriceAdapter(pool=_FakePool(conn))
    stored = await adapter.store_prices_to_db([_price("DE"), _price("LT")])
    assert stored == 1
    # 5 arrays passed to UNNEST: time, region, source, ex_tax, inc_tax.
    assert len(conn.captured_args) == 5
    assert conn.captured_args[1] == ["DE", "LT"]  # regions array


@pytest.mark.asyncio
async def test_store_idempotent_second_run_inserts_zero():
    conn = _FakeConn(inserted_rows=[])  # ON CONFLICT DO NOTHING → no RETURNING rows
    adapter = DieselPriceAdapter(pool=_FakePool(conn))
    assert await adapter.store_prices_to_db([_price("DE")]) == 0


@pytest.mark.asyncio
async def test_store_empty_is_noop():
    adapter = DieselPriceAdapter(pool=_FakePool(_FakeConn(inserted_rows=[])))
    assert await adapter.store_prices_to_db([]) == 0


# ── get_current_prices with a fake client ─────────────────────────────────────


class _FakeClient:
    source = _SRC

    def __init__(self, records):
        self._records = records

    async def iter_prices(self):
        for r in self._records:
            yield r


# ── client source/auth resolution ─────────────────────────────────────────────


def test_client_default_source(monkeypatch):
    from src.adapters.diesel_prices.client import DieselPriceClient, configured_source

    monkeypatch.delenv("DIESEL_PRICE_SOURCE", raising=False)
    assert configured_source() == "eu_oil_bulletin"
    c = DieselPriceClient()
    assert c.source == "eu_oil_bulletin"
    assert "energy.ec.europa.eu" in c._base_url  # noqa: SLF001


def test_client_source_env_override(monkeypatch):
    from src.adapters.diesel_prices.client import DieselPriceClient

    monkeypatch.setenv("DIESEL_PRICE_SOURCE", "tankerkonig")
    monkeypatch.delenv("DIESEL_PRICE_API_BASE_URL", raising=False)
    c = DieselPriceClient()
    assert c.source == "tankerkonig"
    assert "tankerkoenig" in c._base_url  # noqa: SLF001


@pytest.mark.asyncio
async def test_client_fetch_token_open_source_is_empty(monkeypatch):
    from src.adapters.diesel_prices.client import DieselPriceClient

    monkeypatch.delenv("DIESEL_PRICE_API_KEY", raising=False)
    c = DieselPriceClient(source="eu_oil_bulletin")
    assert await c._fetch_token() == ""  # noqa: SLF001


@pytest.mark.asyncio
async def test_client_fetch_token_returns_api_key():
    from src.adapters.diesel_prices.client import DieselPriceClient

    c = DieselPriceClient(source="fuel_prices_eu", api_key="secret-key")
    assert await c._fetch_token() == "secret-key"  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_current_prices_filters_regions_and_skips_bad():
    records = [
        {"country": "DE", "priceExclTaxes": 0.95, "date": "2026-05-20"},
        {"country": "LT", "priceExclTaxes": 0.90, "date": "2026-05-20"},
        {"country": "FR", "priceExclTaxes": 0.99, "date": "2026-05-20"},
        {"priceExclTaxes": 0.50, "date": "2026-05-20"},  # no country → skipped
    ]
    adapter = DieselPriceAdapter(client=_FakeClient(records))
    out = await adapter.get_current_prices(regions=["DE", "LT"])
    assert {p.region for p in out} == {"DE", "LT"}
    assert all(isinstance(p, DieselPrice) for p in out)
