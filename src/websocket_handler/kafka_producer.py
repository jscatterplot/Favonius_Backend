"""Kafka producer for event streaming."""

import asyncio
import json
import time
from typing import Any, Dict, Optional
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from .config import KafkaConfig
from .monitoring import get_logger


class KafkaProducer:
    """Async Kafka producer for streaming charger events."""
    
    def __init__(self, config: KafkaConfig):
        """Initialize Kafka producer."""
        self.config = config
        self.logger = get_logger(__name__)
        
        self.producer: Optional[AIOKafkaProducer] = None
        self._health_status = {"status": "disconnected", "last_error": None}
        
    async def start(self) -> None:
        """Start Kafka producer."""
        try:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=self.config.brokers,
                value_serializer=lambda v: json.dumps(v, separators=(',', ':')).encode('utf-8'),
                key_serializer=lambda k: k.encode('utf-8') if k else None,
                compression_type='snappy',
                batch_size=16384,  # 16KB batches
                linger_ms=10,      # Wait up to 10ms for batching
                request_timeout_ms=30000,
                retry_backoff_ms=100,
                max_request_size=1048576,  # 1MB max request
                security_protocol='PLAINTEXT',  # Configure for your environment
            )
            
            await self.producer.start()
            self._health_status = {"status": "connected", "last_error": None}
            
            self.logger.info(f"Kafka producer started, connected to {self.config.brokers}")
            
        except Exception as e:
            self._health_status = {"status": "error", "last_error": str(e)}
            self.logger.error(f"Failed to start Kafka producer: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop Kafka producer."""
        if self.producer:
            try:
                await self.producer.stop()
                self._health_status = {"status": "disconnected", "last_error": None}
                self.logger.info("Kafka producer stopped")
            except Exception as e:
                self.logger.error(f"Error stopping Kafka producer: {e}")
    
    async def send_event(self, topic: str, event: Dict[str, Any], 
                        key: Optional[str] = None) -> bool:
        """Send event to Kafka topic."""
        if not self.producer:
            self.logger.error("Kafka producer not initialized")
            return False
        
        try:
            # Add metadata to event
            enriched_event = {
                **event,
                "producer_timestamp": time.time(),
                "producer_id": "websocket-handler"
            }
            
            # Use station_id as key for partitioning if not provided
            if not key and "station_id" in event:
                key = event["station_id"]
            
            # Send event
            future = await self.producer.send(topic, enriched_event, key=key)
            
            # Wait for acknowledgment (optional, for reliability)
            record_metadata = await future
            
            self.logger.debug(
                f"Sent event to {topic} partition {record_metadata.partition} "
                f"offset {record_metadata.offset}"
            )
            
            return True
            
        except KafkaError as e:
            self.logger.error(f"Kafka error sending event to {topic}: {e}")
            self._health_status["last_error"] = str(e)
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error sending event to {topic}: {e}")
            self._health_status["last_error"] = str(e)
            return False
    
    async def send_charger_event(self, event: Dict[str, Any]) -> bool:
        """Send charger-specific event."""
        return await self.send_event(self.config.charger_events_topic, event)
    
    async def send_telemetry_event(self, telemetry: Dict[str, Any]) -> bool:
        """Send telemetry data event."""
        return await self.send_event("charger.telemetry", telemetry)
    
    async def send_system_event(self, event: Dict[str, Any]) -> bool:
        """Send system event."""
        return await self.send_event("system.events", event)
    
    async def send_optimization_input(self, data: Dict[str, Any]) -> bool:
        """Send data to optimization engine."""
        return await self.send_event("optimization.inputs", data)
    
    async def send_batch_events(self, events: list) -> int:
        """Send multiple events in batch."""
        if not self.producer or not events:
            return 0
        
        sent_count = 0
        
        try:
            # Group events by topic
            topic_events = {}
            for event in events:
                topic = event.get("_topic", self.config.charger_events_topic)
                if topic not in topic_events:
                    topic_events[topic] = []
                topic_events[topic].append(event)
            
            # Send all events
            send_tasks = []
            for topic, topic_event_list in topic_events.items():
                for event in topic_event_list:
                    # Remove internal topic field
                    event.pop("_topic", None)
                    task = self.send_event(topic, event)
                    send_tasks.append(task)
            
            # Wait for all sends
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            sent_count = sum(1 for result in results if result is True)
            
            self.logger.debug(f"Sent {sent_count}/{len(events)} batch events")
            
        except Exception as e:
            self.logger.error(f"Error sending batch events: {e}")
        
        return sent_count
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get producer health status."""
        return self._health_status.copy()
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform health check by sending test message."""
        try:
            if not self.producer:
                return {"status": "unhealthy", "error": "Producer not initialized"}
            
            # Send test event
            test_event = {
                "event_type": "health_check",
                "timestamp": time.time()
            }
            
            success = await self.send_event("system.health", test_event)
            
            return {
                "status": "healthy" if success else "unhealthy",
                "connected": True,
                "last_error": self._health_status.get("last_error")
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "connected": False
            }


class KafkaConsumer:
    """Kafka consumer for optimization commands (optional component)."""
    
    def __init__(self, config: KafkaConfig, message_handler):
        """Initialize Kafka consumer."""
        self.config = config
        self.message_handler = message_handler
        self.logger = get_logger(f"{__name__}.consumer")
        
        self.consumer = None
        self.running = False
        
    async def start(self) -> None:
        """Start Kafka consumer."""
        try:
            from aiokafka import AIOKafkaConsumer
            
            self.consumer = AIOKafkaConsumer(
                self.config.optimization_commands_topic,
                bootstrap_servers=self.config.brokers,
                group_id=self.config.consumer_group,
                value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                auto_offset_reset='latest',
                enable_auto_commit=True,
                auto_commit_interval_ms=1000,
                session_timeout_ms=30000,
                heartbeat_interval_ms=3000,
            )
            
            await self.consumer.start()
            self.running = True
            
            self.logger.info(f"Kafka consumer started for topic {self.config.optimization_commands_topic}")
            
            # Start message processing
            asyncio.create_task(self._process_messages())
            
        except Exception as e:
            self.logger.error(f"Failed to start Kafka consumer: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop Kafka consumer."""
        self.running = False
        
        if self.consumer:
            try:
                await self.consumer.stop()
                self.logger.info("Kafka consumer stopped")
            except Exception as e:
                self.logger.error(f"Error stopping Kafka consumer: {e}")
    
    async def _process_messages(self) -> None:
        """Process incoming optimization commands."""
        while self.running and self.consumer:
            try:
                # Get next message batch
                message_batch = await self.consumer.getmany(timeout_ms=1000, max_records=10)
                
                for topic_partition, messages in message_batch.items():
                    for message in messages:
                        try:
                            await self._handle_optimization_command(message.value)
                        except Exception as e:
                            self.logger.error(f"Error processing optimization command: {e}")
                
            except Exception as e:
                self.logger.error(f"Error in message processing loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying
    
    async def _handle_optimization_command(self, command: Dict[str, Any]) -> None:
        """Handle optimization command from Kafka."""
        command_type = command.get("command_type")
        station_id = command.get("station_id")
        
        if command_type == "set_charging_profile" and station_id:
            evse_id = command.get("evse_id", 1)
            charging_profile = command.get("charging_profile")
            
            if charging_profile:
                success = await self.message_handler.send_charging_profile(
                    station_id, evse_id, charging_profile
                )
                self.logger.info(f"Applied charging profile to {station_id}: {success}")
        
        elif command_type == "request_schedule" and station_id:
            evse_id = command.get("evse_id", 1)
            duration = command.get("duration", 3600)  # 1 hour default
            
            # Request composite schedule from charger
            # This would be handled by connection manager
            self.logger.info(f"Requesting schedule from {station_id}, EVSE {evse_id}")
        
        else:
            self.logger.warning(f"Unknown optimization command: {command_type}")