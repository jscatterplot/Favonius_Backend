"""Warm-starting implementation for optimization models.

Reference: Development plan Step 1.2, PRD_v2.md Section 8.5
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pyomo.environ as pyo

from ..models import DepotConfig, DepotState, OptimizationResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def warm_start_model(
    model: pyo.ConcreteModel,
    previous_result: OptimizationResult,
    state: DepotState,
    config: DepotConfig,
) -> None:
    """Initialize model variables from previous solution.

    Handles:
    - Vehicle set differences (new/removed vehicles)
    - Timestep alignment (rolling horizon)
    - Missing data gracefully

    Args:
        model: Pyomo model to warm-start
        previous_result: Previous optimization result
        state: Current depot state
        config: Depot configuration
    """
    logger.info("Applying warm-start from previous solution")

    previous_schedule = previous_result.schedule
    current_vehicles = set(state.vehicle_socs.keys())
    previous_vehicles = set(previous_schedule.keys())

    # Initialize vehicle charging variables
    vehicles_to_initialize = current_vehicles & previous_vehicles
    new_vehicles = current_vehicles - previous_vehicles

    logger.debug(
        f"Warm-start: {len(vehicles_to_initialize)} existing vehicles, "
        f"{len(new_vehicles)} new vehicles"
    )

    # Initialize P_charge and y_charge for existing vehicles
    for vehicle_id in vehicles_to_initialize:
        prev_sched = previous_schedule.get(vehicle_id, {})
        prev_power = prev_sched.get("charging_power", [])
        prev_soc = prev_sched.get("soc", [])

        # Handle rolling horizon: align timesteps
        # For simplicity, we assume timesteps align (t=0 in previous = t=0 in current)
        # In practice, you might need to shift based on actual time difference
        for t in model.T:
            if t < len(prev_power) and prev_power[t] is not None:
                # Initialize charging power
                power_val = prev_power[t]
                model.P_charge[vehicle_id, t].value = power_val

                # Initialize binary charging indicator
                if power_val > 0.1:
                    model.y_charge[vehicle_id, t].value = 1
                else:
                    model.y_charge[vehicle_id, t].value = 0
            else:
                # No previous data for this timestep
                model.P_charge[vehicle_id, t].value = 0.0
                model.y_charge[vehicle_id, t].value = 0

            # Initialize SoC if available
            if t < len(prev_soc) and prev_soc[t] is not None:
                soc_val = prev_soc[t]
                # Clamp to bounds
                soc_val = max(0.1, min(1.0, soc_val))
                model.SoC[vehicle_id, t].value = soc_val
            elif t == 0:
                # At least initialize t=0 with current SoC
                current_soc = state.vehicle_socs.get(vehicle_id, 0.5)
                model.SoC[vehicle_id, t].value = current_soc

        # Initialize y_start from previous power schedule (mirrors y_charge transitions)
        if hasattr(model, "y_start"):
            for t in model.T:
                p_t = prev_power[t] if t < len(prev_power) and prev_power[t] is not None else 0.0
                if t == 0:
                    model.y_start[vehicle_id, t].value = 1 if p_t > 0.1 else 0
                else:
                    p_prev = (
                        prev_power[t - 1]
                        if t - 1 < len(prev_power) and prev_power[t - 1] is not None
                        else 0.0
                    )
                    model.y_start[vehicle_id, t].value = (
                        1 if (p_t > 0.1 and p_prev <= 0.1) else 0
                    )

    # Initialize new vehicles with current SoC
    for vehicle_id in new_vehicles:
        current_soc = state.vehicle_socs[vehicle_id]
        for t in model.T:
            # Start with no charging
            model.P_charge[vehicle_id, t].value = 0.0
            model.y_charge[vehicle_id, t].value = 0
            # Initialize SoC to current value (will be updated by dynamics)
            if t == 0:
                model.SoC[vehicle_id, t].value = current_soc
            else:
                # Estimate SoC based on initial value
                model.SoC[vehicle_id, t].value = current_soc
        if hasattr(model, "y_start"):
            for t in model.T:
                model.y_start[vehicle_id, t].value = 0

    # Initialize battery variables
    if previous_result.battery_dispatch:
        for t in model.T:
            if t < len(previous_result.battery_dispatch):
                batt_power = previous_result.battery_dispatch[t]
                if batt_power is not None:
                    # Clamp to bounds
                    batt_power = max(
                        -config.battery_power,
                        min(config.battery_power, batt_power),
                    )
                    model.P_batt[t].value = batt_power

    # Initialize battery SoC
    # OptimizationResult doesn't have battery_soc list, so use current state
    # In practice, you might track battery SoC trajectory separately
    for t in model.T:
        if t == 0:
            model.SoC_batt[t].value = state.battery_soc
        else:
            # Estimate based on initial SoC and battery dispatch
            # Simple linear estimate (could be improved)
            model.SoC_batt[t].value = state.battery_soc

    # Initialize grid power variables
    if previous_result.grid_power:
        for t in model.T:
            if t < len(previous_result.grid_power):
                grid_power = previous_result.grid_power[t]
                if grid_power is not None:
                    model.P_grid[t].value = max(0.0, grid_power)

    # Initialize peak demand
    if previous_result.peak_demand_kw is not None:
        model.P_peak.value = max(0.0, previous_result.peak_demand_kw)

    logger.info("Warm-start initialization complete")
