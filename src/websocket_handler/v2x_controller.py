"""V2X (Vehicle-to-Everything) controller for bidirectional charging modes."""

import asyncio
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from .config import Config
from .monitoring import get_logger


class V2XOperationMode(Enum):
    """V2X operation modes as defined in OCPP 2.1."""
    CENTRAL_SETPOINT = "CentralSetpoint"
    LOCAL_FREQUENCY = "LocalFrequency" 
    LOCAL_LOAD_BALANCING = "LocalLoadBalancing"
    EXTERNAL_SETPOINT = "ExternalSetpoint"


@dataclass
class V2XControllerConfig:
    """V2X controller configuration."""
    enabled: bool = True
    supported_modes: List[str] = None
    update_interval: Dict[str, int] = None  # seconds for each mode
    power_limits: Dict[str, float] = None   # kW limits
    frequency_deadband: float = 0.05        # Hz
    voltage_limits: Dict[str, float] = None # V limits
    
    def __post_init__(self):
        if self.supported_modes is None:
            self.supported_modes = [mode.value for mode in V2XOperationMode]
        
        if self.update_interval is None:
            self.update_interval = {
                V2XOperationMode.CENTRAL_SETPOINT.value: 30,
                V2XOperationMode.LOCAL_FREQUENCY.value: 1,
                V2XOperationMode.LOCAL_LOAD_BALANCING.value: 5,
                V2XOperationMode.EXTERNAL_SETPOINT.value: 10
            }
        
        if self.power_limits is None:
            self.power_limits = {
                "max_charge_power": 150.0,
                "max_discharge_power": 100.0,
                "min_power": 1.0
            }
        
        if self.voltage_limits is None:
            self.voltage_limits = {
                "min_voltage": 380.0,
                "max_voltage": 420.0
            }


@dataclass
class V2XSetpoint:
    """V2X power setpoint."""
    station_id: str
    evse_id: int
    power_kw: float
    mode: V2XOperationMode
    timestamp: str
    duration_seconds: Optional[int] = None
    ramp_rate_kw_per_s: Optional[float] = None
    constraints: Optional[Dict[str, Any]] = None


