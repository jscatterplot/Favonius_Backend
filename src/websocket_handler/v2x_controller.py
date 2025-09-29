"""V2X (Vehicle-to-Everything) controller for bidirectional charging modes."""

import asyncio
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from .config import Config
from .redis_enhanced import EnhancedRedisClient
from .kafka_producer import KafkaProducer
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
        redis_client: EnhancedRedisClient,
        kafka_producer: KafkaProducer,
        config: V2XControllerConfig,
        app_config: Config
    ):
        """Initialize V2X controller."""
        self.redis_client = redis_client
        self.kafka_producer = kafka_producer
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
                # Store in Redis
                await self._store_setpoint_in_redis(setpoint)
                
                # Publish event
                await self.kafka_producer.send_event("v2x.setpoints", {
                    "event_type": "setpoint_applied",
                    "station_id": setpoint.station_id,
                    "evse_id": setpoint.evse_id,
                    "power_kw": setpoint.power_kw,
                    "mode": setpoint.mode.value,
                    "timestamp": setpoint.timestamp
                })
                
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
                    
                    # Clear from Redis
                    await self.redis_client.client.delete(f"v2x:setpoint:{station_id}")
                
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
    
    async def _store_setpoint_in_redis(self, setpoint: V2XSetpoint) -> None:
        """Store setpoint in Redis."""
        setpoint_data = {
            "station_id": setpoint.station_id,
            "evse_id": setpoint.evse_id,
            "power_kw": setpoint.power_kw,
            "mode": setpoint.mode.value,
            "timestamp": setpoint.timestamp,
            "duration_seconds": setpoint.duration_seconds,
            "ramp_rate_kw_per_s": setpoint.ramp_rate_kw_per_s,
            "constraints": json.dumps(setpoint.constraints) if setpoint.constraints else None
        }
        
        await self.redis_client.client.hmset(
            f"v2x:setpoint:{setpoint.station_id}", 
            setpoint_data
        )
        await self.redis_client.client.expire(
            f"v2x:setpoint:{setpoint.station_id}", 
            7200  # 2 hours
        )
    
    async def _store_pending_profile(self, station_id: str, evse_id: int, profile: Dict) -> None:
        """Store pending charging profile to be sent."""
        await self.redis_client.client.hmset(
            f"v2x:pending_profile:{station_id}:{evse_id}",
            {
                "profile": json.dumps(profile),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending"
            }
        )
        await self.redis_client.client.expire(
            f"v2x:pending_profile:{station_id}:{evse_id}",
            300  # 5 minutes
        )
    
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
        # Get frequency measurements from Redis
        # This would come from grid monitoring systems
        pass


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
        # Get site load data and generate balancing setpoints
        pass


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
        # Interface with external EMS systems
        pass
