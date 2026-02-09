"""Configuration management for OCPP WebSocket handler."""

import os
from typing import Optional, List
from pydantic import BaseModel, Field, validator
from .secrets_manager import SecretsManager, SecretsConfig


# Redis and Kafka configuration removed for simplification


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
    max_message_size: int = Field(default=1048576, description="Maximum message size in bytes (PRD: 1 MB)")
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
    expected_stations: int = Field(default=100, description="Expected number of charging stations")
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")


class PriceFeederConfig(BaseModel):
    """Price feeder configuration."""
    enabled: bool = Field(default=True, description="Enable CAISO price feeder")
    base_url: str = Field(
        default="https://oasis.caiso.com/oasisapi/SingleZip",
        description="CAISO OASIS API base URL"
    )
    nodes: List[str] = Field(
        default_factory=lambda: ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
        description="Pricing nodes to monitor"
    )
    fetch_interval_seconds: int = Field(default=900, description="Price refresh interval in seconds")
    lookahead_hours: int = Field(default=24, description="Forecast horizon in hours")


class OptimizationServiceConfig(BaseModel):
    """Optimization engine configuration."""
    enabled: bool = Field(default=True, description="Enable optimization service")
    horizon_hours: int = Field(default=4, description="Optimization horizon in hours")
    timestep_minutes: int = Field(default=60, description="Optimization timestep in minutes")
    soc_minimum: float = Field(default=0.2, description="Minimum allowed state of charge (fraction)")
    soc_target: float = Field(default=0.8, description="Target state of charge before departure")
    charge_power_kw: float = Field(default=22.0, description="Default charge power limit in kW")
    discharge_power_kw: float = Field(default=10.0, description="Default discharge power limit in kW")
    battery_capacity_kwh: float = Field(default=75.0, description="Default battery capacity in kWh")


class VDV463Config(BaseModel):
    """VDV 463 transit operations configuration."""
    enabled: bool = Field(default=True, description="Enable VDV 463 support")
    validation_mode: str = Field(
        default="soft",
        description="Validation mode: 'soft' (warn only, default) or 'hard' (reject invalid)"
    )
    schema_dir: Optional[str] = Field(
        default=None,
        description="Directory containing VDV 463 JSON schemas"
    )
    default_depot_id: Optional[str] = Field(
        default=None,
        description="Default depot ID for VDV 463 connections"
    )