class V2XController:
    """V2X controller for bidirectional charging operations."""
    
    def __init__(
        self, 
        config: V2XControllerConfig,
        app_config: Config
    ):
        """Initialize V2X controller."""
        # Redis client removed for simplification
        self.v2x_config = config
        self.app_config = app_config
        self.logger = get_logger(__name__)
        
        # Active setpoints tracking
        self.active_setpoints: Dict[str, V2XSetpoint] = {}  # station_id -> setpoint
        self.mode_controllers: Dict[V2XOperationMode, Any] = {}
        
        # Background tasks
        self.tasks: Dict[str, asyncio.Task] = {}
        self.running = False
        
        # Initialize mode controllers
        self._initialize_mode_controllers()
    
    def _initialize_mode_controllers(self) -> None:
        """Initialize controllers for different V2X modes."""
        self.mode_controllers = {
            V2XOperationMode.CENTRAL_SETPOINT: CentralSetpointController(self),
            V2XOperationMode.LOCAL_FREQUENCY: LocalFrequencyController(self),
            V2XOperationMode.LOCAL_LOAD_BALANCING: LocalLoadBalancingController(self),
            V2XOperationMode.EXTERNAL_SETPOINT: ExternalSetpointController(self)
        }
    
    async def start(self) -> None:
        """Start V2X controller."""
        if not self.v2x_config.enabled:
            self.logger.info("V2X controller is disabled")
            return
        
        self.running = True
        
        # Start background tasks for each mode
        for mode, controller in self.mode_controllers.items():
            if mode.value in self.v2x_config.supported_modes:
                task_name = f"v2x_{mode.value.lower()}"
                self.tasks[task_name] = asyncio.create_task(
                    controller.run()
                )
        
        # Start main monitoring task
        self.tasks["v2x_monitor"] = asyncio.create_task(self._monitor_setpoints())
        
        self.logger.info(f"V2X controller started with modes: {self.v2x_config.supported_modes}")
    
    async def stop(self) -> None:
        """Stop V2X controller."""
        self.running = False
        
        # Cancel all tasks
        for task_name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self.tasks.clear()
        self.logger.info("V2X controller stopped")
    
    async def set_power_setpoint(self, setpoint: V2XSetpoint) -> bool:
        """Set power setpoint for a station."""
        try:
            # Validate setpoint
            if not self._validate_setpoint(setpoint):
                return False
            
            # Store active setpoint
            self.active_setpoints[setpoint.station_id] = setpoint
            
            # Send to charger via connection manager
            success = await self._send_setpoint_to_charger(setpoint)
            
            if success:
                # Redis storage removed for simplification
                
                # Store setpoint event (Kafka removed for simplification)
                await self._store_setpoint_event(setpoint)
                
                self.logger.info(
                    f"Applied V2X setpoint: {setpoint.station_id} = {setpoint.power_kw}kW "
                    f"({setpoint.mode.value})"
                )
            
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to set V2X setpoint for {setpoint.station_id}: {e}")
            return False
    
    async def get_active_setpoint(self, station_id: str) -> Optional[V2XSetpoint]:
        """Get active setpoint for a station."""
        return self.active_setpoints.get(station_id)
    
    async def clear_setpoint(self, station_id: str) -> bool:
        """Clear active setpoint for a station."""
        try:
            if station_id in self.active_setpoints:
                # Send zero setpoint
                zero_setpoint = V2XSetpoint(
                    station_id=station_id,
                    evse_id=1,  # Default EVSE
                    power_kw=0.0,
                    mode=V2XOperationMode.CENTRAL_SETPOINT,
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                
                success = await self._send_setpoint_to_charger(zero_setpoint)
                
                if success:
                    # Remove from active setpoints
                    del self.active_setpoints[station_id]
                    
                    # Redis clearing removed for simplification
                
                return success
            
            return True  # Already cleared
            
        except Exception as e:
            self.logger.error(f"Failed to clear V2X setpoint for {station_id}: {e}")
            return False
    
    def _validate_setpoint(self, setpoint: V2XSetpoint) -> bool:
        """Validate V2X setpoint."""
        # Check power limits
        max_charge = self.v2x_config.power_limits["max_charge_power"]
        max_discharge = self.v2x_config.power_limits["max_discharge_power"]
        min_power = self.v2x_config.power_limits["min_power"]
        
        if setpoint.power_kw > max_charge:
            self.logger.warning(f"Setpoint exceeds max charge power: {setpoint.power_kw}kW > {max_charge}kW")
            return False
        
        if setpoint.power_kw < -max_discharge:
            self.logger.warning(f"Setpoint exceeds max discharge power: {setpoint.power_kw}kW < -{max_discharge}kW")
            return False
        
        if abs(setpoint.power_kw) > 0 and abs(setpoint.power_kw) < min_power:
            self.logger.warning(f"Setpoint below minimum power: {abs(setpoint.power_kw)}kW < {min_power}kW")
            return False
        
        # Check mode support
        if setpoint.mode.value not in self.v2x_config.supported_modes:
            self.logger.warning(f"Unsupported V2X mode: {setpoint.mode.value}")
            return False
        
        return True
    
    async def _send_setpoint_to_charger(self, setpoint: V2XSetpoint) -> bool:
        """Send setpoint to charger via OCPP."""
        try:
            # Create charging profile for the setpoint
            charging_profile = {
                "id": int(time.time()),
                "stackLevel": 0,
                "chargingProfilePurpose": "ChargingStationMaxProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "id": int(time.time()),
                    "startSchedule": setpoint.timestamp,
                    "duration": setpoint.duration_seconds or 3600,  # Default 1 hour
                    "chargingRateUnit": "W",
                    "chargingSchedulePeriod": [
                        {
                            "startPeriod": 0,
                            "limit": int(abs(setpoint.power_kw) * 1000),  # Convert to W
                            "numberPhases": 3
                        }
                    ]
                }
            }
            
            # For discharge (negative power), we need different approach
            if setpoint.power_kw < 0:
                # V2G discharge profile
                charging_profile["chargingProfilePurpose"] = "V2XProfile"
                charging_profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] = int(abs(setpoint.power_kw) * 1000)
            
            # Get connection manager from server (this would be injected in real implementation)
            # For now, we'll store the profile and let the message handler send it
            await self._store_pending_profile(setpoint.station_id, setpoint.evse_id, charging_profile)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to send setpoint to charger {setpoint.station_id}: {e}")
            return False
    
    async def _store_setpoint_event(self, setpoint: V2XSetpoint) -> None:
        """Store setpoint event."""
        try:
            # This would store the event in the database
            # For now, just log it
            self.logger.info(f"Stored setpoint event: {setpoint.station_id} = {setpoint.power_kw}kW")
            
        except Exception as e:
            self.logger.error(f"Error storing setpoint event: {e}")
    
    async def _store_pending_profile(self, station_id: str, evse_id: int, profile: Dict[str, Any]) -> None:
        """Store pending charging profile."""
        try:
            # This would store the profile in the database
            # For now, just log it
            self.logger.info(f"Stored pending profile for {station_id}, EVSE {evse_id}")
            
        except Exception as e:
            self.logger.error(f"Error storing pending profile: {e}")
    
    async def _monitor_setpoints(self) -> None:
        """Monitor active setpoints and handle timeouts."""
        while self.running:
            try:
                current_time = time.time()
                expired_setpoints = []
                
                for station_id, setpoint in self.active_setpoints.items():
                    # Check if setpoint has expired
                    setpoint_time = datetime.fromisoformat(setpoint.timestamp.replace('Z', '+00:00'))
                    setpoint_timestamp = setpoint_time.timestamp()
                    
                    if setpoint.duration_seconds:
                        if current_time > setpoint_timestamp + setpoint.duration_seconds:
                            expired_setpoints.append(station_id)
                
                # Clear expired setpoints
                for station_id in expired_setpoints:
                    await self.clear_setpoint(station_id)
                    self.logger.info(f"Cleared expired V2X setpoint for {station_id}")
                
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in V2X setpoint monitoring: {e}")
                await asyncio.sleep(10)


