"""Enhanced Redis integration layer for WebSocket handler."""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from .redis_enhanced import EnhancedRedisClient, ChargerState, TelemetryData, OptimizationDecision
from .monitoring import get_logger, PerformanceTimer


class RedisIntegrationService:
    """Integration service bridging WebSocket operations with enhanced Redis functionality."""
    
    def __init__(self, redis_client: EnhancedRedisClient):
        """Initialize Redis integration service."""
        self.redis = redis_client
        self.logger = get_logger(__name__)
    
    # ==========================================
    # WebSocket Integration Methods
    # ==========================================
    
    async def handle_charger_boot(self, station_id: str, boot_payload: Dict[str, Any]) -> None:
        """Handle charger boot notification with full state setup."""
        try:
            charging_station = boot_payload.get("chargingStation", {})
            
            # Create initial charger state
            initial_state = ChargerState(
                station_id=station_id,
                status="Available",  # Default status after boot
                power_kw=0.0,
                max_charge_power_kw=float(charging_station.get("maxChargePower", 150.0)),
                max_discharge_power_kw=float(charging_station.get("maxDischargePower", 100.0)),
                operation_mode="None",
                vehicle_connected=False,
                updated_at=datetime.now(timezone.utc).isoformat()
            )
            
            # Store in Redis using atomic operation
            await self.redis.update_charger_state_atomic(station_id, initial_state)
            
            # Store station information
            station_info = {
                "model": charging_station.get("model"),
                "vendor_name": charging_station.get("vendorName"),
                "firmware_version": charging_station.get("firmwareVersion"),
                "serial_number": charging_station.get("serialNumber"),
                "ocpp_version": "2.1",
                "boot_time": datetime.now(timezone.utc).isoformat(),
                "max_charge_power_kw": initial_state.max_charge_power_kw,
                "max_discharge_power_kw": initial_state.max_discharge_power_kw,
                "v2g_capable": charging_station.get("v2gCapable", False)
            }
            
            await self.redis.update_station_info(station_id, station_info)
            
            self.logger.info(f"Charger {station_id} boot processed and state initialized")
            
        except Exception as e:
            self.logger.error(f"Error handling charger boot for {station_id}: {e}")
    
    async def handle_telemetry_update(self, station_id: str, meter_values: List[Dict[str, Any]]) -> None:
        """Handle high-frequency telemetry data with time series storage."""
        try:
            for meter_value in meter_values:
                timestamp = meter_value.get("timestamp", datetime.now(timezone.utc).isoformat())
                sampled_values = meter_value.get("sampledValue", [])
                
                # Create telemetry data object
                telemetry = TelemetryData(
                    timestamp=timestamp,
                    station_id=station_id,
                    evse_id=meter_value.get("evseId", 1),
                    connector_id=1
                )
                
                # Process each measurement
                for sampled_value in sampled_values:
                    measurand = sampled_value.get("measurand", "Energy.Active.Import.Register")
                    value = float(sampled_value.get("value", 0))
                    unit = sampled_value.get("unitOfMeasure", {}).get("unit", "Wh")
                    
                    # Map OCPP measurands to telemetry fields
                    if measurand == "Power.Active.Import":
                        telemetry.power_kw = value / 1000 if unit == "W" else value
                    elif measurand == "Power.Active.Export":
                        telemetry.power_kw = -(value / 1000 if unit == "W" else value)
                    elif measurand == "Energy.Active.Import.Register":
                        telemetry.energy_kwh = value / 1000 if unit == "Wh" else value
                    elif measurand == "SoC":
                        telemetry.soc_percent = value
                    elif measurand == "Voltage":
                        telemetry.voltage_v = value
                    elif measurand == "Current.Import":
                        telemetry.current_a = value
                    elif measurand == "Frequency":
                        telemetry.frequency_hz = value
                    elif measurand == "Temperature":
                        telemetry.temperature_c = value
                    elif measurand == "Power.Reactive.Import":
                        telemetry.reactive_power_kvar = value / 1000 if unit == "var" else value
                    elif measurand == "Power.Factor":
                        telemetry.power_factor = value
                
                # Store in Redis time series
                await self.redis.store_telemetry_timeseries(station_id, telemetry)
                
                # Update current charger state if power or SoC changed
                if telemetry.power_kw is not None or telemetry.soc_percent is not None:
                    await self._update_charger_state_from_telemetry(station_id, telemetry)
            
        except Exception as e:
            self.logger.error(f"Error handling telemetry for {station_id}: {e}")
    
    async def _update_charger_state_from_telemetry(self, station_id: str, telemetry: TelemetryData) -> None:
        """Update charger state based on telemetry data."""
        try:
            # Get current state
            current_state = await self.redis.get_charger_state(station_id)
            
            if current_state:
                # Update power and SoC if available
                if telemetry.power_kw is not None:
                    current_state.power_kw = telemetry.power_kw
                    
                    # Update status based on power flow
                    if telemetry.power_kw > 0.1:
                        current_state.status = "Charging"
                    elif telemetry.power_kw < -0.1:
                        current_state.status = "Discharging"
                    elif abs(telemetry.power_kw) <= 0.1:
                        current_state.status = "Available" if current_state.vehicle_connected else "Available"
                
                if telemetry.soc_percent is not None:
                    current_state.soc_percent = telemetry.soc_percent
                
                current_state.updated_at = telemetry.timestamp
                
                # Update in Redis
                await self.redis.update_charger_state_atomic(station_id, current_state)
        
        except Exception as e:
            self.logger.error(f"Error updating charger state from telemetry for {station_id}: {e}")
    
    # ==========================================
    # Fleet Management Integration
    # ==========================================
    
    async def update_fleet_status(self, operator_id: str, 
                                vehicle_data: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        """Update comprehensive fleet status."""
        try:
            # Extract availability information
            availability_states = {}
            fleet_constraints = {}
            charging_queue_updates = []
            
            for vehicle_index, vehicle_info in vehicle_data.items():
                # Determine availability
                is_available = vehicle_info.get("connected", False) and vehicle_info.get("soc", 0) > 20
                availability_states[vehicle_index] = is_available
                
                # Add to charging queue if needs charging
                soc = vehicle_info.get("soc", 100)
                departure_time = vehicle_info.get("departure_time")
                
                if soc < 80 and is_available:  # Needs charging
                    # Calculate priority score (lower SoC + earlier departure = higher priority)
                    urgency_weight = 100 - soc  # 0-80 range
                    time_weight = 0
                    
                    if departure_time:
                        try:
                            departure_timestamp = datetime.fromisoformat(departure_time).timestamp()
                            time_weight = max(0, departure_timestamp - time.time()) / 3600  # Hours until departure
                        except (ValueError, TypeError):
                            time_weight = 24  # Default 24 hours if parsing fails
                    
                    priority_score = urgency_weight + (1000 / max(time_weight, 0.1))  # Higher score = higher priority
                    
                    charging_queue_updates.append({
                        "vehicle_id": str(vehicle_index),
                        "priority_score": priority_score
                    })
            
            # Update fleet availability bitmap
            available_count = await self.redis.update_fleet_availability(operator_id, availability_states)
            
            # Update charging queue
            for queue_item in charging_queue_updates:
                await self.redis.add_to_charging_queue(
                    operator_id, 
                    queue_item["vehicle_id"], 
                    queue_item["priority_score"]
                )
            
            # Update fleet constraints
            total_vehicles = len(vehicle_data)
            avg_soc = sum(v.get("soc", 50) for v in vehicle_data.values()) / max(total_vehicles, 1)
            
            fleet_constraints = {
                "total_vehicles": total_vehicles,
                "available_vehicles": available_count,
                "average_soc": round(avg_soc, 1),
                "min_fleet_soc": min(v.get("soc", 100) for v in vehicle_data.values()),
                "max_fleet_soc": max(v.get("soc", 0) for v in vehicle_data.values()),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            
            await self.redis.update_fleet_constraints(operator_id, fleet_constraints)
            
            return {
                "available_count": available_count,
                "total_count": total_vehicles,
                "charging_queue_size": len(charging_queue_updates),
                "average_soc": avg_soc,
                "status": "updated"
            }
            
        except Exception as e:
            self.logger.error(f"Error updating fleet status for {operator_id}: {e}")
            return {"status": "error", "error": str(e)}
    
    async def get_fleet_optimization_data(self, operator_id: str) -> Dict[str, Any]:
        """Get comprehensive fleet data for optimization engine."""
        try:
            with PerformanceTimer("redis_get_fleet_optimization_data"):
                # Get basic fleet metrics
                available_count = await self.redis.get_fleet_availability_count(operator_id)
                available_vehicles = await self.redis.get_fleet_availability_list(operator_id)
                constraints = await self.redis.get_fleet_constraints(operator_id)
                charging_queue = await self.redis.get_charging_queue(operator_id, 20)
                
                # Get active sessions
                active_sessions = await self.redis.get_active_sessions(operator_id)
                
                return {
                    "fleet_operator_id": operator_id,
                    "available_count": available_count,
                    "available_vehicles": available_vehicles,
                    "constraints": constraints,
                    "charging_queue": charging_queue,
                    "active_sessions": active_sessions,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
        
        except Exception as e:
            self.logger.error(f"Error getting fleet optimization data for {operator_id}: {e}")
            return {"error": str(e)}
    
    # ==========================================
    # Market Data Integration
    # ==========================================
    
    async def update_market_prices(self, node_id: str, price_updates: Dict[str, Any]) -> None:
        """Update electricity market prices."""
        try:
            # Update current LMP prices
            if "current_prices" in price_updates:
                await self.redis.update_electricity_prices(
                    node_id, "lmp", price_updates["current_prices"]
                )
            
            # Update price forecasts
            if "forecasts" in price_updates:
                await self.redis.store_price_forecast(node_id, price_updates["forecasts"])
            
            self.logger.debug(f"Market prices updated for node {node_id}")
            
        except Exception as e:
            self.logger.error(f"Error updating market prices for {node_id}: {e}")
    
    async def get_current_market_data(self, node_id: str) -> Dict[str, Any]:
        """Get current market data for optimization."""
        try:
            # Get current prices
            current_prices = await self.redis.get_current_prices(node_id, "lmp")
            
            # Get near-term forecasts (next 4 hours)
            forecasts = await self.redis.get_price_forecast(
                node_id, 
                start_time=time.time(),
                end_time=time.time() + 14400  # 4 hours
            )
            
            return {
                "node_id": node_id,
                "current_prices": current_prices,
                "forecasts": forecasts,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting market data for {node_id}: {e}")
            return {"error": str(e)}
    
    # ==========================================
    # Optimization Engine Integration
    # ==========================================
    
    async def store_optimization_result(self, decision: OptimizationDecision,
                                      charging_schedules: List[Dict[str, Any]]) -> None:
        """Store optimization decision and resulting charging schedules."""
        try:
            # Store decision
            await self.redis.store_optimization_decision(decision)
            
            # Store individual charging schedules
            for schedule in charging_schedules:
                station_id = schedule.get("station_id")
                evse_id = schedule.get("evse_id", 1)
                profile = schedule.get("charging_profile")
                
                if station_id and profile:
                    # Store as pending profile to be sent
                    await self.redis._store_pending_profile(station_id, evse_id, profile)
            
            # Publish optimization completion event
            await self.redis.publish_notification("notify:optimization:complete", {
                "decision_id": decision.decision_id,
                "fleet_operator_id": decision.fleet_operator_id,
                "schedules_count": len(charging_schedules),
                "timestamp": decision.computed_at
            })
            
            self.logger.info(f"Optimization result stored: {decision.decision_id}")
            
        except Exception as e:
            self.logger.error(f"Error storing optimization result: {e}")
    
    async def get_optimization_inputs(self, fleet_operator_id: str) -> Dict[str, Any]:
        """Get all data needed for optimization."""
        try:
            # Get fleet data
            fleet_data = await self.get_fleet_optimization_data(fleet_operator_id)
            
            # Get station states for fleet
            # This would require fleet-to-station mapping
            # For now, we'll get constraints and let optimization engine query specific stations
            
            # Get market data (assuming single pricing node for now)
            market_data = await self.get_current_market_data("CAISO_SP15")  # Example node
            
            # Get grid signals
            grid_signals = await self.redis.get_grid_signals(count=10)
            
            return {
                "fleet_data": fleet_data,
                "market_data": market_data,
                "grid_signals": grid_signals,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting optimization inputs for {fleet_operator_id}: {e}")
            return {"error": str(e)}
    
    # ==========================================
    # Real-time Analytics
    # ==========================================
    
    async def get_real_time_analytics(self, time_window_minutes: int = 60) -> Dict[str, Any]:
        """Get real-time analytics across the platform."""
        try:
            # Get performance stats
            performance_stats = await self.redis.get_performance_stats()
            
            # This would be expanded to include:
            # - Total power flow across all stations
            # - Fleet utilization rates
            # - Grid service participation
            # - Cost savings metrics
            
            return {
                "performance_stats": performance_stats,
                "time_window_minutes": time_window_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting real-time analytics: {e}")
            return {"error": str(e)}
    
    # ==========================================
    # Health and Monitoring
    # ==========================================
    
    async def get_system_health(self) -> Dict[str, Any]:
        """Get comprehensive system health status."""
        try:
            # Redis health
            redis_health = await self.redis.health_check()
            
            # Performance metrics
            performance = await self.redis.get_performance_stats()
            
            # Connection status summary
            # This would include active connections, message rates, etc.
            
            return {
                "redis_health": redis_health,
                "performance_metrics": performance,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting system health: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }