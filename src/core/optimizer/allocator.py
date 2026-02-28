"""Post-optimization charger allocation.

Allocates aggregated charging power to individual physical chargers,
respecting physical accessibility constraints.

Reference: PRD_v2.md Section 8.3 (Charger Aggregation Strategy)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..models import DepotConfig, OptimizationResult

logger = logging.getLogger(__name__)


@dataclass
class ChargerAssignment:
    """Assignment of a vehicle to a charger at a timestep."""

    charger_id: str
    vehicle_id: str
    timestep: int
    power_kw: float


def allocate_chargers(
    result: OptimizationResult,
    config: DepotConfig,
    charger_ids_by_rating: dict[float, list[str]],  # rated_kw -> list of charger_ids
    vehicle_priorities: Optional[
        dict[str, float]
    ] = None,  # vehicle_id -> priority (lower = higher priority)
) -> list[ChargerAssignment]:
    """Allocate optimized power to individual chargers.

    Per PRD Section 8.3, this post-optimization step allocates aggregated
    charging power from the MILP solution to individual physical chargers,
    respecting physical accessibility constraints.

    Args:
        result: Optimization result with aggregated power
        config: Depot configuration with accessibility matrix
        charger_ids_by_rating: Mapping of power rating to charger IDs
        vehicle_priorities: Optional priority for each vehicle (based on departure time, SoC deficit).
                          If None, vehicles are prioritized by departure time (earlier = higher priority)

    Returns:
        List of ChargerAssignment objects for each timestep

    Note:
        Per PRD Section 8.3, the allocation algorithm:
        1. Sorts vehicles by priority (departure time, SoC deficit)
        2. Checks physical accessibility (charger_vehicle_access)
        3. Allocates power to chargers within group using fair allocation
        4. Respects individual charger rated_kw limits
        5. Handles charger status (Available/Unavailable) - assumes all Available for MVP
    """
    assignments = []
    n_timesteps = len(result.grid_power)

    # Build vehicle priorities if not provided
    if vehicle_priorities is None:
        # Default: prioritize by departure time (earlier = higher priority)
        # Extract from schedule - vehicles with earlier charging start get priority
        vehicle_priorities = {}
        for vid, schedule_data in result.schedule.items():
            # Find first timestep where vehicle charges
            charging_power = schedule_data.get("charging_power", [])
            first_charge_t = next(
                (t for t, power in enumerate(charging_power) if power > 0.1),
                n_timesteps,
            )
            vehicle_priorities[vid] = float(first_charge_t)

    # Sticky assignment: remember each vehicle's charger from the previous timestep.
    # Cleared automatically for vehicles that stop charging.
    vehicle_charger: dict[str, str] = {}

    for t in range(n_timesteps):
        # Get vehicles that need charging this timestep
        charging_vehicles = [
            (vid, result.schedule[vid]["charging_power"][t])
            for vid in result.schedule
            if result.schedule[vid]["charging_power"][t] > 0.1
        ]

        if not charging_vehicles:
            vehicle_charger = {}  # all vehicles stopped — clear sticky state
            continue

        # Sort by priority (lower priority value = higher priority)
        charging_vehicles.sort(key=lambda x: vehicle_priorities.get(x[0], float("inf")))

        # Track which chargers are used this timestep
        used_chargers: set[str] = set()
        # Track remaining power capacity per charger
        charger_capacity: dict[str, float] = {}
        for rated_kw, charger_ids in charger_ids_by_rating.items():
            for charger_id in charger_ids:
                charger_capacity[charger_id] = rated_kw

        # Track which charger each vehicle actually used this timestep (for next-timestep sticky)
        t_vehicle_charger: dict[str, str] = {}

        for vehicle_id, power_kw in charging_vehicles:
            remaining_power = power_kw

            # --- Sticky assignment: try to reuse the charger from the previous timestep ---
            prev_charger = vehicle_charger.get(vehicle_id)
            if prev_charger is not None and prev_charger not in used_chargers:
                available_capacity = charger_capacity.get(prev_charger, 0.0)
                accessible_vehicles = config.charger_vehicle_access.get(prev_charger, set())
                accessible = not accessible_vehicles or vehicle_id in accessible_vehicles
                if accessible and available_capacity >= 0.1:
                    allocated_power = min(remaining_power, available_capacity)
                    assignments.append(
                        ChargerAssignment(
                            charger_id=prev_charger,
                            vehicle_id=vehicle_id,
                            timestep=t,
                            power_kw=allocated_power,
                        )
                    )
                    charger_capacity[prev_charger] -= allocated_power
                    if charger_capacity[prev_charger] < 0.1:
                        used_chargers.add(prev_charger)
                    remaining_power -= allocated_power
                    t_vehicle_charger[vehicle_id] = prev_charger

            # --- Greedy fallback: allocate remaining power to accessible chargers ---
            # Sort charger groups by power rating (higher first for better matching)
            sorted_groups = sorted(charger_ids_by_rating.items(), key=lambda x: x[0], reverse=True)

            for rated_kw, charger_ids in sorted_groups:
                if remaining_power <= 0:
                    break

                # Filter to accessible, available chargers
                accessible_chargers = []
                for charger_id in charger_ids:
                    if charger_id in used_chargers:
                        continue

                    # Check physical accessibility
                    accessible_vehicles = config.charger_vehicle_access.get(charger_id, set())
                    if accessible_vehicles and vehicle_id not in accessible_vehicles:
                        continue

                    accessible_chargers.append(charger_id)

                # Allocate power to accessible chargers
                for charger_id in accessible_chargers:
                    if remaining_power <= 0:
                        break

                    available_capacity = charger_capacity.get(charger_id, 0.0)
                    if available_capacity <= 0:
                        continue

                    # Allocate up to available capacity
                    allocated_power = min(remaining_power, available_capacity)
                    assignments.append(
                        ChargerAssignment(
                            charger_id=charger_id,
                            vehicle_id=vehicle_id,
                            timestep=t,
                            power_kw=allocated_power,
                        )
                    )

                    # Update capacity and mark as used if fully allocated
                    charger_capacity[charger_id] -= allocated_power
                    if charger_capacity[charger_id] < 0.1:
                        used_chargers.add(charger_id)

                    remaining_power -= allocated_power

                    # Record first greedy charger for next-timestep stickiness
                    if vehicle_id not in t_vehicle_charger:
                        t_vehicle_charger[vehicle_id] = charger_id

            if remaining_power > 0.1:
                logger.warning(
                    f"Could not fully allocate {remaining_power:.1f}kW for vehicle "
                    f"{vehicle_id} at timestep {t}. This may indicate an optimization "
                    "constraint violation or missing charger accessibility."
                )

        # Carry forward only the vehicles that charged this timestep
        vehicle_charger = t_vehicle_charger

    logger.info(f"Allocated {len(assignments)} charger assignments across {n_timesteps} timesteps")
    return assignments