class CentralSetpointController:
    """Central setpoint mode controller."""
    
    def __init__(self, v2x_controller: V2XController):
        self.v2x_controller = v2x_controller
        self.logger = get_logger(f"{__name__}.central")
    
    async def run(self) -> None:
        """Run central setpoint controller."""
        while self.v2x_controller.running:
            try:
                # Listen for optimization commands from Redis/Kafka
                await self._process_optimization_commands()
                
                interval = self.v2x_controller.v2x_config.update_interval[V2XOperationMode.CENTRAL_SETPOINT.value]
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"Central setpoint controller error: {e}")
                await asyncio.sleep(5)
    
    async def _process_optimization_commands(self) -> None:
        """Process optimization commands for central setpoint mode."""
        # This would interface with the optimization engine
        # For now, we'll check Redis for pending commands
        pass


class LocalFrequencyController:
    """Local frequency response controller."""
    
    def __init__(self, v2x_controller: V2XController):
        self.v2x_controller = v2x_controller
        self.logger = get_logger(f"{__name__}.frequency")
        self.nominal_frequency = 50.0  # Hz (or 60.0 for US)
    
    async def run(self) -> None:
        """Run frequency response controller."""
        while self.v2x_controller.running:
            try:
                # Monitor grid frequency and respond
                await self._monitor_frequency()
                
                interval = self.v2x_controller.v2x_config.update_interval[V2XOperationMode.LOCAL_FREQUENCY.value]
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"Frequency controller error: {e}")
                await asyncio.sleep(1)
    
    async def _monitor_frequency(self) -> None:
        """Monitor grid frequency and generate response setpoints."""
        try:
            # Get current frequency from grid monitoring
            current_frequency = await self._get_current_frequency()
            
            if current_frequency is None:
                return
            
            # Calculate frequency deviation
            frequency_deviation = current_frequency - self.nominal_frequency
            
            # Check if deviation exceeds deadband
            if abs(frequency_deviation) > self.v2x_controller.v2x_config.frequency_deadband:
                # Calculate power response
                power_response = await self._calculate_frequency_response(frequency_deviation)
                
                if power_response != 0:
                    # Apply setpoint to all V2G-capable stations
                    await self._apply_frequency_response(power_response)
            
        except Exception as e:
            self.logger.error(f"Error monitoring frequency: {e}")
    
    async def _get_current_frequency(self) -> Optional[float]:
        """Get current grid frequency."""
        try:
            # This would integrate with grid monitoring systems
            # For now, return a mock frequency
            return 50.0  # Hz
            
        except Exception as e:
            self.logger.error(f"Error getting current frequency: {e}")
            return None
    
    async def _calculate_frequency_response(self, frequency_deviation: float) -> float:
        """Calculate power response based on frequency deviation."""
        try:
            # Frequency response parameters
            droop_factor = 0.05  # 5% droop
            max_response_power = 100.0  # kW
            
            # Calculate power response (negative for discharge, positive for charge)
            if frequency_deviation > 0:
                # Frequency too high - discharge to reduce frequency
                power_response = -min(frequency_deviation * droop_factor * max_response_power, max_response_power)
            else:
                # Frequency too low - charge to increase frequency
                power_response = min(abs(frequency_deviation) * droop_factor * max_response_power, max_response_power)
            
            return power_response
            
        except Exception as e:
            self.logger.error(f"Error calculating frequency response: {e}")
            return 0.0
    
    async def _apply_frequency_response(self, power_response: float) -> None:
        """Apply frequency response to V2G-capable stations."""
        try:
            # Get all V2G-capable stations
            v2g_stations = await self._get_v2g_capable_stations()
            
            for station_id in v2g_stations:
                setpoint = V2XSetpoint(
                    station_id=station_id,
                    evse_id=1,
                    power_kw=power_response,
                    mode=V2XOperationMode.LOCAL_FREQUENCY,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=60  # 1-minute response
                )
                
                await self.v2x_controller.set_power_setpoint(setpoint)
            
        except Exception as e:
            self.logger.error(f"Error applying frequency response: {e}")
    
    async def _get_v2g_capable_stations(self) -> List[str]:
        """Get list of V2G-capable stations."""
        try:
            # This would query the database for V2G-capable stations
            # For now, return empty list
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting V2G-capable stations: {e}")
            return []


