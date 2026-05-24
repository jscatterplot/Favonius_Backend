"""Tests for ``GET /depots/{depot_id}/savings-summary`` and the underlying
:func:`src.api.savings.compute_savings_summary` computation.

Covers the seven required response fields, every missing-data path (no
sessions, no bidding zone, no price rows in window), the timezone
boundary handling (depot tz vs UTC), and RBAC against the standard
``_require_depot_access`` gate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.api.main import app
from src.api.savings import (
    SavingsSummary,
    _month_start_local_as_utc,
    compute_savings_summary,
)
from src.security.tenant_mirror import ensure_tenant_mirrored

AUTH_HDR = {"Authorization": "Bearer test-token"}


def _user(role: str = "customer_admin", org_id: str | None = None) -> dict:
    meta: dict = {"favonius_role": role}
    if role != "favonius_admin":
        meta["organization_id"] = org_id or str(uuid4())
    return {"sub": str(uuid4()), "app_metadata": meta}


def _override_user(user: dict) -> None:
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user


# ── _month_start_local_as_utc ───────────────────────────────────────────────


class TestMonthStartLocalAsUtc:
    def test_utc_depot_returns_naive_month_start(self):
        now = datetime(2026, 5, 22, 20, 40, tzinfo=timezone.utc)
        result = _month_start_local_as_utc(now, "UTC")
        assert result == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    def test_vilnius_tz_shifts_back_to_april_30_utc(self):
        """At 2026-05-01 01:30 Vilnius (UTC+3 DST), the month already
        started locally — UTC for month start is 2026-04-30 21:00."""
        now = datetime(2026, 5, 1, 1, 30, tzinfo=timezone.utc)
        # Vilnius is UTC+3 in May → local "now" is 04:30 on May 1.
        result = _month_start_local_as_utc(now, "Europe/Vilnius")
        # Local month start: 2026-05-01 00:00 +03:00 = 2026-04-30 21:00 UTC.
        assert result == datetime(2026, 4, 30, 21, 0, tzinfo=timezone.utc)

    def test_missing_tz_falls_back_to_utc(self):
        now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        assert _month_start_local_as_utc(now, None) == datetime(
            2026, 3, 1, 0, 0, tzinfo=timezone.utc
        )

    def test_invalid_tz_falls_back_to_utc(self):
        now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        # Don't error on a bad tz string — operational savings summary
        # should never 500 on a depot with a typo'd timezone.
        assert _month_start_local_as_utc(now, "Not/A_Real_TZ") == datetime(
            2026, 3, 1, 0, 0, tzinfo=timezone.utc
        )


# ── compute_savings_summary ─────────────────────────────────────────────────


@pytest.fixture
def pools_with_data():
    """Build a pair of (static_pool, ts_pool) mocks with sensible defaults.

    Tests override individual ``fetchrow`` return values as needed.
    """
    static_pool = MagicMock()
    static_conn = AsyncMock()
    static_pool.acquire.return_value.__aenter__.return_value = static_conn
    static_pool.acquire.return_value.__aexit__.return_value = None

    ts_pool = MagicMock()
    ts_conn = AsyncMock()
    ts_pool.acquire.return_value.__aenter__.return_value = ts_conn
    ts_pool.acquire.return_value.__aexit__.return_value = None

    return static_pool, ts_pool, static_conn, ts_conn


def _run(coro):
    return asyncio.run(coro)


class TestComputeSavingsSummary:
    def test_happy_path_returns_seven_required_fields(self, pools_with_data):
        static_pool, ts_pool, _, ts_conn = pools_with_data
        depot_id = str(uuid4())
        now = datetime(2026, 5, 22, 20, 40, tzinfo=timezone.utc)

        ts_conn.fetchrow.side_effect = [
            {"actual": 2143.50, "energy": 17880.0},  # sessions agg
            {"avg_eur_mwh": 152.75},  # 0.15275 €/kWh
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "Europe/Vilnius"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4()), "acme-002": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, depot_id, now=now))

        assert isinstance(result, SavingsSummary)
        assert result.current_month_eur == pytest.approx(2143.50)
        # baseline = 17880 kWh × 0.15275 €/kWh = 2731.17
        assert result.baseline_month_eur == pytest.approx(2731.17, abs=0.01)
        # savings = baseline - actual ≈ 587.67
        assert result.saved_eur == pytest.approx(587.67, abs=0.02)
        # pct = 587.67 / 2731.17 × 100 ≈ 21.5%
        assert result.saved_pct == pytest.approx(21.5, abs=0.1)
        assert result.period_start == datetime(2026, 4, 30, 21, 0, tzinfo=timezone.utc)
        assert result.period_end == now
        assert result.as_of == now

    def test_missing_depot_raises_value_error(self, pools_with_data):
        """No sites row → ValueError('... not found') → 404 via global handler.

        Matters for favonius_admin, whose access check bypasses the
        depot-existence lookup; without this a bad UUID would read as an
        empty month (200 + zeros) instead of a 404.
        """
        static_pool, ts_pool, _, ts_conn = pools_with_data
        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            with pytest.raises(ValueError, match="not found"):
                _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))
        # We must not have touched the time-series pool for a depot that
        # doesn't exist.
        ts_conn.fetchrow.assert_not_called()

    def test_no_sessions_yet_returns_all_zeros(self, pools_with_data):
        static_pool, ts_pool, _, ts_conn = pools_with_data
        # Only the sessions aggregation runs — price lookup is skipped
        # because total_energy_kwh == 0.
        ts_conn.fetchrow.return_value = {"actual": 0.0, "energy": 0.0}

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        assert result.current_month_eur == 0.0
        assert result.baseline_month_eur == 0.0
        assert result.saved_eur == 0.0
        assert result.saved_pct == 0.0
        # Price lookup must NOT happen when there's no energy to baseline against.
        assert ts_conn.fetchrow.call_count == 1

    def test_no_bidding_zone_zero_baseline(self, pools_with_data):
        """Depot with no resolvable bidding zone → baseline degrades to 0."""
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.return_value = {"actual": 100.0, "energy": 500.0}

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        assert result.current_month_eur == 100.0
        assert result.baseline_month_eur == 0.0
        assert result.saved_eur == -100.0
        assert result.saved_pct == 0.0  # baseline 0 → forced to 0
        # No price query attempted.
        assert ts_conn.fetchrow.call_count == 1

    def test_zone_known_but_no_prices_zero_baseline(self, pools_with_data):
        """electricity_prices has zero rows in window → avg returns None → baseline = 0."""
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.side_effect = [
            {"actual": 100.0, "energy": 500.0},
            {"avg_eur_mwh": None},  # AVG over zero rows
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        assert result.baseline_month_eur == 0.0
        assert result.saved_pct == 0.0

    def test_imported_only_depot_no_chargers(self, pools_with_data):
        """Depot with no live chargers (only XLSX-imported sessions): site_id
        fallback must still match the import rows."""
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.side_effect = [
            {"actual": 50.0, "energy": 200.0},
            {"avg_eur_mwh": 100.0},
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},  # no chargers
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10Y1001A1001A82H",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        assert result.current_month_eur == 50.0
        # 200 kWh × 0.100 €/kWh = 20.00
        assert result.baseline_month_eur == pytest.approx(20.00, abs=0.01)
        # Note: this scenario produces negative savings (actual > baseline) —
        # the spec doesn't say to clamp, and the frontend can render it.

    def test_baseline_higher_than_actual_positive_savings(self, pools_with_data):
        """Standard sunny-day case: smart charging beats the flat-rate baseline."""
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.side_effect = [
            {"actual": 800.0, "energy": 10_000.0},
            {"avg_eur_mwh": 100.0},  # 0.10 €/kWh
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        # baseline = 10000 kWh × 0.10 = 1000.00; saved = 1000 - 800 = 200
        assert result.baseline_month_eur == pytest.approx(1000.0)
        assert result.saved_eur == pytest.approx(200.0)
        assert result.saved_pct == pytest.approx(20.0)

    def test_negative_avg_price_preserves_signed_baseline(self, pools_with_data):
        """ENTSO-E day-ahead prices go negative in high-renewable hours.

        A negative average is real data, not a missing-data sentinel — the
        baseline must keep its sign instead of collapsing to 0 (Codex P2 on
        PR #232). Here the unmanaged baseline is negative (you'd have been
        paid to consume), and the depot did worse than that baseline, so
        saved_eur and saved_pct are both negative.
        """
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.side_effect = [
            {"actual": -5.0, "energy": 1000.0},  # we were paid €5
            {"avg_eur_mwh": -10.0},  # -0.01 €/kWh average
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        # baseline = 1000 kWh × -0.01 €/kWh = -10.00 (NOT zeroed out)
        assert result.baseline_month_eur == pytest.approx(-10.0)
        assert result.current_month_eur == pytest.approx(-5.0)
        # saved = -10.00 - (-5.00) = -5.00 → we did €5 worse than baseline
        assert result.saved_eur == pytest.approx(-5.0)
        # pct uses abs(baseline) denominator so the sign reflects worse(-)
        # vs better(+): -5 / |−10| × 100 = -50.0%
        assert result.saved_pct == pytest.approx(-50.0)

    def test_negative_price_but_optimized_better_positive_savings(self, pools_with_data):
        """Negative-price month where smart charging beat the baseline.

        baseline is negative (you'd have been paid), and the depot got
        paid even more by loading into the negative hours → positive
        savings.
        """
        static_pool, ts_pool, _, ts_conn = pools_with_data
        ts_conn.fetchrow.side_effect = [
            {"actual": -20.0, "energy": 1000.0},  # we were paid €20
            {"avg_eur_mwh": -10.0},  # baseline -0.01 €/kWh
        ]

        with (
            patch(
                "src.api.savings.db_queries.get_depot_by_id",
                new_callable=AsyncMock,
                return_value={"timezone": "UTC"},
            ),
            patch(
                "src.api.savings.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"acme-001": str(uuid4())},
            ),
            patch(
                "src.api.savings.db_queries.resolve_bidding_zone",
                new_callable=AsyncMock,
                return_value="10YLT-1001A0008Q",
            ),
        ):
            result = _run(compute_savings_summary(static_pool, ts_pool, str(uuid4())))

        # baseline = -10.00; saved = -10.00 - (-20.00) = +10.00 (we beat it)
        assert result.baseline_month_eur == pytest.approx(-10.0)
        assert result.saved_eur == pytest.approx(10.0)
        # +10 / |−10| × 100 = +100.0%
        assert result.saved_pct == pytest.approx(100.0)


# ── /depots/{depot_id}/savings-summary endpoint ─────────────────────────────


@pytest.fixture(autouse=True)
def _clear_deps():
    yield
    app.dependency_overrides.clear()


class TestSavingsSummaryEndpoint:
    URL_TMPL = "/depots/{}/savings-summary"

    def test_happy_path_returns_all_seven_required_fields(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("customer_admin"))

        fake_summary = SavingsSummary(
            current_month_eur=2143.50,
            baseline_month_eur=2731.20,
            saved_eur=587.70,
            saved_pct=21.5,
            period_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 22, 20, 40, tzinfo=timezone.utc),
            as_of=datetime(2026, 5, 22, 20, 38, tzinfo=timezone.utc),
        )
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.compute_savings_summary",
                new_callable=AsyncMock,
                return_value=fake_summary,
            ),
        ):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)

        assert response.status_code == 200
        body = response.json()
        # All seven fields present and non-null.
        for key in (
            "current_month_eur",
            "baseline_month_eur",
            "saved_eur",
            "saved_pct",
            "period_start",
            "period_end",
            "as_of",
        ):
            assert key in body, f"Missing required field: {key}"
            assert body[key] is not None, f"Field {key} must be non-null"

        assert body["current_month_eur"] == pytest.approx(2143.50)
        assert body["baseline_month_eur"] == pytest.approx(2731.20)
        assert body["saved_eur"] == pytest.approx(587.70)
        assert body["saved_pct"] == pytest.approx(21.5)
        # ISO 8601 with explicit UTC Z suffix.
        assert body["period_start"].endswith("Z")
        assert body["period_end"].endswith("Z")
        assert body["as_of"].endswith("Z")

    def test_empty_depot_returns_all_zeros(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("customer_operator"))

        fake_summary = SavingsSummary(
            current_month_eur=0.0,
            baseline_month_eur=0.0,
            saved_eur=0.0,
            saved_pct=0.0,
            period_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 22, 20, 40, tzinfo=timezone.utc),
            as_of=datetime(2026, 5, 22, 20, 38, tzinfo=timezone.utc),
        )
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.compute_savings_summary",
                new_callable=AsyncMock,
                return_value=fake_summary,
            ),
        ):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)

        assert response.status_code == 200
        body = response.json()
        assert body["current_month_eur"] == 0.0
        assert body["baseline_month_eur"] == 0.0
        assert body["saved_eur"] == 0.0
        assert body["saved_pct"] == 0.0

    def test_customer_operator_can_read(self, client, mock_db_pool):
        """RBAC: spec requires customer_operator can read this endpoint."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("customer_operator"))

        fake_summary = SavingsSummary(
            current_month_eur=10.0,
            baseline_month_eur=12.0,
            saved_eur=2.0,
            saved_pct=16.7,
            period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 22, tzinfo=timezone.utc),
            as_of=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.compute_savings_summary",
                new_callable=AsyncMock,
                return_value=fake_summary,
            ),
        ):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)
        assert response.status_code == 200

    def test_favonius_admin_can_read(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("favonius_admin"))

        fake_summary = SavingsSummary(
            current_month_eur=10.0,
            baseline_month_eur=12.0,
            saved_eur=2.0,
            saved_pct=16.7,
            period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 22, tzinfo=timezone.utc),
            as_of=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.compute_savings_summary",
                new_callable=AsyncMock,
                return_value=fake_summary,
            ),
        ):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)
        assert response.status_code == 200

    def test_viewer_role_blocked(self, client, mock_db_pool):
        """verify_depot_access rejects ``viewer`` — spec says only the three roles can read."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("viewer"))

        # Don't patch verify_depot_access — let the real one reject the
        # viewer role.
        with patch("src.api.main.db_pools", pool):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)
        assert response.status_code == 403

    def test_invalid_depot_uuid_400(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        _override_user(_user("customer_admin"))
        with patch("src.api.main.db_pools", pool):
            response = client.get(
                "/depots/not-a-uuid/savings-summary", headers=AUTH_HDR
            )
        assert response.status_code == 400

    def test_missing_depot_returns_404(self, client, mock_db_pool):
        """favonius_admin + valid-but-nonexistent depot → 404, not 200 zeros.

        favonius_admin bypasses the depot-existence check in
        verify_depot_access, so the ValueError raised by
        compute_savings_summary is what produces the documented 404.
        """
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        _override_user(_user("favonius_admin"))
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.compute_savings_summary",
                new_callable=AsyncMock,
                side_effect=ValueError(f"Depot {depot_id} not found"),
            ),
        ):
            response = client.get(self.URL_TMPL.format(depot_id), headers=AUTH_HDR)
        assert response.status_code == 404
