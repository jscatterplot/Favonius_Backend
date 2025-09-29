"""Configuration management for OCPP WebSocket handler."""

import os
from typing import List, Optional
from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    """Redis configuration."""
    url: str = Field(default="redis://localhost:6379", description="Redis connection URL")
    password: Optional[str] = Field(default=None, description="Redis password")
    max_connections: int = Field(default=100, description="Maximum Redis connections")
    socket_timeout: int = Field(default=5, description="Socket timeout in seconds")
    retry_on_timeout: bool = Field(default=True, description="Retry on timeout")
    health_check_interval: int = Field(default=30, description="Health check interval")


class KafkaConfig(BaseModel):
    """Kafka configuration."""
    brokers: List[str] = Field(default=["localhost:9092"], description="Kafka brokers")
    charger_events_topic: str = Field(default="charger.events", description="Charger events topic")
    optimization_commands_topic: str = Field(default="optimization.commands", description="Optimization commands topic")
    consumer_group: str = Field(default="websocket-handler", description="Consumer group ID")


class TLSConfig(BaseModel):
    """TLS configuration."""
    cert_path: Optional[str] = Field(default=None, description="TLS certificate path")
    key_path: Optional[str] = Field(default=None, description="TLS private key path")
    ca_path: Optional[str] = Field(default=None, description="CA certificate path")
    verify_client: bool = Field(default=False, description="Verify client certificates")


class WebSocketConfig(BaseModel):
    """WebSocket server configuration."""
    port: int = Field(default=9000, description="WebSocket server port")
    host: str = Field(default="0.0.0.0", description="WebSocket server host")
    max_connections: int = Field(default=10000, description="Maximum concurrent connections")
    heartbeat_interval: int = Field(default=30, description="Heartbeat interval in seconds")
    message_timeout: int = Field(default=60, description="Message timeout in seconds")
    max_message_size: int = Field(default=65536, description="Maximum message size in bytes")
    rate_limit_per_minute: int = Field(default=100, description="Rate limit per connection per minute")


class MonitoringConfig(BaseModel):
    """Monitoring and observability configuration."""
    metrics_port: int = Field(default=8080, description="Prometheus metrics port")
    log_level: str = Field(default="INFO", description="Log level")
    enable_telemetry: bool = Field(default=True, description="Enable telemetry collection")
    health_check_port: int = Field(default=8081, description="Health check port")


class Config(BaseModel):
    """Main application configuration."""
    redis: RedisConfig = Field(default_factory=RedisConfig)
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    tls: TLSConfig = Field(default_factory=TLSConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    
    # Environment-specific settings
    environment: str = Field(default="development", description="Environment (development/staging/production)")
    debug: bool = Field(default=False, description="Debug mode")
    
    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        return cls(
            redis=RedisConfig(
                url=os.getenv("REDIS_URL", "redis://localhost:6379"),
                password=os.getenv("REDIS_PASSWORD"),
                max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "100")),
                socket_timeout=int(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
            ),
            kafka=KafkaConfig(
                brokers=os.getenv("KAFKA_BROKERS", "localhost:9092").split(","),
                charger_events_topic=os.getenv("KAFKA_CHARGER_EVENTS_TOPIC", "charger.events"),
                optimization_commands_topic=os.getenv("KAFKA_OPTIMIZATION_COMMANDS_TOPIC", "optimization.commands"),
                consumer_group=os.getenv("KAFKA_CONSUMER_GROUP", "websocket-handler"),
            ),
            tls=TLSConfig(
                cert_path=os.getenv("TLS_CERT_PATH"),
                key_path=os.getenv("TLS_KEY_PATH"),
                ca_path=os.getenv("TLS_CA_PATH"),
                verify_client=os.getenv("TLS_VERIFY_CLIENT", "false").lower() == "true",
            ),
            websocket=WebSocketConfig(
                port=int(os.getenv("WEBSOCKET_PORT", "9000")),
                host=os.getenv("WEBSOCKET_HOST", "0.0.0.0"),
                max_connections=int(os.getenv("MAX_CONNECTIONS", "10000")),
                heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "30")),
                message_timeout=int(os.getenv("MESSAGE_TIMEOUT", "60")),
                max_message_size=int(os.getenv("MAX_MESSAGE_SIZE", "65536")),
                rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "100")),
            ),
            monitoring=MonitoringConfig(
                metrics_port=int(os.getenv("METRICS_PORT", "8080")),
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                enable_telemetry=os.getenv("ENABLE_TELEMETRY", "true").lower() == "true",
                health_check_port=int(os.getenv("HEALTH_CHECK_PORT", "8081")),
            ),
            environment=os.getenv("ENVIRONMENT", "development"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
        )
