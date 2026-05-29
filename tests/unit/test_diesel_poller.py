"""Unit tests for the diesel-price poller (fakes only — no DB, no HTTP)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.adapters.diesel_prices import poller as poller_mod
from src.adapters.diesel_prices.mapping import DieselPrice


class _FakeAdapter:
    """Stand-in for DieselPriceAdapter capturing store calls."""

    def __init__(self, *, prices=None, raise_on_fetch=False):
        self._prices = prices or []
        self._raise = raise_on_fetch
        self.stored: list[DieselPrice] = []
        self.closed = False

        class _Client:
            source = "eu_oil_bulletin"

        self.client = _Client()

    async def get_current_prices(self, regions):
        if self._raise:
            raise RuntimeError("source down")
        return [p for p in self._prices if p.region in set(regions)]

    async def store_prices_to_db(self, prices):
        self.stored.extend(prices)
        return len(prices)

    async def aclose(self):
        self.closed = True


def _price(region):
    return DieselPrice(
        time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        region=region,
        price_eur_per_l=1.5,
        source="eu_oil_bulletin",
    )


@pytest.mark.asyncio
async def test_poll_once_fetches_and_stores_depot_regions(monkeypatch):
    # Two depot regions resolved; adapter returns a price for each.
    async def fake_regions(static_pool):
        return ["DE", "LT"]

    monkeypatch.setattr(poller_mod, "_depot_regions", fake_regions)
    adapter = _FakeAdapter(prices=[_price("DE"), _price("LT")])
    summary = await poller_mod.poll_once(object(), object(), adapter)
    assert summary["regions"] == ["DE", "LT"]
    assert summary["fetched"] == 2
    assert summary["written"] == 2
    assert {p.region for p in adapter.stored} == {"DE", "LT"}


@pytest.mark.asyncio
async def test_poll_once_no_regions_is_noop(monkeypatch):
    async def fake_regions(static_pool):
        return []

    monkeypatch.setattr(poller_mod, "_depot_regions", fake_regions)
    adapter = _FakeAdapter(prices=[_price("DE")])
    summary = await poller_mod.poll_once(object(), object(), adapter)
    assert summary == {"regions": [], "written": 0}
    assert adapter.stored == []


@pytest.mark.asyncio
async def test_run_loop_disabled_returns_immediately(monkeypatch):
    monkeypatch.setenv("DIESEL_PRICE_POLL_ENABLED", "false")
    # Should return without constructing a client or looping.
    await poller_mod.run_diesel_poll_loop(object(), object())


@pytest.mark.asyncio
async def test_run_loop_one_cycle_then_stop(monkeypatch):
    monkeypatch.setenv("DIESEL_PRICE_POLL_ENABLED", "true")

    async def fake_regions(static_pool):
        return ["DE"]

    monkeypatch.setattr(poller_mod, "_depot_regions", fake_regions)

    # Inject a fake client; break the loop after the first sleep.
    class _FakeClient:
        source = "eu_oil_bulletin"

        async def aclose(self):
            pass

    # Make poll_once a no-op spy and asyncio.sleep raise to exit the loop.
    calls = {"n": 0}

    async def fake_poll_once(static_pool, ts_pool, adapter):
        calls["n"] += 1
        return {"regions": ["DE"], "fetched": 0, "written": 0}

    monkeypatch.setattr(poller_mod, "poll_once", fake_poll_once)

    import asyncio

    async def boom(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(poller_mod.asyncio, "sleep", boom)

    with pytest.raises(asyncio.CancelledError):
        await poller_mod.run_diesel_poll_loop(
            object(), object(), interval_s=0.01, client=_FakeClient()
        )
    assert calls["n"] == 1  # ran exactly one cycle before the sleep aborted


@pytest.mark.asyncio
async def test_run_loop_isolates_cycle_error(monkeypatch):
    monkeypatch.setenv("DIESEL_PRICE_POLL_ENABLED", "true")

    class _FakeClient:
        source = "eu_oil_bulletin"

        async def aclose(self):
            pass

    async def boom_poll(static_pool, ts_pool, adapter):
        raise RuntimeError("cycle failed")

    monkeypatch.setattr(poller_mod, "poll_once", boom_poll)

    import asyncio

    async def stop_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(poller_mod.asyncio, "sleep", stop_sleep)

    # A failing cycle must be caught (logged), then the loop reaches sleep and
    # we abort there — i.e. the error did NOT propagate out of the cycle.
    with pytest.raises(asyncio.CancelledError):
        await poller_mod.run_diesel_poll_loop(
            object(), object(), interval_s=0.01, client=_FakeClient()
        )
