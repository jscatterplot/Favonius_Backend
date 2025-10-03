"""Configuration management for OCPP WebSocket handler."""

import os
from typing import List, Optional
from pydantic import BaseModel, Field


# Redis configuration removed for simplification


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
    max_connections: int = Field(default=100, description="Maximum concurrent connections (simplified)")
    heartbeat_interval: int = Field(default=30, description="Heartbeat interval in seconds")
    message_timeout: int = Field(default=60, description="Message timeout in seconds")
    max_message_size: int = Field(default=65536, description="Maximum message size in bytes")
    rate_limit_per_minute: int = Field(default=100, description="Rate limit per connection per minute")


class TimescaleConfig(BaseModel):
    """TimescaleDB configuration."""
    service_url: str = Field(description="TimescaleDB service URL")
    host: str = Field(description="Database host")
    port: int = Field(default=5432, description="Database port")
    database: str = Field(default="tsdb", description="Database name")
    user: str = Field(description="Database user")
    password: str = Field(description="Database password")
    sslmode: str = Field(default="require", description="SSL mode")
    max_connections: int = Field(default=100, description="Maximum database connections")
    pool_size: int = Field(default=20, description="Connection pool size")
    statement_timeout: int = Field(default=30, description="Statement timeout in seconds")
    idle_timeout: int = Field(default=600, description="Idle timeout in seconds")
    chunk_time_interval: str = Field(default="1 day", description="Hypertable chunk interval")
    compression_after: str = Field(default="7 days", description="Compression policy interval")
    retention_period: str = Field(default="2 years", description="Data retention period")


class SupabaseConfig(BaseModel):
    """Supabase configuration."""
    url: str = Field(description="Supabase project URL")
    anon_key: str = Field(description="Supabase anonymous key")
    service_key: str = Field(description="Supabase service role key")
    db_host: str = Field(description="Database host")
    db_port: int = Field(default=5432, description="Database port")
    db_name: str = Field(default="postgres", description="Database name")
    db_user: str = Field(description="Database user")
    db_password: str = Field(description="Database password")
    max_connections: int = Field(default=20, description="Maximum database connections")
    connection_timeout: int = Field(default=30, description="Connection timeout in seconds")
    enable_realtime: bool = Field(default=True, description="Enable real-time subscriptions")


class MonitoringConfig(BaseModel):
    """Monitoring and observability configuration."""
    metrics_port: int = Field(default=8080, description="Prometheus metrics port")
    log_level: str = Field(default="INFO", description="Log level")
    enable_telemetry: bool = Field(default=True, description="Enable telemetry collection")
    health_check_port: int = Field(default=8081, description="Health check port")


class Config(BaseModel):
    """Main application configuration."""
    # Redis removed for simplification
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    tls: TLSConfig = Field(default_factory=TLSConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    timescale: TimescaleConfig = Field(default_factory=TimescaleConfig)
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    
    # Environment-specific settings
    environment: str = Field(default="development", description="Environment (development/staging/production)")
    debug: bool = Field(default=False, description="Debug mode")
    
    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        return cls(
            # Redis configuration removed for simplification
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
                max_connections=int(os.getenv("MAX_CONNECTIONS", "100")),
                heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "30")),
                message_timeout=int(os.getenv("MESSAGE_TIMEOUT", "60")),
                max_message_size=int(os.getenv("MAX_MESSAGE_SIZE", "65536")),
                rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "100")),
            ),
            timescale=TimescaleConfig(
                service_url=os.getenv("TIMESCALE_SERVICE_URL", "postgres://tsdbadmin:lyqgv8a0j1bt1zaa@avws3fxn3w.rspy6d4hg0.tsdb.cloud.timescale.com:32634/tsdb?sslmode=require"),
                host=os.getenv("PGHOST", "avws3fxn3w.rspy6d4hg0.tsdb.cloud.timescale.com"),
                port=int(os.getenv("PGPORT", "32634")),
                database=os.getenv("PGDATABASE", "tsdb"),
                user=os.getenv("PGUSER", "tsdbadmin"),
                password=os.getenv("PGPASSWORD", "lyqgv8a0j1bt1zaa"),
                sslmode=os.getenv("PGSSLMODE", "require"),
                max_connections=int(os.getenv("TIMESCALE_MAX_CONNECTIONS", "100")),
                pool_size=int(os.getenv("TIMESCALE_POOL_SIZE", "20")),
                statement_timeout=int(os.getenv("TIMESCALE_STATEMENT_TIMEOUT", "30")),
                idle_timeout=int(os.getenv("TIMESCALE_IDLE_TIMEOUT", "600")),
                chunk_time_interval=os.getenv("TIMESCALE_CHUNK_INTERVAL", "1 day"),
                compression_after=os.getenv("TIMESCALE_COMPRESSION_AFTER", "7 days"),
                retention_period=os.getenv("TIMESCALE_RETENTION_PERIOD", "2 years"),
            ),
            supabase=SupabaseConfig(
                url=os.getenv("SUPABASE_URL", ""),
                anon_key=os.getenv("SUPABASE_ANON_KEY", ""),
                service_key=os.getenv("SUPABASE_SERVICE_KEY", ""),
                db_host=os.getenv("SUPABASE_DB_HOST", "aws-1-us-east-2.pooler.supabase.com"),
                db_port=int(os.getenv("SUPABASE_DB_PORT", "6543")),
                db_name=os.getenv("SUPABASE_DB_NAME", "postgres"),
                db_user=os.getenv("SUPABASE_DB_USER", "postgres.evdehwjbbgdgiwdvjqfk"),
                db_password=os.getenv("SUPABASE_DB_PASSWORD", "1NDLK5slwkI8Ka7b"),
                max_connections=int(os.getenv("SUPABASE_MAX_CONNECTIONS", "20")),
                connection_timeout=int(os.getenv("SUPABASE_CONNECTION_TIMEOUT", "30")),
                enable_realtime=os.getenv("SUPABASE_ENABLE_REALTIME", "true").lower() == "true",
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
