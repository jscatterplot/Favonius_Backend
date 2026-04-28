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
        - One bus ↔ one charger per timestep; power capped at that charger's capacity.
        - Sticky assignment: same bus keeps same charger across timesteps (no bus→charger switch).
        - When a charger switches to a different bus, config.charger_switch_gap_timesteps idle
          period and optional charger_reassignment_allowed_windows are enforced.
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

    # Sticky assignment: same bus stays on same charger for the night (no bus→charger switching).
    vehicle_charger: dict[str, str] = {}
    # Reassignment rules: when one charger serves a different bus, enforce min gap and optional windows.
    charger_last_used_t: dict[str, int] = {}
    charger_last_vehicle_id: dict[str, str] = {}
    gap = getattr(config, "charger_switch_gap_timesteps", 1)
    allowed_windows = getattr(config, "charger_reassignment_allowed_windows", None)

    def _reassignment_allowed(charger_id: str, vehicle_id: str, timestep: int) -> bool:
        """True if charger can be assigned to this vehicle at this timestep (reassignment rules)."""
        t_last = charger_last_used_t.get(charger_id)
        if t_last is None:
            return True
        if charger_last_vehicle_id.get(charger_id) == vehicle_id:
            return True  # same bus continuing on same charger
        # Reassignment: different bus. Require min idle gap.
        if timestep < t_last + 1 + gap:
            return False
        if allowed_windows is None:
            return True
        return any(start <= timestep <= end for (start, end) in allowed_windows)

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
                    # One charger per bus: allocate up to this charger's capacity only
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

            # --- Greedy fallback: at most one charger per bus per timestep ---
            if remaining_power > 0.1:
                sorted_groups = sorted(
                    charger_ids_by_rating.items(), key=lambda x: x[0], reverse=True
                )
                assigned = False
                for rated_kw, charger_ids in sorted_groups:
                    if assigned:
                        break
                    for charger_id in charger_ids:
                        if charger_id in used_chargers:
                            continue
                        accessible_vehicles = config.charger_vehicle_access.get(
                            charger_id, set()
                        )
                        if accessible_vehicles and vehicle_id not in accessible_vehicles:
                            continue
                        if not _reassignment_allowed(charger_id, vehicle_id, t):
                            continue
                        available_capacity = charger_capacity.get(charger_id, 0.0)
                        if available_capacity < 0.1:
                            continue
                        allocated_power = min(remaining_power, available_capacity)
                        assignments.append(
                            ChargerAssignment(
                                charger_id=charger_id,
                                vehicle_id=vehicle_id,
                                timestep=t,
                                power_kw=allocated_power,
                            )
                        )
                        charger_capacity[charger_id] -= allocated_power
                        if charger_capacity[charger_id] < 0.1:
                            used_chargers.add(charger_id)
                        remaining_power -= allocated_power
                        t_vehicle_charger[vehicle_id] = charger_id
                        assigned = True
                        break

            if remaining_power > 0.1:
                logger.warning(
                    f"Could not fully allocate {remaining_power:.1f}kW for vehicle "
                    f"{vehicle_id} at timestep {t} (one charger per bus; cap at charger capacity)."
                )

        # Carry forward sticky assignment and charger reassignment state
        vehicle_charger = t_vehicle_charger
        for vid, cid in t_vehicle_charger.items():
            charger_last_used_t[cid] = t
            charger_last_vehicle_id[cid] = vid

    logger.info(f"Allocated {len(assignments)} charger assignments across {n_timesteps} timesteps")
    return assignments
