"""Telemetry data ingestion service for TimescaleDB."""

import asyncio
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

from .config import TimescaleConfig
from .timescale_client import TimescaleClient
from .monitoring import get_logger


class TelemetryIngestionService:
    """Service for ingesting telemetry data directly to TimescaleDB."""
    
    def __init__(self, timescale_config: TimescaleConfig):
        """Initialize telemetry ingestion service."""
        self.timescale_config = timescale_config
        self.logger = get_logger(__name__)
        
        # Components
        self.timescale_client: Optional[TimescaleClient] = None
        
        # Configuration
        self.batch_size = 1000
        self.batch_timeout = 5  # seconds
        self.max_retries = 3
        self.retry_delay = 1  # seconds
        
        # State
        self.running = False
        self.batch_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.batch_size))
        self.last_batch_time: Dict[str, datetime] = {}
        
        # Metrics
        self.messages_processed = 0
        self.batches_inserted = 0
        self.errors_count = 0
        
    async def start(self) -> None:
        """Start the telemetry ingestion service."""
        try:
            # Initialize TimescaleDB client
            self.timescale_client = TimescaleClient(self.timescale_config)
            await self.timescale_client.connect()
            
            self.running = True
            
            # Start background tasks
            asyncio.create_task(self._batch_processor())
            
            self.logger.info("Telemetry ingestion service started")
            
        except Exception as e:
            self.logger.error(f"Failed to start telemetry ingestion service: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop the telemetry ingestion service."""
        self.running = False
        
        # Process remaining batches
        await self._flush_all_batches()
        
        # Close connections
        if self.timescale_client:
            await self.timescale_client.disconnect()
        
        self.logger.info("Telemetry ingestion service stopped")
    
    async def ingest_telemetry_data(self, data: Dict[str, Any]) -> None:
        """Ingest telemetry data directly."""
        try:
            await self._handle_telemetry_message(data)
            self.messages_processed += 1
            
        except Exception as e:
            self.logger.error(f"Error processing telemetry data: {e}")
            self.errors_count += 1
            raise
    
    async def ingest_session_data(self, data: Dict[str, Any]) -> None:
        """Ingest session data directly."""
        try:
            message_type = data.get('type', 'unknown')
            
            if message_type == 'session_start':
                await self._handle_session_start(data)
            elif message_type == 'session_end':
                await self._handle_session_end(data)
            else:
                self.logger.warning(f"Unknown session message type: {message_type}")
                
        except Exception as e:
            self.logger.error(f"Error handling session data: {e}")
            raise
    
    async def ingest_optimization_data(self, data: Dict[str, Any]) -> None:
        """Ingest optimization data directly."""
        try:
            message_type = data.get('type', 'unknown')
            
            if message_type == 'optimization_decision':
                await self._handle_optimization_decision(data)
            elif message_type == 'charging_schedule':
                await self._handle_charging_schedule(data)
            else:
                self.logger.warning(f"Unknown optimization message type: {message_type}")
                
        except Exception as e:
            self.logger.error(f"Error handling optimization data: {e}")
            raise
    
    async def ingest_price_data(self, data: Dict[str, Any]) -> None:
        """Ingest electricity price data directly."""
        try:
            await self._handle_electricity_price(data)
            
        except Exception as e:
            self.logger.error(f"Error handling price data: {e}")
            raise
    
    async def _handle_telemetry_message(self, data: Dict[str, Any]) -> None:
        """Handle telemetry data message."""
        try:
            # Extract telemetry data
            telemetry_data = {
                'time': datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')),
                'station_id': data['station_id'],
                'evse_id': data['evse_id'],
                'connector_id': data['connector_id'],
                'session_id': data.get('session_id'),
                'power_kw': data.get('power_kw'),
                'energy_kwh': data.get('energy_kwh'),
                'voltage_v': data.get('voltage_v'),
                'current_a': data.get('current_a'),
                'frequency_hz': data.get('frequency_hz'),
                'soc_percent': data.get('soc_percent'),
                'temperature_c': data.get('temperature_c'),
                'grid_frequency_mhz': data.get('grid_frequency_mhz'),
                'reactive_power_kvar': data.get('reactive_power_kvar'),
                'power_factor': data.get('power_factor')
            }
            
            # Add to batch buffer
            station_id = telemetry_data['station_id']
            self.batch_buffer[station_id].append(telemetry_data)
            
            # Update last batch time
            self.last_batch_time[station_id] = datetime.now(timezone.utc)
            
        except Exception as e:
            self.logger.error(f"Error handling telemetry message: {e}")
            raise
    
    async def _handle_session_start(self, data: Dict[str, Any]) -> None:
        """Handle charging session start message."""
        try:
            session_data = {
                'station_id': data['station_id'],
                'evse_id': data['evse_id'],
                'connector_id': data['connector_id'],
                'vehicle_id': data.get('vehicle_id'),
                'id_token': data.get('id_token'),
                'start_time': datetime.fromisoformat(data['start_time'].replace('Z', '+00:00')),
                'start_soc_percent': data.get('start_soc_percent'),
                'operation_mode': data.get('operation_mode'),
                'fleet_operator_id': data.get('fleet_operator_id'),
                'site_id': data.get('site_id')
            }
            
            session_id = await self.timescale_client.create_charging_session(session_data)
            self.logger.info(f"Created charging session: {session_id}")
            
        except Exception as e:
            self.logger.error(f"Error handling session start: {e}")
            raise
    
    async def _handle_session_end(self, data: Dict[str, Any]) -> None:
        """Handle charging session end message."""
        try:
            session_id = data['session_id']
            updates = {
                'end_time': datetime.fromisoformat(data['end_time'].replace('Z', '+00:00')),
                'end_soc_percent': data.get('end_soc_percent'),
                'energy_delivered_kwh': data.get('energy_delivered_kwh'),
                'energy_received_kwh': data.get('energy_received_kwh'),
                'max_charge_power_kw': data.get('max_charge_power_kw'),
                'max_discharge_power_kw': data.get('max_discharge_power_kw')
            }
            
            await self.timescale_client.update_charging_session(session_id, updates)
            self.logger.info(f"Updated charging session: {session_id}")
            
        except Exception as e:
            self.logger.error(f"Error handling session end: {e}")
            raise
    
    async def _handle_optimization_decision(self, data: Dict[str, Any]) -> None:
        """Handle optimization decision message."""
        try:
            decision_data = {
                'time': datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')),
                'optimization_window_start': datetime.fromisoformat(data['window_start'].replace('Z', '+00:00')),
                'optimization_window_end': datetime.fromisoformat(data['window_end'].replace('Z', '+00:00')),
                'fleet_operator_id': data.get('fleet_operator_id'),
                'site_id': data.get('site_id'),
                'algorithm_version': data.get('algorithm_version'),
                'objective_function': data.get('objective_function'),
                'objective_value': data.get('objective_value'),
                'computation_time_ms': data.get('computation_time_ms'),
                'constraints_satisfied': data.get('constraints_satisfied'),
                'decision_payload': data.get('decision_payload', {})
            }
            
            decision_id = await self.timescale_client.store_optimization_decision(decision_data)
            self.logger.info(f"Stored optimization decision: {decision_id}")
            
        except Exception as e:
            self.logger.error(f"Error handling optimization decision: {e}")
            raise
    
    async def _handle_charging_schedule(self, data: Dict[str, Any]) -> None:
        """Handle charging schedule message."""
        try:
            schedule_data = {
                'station_id': data['station_id'],
                'evse_id': data['evse_id'],
                'decision_id': data.get('decision_id'),
                'profile_id': data['profile_id'],
                'start_time': datetime.fromisoformat(data['start_time'].replace('Z', '+00:00')),
                'end_time': datetime.fromisoformat(data['end_time'].replace('Z', '+00:00')),
                'schedule_periods': data['schedule_periods'],
                'priority': data.get('priority', 0),
                'stacking_level': data.get('stacking_level', 0),
                'purpose': data.get('purpose')
            }
            
            schedule_id = await self.timescale_client.store_charging_schedule(schedule_data)
            self.logger.info(f"Stored charging schedule: {schedule_id}")
            
        except Exception as e:
            self.logger.error(f"Error handling charging schedule: {e}")
            raise
    
    async def _handle_electricity_price(self, data: Dict[str, Any]) -> None:
        """Handle electricity price message."""
        try:
            price_data = [{
                'time': datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')),
                'node_id': data['node_id'],
                'market_type': data['market_type'],
                'lmp_price_mwh': data.get('lmp_price_mwh'),
                'energy_component_mwh': data.get('energy_component_mwh'),
                'congestion_component_mwh': data.get('congestion_component_mwh'),
                'loss_component_mwh': data.get('loss_component_mwh'),
                'ghg_adder_mwh': data.get('ghg_adder_mwh'),
                'price_confidence': data.get('price_confidence'),
                'forecast_horizon_minutes': data.get('forecast_horizon_minutes')
            }]
            
            await self.timescale_client.store_electricity_prices(price_data)
            self.logger.debug(f"Stored electricity price for node {data['node_id']}")
            
        except Exception as e:
            self.logger.error(f"Error handling electricity price: {e}")
            raise
    
    async def _handle_grid_signal(self, data: Dict[str, Any]) -> None:
        """Handle grid signal message."""
        try:
            # This would be implemented to store grid signals
            # For now, just log the signal
            self.logger.info(f"Received grid signal: {data['signal_type']} = {data['signal_value']}")
            
        except Exception as e:
            self.logger.error(f"Error handling grid signal: {e}")
            raise
    
    async def _batch_processor(self) -> None:
        """Process batches of telemetry data."""
        while self.running:
            try:
                await self._process_batches()
                await asyncio.sleep(1)  # Check every second
                
            except Exception as e:
                self.logger.error(f"Error in batch processor: {e}")
                await asyncio.sleep(5)  # Wait before retry
    
    async def _process_batches(self) -> None:
        """Process all ready batches."""
        current_time = datetime.now(timezone.utc)
        
        for station_id in list(self.batch_buffer.keys()):
            buffer = self.batch_buffer[station_id]
            last_batch_time = self.last_batch_time.get(station_id, current_time)
            
            # Check if batch is ready (size or timeout)
            time_since_last = (current_time - last_batch_time).total_seconds()
            
            if len(buffer) >= self.batch_size or time_since_last >= self.batch_timeout:
                await self._process_station_batch(station_id)
    
    async def _process_station_batch(self, station_id: str) -> None:
        """Process batch for a specific station."""
        try:
            buffer = self.batch_buffer[station_id]
            if not buffer:
                return
            
            # Convert deque to list
            batch_data = list(buffer)
            buffer.clear()
            
            # Insert batch into TimescaleDB
            await self.timescale_client.insert_telemetry_batch(batch_data)
            
            self.batches_inserted += 1
            self.logger.debug(f"Processed batch for station {station_id}: {len(batch_data)} records")
            
        except Exception as e:
            self.logger.error(f"Error processing batch for station {station_id}: {e}")
            # Re-add data to buffer for retry
            if batch_data:
                self.batch_buffer[station_id].extendleft(reversed(batch_data))
    
    async def _flush_all_batches(self) -> None:
        """Flush all remaining batches."""
        for station_id in list(self.batch_buffer.keys()):
            await self._process_station_batch(station_id)
    
    async def get_metrics(self) -> Dict[str, Any]:
        """Get service metrics."""
        return {
            'running': self.running,
            'messages_processed': self.messages_processed,
            'batches_inserted': self.batches_inserted,
            'errors_count': self.errors_count,
            'buffer_sizes': {station_id: len(buffer) for station_id, buffer in self.batch_buffer.items()},
            'last_batch_times': {station_id: time.isoformat() for station_id, time in self.last_batch_time.items()}
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check service health."""
        try:
            if not self.running:
                return {"status": "stopped"}
            
            # Check TimescaleDB connection
            timescale_health = await self.timescale_client.health_check()
            
            return {
                "status": "healthy" if timescale_health["status"] == "healthy" else "degraded",
                "timescale": timescale_health,
                "metrics": await self.get_metrics()
            }
            
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