class Config(BaseModel):
    """Main application configuration."""
    tls: TLSConfig = Field(default_factory=TLSConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    timescale: TimescaleConfig = Field(default_factory=TimescaleConfig)
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    price_feeder: PriceFeederConfig = Field(default_factory=PriceFeederConfig)
    optimization: OptimizationServiceConfig = Field(default_factory=OptimizationServiceConfig)
    vdv463: VDV463Config = Field(default_factory=VDV463Config)
    
    # Environment-specific settings
    environment: str = Field(default="development", description="Environment (development/staging/production)")
    debug: bool = Field(default=False, description="Debug mode")
    
    # Secrets management
    secrets_manager: Optional[SecretsManager] = None
    
    @validator('timescale', 'supabase', pre=True, always=True)
    def validate_credentials(cls, v, values):
        """Validate that required credentials are present."""
        if hasattr(v, 'password') and not v.password:
            raise ValueError("Database password is required")
        if hasattr(v, 'service_key') and not v.service_key:
            raise ValueError("Supabase service key is required")
        return v
    
    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        # Initialize secrets manager
        secrets_config = SecretsConfig(
            secrets_file=os.getenv("SECRETS_FILE"),
            encryption_key=os.getenv("SECRETS_ENCRYPTION_KEY"),
            use_kubernetes_secrets=os.getenv("USE_KUBERNETES_SECRETS", "true").lower() == "true",
            fallback_to_env=os.getenv("FALLBACK_TO_ENV", "true").lower() == "true"
        )
        secrets_manager = SecretsManager(secrets_config)
        
        return cls(
            tls=TLSConfig(
                cert_path=os.getenv("TLS_CERT_PATH"),
                key_path=os.getenv("TLS_KEY_PATH"),
                ca_path=os.getenv("TLS_CA_PATH"),
                verify_client=os.getenv("TLS_VERIFY_CLIENT", "false").lower() == "true",
            ),
            websocket=WebSocketConfig(
                port=int(os.getenv("WEBSOCKET_PORT") or os.getenv("PORT", "9000")),
                host=os.getenv("WEBSOCKET_HOST", "0.0.0.0"),
                max_connections=int(os.getenv("MAX_CONNECTIONS", "100")),
                heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "30")),
                message_timeout=int(os.getenv("MESSAGE_TIMEOUT", "60")),
                max_message_size=int(os.getenv("MAX_MESSAGE_SIZE", "1048576")),
                rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "100")),
            ),
            timescale=TimescaleConfig(
                service_url=secrets_manager.get_secret("TIMESCALE_SERVICE_URL") or os.getenv("TIMESCALE_SERVICE_URL"),
                host=secrets_manager.get_secret("PGHOST") or os.getenv("PGHOST"),
                port=int(secrets_manager.get_secret("PGPORT") or os.getenv("PGPORT", "5432")),
                database=secrets_manager.get_secret("PGDATABASE") or os.getenv("PGDATABASE", "tsdb"),
                user=secrets_manager.get_secret("PGUSER") or os.getenv("PGUSER"),
                password=secrets_manager.get_secret("PGPASSWORD") or os.getenv("PGPASSWORD"),
                sslmode=secrets_manager.get_secret("PGSSLMODE") or os.getenv("PGSSLMODE", "require"),
                max_connections=int(secrets_manager.get_secret("TIMESCALE_MAX_CONNECTIONS") or os.getenv("TIMESCALE_MAX_CONNECTIONS", "100")),
                pool_size=int(secrets_manager.get_secret("TIMESCALE_POOL_SIZE") or os.getenv("TIMESCALE_POOL_SIZE", "20")),
                statement_timeout=int(secrets_manager.get_secret("TIMESCALE_STATEMENT_TIMEOUT") or os.getenv("TIMESCALE_STATEMENT_TIMEOUT", "30")),
                idle_timeout=int(secrets_manager.get_secret("TIMESCALE_IDLE_TIMEOUT") or os.getenv("TIMESCALE_IDLE_TIMEOUT", "600")),
                chunk_time_interval=secrets_manager.get_secret("TIMESCALE_CHUNK_INTERVAL") or os.getenv("TIMESCALE_CHUNK_INTERVAL", "1 day"),
                compression_after=secrets_manager.get_secret("TIMESCALE_COMPRESSION_AFTER") or os.getenv("TIMESCALE_COMPRESSION_AFTER", "7 days"),
                retention_period=secrets_manager.get_secret("TIMESCALE_RETENTION_PERIOD") or os.getenv("TIMESCALE_RETENTION_PERIOD", "2 years"),
            ),
            supabase=SupabaseConfig(
                url=secrets_manager.get_secret("SUPABASE_URL") or os.getenv("SUPABASE_URL"),
                anon_key=secrets_manager.get_secret("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_ANON_KEY"),
                service_key=secrets_manager.get_secret("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY"),
                db_host=secrets_manager.get_secret("SUPABASE_DB_HOST") or os.getenv("SUPABASE_DB_HOST"),
                db_port=int(secrets_manager.get_secret("SUPABASE_DB_PORT") or os.getenv("SUPABASE_DB_PORT", "6543")),
                db_name=secrets_manager.get_secret("SUPABASE_DB_NAME") or os.getenv("SUPABASE_DB_NAME", "postgres"),
                db_user=secrets_manager.get_secret("SUPABASE_DB_USER") or os.getenv("SUPABASE_DB_USER"),
                db_password=secrets_manager.get_secret("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_DB_PASSWORD"),
                max_connections=int(secrets_manager.get_secret("SUPABASE_MAX_CONNECTIONS") or os.getenv("SUPABASE_MAX_CONNECTIONS", "20")),
                connection_timeout=int(secrets_manager.get_secret("SUPABASE_CONNECTION_TIMEOUT") or os.getenv("SUPABASE_CONNECTION_TIMEOUT", "30")),
                enable_realtime=os.getenv("SUPABASE_ENABLE_REALTIME", "true").lower() == "true",
            ),
            monitoring=MonitoringConfig(
                metrics_port=int(os.getenv("METRICS_PORT", "8080")),
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                enable_telemetry=os.getenv("ENABLE_TELEMETRY", "true").lower() == "true",
                health_check_port=int(os.getenv("HEALTH_CHECK_PORT", "8081")),
            ),
            price_feeder=PriceFeederConfig(
                enabled=os.getenv("PRICE_FEEDER_ENABLED", "true").lower() == "true",
                base_url=os.getenv("PRICE_FEEDER_BASE_URL", "https://oasis.caiso.com/oasisapi/SingleZip"),
                nodes=[node.strip() for node in os.getenv("PRICE_FEEDER_NODES", "TH_SP15_GEN-APND,TH_NP15_GEN-APND").split(",") if node.strip()],
                fetch_interval_seconds=int(os.getenv("PRICE_FEEDER_FETCH_INTERVAL", "900")),
                lookahead_hours=int(os.getenv("PRICE_FEEDER_LOOKAHEAD_HOURS", "24")),
            ),
            optimization=OptimizationServiceConfig(
                enabled=os.getenv("OPTIMIZATION_ENABLED", "true").lower() == "true",
                horizon_hours=int(os.getenv("OPTIMIZATION_HORIZON_HOURS", "4")),
                timestep_minutes=int(os.getenv("OPTIMIZATION_TIMESTEP_MINUTES", "60")),
                soc_minimum=float(os.getenv("OPTIMIZATION_SOC_MIN", "0.2")),
                soc_target=float(os.getenv("OPTIMIZATION_SOC_TARGET", "0.8")),
                charge_power_kw=float(os.getenv("OPTIMIZATION_CHARGE_POWER_KW", "22.0")),
                discharge_power_kw=float(os.getenv("OPTIMIZATION_DISCHARGE_POWER_KW", "10.0")),
                battery_capacity_kwh=float(os.getenv("OPTIMIZATION_BATTERY_CAPACITY_KWH", "75.0")),
            ),
            vdv463=VDV463Config(
                enabled=os.getenv("VDV463_ENABLED", "true").lower() == "true",
                validation_mode=os.getenv("VDV463_VALIDATION_MODE", "soft"),
                schema_dir=os.getenv("VDV463_SCHEMA_DIR"),
                default_depot_id=os.getenv("VDV463_DEFAULT_DEPOT_ID"),
            ),
            environment=os.getenv("ENVIRONMENT", "development"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            secrets_manager=secrets_manager,
        )
