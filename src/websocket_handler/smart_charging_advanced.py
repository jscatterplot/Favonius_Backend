"""Advanced Smart Charging with composite schedule calculator, load balancing, and grid constraint management."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .charging_profile_manager import (
    ChargingProfile,
    ChargingProfileManager,
    ChargingSchedule,
    ChargingSchedulePeriod,
)
from .monitoring import get_logger
from .timescale_client import TimescaleClient


class ConstraintType(Enum):
    """Grid constraint types."""

    GRID_LIMIT = "GridLimit"
    BUILDING_LIMIT = "BuildingLimit"
    TRANSFORMER_LIMIT = "TransformerLimit"
    CABLE_LIMIT = "CableLimit"
    EVSE_LIMIT = "EVSELimit"
    CONNECTOR_LIMIT = "ConnectorLimit"
    V2G_LIMIT = "V2GLimit"
    DEMAND_RESPONSE = "DemandResponse"


class LoadBalancingStrategy(Enum):
    """Load balancing strategies."""

    EQUAL_SHARING = "EqualSharing"
    PRIORITY_BASED = "PriorityBased"
    PRICE_BASED = "PriceBased"
    TIME_BASED = "TimeBased"
    V2G_OPTIMIZED = "V2GOptimized"


class DemandResponseSignal(Enum):
    """Demand response signals."""

    NORMAL = "Normal"
    REDUCE_LOAD = "ReduceLoad"
    INCREASE_LOAD = "IncreaseLoad"
    EMERGENCY_SHED = "EmergencyShed"
    V2G_DISCHARGE = "V2GDischarge"


@dataclass
class GridConstraint:
    """Grid constraint definition."""

    constraint_id: str
    constraint_type: ConstraintType
    location: str  # Station ID or site ID
    max_power: float
    min_power: float = 0.0
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    priority: int = 1  # Higher number = higher priority
    description: Optional[str] = None


@dataclass
class LoadBalancingConfig:
    """Load balancing configuration."""

    strategy: LoadBalancingStrategy = LoadBalancingStrategy.EQUAL_SHARING
    max_power_per_evse: float = 22.0
    min_power_per_evse: float = 0.0
    priority_weights: Dict[str, float] = field(
        default_factory=lambda: {"high": 2.0, "medium": 1.5, "low": 1.0}
    )
    price_sensitivity: float = 1.0
    v2g_enabled: bool = True
    v2g_discharge_limit: float = 0.0  # Maximum discharge power


@dataclass
class CompositeSchedule:
    """Composite charging schedule."""

    evse_id: int
    connector_id: int
    schedule_start: datetime
    duration: int
    charging_rate_unit: str
    charging_schedule_period: List[ChargingSchedulePeriod]
    total_energy: float
    total_cost: float
    constraints_applied: List[str] = field(default_factory=list)


@dataclass
class DemandResponseEvent:
    """Demand response event."""

    event_id: str
    signal: DemandResponseSignal
    start_time: datetime
    end_time: datetime
    target_reduction: Optional[float] = None
    target_increase: Optional[float] = None
    affected_stations: List[str] = field(default_factory=list)
    priority: int = 1


class SmartChargingAdvanced:
    """Advanced smart charging with composite schedules and grid constraints."""

    def __init__(
        self, timescale_client: TimescaleClient, charging_profile_manager: ChargingProfileManager
    ):
        self.timescale_client = timescale_client
        self.charging_profile_manager = charging_profile_manager
        self.logger = get_logger(__name__)

        # Grid constraints cache
        self.grid_constraints: Dict[str, GridConstraint] = {}

        # Load balancing configuration
        self.load_balancing_config = LoadBalancingConfig()

        # Active demand response events
        self.demand_response_events: Dict[str, DemandResponseEvent] = {}

        # Composite schedule cache
        self.composite_schedule_cache: Dict[str, CompositeSchedule] = {}

        # Load balancing state
        self.current_load_distribution: Dict[str, float] = {}

        # Price data cache
        self.price_data_cache: Dict[str, Dict[str, Any]] = {}

    async def calculate_composite_schedule(
        self, station_id: str, evse_id: int, duration: int, charging_rate_unit: str = "W"
    ) -> CompositeSchedule:
        """Calculate composite schedule with all constraints applied."""
        try:
            # Get active charging profiles
            profiles = await self.charging_profile_manager._get_active_profiles(station_id, evse_id)

            if not profiles:
                # Return empty schedule
                return CompositeSchedule(
                    evse_id=evse_id,
                    connector_id=1,
                    schedule_start=datetime.now(timezone.utc),
                    duration=duration,
                    charging_rate_unit=charging_rate_unit,
                    charging_schedule_period=[],
                    total_energy=0.0,
                    total_cost=0.0,
                )

            # Calculate base composite schedule
            base_schedule = await self._calculate_base_composite_schedule(
                profiles, duration, charging_rate_unit
            )

            # Apply grid constraints
            constrained_schedule = await self._apply_grid_constraints(
                station_id, evse_id, base_schedule
            )

            # Apply load balancing
            balanced_schedule = await self._apply_load_balancing(
                station_id, evse_id, constrained_schedule
            )

            # Apply demand response
            final_schedule = await self._apply_demand_response(
                station_id, evse_id, balanced_schedule
            )

            # Calculate total energy and cost
            total_energy = sum(
                period.limit * 60 / 1000 for period in final_schedule.charging_schedule_period
            )  # kWh
            total_cost = await self._calculate_schedule_cost(station_id, final_schedule)

            # Create composite schedule
            composite_schedule = CompositeSchedule(
                evse_id=evse_id,
                connector_id=1,
                schedule_start=datetime.now(timezone.utc),
                duration=duration,
                charging_rate_unit=charging_rate_unit,
                charging_schedule_period=final_schedule.charging_schedule_period,
                total_energy=total_energy,
                total_cost=total_cost,
                constraints_applied=[
                    constraint.constraint_id for constraint in self.grid_constraints.values()
                ],
            )

            # Cache the result
            cache_key = f"{station_id}:{evse_id}:{duration}"
            self.composite_schedule_cache[cache_key] = composite_schedule

            self.logger.info(f"Calculated composite schedule for {station_id}, EVSE {evse_id}")
            return composite_schedule

        except Exception as e:
            self.logger.error(f"Error calculating composite schedule: {e}")
            raise

    async def add_grid_constraint(self, constraint: GridConstraint) -> bool:
        """Add grid constraint."""
        try:
            # Store constraint in database
            await self.timescale_client.store_grid_constraint(
                {
                    "constraint_id": constraint.constraint_id,
                    "constraint_type": constraint.constraint_type.value,
                    "location": constraint.location,
                    "max_power": constraint.max_power,
                    "min_power": constraint.min_power,
                    "valid_from": constraint.valid_from,
                    "valid_to": constraint.valid_to,
                    "priority": constraint.priority,
                    "description": constraint.description,
                }
            )

            # Update cache
            self.grid_constraints[constraint.constraint_id] = constraint

            # Invalidate affected composite schedules
            await self._invalidate_affected_schedules(constraint.location)

            self.logger.info(f"Added grid constraint {constraint.constraint_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error adding grid constraint: {e}")
            return False

    async def remove_grid_constraint(self, constraint_id: str) -> bool:
        """Remove grid constraint."""
        try:
            # Remove from database
            await self.timescale_client.remove_grid_constraint(constraint_id)

            # Remove from cache
            constraint = self.grid_constraints.pop(constraint_id, None)

            if constraint:
                # Invalidate affected composite schedules
                await self._invalidate_affected_schedules(constraint.location)

            self.logger.info(f"Removed grid constraint {constraint_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error removing grid constraint: {e}")
            return False

    async def get_active_constraints(self, location: str) -> List[GridConstraint]:
        """Get active grid constraints for location."""
        try:
            constraints_data = await self.timescale_client.get_active_grid_constraints(location)

            constraints = []
            for constraint_data in constraints_data:
                constraint = GridConstraint(
                    constraint_id=constraint_data["constraint_id"],
                    constraint_type=ConstraintType(constraint_data["constraint_type"]),
                    location=constraint_data["location"],
                    max_power=constraint_data["max_power"],
                    min_power=constraint_data["min_power"],
                    valid_from=constraint_data.get("valid_from"),
                    valid_to=constraint_data.get("valid_to"),
                    priority=constraint_data["priority"],
                    description=constraint_data.get("description"),
                )
                constraints.append(constraint)

            return constraints

        except Exception as e:
            self.logger.error(f"Error getting active constraints: {e}")
            return []

    async def balance_load_across_evses(
        self, station_id: str, evse_ids: List[int], total_available_power: float
    ) -> Dict[int, float]:
        """Balance load across multiple EVSEs."""
        try:
            if not evse_ids:
                return {}

            # Get current load for each EVSE
            current_loads = {}
            for evse_id in evse_ids:
                current_load = await self._get_current_evse_load(station_id, evse_id)
                current_loads[evse_id] = current_load

            # Calculate load distribution based on strategy
            if self.load_balancing_config.strategy == LoadBalancingStrategy.EQUAL_SHARING:
                return await self._equal_sharing_balance(
                    evse_ids, total_available_power, current_loads
                )
            elif self.load_balancing_config.strategy == LoadBalancingStrategy.PRIORITY_BASED:
                return await self._priority_based_balance(
                    station_id, evse_ids, total_available_power, current_loads
                )
            elif self.load_balancing_config.strategy == LoadBalancingStrategy.PRICE_BASED:
                return await self._price_based_balance(
                    station_id, evse_ids, total_available_power, current_loads
                )
            elif self.load_balancing_config.strategy == LoadBalancingStrategy.V2G_OPTIMIZED:
                return await self._v2g_optimized_balance(
                    station_id, evse_ids, total_available_power, current_loads
                )
            else:
                return await self._equal_sharing_balance(
                    evse_ids, total_available_power, current_loads
                )

        except Exception as e:
            self.logger.error(f"Error balancing load: {e}")
            return {}

    async def handle_demand_response_signal(self, event: DemandResponseEvent) -> bool:
        """Handle demand response signal."""
        try:
            # Store demand response event
            await self.timescale_client.store_demand_response_event(
                {
                    "event_id": event.event_id,
                    "signal": event.signal.value,
                    "start_time": event.start_time,
                    "end_time": event.end_time,
                    "target_reduction": event.target_reduction,
                    "target_increase": event.target_increase,
                    "affected_stations": json.dumps(event.affected_stations),
                    "priority": event.priority,
                }
            )

            # Update cache
            self.demand_response_events[event.event_id] = event

            # Apply demand response to affected stations
            for station_id in event.affected_stations:
                await self._apply_demand_response_to_station(station_id, event)

            self.logger.info(
                f"Applied demand response signal {event.signal.value} to {len(event.affected_stations)} stations"
            )
            return True

        except Exception as e:
            self.logger.error(f"Error handling demand response signal: {e}")
            return False

    async def get_load_balancing_status(self, station_id: str) -> Dict[str, Any]:
        """Get load balancing status for station."""
        try:
            # Get all EVSEs for station
            evse_ids = await self.timescale_client.get_station_evse_ids(station_id)

            # Get current load distribution
            load_distribution = {}
            total_load = 0.0

            for evse_id in evse_ids:
                current_load = await self._get_current_evse_load(station_id, evse_id)
                load_distribution[evse_id] = current_load
                total_load += current_load

            # Get grid constraints
            constraints = await self.get_active_constraints(station_id)

            # Get demand response events
            active_dr_events = [
                event
                for event in self.demand_response_events.values()
                if station_id in event.affected_stations
                and event.start_time <= datetime.now(timezone.utc) <= event.end_time
            ]

            return {
                "station_id": station_id,
                "total_load": total_load,
                "load_distribution": load_distribution,
                "active_constraints": len(constraints),
                "active_demand_response_events": len(active_dr_events),
                "load_balancing_strategy": self.load_balancing_config.strategy.value,
                "v2g_enabled": self.load_balancing_config.v2g_enabled,
            }

        except Exception as e:
            self.logger.error(f"Error getting load balancing status: {e}")
            return {}

    async def _calculate_base_composite_schedule(
        self, profiles: List[ChargingProfile], duration: int, charging_rate_unit: str
    ) -> ChargingSchedule:
        """Calculate base composite schedule from profiles."""
        # Sort profiles by stack level (highest first)
        sorted_profiles = sorted(profiles, key=lambda p: p.stack_level, reverse=True)

        # Create time slots
        time_slots = {}
        for i in range(0, duration, 60):  # 1-minute intervals
            time_slots[i] = {"power": 0.0, "source": None}

        # Apply profiles in order
        for profile in sorted_profiles:
            await self._apply_profile_to_slots(profile, time_slots, duration)

        # Convert to charging schedule periods
        periods = []
        current_power = None
        start_period = 0

        for time_slot in sorted(time_slots.keys()):
            power = time_slots[time_slot]["power"]

            if current_power != power:
                if current_power is not None:
                    periods.append(
                        ChargingSchedulePeriod(
                            start_period=start_period, limit=current_power, number_phases=3
                        )
                    )

                current_power = power
                start_period = time_slot

        # Add final period
        if current_power is not None:
            periods.append(
                ChargingSchedulePeriod(
                    start_period=start_period, limit=current_power, number_phases=3
                )
            )

        return ChargingSchedule(
            id=int(datetime.now().timestamp()),
            start_schedule=datetime.now(timezone.utc).isoformat(),
            duration=duration,
            charging_rate_unit=charging_rate_unit,
            charging_schedule_period=periods,
        )

    async def _apply_profile_to_slots(
        self, profile: ChargingProfile, time_slots: Dict[int, Dict[str, Any]], duration: int
    ) -> None:
        """Apply a profile to time slots."""
        for period in profile.charging_schedule.charging_schedule_period:
            start_time = period.start_period
            end_time = min(start_time + 60, duration)  # Assume 1-minute periods

            for slot in range(start_time, end_time, 60):
                if slot in time_slots:
                    # Apply the limit (higher stack level overrides lower)
                    if (
                        time_slots[slot]["source"] is None
                        or profile.stack_level > time_slots[slot]["source"]
                    ):
                        time_slots[slot]["power"] = period.limit
                        time_slots[slot]["source"] = profile.stack_level

    async def _apply_grid_constraints(
        self, station_id: str, evse_id: int, schedule: ChargingSchedule
    ) -> ChargingSchedule:
        """Apply grid constraints to schedule."""
        try:
            # Get active constraints for station
            constraints = await self.get_active_constraints(station_id)

            if not constraints:
                return schedule

            # Sort constraints by priority (highest first)
            sorted_constraints = sorted(constraints, key=lambda c: c.priority, reverse=True)

            # Apply constraints to each period
            constrained_periods = []
            for period in schedule.charging_schedule_period:
                constrained_power = period.limit

                # Apply each constraint
                for constraint in sorted_constraints:
                    if constraint.max_power < constrained_power:
                        constrained_power = constraint.max_power

                    if constraint.min_power > constrained_power:
                        constrained_power = constraint.min_power

                # Create constrained period
                constrained_periods.append(
                    ChargingSchedulePeriod(
                        start_period=period.start_period,
                        limit=constrained_power,
                        number_phases=period.number_phases,
                    )
                )

            # Create constrained schedule
            return ChargingSchedule(
                id=schedule.id,
                start_schedule=schedule.start_schedule,
                duration=schedule.duration,
                charging_rate_unit=schedule.charging_rate_unit,
                charging_schedule_period=constrained_periods,
                min_charging_rate=schedule.min_charging_rate,
            )

        except Exception as e:
            self.logger.error(f"Error applying grid constraints: {e}")
            return schedule

    async def _apply_load_balancing(
        self, station_id: str, evse_id: int, schedule: ChargingSchedule
    ) -> ChargingSchedule:
        """Apply load balancing to schedule."""
        try:
            # Get all EVSEs for station
            evse_ids = await self.timescale_client.get_station_evse_ids(station_id)

            if len(evse_ids) <= 1:
                return schedule

            # Calculate total available power
            total_available_power = sum(
                period.limit for period in schedule.charging_schedule_period
            )

            # Balance load across EVSEs
            load_distribution = await self.balance_load_across_evses(
                station_id, evse_ids, total_available_power
            )

            # Get power allocation for this EVSE
            evse_power = load_distribution.get(evse_id, 0.0)

            # Create balanced periods
            balanced_periods = []
            for period in schedule.charging_schedule_period:
                # Scale power based on load balancing
                balanced_power = min(period.limit, evse_power)

                balanced_periods.append(
                    ChargingSchedulePeriod(
                        start_period=period.start_period,
                        limit=balanced_power,
                        number_phases=period.number_phases,
                    )
                )

            # Create balanced schedule
            return ChargingSchedule(
                id=schedule.id,
                start_schedule=schedule.start_schedule,
                duration=schedule.duration,
                charging_rate_unit=schedule.charging_rate_unit,
                charging_schedule_period=balanced_periods,
                min_charging_rate=schedule.min_charging_rate,
            )

        except Exception as e:
            self.logger.error(f"Error applying load balancing: {e}")
            return schedule

    async def _apply_demand_response(
        self, station_id: str, evse_id: int, schedule: ChargingSchedule
    ) -> ChargingSchedule:
        """Apply demand response to schedule."""
        try:
            # Get active demand response events for station
            active_events = [
                event
                for event in self.demand_response_events.values()
                if station_id in event.affected_stations
                and event.start_time <= datetime.now(timezone.utc) <= event.end_time
            ]

            if not active_events:
                return schedule

            # Sort events by priority (highest first)
            sorted_events = sorted(active_events, key=lambda e: e.priority, reverse=True)

            # Apply demand response to each period
            dr_periods = []
            for period in schedule.charging_schedule_period:
                adjusted_power = period.limit

                # Apply each demand response event
                for event in sorted_events:
                    if event.signal == DemandResponseSignal.REDUCE_LOAD:
                        if event.target_reduction:
                            adjusted_power = max(0, adjusted_power - event.target_reduction)
                        else:
                            adjusted_power = adjusted_power * 0.5  # Default 50% reduction

                    elif event.signal == DemandResponseSignal.INCREASE_LOAD:
                        if event.target_increase:
                            adjusted_power = adjusted_power + event.target_increase
                        else:
                            adjusted_power = adjusted_power * 1.5  # Default 50% increase

                    elif event.signal == DemandResponseSignal.EMERGENCY_SHED:
                        adjusted_power = 0.0  # Emergency load shedding

                    elif event.signal == DemandResponseSignal.V2G_DISCHARGE:
                        if self.load_balancing_config.v2g_enabled:
                            adjusted_power = -self.load_balancing_config.v2g_discharge_limit

                # Create demand response period
                dr_periods.append(
                    ChargingSchedulePeriod(
                        start_period=period.start_period,
                        limit=adjusted_power,
                        number_phases=period.number_phases,
                    )
                )

            # Create demand response schedule
            return ChargingSchedule(
                id=schedule.id,
                start_schedule=schedule.start_schedule,
                duration=schedule.duration,
                charging_rate_unit=schedule.charging_rate_unit,
                charging_schedule_period=dr_periods,
                min_charging_rate=schedule.min_charging_rate,
            )

        except Exception as e:
            self.logger.error(f"Error applying demand response: {e}")
            return schedule

    async def _equal_sharing_balance(
        self, evse_ids: List[int], total_power: float, current_loads: Dict[int, float]
    ) -> Dict[int, float]:
        """Equal sharing load balancing."""
        if not evse_ids:
            return {}

        # Calculate equal share
        equal_share = total_power / len(evse_ids)

        # Distribute power equally
        distribution = {}
        for evse_id in evse_ids:
            distribution[evse_id] = min(equal_share, self.load_balancing_config.max_power_per_evse)

        return distribution

    async def _priority_based_balance(
        self,
        station_id: str,
        evse_ids: List[int],
        total_power: float,
        current_loads: Dict[int, float],
    ) -> Dict[int, float]:
        """Priority-based load balancing."""
        try:
            # Get EVSE priorities
            evse_priorities = {}
            for evse_id in evse_ids:
                priority = await self.timescale_client.get_evse_priority(station_id, evse_id)
                evse_priorities[evse_id] = priority or "medium"

            # Calculate priority weights
            total_weight = 0.0
            for evse_id in evse_ids:
                priority = evse_priorities[evse_id]
                weight = self.load_balancing_config.priority_weights.get(priority, 1.0)
                total_weight += weight

            # Distribute power based on priority
            distribution = {}
            remaining_power = total_power

            for evse_id in sorted(evse_ids, key=lambda x: evse_priorities[x], reverse=True):
                priority = evse_priorities[evse_id]
                weight = self.load_balancing_config.priority_weights.get(priority, 1.0)

                # Calculate allocation
                allocation = (weight / total_weight) * total_power
                allocation = min(allocation, self.load_balancing_config.max_power_per_evse)
                allocation = min(allocation, remaining_power)

                distribution[evse_id] = allocation
                remaining_power -= allocation

            return distribution

        except Exception as e:
            self.logger.error(f"Error in priority-based balancing: {e}")
            return await self._equal_sharing_balance(evse_ids, total_power, current_loads)

    async def _price_based_balance(
        self,
        station_id: str,
        evse_ids: List[int],
        total_power: float,
        current_loads: Dict[int, float],
    ) -> Dict[int, float]:
        """Price-based load balancing."""
        try:
            # Get current electricity prices
            prices = await self._get_current_prices(station_id)

            if not prices:
                return await self._equal_sharing_balance(evse_ids, total_power, current_loads)

            # Calculate price-based weights
            total_weight = 0.0
            evse_weights = {}

            for evse_id in evse_ids:
                # Get EVSE price sensitivity
                price_sensitivity = await self.timescale_client.get_evse_price_sensitivity(
                    station_id, evse_id
                )
                price_sensitivity = (
                    price_sensitivity or self.load_balancing_config.price_sensitivity
                )

                # Calculate weight based on price (lower price = higher weight)
                current_price = prices.get("current", 0.0)
                weight = 1.0 / (1.0 + current_price * price_sensitivity)

                evse_weights[evse_id] = weight
                total_weight += weight

            # Distribute power based on price weights
            distribution = {}
            for evse_id in evse_ids:
                weight = evse_weights[evse_id]
                allocation = (weight / total_weight) * total_power
                allocation = min(allocation, self.load_balancing_config.max_power_per_evse)

                distribution[evse_id] = allocation

            return distribution

        except Exception as e:
            self.logger.error(f"Error in price-based balancing: {e}")
            return await self._equal_sharing_balance(evse_ids, total_power, current_loads)

    async def _v2g_optimized_balance(
        self,
        station_id: str,
        evse_ids: List[int],
        total_power: float,
        current_loads: Dict[int, float],
    ) -> Dict[int, float]:
        """V2G-optimized load balancing."""
        try:
            # Get V2G capabilities for each EVSE
            v2g_capabilities = {}
            for evse_id in evse_ids:
                capability = await self.timescale_client.get_evse_v2g_capability(
                    station_id, evse_id
                )
                v2g_capabilities[evse_id] = capability or False

            # Get current electricity prices
            prices = await self._get_current_prices(station_id)

            # Calculate V2G-optimized distribution
            distribution = {}

            for evse_id in evse_ids:
                if v2g_capabilities[evse_id]:
                    # V2G-capable EVSE
                    current_price = prices.get("current", 0.0)

                    if current_price < 0:  # Negative price - discharge
                        allocation = -self.load_balancing_config.v2g_discharge_limit
                    else:  # Positive price - charge
                        allocation = min(
                            self.load_balancing_config.max_power_per_evse,
                            total_power / len(evse_ids),
                        )
                else:
                    # Non-V2G EVSE - standard charging
                    allocation = min(
                        self.load_balancing_config.max_power_per_evse, total_power / len(evse_ids)
                    )

                distribution[evse_id] = allocation

            return distribution

        except Exception as e:
            self.logger.error(f"Error in V2G-optimized balancing: {e}")
            return await self._equal_sharing_balance(evse_ids, total_power, current_loads)

    async def _get_current_evse_load(self, station_id: str, evse_id: int) -> float:
        """Get current load for EVSE."""
        try:
            # Get current power consumption from telemetry
            current_power = await self.timescale_client.get_current_evse_power(station_id, evse_id)
            return current_power or 0.0

        except Exception as e:
            self.logger.error(f"Error getting current EVSE load: {e}")
            return 0.0

    async def _get_current_prices(self, station_id: str) -> Dict[str, float]:
        """Get current electricity prices."""
        try:
            # Get from cache first
            cache_key = f"{station_id}:prices"
            if cache_key in self.price_data_cache:
                cached_data = self.price_data_cache[cache_key]
                if datetime.now(timezone.utc) - cached_data["timestamp"] < timedelta(minutes=5):
                    return cached_data["prices"]

            # Get from database
            prices = await self.timescale_client.get_current_electricity_prices(station_id)

            # Cache the result
            self.price_data_cache[cache_key] = {
                "prices": prices,
                "timestamp": datetime.now(timezone.utc),
            }

            return prices

        except Exception as e:
            self.logger.error(f"Error getting current prices: {e}")
            return {}

    async def _calculate_schedule_cost(self, station_id: str, schedule: ChargingSchedule) -> float:
        """Calculate total cost for schedule."""
        try:
            # Get electricity prices
            prices = await self._get_current_prices(station_id)

            if not prices:
                return 0.0

            total_cost = 0.0
            current_price = prices.get("current", 0.0)

            # Calculate cost for each period
            for period in schedule.charging_schedule_period:
                # Convert power to energy (assuming 1-minute periods)
                energy_kwh = (period.limit * 60) / 1000  # kWh
                period_cost = energy_kwh * current_price
                total_cost += period_cost

            return total_cost

        except Exception as e:
            self.logger.error(f"Error calculating schedule cost: {e}")
            return 0.0

    async def _invalidate_affected_schedules(self, location: str) -> None:
        """Invalidate composite schedules affected by constraint changes."""
        try:
            # Clear cache for affected location
            keys_to_remove = [
                key
                for key in self.composite_schedule_cache.keys()
                if key.startswith(f"{location}:")
            ]

            for key in keys_to_remove:
                self.composite_schedule_cache.pop(key, None)

        except Exception as e:
            self.logger.error(f"Error invalidating schedules: {e}")

    async def _apply_demand_response_to_station(
        self, station_id: str, event: DemandResponseEvent
    ) -> None:
        """Apply demand response event to station."""
        try:
            # Get all EVSEs for station
            evse_ids = await self.timescale_client.get_station_evse_ids(station_id)

            # Apply demand response to each EVSE
            for evse_id in evse_ids:
                # Invalidate cached composite schedule
                keys_to_remove = [
                    key
                    for key in self.composite_schedule_cache.keys()
                    if key.startswith(f"{station_id}:{evse_id}:")
                ]

                for key in keys_to_remove:
                    self.composite_schedule_cache.pop(key, None)

                # Update EVSE power limit if needed
                if event.signal == DemandResponseSignal.REDUCE_LOAD:
                    await self.timescale_client.update_evse_power_limit(
                        station_id, evse_id, event.target_reduction or 0.5
                    )
                elif event.signal == DemandResponseSignal.INCREASE_LOAD:
                    await self.timescale_client.update_evse_power_limit(
                        station_id, evse_id, event.target_increase or 1.5
                    )

        except Exception as e:
            self.logger.error(f"Error applying demand response to station: {e}")
