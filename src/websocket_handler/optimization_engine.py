"""Advanced optimization engine with Julia MIP solver integration."""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

from .config import OptimizationServiceConfig
from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .supabase_client import SupabaseClient
from .connection_manager import ConnectionManager
from .julia_bridge import JuliaBridge


@dataclass
class StationState:
    station_id: str
    evse_id: int
    connector_id: int
    soc: float
    power_kw: float
    max_charge_kw: float
    max_discharge_kw: float


class OptimizationEngine:
    """Advanced optimization engine with Julia MIP solver integration."""

    def __init__(
        self,
        config: OptimizationServiceConfig,
        timescale_client: TimescaleClient,
        supabase_client: SupabaseClient,
        connection_manager: Optional[ConnectionManager],
    ) -> None:
        self.config = config
        self.timescale_client = timescale_client
        self.supabase_client = supabase_client
        self.connection_manager = connection_manager
        self.logger = get_logger(__name__)
        self._lock = asyncio.Lock()
        self._pending_reason: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        
        # Initialize Julia bridge
        self.julia_bridge = JuliaBridge()
        self._use_mip_solver = True  # Flag to enable/disable MIP solver

    def set_connection_manager(self, connection_manager: ConnectionManager) -> None:
        """Set the connection manager (called after server initialization)."""
        self.connection_manager = connection_manager
        self.logger.info("Connection manager set for optimization engine")
    
    async def start(self) -> None:
        if not self.config.enabled:
            self.logger.info("Optimization engine disabled via configuration")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.logger.info("Optimization engine started with Julia MIP solver")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.logger.info("Optimization engine stopped")

    async def request_run(self, reason: str) -> None:
        async with self._lock:
            self._pending_reason = reason

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while self._running:
            reason = None
            async with self._lock:
                reason = self._pending_reason
                self._pending_reason = None
            if reason:
                try:
                    await self._run_optimization(reason)
                except Exception as exc:
                    self.logger.error(f"Optimization run failed: {exc}")
            await asyncio.sleep(30)  # Run every 30 seconds for MIP solver

    async def _run_optimization(self, trigger_reason: str) -> None:
        self.logger.info("Running optimization triggered by %s", trigger_reason)

        now = datetime.now(timezone.utc)
        horizon_end = now + timedelta(hours=self.config.horizon_hours)

        # Get active vehicles and routes
        vehicles = await self.timescale_client.get_active_vehicles()
        routes = await self.timescale_client.get_active_routes()
        
        if not vehicles:
            self.logger.info("No active vehicles to optimize")
            return

        # Get electricity prices
        prices = await self.timescale_client.get_latest_prices(
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            start=now - timedelta(hours=1),
        )

        if self._use_mip_solver:
            await self._run_mip_optimization(vehicles, routes, prices, now, horizon_end, trigger_reason)
        else:
            await self._run_heuristic_optimization(vehicles, routes, prices, now, horizon_end, trigger_reason)

    async def _run_mip_optimization(
        self, 
        vehicles: List[Dict[str, Any]], 
        routes: List[Dict[str, Any]], 
        prices: List[Dict[str, Any]], 
        now: datetime, 
        horizon_end: datetime,
        trigger_reason: str
    ) -> None:
        """Run optimization using Julia MIP solver."""
        try:
            # Prepare vehicle data for Julia solver
            vehicle_data = []
            for vehicle in vehicles:
                # Find route for this vehicle
                vehicle_route = next((r for r in routes if r["vehicle_id"] == vehicle["vehicle_id"]), None)
                
                vehicle_info = self.julia_bridge.prepare_vehicle_data(
                    vehicle_id=vehicle["vehicle_id"],
                    battery_capacity_kwh=vehicle.get("battery_capacity_kwh", 75.0),
                    max_charge_rate_kw=vehicle.get("max_charge_rate_kw", 22.0),
                    max_discharge_rate_kw=vehicle.get("max_discharge_rate_kw", 10.0),
                    initial_soc_kwh=vehicle.get("current_soc_kwh", 30.0),
                    min_soc_kwh=vehicle.get("min_soc_kwh", 15.0),
                    departure_time=datetime.fromisoformat(vehicle_route["departure_time"]) if vehicle_route else None,
                    required_soc_kwh=vehicle_route["required_soc_percent"] * vehicle.get("battery_capacity_kwh", 75.0) / 100 if vehicle_route else None
                )
                vehicle_data.append(vehicle_info)
            
            # Prepare electricity prices
            price_by_time: Dict[datetime, float] = {}
            for price in prices:
                price_by_time[price["time"]] = price.get("lmp_price_mwh", 0.0) / 1000  # Convert to $/kWh
            
            # Generate price array for optimization horizon
            n_timesteps = int(self.config.horizon_hours * 60 / self.config.timestep_minutes)
            electricity_prices = []
            for i in range(n_timesteps):
                timestep_time = now + timedelta(minutes=i * self.config.timestep_minutes)
                price_time = timestep_time.replace(second=0, microsecond=0)
                price = price_by_time.get(price_time, 0.12)  # Default price
                electricity_prices.append(price)
            
            # Prepare optimization parameters
            params = self.julia_bridge.prepare_optimization_params(
                horizon_hours=self.config.horizon_hours,
                timestep_minutes=self.config.timestep_minutes,
                facility_capacity_kw=self.config.facility_capacity_kw,
                demand_charge_rate_per_kw=self.config.demand_charge_rate_per_kw,
                electricity_prices=electricity_prices,
                cost_weight=1.0,
                peak_weight=0.1
            )
            
            # Solve optimization
            start_time = datetime.now()
            result = await self.julia_bridge.solve_optimization(vehicle_data, params, timeout_seconds=30)
            solve_time = (datetime.now() - start_time).total_seconds() * 1000
            
            if result["status"] == "OPTIMAL":
                await self._process_mip_result(result, vehicles, now, horizon_end, trigger_reason, solve_time)
            else:
                self.logger.warning(f"MIP solver failed with status: {result['status']}")
                # Fallback to heuristic
                await self._run_heuristic_optimization(vehicles, routes, prices, now, horizon_end, trigger_reason)
                
        except Exception as e:
            self.logger.error(f"MIP optimization failed: {e}")
            # Fallback to heuristic
            await self._run_heuristic_optimization(vehicles, routes, prices, now, horizon_end, trigger_reason)

    async def _process_mip_result(
        self, 
        result: Dict[str, Any], 
        vehicles: List[Dict[str, Any]], 
        now: datetime, 
        horizon_end: datetime,
        trigger_reason: str,
        solve_time: float
    ) -> None:
        """Process MIP optimization result and create charging schedules."""
        charging_schedule = result["charging_schedule"]
        discharging_schedule = result["discharging_schedule"]
        soc_trajectory = result["soc_trajectory"]
        
        schedules = []
        
        for i, vehicle in enumerate(vehicles):
            if i >= len(charging_schedule):
                continue
                
            # Create charging schedule for this vehicle
            periods = []
            current_time = now
            
            for t in range(len(charging_schedule[i])):
                # Calculate net power (charging - discharging)
                net_power = charging_schedule[i][t] - discharging_schedule[i][t]
                
                periods.append({
                    "startPeriod": int((current_time - now).total_seconds()),
                    "limit": net_power * 1000,  # Convert to watts
                    "numberPhases": 3,
                })
                
                current_time += timedelta(minutes=self.config.timestep_minutes)
            
            schedule = {
                "station_id": vehicle["station_id"],
                "evse_id": 1,  # Default EVSE ID
                "decision_id": None,
                "profile_id": f"mip-{vehicle['vehicle_id']}-{int(now.timestamp())}",
                "start_time": now,
                "end_time": horizon_end,
                "schedule_periods": periods,
                "priority": 0,
                "stacking_level": 0,
                "purpose": "mip_optimization",
            }
            schedules.append(schedule)
            await self.timescale_client.store_charging_schedule(schedule)
        
        # Store optimization decision
        await self.timescale_client.store_optimization_decision({
            "time": now,
            "optimization_window_start": now,
            "optimization_window_end": horizon_end,
            "fleet_operator_id": None,
            "site_id": None,
            "algorithm_version": "julia-mip-v1",
            "objective_function": "cost_minimization_with_peak_shaving",
            "objective_value": result.get("objective_value"),
            "computation_time_ms": int(solve_time),
            "constraints_satisfied": True,
            "decision_payload": {
                "schedules": schedules,
                "mip_result": result,
                "trigger_reason": trigger_reason
            },
            "sync_status": "completed",
        })
        
        self.logger.info(f"MIP optimization completed: {len(schedules)} schedules, objective: {result.get('objective_value', 'N/A')}")

    async def _run_heuristic_optimization(
        self, 
        vehicles: List[Dict[str, Any]], 
        routes: List[Dict[str, Any]], 
        prices: List[Dict[str, Any]], 
        now: datetime, 
        horizon_end: datetime,
        trigger_reason: str
    ) -> None:
        """Run optimization using heuristic approach (fallback)."""
        self.logger.info("Running heuristic optimization (fallback)")

        price_by_time: Dict[datetime, float] = {}
        for price in prices:
            price_by_time[price["time"]] = price.get("lmp_price_mwh", 0.0)

        schedules = []
        for vehicle in vehicles:
            station_id = vehicle["station_id"]
            evse_id = 1  # Default EVSE ID
            start_time = now
            end_time = horizon_end

            periods = []
            current_time = start_time
            soc = vehicle.get("current_soc_kwh", 30.0) / vehicle.get("battery_capacity_kwh", 75.0)
            
            while current_time < end_time:
                price = price_by_time.get(current_time.replace(second=0, microsecond=0), 0.0)
                power_kw = self._determine_power(price, soc, vehicle)
                periods.append({
                        "startPeriod": int((current_time - start_time).total_seconds()),
                        "limit": power_kw * 1000,
                        "numberPhases": 3,
                })
                soc = min(1.0, max(0.0, soc + (power_kw * (self.config.timestep_minutes / 60.0)) / vehicle.get("battery_capacity_kwh", 75.0)))
                current_time += timedelta(minutes=self.config.timestep_minutes)

            schedule = {
                "station_id": station_id,
                "evse_id": evse_id,
                "decision_id": None,
                "profile_id": f"heuristic-{vehicle['vehicle_id']}-{int(now.timestamp())}",
                "start_time": start_time,
                "end_time": end_time,
                "schedule_periods": periods,
                "priority": 0,
                "stacking_level": 0,
                "purpose": "heuristic_fallback",
            }
            schedules.append(schedule)
            await self.timescale_client.store_charging_schedule(schedule)

        await self.timescale_client.store_optimization_decision({
                "time": now,
                "optimization_window_start": now,
                "optimization_window_end": horizon_end,
                "fleet_operator_id": None,
                "site_id": None,
                "algorithm_version": "heuristic-v1",
                "objective_function": "price_driven",
                "objective_value": None,
                "computation_time_ms": 0,
                "constraints_satisfied": True,
            "decision_payload": {"schedules": schedules, "trigger_reason": trigger_reason},
                "sync_status": "completed",
        })
        self.logger.info("Heuristic optimization created %d schedules", len(schedules))

    def _determine_power(self, price: float, soc: float, vehicle: Dict[str, Any]) -> float:
        """Enhanced heuristic mapping price and SOC to power setpoints."""
        battery_capacity = vehicle.get("battery_capacity_kwh", 75.0)
        max_charge_rate = vehicle.get("max_charge_rate_kw", 22.0)
        max_discharge_rate = vehicle.get("max_discharge_rate_kw", 10.0)
        min_soc = vehicle.get("min_soc_kwh", 15.0) / battery_capacity
        
        if soc < min_soc:
            return max_charge_rate
        if price < 0:
            return max_charge_rate
        if price > 150:
            return -min(max_discharge_rate, soc * max_charge_rate)
        return 0.0
