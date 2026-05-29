"""Unit tests for the EV-vs-diesel comparison math (pure core)."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.billing.cost_per_km import (
    VehicleInput,
    build_fleet_comparison,
)

_PS = date(2026, 5, 18)
_PE = date(2026, 5, 24)


def _vi(vid, *, vtype="bus", distance, energy=100.0, cost, l100=30.0, default=False):
    return VehicleInput(
        vehicle_id=vid,
        vehicle_type=vtype,
        distance_km=distance,
        ev_energy_kwh=energy,
        ev_cost_eur=cost,
        diesel_l_per_100km=l100,
        baseline_is_default=default,
    )


def _build(vehicles, *, price=1.50, currency="EUR"):
    return build_fleet_comparison(
        vehicles,
        diesel_price_eur_per_l=price,
        diesel_region="LT",
        currency=currency,
        period_start=_PS,
        period_end=_PE,
    )


# ── Happy path ────────────────────────────────────────────────────────────────


def test_single_vehicle_math():
    # 200 km, EV cost €40 → €0.20/km. Diesel: 30 L/100km × 200km = 60 L × €1.50
    # = €90 → €0.45/km. Savings = (0.45-0.20)/0.45 = 55.56%.
    res = _build([_vi("v1", distance=200.0, cost=40.0, l100=30.0)], price=1.50)
    r = res.per_vehicle[0]
    assert r.ev_eur_per_km == pytest.approx(0.20)
    assert r.diesel_litres == pytest.approx(60.0)
    assert r.diesel_cost_eur == pytest.approx(90.0)
    assert r.diesel_eur_per_km == pytest.approx(0.45)
    assert r.pct_difference == pytest.approx(55.5556, abs=1e-3)
    assert r.priceable is True
    assert res.fleet_pct_difference == pytest.approx(55.5556, abs=1e-3)
    assert res.priceable_vehicle_count == 1
    assert res.unpriceable_vehicle_count == 0


def test_fleet_is_sum_over_sum_not_mean_of_ratios():
    # v1: 100 km, €10 EV → €0.10/km. v2: 900 km, €270 EV → €0.30/km.
    # Mean of ratios = 0.20. Σcost/Σdist = 280/1000 = €0.28/km (correct, weighted
    # toward the long runner). Diesel L/100km=10 for both, price €1.00:
    #   v1 diesel = 10 L × €1 = €10 → €0.10/km; v2 = 90 L × €1 = €90 → €0.10/km.
    res = _build(
        [
            _vi("v1", distance=100.0, cost=10.0, l100=10.0),
            _vi("v2", distance=900.0, cost=270.0, l100=10.0),
        ],
        price=1.00,
    )
    assert res.fleet_ev_eur_per_km == pytest.approx(0.28)  # 280/1000, NOT 0.20
    assert res.fleet_diesel_eur_per_km == pytest.approx(0.10)
    assert res.total_distance_km == pytest.approx(1000.0)


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_no_distance_excluded_from_fleet_but_energy_shown():
    res = _build(
        [
            _vi("v1", distance=200.0, cost=40.0, l100=30.0),
            _vi("v2", distance=None, energy=55.0, cost=20.0, l100=30.0),
        ],
        price=1.50,
    )
    v2 = next(r for r in res.per_vehicle if r.vehicle_id == "v2")
    assert v2.ev_eur_per_km is None
    assert v2.priceable is False
    assert "no_distance" in v2.notes
    assert v2.ev_energy_kwh == pytest.approx(55.0)  # energy still reported
    # Fleet aggregates exclude v2 entirely.
    assert res.total_distance_km == pytest.approx(200.0)
    assert res.priceable_vehicle_count == 1
    assert res.unpriceable_vehicle_count == 1


def test_zero_distance_no_divide_by_zero():
    res = _build([_vi("v1", distance=0.0, cost=40.0)], price=1.50)
    r = res.per_vehicle[0]
    assert r.ev_eur_per_km is None
    assert r.diesel_eur_per_km is None
    assert "zero_distance" in r.notes
    assert r.priceable is False
    assert res.fleet_ev_eur_per_km is None  # no priceable distance


def test_no_diesel_price_marks_unpriceable():
    res = _build([_vi("v1", distance=200.0, cost=40.0)], price=None)
    r = res.per_vehicle[0]
    assert r.ev_eur_per_km == pytest.approx(0.20)  # EV side still computed
    assert r.diesel_eur_per_km is None
    assert "no_diesel_price" in r.notes
    assert r.priceable is False
    assert res.unpriceable_vehicle_count == 1
    assert res.fleet_pct_difference is None
    assert "no_diesel_price_for_region" in res.notes


def test_no_ev_cost_excluded():
    # A vehicle with distance but no EV cost (e.g. unpriceable sessions) → no €/km.
    res = _build([_vi("v1", distance=200.0, cost=None)], price=1.50)
    r = res.per_vehicle[0]
    assert r.ev_eur_per_km is None
    assert r.priceable is False


def test_baseline_default_flagged():
    res = _build([_vi("v1", distance=100.0, cost=10.0, default=True)], price=1.0)
    assert "baseline_default" in res.per_vehicle[0].notes


def test_currency_mismatch_flagged():
    res = _build([_vi("v1", distance=100.0, cost=10.0)], currency="USD")
    assert res.currency_mismatch is True
    assert any("mixes currencies" in n for n in res.notes)


def test_currency_match_no_flag():
    res = _build([_vi("v1", distance=100.0, cost=10.0)], currency="EUR")
    assert res.currency_mismatch is False


def test_by_vehicle_type_rollup():
    res = _build(
        [
            _vi("v1", vtype="bus", distance=100.0, cost=10.0, l100=30.0),
            _vi("v2", vtype="bus", distance=100.0, cost=20.0, l100=30.0),
            _vi("v3", vtype="van", distance=200.0, cost=20.0, l100=10.0),
        ],
        price=1.0,
    )
    by_type = {a.vehicle_type: a for a in res.by_vehicle_type}
    assert by_type["bus"].vehicle_count == 2
    assert by_type["bus"].distance_km == pytest.approx(200.0)
    assert by_type["bus"].ev_cost_eur == pytest.approx(30.0)
    assert by_type["bus"].ev_eur_per_km == pytest.approx(0.15)  # 30/200
    assert by_type["van"].vehicle_count == 1


def test_empty_fleet():
    res = _build([], price=1.50)
    assert res.per_vehicle == []
    assert res.fleet_ev_eur_per_km is None
    assert res.fleet_pct_difference is None
    assert res.priceable_vehicle_count == 0


def test_diesel_more_expensive_positive_savings():
    # EV €0.10/km vs diesel €0.30/km → +66.7% savings (positive = EV cheaper).
    res = _build([_vi("v1", distance=100.0, cost=10.0, l100=20.0)], price=1.5)
    # diesel: 20 L × €1.5 = €30 → €0.30/km.
    assert res.fleet_pct_difference == pytest.approx(66.6667, abs=1e-3)


def test_ev_more_expensive_negative_savings():
    # EV €0.50/km vs diesel €0.30/km → -66.7% (negative = EV pricier).
    res = _build([_vi("v1", distance=100.0, cost=50.0, l100=20.0)], price=1.5)
    assert res.fleet_pct_difference == pytest.approx(-66.6667, abs=1e-3)