class LocalLoadBalancingController:
    """Local load balancing controller."""
    
    def __init__(self, v2x_controller: V2XController):
        self.v2x_controller = v2x_controller
        self.logger = get_logger(f"{__name__}.load_balance")
    
    async def run(self) -> None:
        """Run load balancing controller."""
        while self.v2x_controller.running:
            try:
                # Monitor building/site load and balance with V2G
                await self._balance_site_load()
                
                interval = self.v2x_controller.v2x_config.update_interval[V2XOperationMode.LOCAL_LOAD_BALANCING.value]
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"Load balancing controller error: {e}")
                await asyncio.sleep(5)
    
    async def _balance_site_load(self) -> None:
        """Balance site load using available V2G capacity."""
        try:
            # Get current site load
            site_load = await self._get_current_site_load()
            
            if site_load is None:
                return
            
            # Get target load (from building management system)
            target_load = await self._get_target_site_load()
            
            if target_load is None:
                return
            
            # Calculate load imbalance
            load_imbalance = site_load - target_load
            
            # Check if imbalance exceeds threshold
            imbalance_threshold = 10.0  # kW
            if abs(load_imbalance) > imbalance_threshold:
                # Calculate V2G response
                v2g_response = await self._calculate_load_balancing_response(load_imbalance)
                
                if v2g_response != 0:
                    # Apply load balancing setpoints
                    await self._apply_load_balancing_response(v2g_response)
            
        except Exception as e:
            self.logger.error(f"Error balancing site load: {e}")
    
    async def _get_current_site_load(self) -> Optional[float]:
        """Get current site load."""
        try:
            # This would integrate with building management systems
            # For now, return a mock load
            return 100.0  # kW
            
        except Exception as e:
            self.logger.error(f"Error getting current site load: {e}")
            return None
    
    async def _get_target_site_load(self) -> Optional[float]:
        """Get target site load."""
        try:
            # This would integrate with building management systems
            # For now, return a mock target
            return 80.0  # kW
            
        except Exception as e:
            self.logger.error(f"Error getting target site load: {e}")
            return None
    
    async def _calculate_load_balancing_response(self, load_imbalance: float) -> float:
        """Calculate V2G response for load balancing."""
        try:
            # Load balancing parameters
            max_v2g_power = 50.0  # kW
            response_factor = 0.8  # 80% of imbalance
            
            # Calculate V2G response
            if load_imbalance > 0:
                # Site load too high - discharge to reduce load
                v2g_response = -min(load_imbalance * response_factor, max_v2g_power)
            else:
                # Site load too low - charge to increase load
                v2g_response = min(abs(load_imbalance) * response_factor, max_v2g_power)
            
            return v2g_response
            
        except Exception as e:
            self.logger.error(f"Error calculating load balancing response: {e}")
            return 0.0
    
    async def _apply_load_balancing_response(self, v2g_response: float) -> None:
        """Apply load balancing response to V2G-capable stations."""
        try:
            # Get all V2G-capable stations
            v2g_stations = await self._get_v2g_capable_stations()
            
            for station_id in v2g_stations:
                setpoint = V2XSetpoint(
                    station_id=station_id,
                    evse_id=1,
                    power_kw=v2g_response,
                    mode=V2XOperationMode.LOCAL_LOAD_BALANCING,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=300  # 5-minute response
                )
                
                await self.v2x_controller.set_power_setpoint(setpoint)
            
        except Exception as e:
            self.logger.error(f"Error applying load balancing response: {e}")
    
    async def _get_v2g_capable_stations(self) -> List[str]:
        """Get list of V2G-capable stations."""
        try:
            # This would query the database for V2G-capable stations
            # For now, return empty list
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting V2G-capable stations: {e}")
            return []


