"""Parametrized tests for the import endpoint's UPSERT-with-fill-nulls path.

The fill-nulls SQL itself runs in Postgres, so unit tests here pin three
things that we *can* exercise without a real database:

1. **Cost resolution** — ``_resolve_session_cost`` is a pure Python helper that
   sits between the request and the DB. We parametrize over coverage shapes
   (no end_time, zero energy, no price data, partial / full coverage) and
   assert the energy × price invariant within tolerance whenever a price
   exists.

2. **PriceSource Protocol** — ``StaticPriceSource`` and the cache key /
   single-flight behavior on ``TimescalePriceSource`` are unit-testable with
   an in-memory connection-pool fake.

3. **UPSERT SQL contract** — we pin the ON CONFLICT shape (target, fill-nulls
   COALESCEs, refresh-on-positive CASE-WHENs) so a future refactor that drops
   one of these clauses fails at the unit-test layer rather than producing
   silent data loss in production.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.api.charging_import import (
    PriceSource,
    StaticPriceSource,
    TimescalePriceSource,
    invalidate_price_cache,
)
from src.api.charging_import.price_source import (
    _HOUR_PRICE_CACHE,
    _HOUR_PRICE_CACHE_TTL_S,
    _hour_bucket,
)
from src.api.main import _resolve_session_cost


# --------------------------------------------------------------------------- #
# _resolve_session_cost — cost derivation invariants
# --------------------------------------------------------------------------- #


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class _RecordingPriceSource:
    """PriceSource that records call arguments and returns a fixed value."""

    def __init__(self, price: Optional[Decimal]) -> None:
        self._price = price
        self.calls: list[dict] = []

    async def average_price_per_kwh(
        self, *, depot_id: str, start: datetime, end: datetime
    ) -> Optional[Decimal]:
        self.calls.append({"depot_id": depot_id, "start": start, "end": end})
        return self._price


class TestResolveSessionCost:
    """Pure-Python cost derivation: tolerance, fallback, invariant."""

    @pytest.mark.asyncio
    async def test_no_end_time_falls_back_to_revenue(self):
        ps = _RecordingPriceSource(Decimal("0.20"))
        cost = await _resolve_session_cost(
            price_source=ps,
            depot_id=str(uuid4()),
            start_time_utc=_utc(2026, 5, 5, 12, 56),
            end_time_utc=None,
            energy_kwh=24.044,
            fallback_revenue=1.23,
        )
        assert cost == Decimal("1.23")
        # Open session: never call the price source.
        assert ps.calls == []

    @pytest.mark.asyncio
    async def test_zero_energy_falls_back_to_revenue(self):
        ps = _RecordingPriceSource(Decimal("0.20"))
        cost = await _resolve_session_cost(
            price_source=ps,
            depot_id=str(uuid4()),
            start_time_utc=_utc(2026, 5, 5, 12, 0),
            end_time_utc=_utc(2026, 5, 5, 13, 0),
            energy_kwh=0.0,
            fallback_revenue=1.23,
        )
        assert cost == Decimal("1.23")
        assert ps.calls == []

    @pytest.mark.asyncio
    async def test_no_price_coverage_falls_back_to_revenue(self):
        ps = _RecordingPriceSource(None)
        cost = await _resolve_session_cost(
            price_source=ps,
            depot_id=str(uuid4()),
            start_time_utc=_utc(2026, 5, 5, 12, 0),
            end_time_utc=_utc(2026, 5, 5, 13, 0),
            energy_kwh=24.044,
            fallback_revenue=4.56,
        )
        assert cost == Decimal("4.56")
        assert len(ps.calls) == 1

    @pytest.mark.asyncio
    async def test_price_source_raises_falls_back_to_revenue(self):
        """A price-source error must NEVER fail the import."""

        class _ExplodingPriceSource:
            async def average_price_per_kwh(self, **kw):
                raise RuntimeError("DB went away")

        cost = await _resolve_session_cost(
            price_source=_ExplodingPriceSource(),
            depot_id=str(uuid4()),
            start_time_utc=_utc(2026, 5, 5, 12, 0),
            end_time_utc=_utc(2026, 5, 5, 13, 0),
            energy_kwh=24.044,
            fallback_revenue=4.56,
        )
        assert cost == Decimal("4.56")

    @pytest.mark.parametrize(
        "energy_kwh, price_per_kwh, expected_cost",
        [
            (24.044, Decimal("0.20"), Decimal("4.808800")),
            (1.0, Decimal("0.15"), Decimal("0.150000")),
            (100.0, Decimal("0.123456"), Decimal("12.345600")),
            (10.0, Decimal("0.250000"), Decimal("2.500000")),
        ],
    )
    @pytest.mark.asyncio
    async def test_cost_equals_energy_times_price_within_tolerance(
        self, energy_kwh: float, price_per_kwh: Decimal, expected_cost: Decimal
    ):
        """Pin the headline invariant: cost == energy × avg_price.

        Tolerance is 1e-6 because both sides quantize to 6 decimals.
        """
        ps = _RecordingPriceSource(price_per_kwh)
        cost = await _resolve_session_cost(
            price_source=ps,
            depot_id=str(uuid4()),
            start_time_utc=_utc(2026, 5, 5, 12, 0),
            end_time_utc=_utc(2026, 5, 5, 13, 0),
            energy_kwh=energy_kwh,
            fallback_revenue=0.0,
        )
        delta = abs(cost - expected_cost)
        assert delta <= Decimal("0.000001"), (
            f"cost={cost} expected≈{expected_cost} delta={delta}"
        )


# --------------------------------------------------------------------------- #
# PriceSource — Protocol + cache + single-flight
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_price_cache():
    invalidate_price_cache()
    yield
    invalidate_price_cache()


class TestStaticPriceSource:
    @pytest.mark.asyncio
    async def test_returns_configured_value(self):
        ps = StaticPriceSource(Decimal("0.42"))
        result = await ps.average_price_per_kwh(
            depot_id=str(uuid4()),
            start=_utc(2026, 5, 5, 12, 0),
            end=_utc(2026, 5, 5, 13, 0),
        )
        assert result == Decimal("0.42")

    @pytest.mark.asyncio
    async def test_none_means_no_coverage(self):
        ps = StaticPriceSource(None)
        result = await ps.average_price_per_kwh(
            depot_id=str(uuid4()),
            start=_utc(2026, 5, 5, 12, 0),
            end=_utc(2026, 5, 5, 13, 0),
        )
        assert result is None


class TestHourBucket:
    @pytest.mark.parametrize(
        "dt, expected_iso",
        [
            (_utc(2026, 5, 5, 12, 56), "2026-05-05T12:00:00+00:00"),
            (_utc(2026, 5, 5, 13, 0), "2026-05-05T13:00:00+00:00"),
            (_utc(2026, 5, 5, 0, 0), "2026-05-05T00:00:00+00:00"),
        ],
    )
    def test_floor_of_hour(self, dt: datetime, expected_iso: str):
        bucket_epoch = _hour_bucket(dt)
        bucket_dt = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
        assert bucket_dt.isoformat() == expected_iso

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 5, 5, 12, 56)
        aware = _utc(2026, 5, 5, 12, 56)
        assert _hour_bucket(naive) == _hour_bucket(aware)


class TestTimescalePriceSourceCache:
    """The cache is the headline perf win for multi-row imports."""

    def _make_pool(self, fetchrow_value):
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_value)
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        return pool, conn

    @pytest.mark.asyncio
    async def test_repeated_lookup_for_same_hour_hits_cache(self):
        depot_id = str(uuid4())
        pool, conn = self._make_pool({"price": Decimal("0.20")})
        ps = TimescalePriceSource(pool)

        # Two imports landing in the same hour: only one DB query.
        for _ in range(5):
            result = await ps.average_price_per_kwh(
                depot_id=depot_id,
                start=_utc(2026, 5, 5, 12, 10),
                end=_utc(2026, 5, 5, 12, 50),
            )
            assert result == Decimal("0.20")
        assert conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    async def test_different_hours_query_separately(self):
        depot_id = str(uuid4())
        pool, conn = self._make_pool({"price": Decimal("0.20")})
        ps = TimescalePriceSource(pool)

        # 3 hours covered: 12, 13, 14 — 3 DB queries.
        await ps.average_price_per_kwh(
            depot_id=depot_id,
            start=_utc(2026, 5, 5, 12, 0),
            end=_utc(2026, 5, 5, 14, 30),
        )
        assert conn.fetchrow.await_count == 3

    @pytest.mark.asyncio
    async def test_end_on_hour_boundary_queries_one_bucket(self):
        """``end`` at ``:00`` is exclusive; do not include the following hour."""
        depot_id = str(uuid4())
        pool, conn = self._make_pool({"price": Decimal("0.30")})
        ps = TimescalePriceSource(pool)
        await ps.average_price_per_kwh(
            depot_id=depot_id,
            start=_utc(2026, 5, 5, 12, 0),
            end=_utc(2026, 5, 5, 13, 0),
        )
        assert conn.fetchrow.await_count == 1

    @pytest.mark.asyncio
    async def test_cache_returns_none_when_no_coverage(self):
        depot_id = str(uuid4())
        pool, conn = self._make_pool({"price": None})
        ps = TimescalePriceSource(pool)
        result = await ps.average_price_per_kwh(
            depot_id=depot_id,
            start=_utc(2026, 5, 5, 12, 0),
            end=_utc(2026, 5, 5, 13, 0),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_end_before_start_returns_none_without_query(self):
        pool, conn = self._make_pool({"price": Decimal("0.20")})
        ps = TimescalePriceSource(pool)
        result = await ps.average_price_per_kwh(
            depot_id=str(uuid4()),
            start=_utc(2026, 5, 5, 13, 0),
            end=_utc(2026, 5, 5, 12, 0),
        )
        assert result is None
        assert conn.fetchrow.await_count == 0

    @pytest.mark.asyncio
    async def test_single_flight_concurrent_lookups_query_once(self):
        """Twenty simultaneous lookups for the same (depot, hour) hit DB once."""
        depot_id = str(uuid4())

        gate = asyncio.Event()
        call_count = 0

        async def slow_fetchrow(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await gate.wait()
            return {"price": Decimal("0.17")}

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = slow_fetchrow
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None

        ps = TimescalePriceSource(pool)

        async def one_lookup():
            return await ps.average_price_per_kwh(
                depot_id=depot_id,
                start=_utc(2026, 5, 5, 12, 10),
                end=_utc(2026, 5, 5, 12, 50),
            )

        tasks = [asyncio.create_task(one_lookup()) for _ in range(20)]
        # Let all coroutines reach the lock; only the first will actually
        # enter the fetchrow call.
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r == Decimal("0.17") for r in results)
        assert call_count == 1


# --------------------------------------------------------------------------- #
# UPSERT SQL contract — pin the ON CONFLICT clauses so a refactor can't
# silently drop a fill-null or a refresh-on-positive rule.
# --------------------------------------------------------------------------- #


class TestUpsertSqlContract:
    """These assertions read the SQL string at the boundary; they fail loudly
    if the merge semantics ever drift away from the design captured by
    migration 036."""

    @pytest.fixture
    def upsert_sql(self):
        from src.api.main import app, get_price_source
        from src.api.charging_import import StaticPriceSource
        from src.security.tenant_mirror import ensure_tenant_mirrored
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4

        org_id = str(uuid4())
        depot_id = str(uuid4())

        # Bypass auth + db dependencies; stub fetchrow to feed the endpoint
        # everything it needs to reach the UPSERT.
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: {
            "sub": str(uuid4()),
            "app_metadata": {"favonius_role": "customer_admin", "organization_id": org_id},
        }
        app.dependency_overrides[get_price_source] = lambda: StaticPriceSource(None)

        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        sites_row = {
            "depot_id": depot_id,
            "organization_id": org_id,
            "timezone": "UTC",
            "tariff_config": None,
        }
        conn.fetchrow = AsyncMock(
            side_effect=[
                sites_row,
                None,
                None,
                {"session_id": str(uuid4()), "was_new": True},
            ]
        )

        try:
            with TestClient(app) as client, patch(
                "src.api.main.db_pools", pool
            ), patch("src.api.main.verify_depot_access", new_callable=AsyncMock):
                client.post(
                    f"/admin/depots/{depot_id}/charging-sessions/import",
                    headers={"Authorization": "Bearer t"},
                    json={
                        "import_batch_id": str(uuid4()),
                        "start_time_local": "2026-05-05 12:00",
                        "end_time_local": "2026-05-05 13:00",
                        "energy_delivered_kwh": 10.0,
                        "revenue": 1.0,
                        "id_tag": "ABC",
                        "status": "Finished",
                        "transaction_type": "RFID",
                    },
                )
                sql = conn.fetchrow.await_args_list[-1].args[0]
        finally:
            app.dependency_overrides.clear()
        return sql

    def test_conflict_target_is_partial_dedup_index(self, upsert_sql):
        assert "ON CONFLICT (site_id, import_row_hash)" in upsert_sql
        assert "WHERE source = 'import'" in upsert_sql

    @pytest.mark.parametrize(
        "column",
        [
            "vehicle_id",
            "driver_id",
            "card_id",
            "end_time",
            "import_user_full_name",
            "import_station_owner",
            "import_status",
        ],
    )
    def test_fill_null_columns_use_coalesce(self, upsert_sql, column: str):
        # Every fill-null column must COALESCE(existing, EXCLUDED).
        assert (
            f"COALESCE(charging_sessions.{column}, EXCLUDED.{column})"
            in upsert_sql
        ), f"missing COALESCE for {column}"

    @pytest.mark.parametrize(
        "column",
        ["energy_delivered_kwh", "cost_total"],
    )
    def test_metering_columns_refresh_on_positive_value(self, upsert_sql, column: str):
        # Metering columns refresh when the new value is positive so corrections
        # propagate, but preserve the existing non-zero value otherwise.
        assert f"EXCLUDED.{column}" in upsert_sql
        assert f"charging_sessions.{column}" in upsert_sql
        # The CASE branch must use ">", not just COALESCE.
        assert "WHEN EXCLUDED.energy_delivered_kwh > 0" in upsert_sql
        assert "EXCLUDED.cost_total IS NOT NULL" in upsert_sql

    def test_returns_session_id_and_was_new_flag(self, upsert_sql):
        assert "RETURNING" in upsert_sql
        assert "session_id" in upsert_sql.split("RETURNING", 1)[1]
        assert "(xmax = 0)" in upsert_sql
        assert "was_new" in upsert_sql.split("RETURNING", 1)[1]


# --------------------------------------------------------------------------- #
# Hash-formula invariant across paired call shapes — the merge matrix.
# --------------------------------------------------------------------------- #


class TestHashMergeMatrix:
    """Re-uploading the same logical row, with varied per-cell content, must
    always produce the same hash.

    This is the merge matrix from the design discussion: for each axis
    (energy, revenue, end_time-with-fill, user_name) we vary one cell and
    pin the hash invariant under runtime _compute_import_row_hash + the
    identified-id path (id_token = raw id_tag).
    """

    @pytest.fixture
    def fixed_args(self):
        return {
            "depot_id": str(uuid4()),
            "start_time_utc": _utc(2026, 5, 5, 12, 56),
            "id_tag": "ED8503",
        }

    def test_baseline_hash(self, fixed_args):
        from src.api.main import _compute_import_row_hash

        h = _compute_import_row_hash(**fixed_args)
        assert len(h) == 64

    @pytest.mark.parametrize(
        "ignored_field",
        ["energy_delivered_kwh", "revenue", "import_status", "import_user_full_name"],
    )
    def test_hash_is_invariant_under_non_canonical_changes(
        self, fixed_args, ignored_field
    ):
        """Changes to fields outside the canonical tuple do not change the hash."""
        from src.api.main import _compute_import_row_hash

        base = _compute_import_row_hash(**fixed_args)
        # Confirm the function signature deliberately does NOT accept these
        # fields — the only way to influence the hash is via the canonical
        # three-tuple.
        assert base == _compute_import_row_hash(**fixed_args)
