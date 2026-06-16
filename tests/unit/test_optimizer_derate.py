"""Tests for the depot-level static building-load derate.

Reference: migration 020, PRD §9.4 (post-pilot revision).

When a depot has no live meter/forecast source it can opt into a static
``building_load_assumption_kw`` value. The optimizer treats this as a
constant baseline load on the grid so ``max_grid_kw`` is still respected.
"""

from __future__ import annotations

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize


def _config(*, max_site: float, derate: float) -> DepotConfig:
    vehicle_ids = ["bus_1", "bus_2"]
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 2},
        charger_efficiency=0.95,
        charger_vehicle_access={
            "charger_a": {"bus_1", "bus_2"},
            "charger_b": {"bus_1"},
        },
        # Battery present but small — the optimizer's input validator
        # rejects battery_capacity=0. We don't want battery dispatch to
        # absorb the test signal, so cap the power tightly.
        battery_capacity=10.0,
        battery_power=1.0,
        max_site_power=max_site,
        building_load_assumption_kw=derate,
    )


def _state(n: int = 96) -> DepotState:
    return DepotState(
        vehicle_socs={"bus_1": 0.30, "bus_2": 0.40},
        battery_soc=0.5,
        prices=[0.10] * n,
        demand_charge_rate=15.0,
        current_month_peak=0.0,
        vehicle_availability={"bus_1": [True] * n, "bus_2": [True] * n},
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        # No live building load — assumption_kw on the config is the
        # only baseline applied. Static-assumption mode returns zeros.
        building_power=[0.0] * n,
    )


def test_derate_reduces_available_grid_headroom():
    """With derate=30 and max_site=100, grid+30 must never exceed 100.

    Equivalently: the optimizer must never plan total charging (grid
    draw, since building_power=[0] and battery=0) above 70 kW.
    """
    config = _config(max_site=100.0, derate=30.0)
    state = _state()
    result = optimize(state, config, time_limit=30.0)

    # MILP grid-balance: P_grid = sum(P_charge) + 0 + 30 + 0 - 0
    # so P_grid >= 30 always; charging headroom is max_site - 30 = 70.
    for t, grid_power in enumerate(result.grid_power):
        assert grid_power <= 100.0 + 1e-6, f"Site limit violated at t={t}: P_grid={grid_power}"
    # Total charging at any timestep cannot exceed (max_site - derate).
    for t in range(len(result.grid_power)):
        total_charge = sum(result.schedule[vid]["charging_power"][t] for vid in result.schedule)
        assert (
            total_charge <= 70.0 + 1e-6
        ), f"Charging exceeded headroom at t={t}: {total_charge} > 70"


def test_zero_derate_matches_baseline_behaviour():
    """With derate=0, the optimizer behaves exactly like before."""
    config = _config(max_site=100.0, derate=0.0)
    state = _state()
    result = optimize(state, config, time_limit=30.0)

    # No baseline → grid headroom is the full 100 kW.
    for grid_power in result.grid_power:
        assert grid_power <= 100.0 + 1e-6


@pytest.mark.parametrize("derate_kw", [10.0, 30.0, 50.0])
def test_derate_value_propagates_to_constraint(derate_kw):
    """The active site-power constraint shifts with the derate value.

    Larger derate values can drive the model infeasible at the high end
    (not enough headroom over 24 h to satisfy departure SoC). We bound
    the parametrize range so each case is feasible — the goal is to
    verify the constraint shifts, not to stress-test feasibility.
    """
    config = _config(max_site=200.0, derate=derate_kw)
    state = _state()
    result = optimize(state, config, time_limit=30.0)

    headroom = 200.0 - derate_kw
    for t in range(len(result.grid_power)):
        total_charge = sum(result.schedule[vid]["charging_power"][t] for vid in result.schedule)
        assert (
            total_charge <= headroom + 1e-6
        ), f"derate={derate_kw}: charging {total_charge} > headroom {headroom} at t={t}"