class ExternalSetpointController:
    """External setpoint controller for third-party EMS integration."""
    
    def __init__(self, v2x_controller: V2XController):
        self.v2x_controller = v2x_controller
        self.logger = get_logger(f"{__name__}.external")
    
    async def run(self) -> None:
        """Run external setpoint controller."""
        while self.v2x_controller.running:
            try:
                # Listen for external setpoint commands
                await self._process_external_commands()
                
                interval = self.v2x_controller.v2x_config.update_interval[V2XOperationMode.EXTERNAL_SETPOINT.value]
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"External setpoint controller error: {e}")
                await asyncio.sleep(5)
    
    async def _process_external_commands(self) -> None:
        """Process external setpoint commands."""
        try:
            # Get external setpoint commands from API or message queue
            external_commands = await self._get_external_commands()
            
            for command in external_commands:
                await self._process_external_command(command)
            
        except Exception as e:
            self.logger.error(f"Error processing external commands: {e}")
    
    async def _get_external_commands(self) -> List[Dict[str, Any]]:
        """Get external setpoint commands."""
        try:
            # This would integrate with external EMS systems via API or message queue
            # For now, return empty list
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting external commands: {e}")
            return []
    
    async def _process_external_command(self, command: Dict[str, Any]) -> None:
        """Process individual external command."""
        try:
            # Validate command
            if not self._validate_external_command(command):
                return
            
            # Create setpoint from command
            setpoint = V2XSetpoint(
                station_id=command["station_id"],
                evse_id=command.get("evse_id", 1),
                power_kw=command["power_kw"],
                mode=V2XOperationMode.EXTERNAL_SETPOINT,
                timestamp=datetime.now(timezone.utc).isoformat(),
                duration_seconds=command.get("duration_seconds", 3600),
                ramp_rate_kw_per_s=command.get("ramp_rate_kw_per_s"),
                constraints=command.get("constraints")
            )
            
            # Apply setpoint
            success = await self.v2x_controller.set_power_setpoint(setpoint)
            
            if success:
                self.logger.info(f"Applied external setpoint: {command['station_id']} = {command['power_kw']}kW")
            else:
                self.logger.warning(f"Failed to apply external setpoint: {command['station_id']}")
            
        except Exception as e:
            self.logger.error(f"Error processing external command: {e}")
    
    def _validate_external_command(self, command: Dict[str, Any]) -> bool:
        """Validate external command."""
        try:
            # Check required fields
            required_fields = ["station_id", "power_kw"]
            for field in required_fields:
                if field not in command:
                    self.logger.warning(f"External command missing required field: {field}")
                    return False
            
            # Check power limits
            power_kw = command["power_kw"]
            max_charge = self.v2x_controller.v2x_config.power_limits["max_charge_power"]
            max_discharge = self.v2x_controller.v2x_config.power_limits["max_discharge_power"]
            
            if power_kw > max_charge or power_kw < -max_discharge:
                self.logger.warning(f"External command power out of range: {power_kw}kW")
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating external command: {e}")
            return False
