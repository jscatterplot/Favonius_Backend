"""Configuration management for OCPP WebSocket handler."""

import os
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, validator

from src.db.postgres_url import (
    build_postgres_dsn,
    is_postgres_url,
    merge_timescale_params_from_url,
)

from .secrets_manager import SecretsConfig, SecretsManager


def _parse_int_env(primary_name: str, fallback_name: Optional[str] = None, default: int = 0) -> int:
    """Parse integer env vars safely, tolerating unresolved templates like '$PORT'."""
    candidates = [primary_name]
    if fallback_name:
        candidates.append(fallback_name)

    for name in candidates:
        value = os.getenv(name)
        if value is None:
            continue
        value = value.strip()
        if not value or value.startswith("$"):
            continue
        try:
            return int(value)
        except ValueError:
            continue

    return default


def _parse_int_value(raw_value: Optional[str], default: int) -> int:
    """Parse an integer-like string and fall back for unresolved templates/invalid values."""
    if raw_value is None:
        return default
    value = str(raw_value).strip()
    if not value or value.startswith("$"):
        return default
    try:
        return int(value)
    except ValueError:
        return default


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
    max_connections: int = Field(
        default=100, description="Maximum concurrent connections (simplified)"
    )
    heartbeat_interval: int = Field(default=30, description="Heartbeat interval in seconds")
    ping_interval: int = Field(default=45, description="Server ping interval in seconds")
    ping_timeout: int = Field(default=30, description="Server ping timeout in seconds")
    absolute_silence_seconds: int = Field(
        default=1800,
        description=(
            "Kill an OCPP session if no frame has been received for this long, "
            "even when the WebSocket is still alive at ping/pong layer. Safety "
            "net for chargers that handshake but never participate."
        ),
    )
    message_timeout: int = Field(default=60, description="Message timeout in seconds")
    max_message_size: int = Field(
        default=1048576, description="Maximum message size in bytes (PRD: 1 MB)"
    )
    rate_limit_per_minute: int = Field(
        default=100, description="Rate limit per connection per minute"
    )


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

    metrics_port: int = Field(default=9090, description="Prometheus metrics port")
    log_level: str = Field(default="INFO", description="Log level")
    enable_telemetry: bool = Field(default=True, description="Enable telemetry collection")
    health_check_port: int = Field(default=8081, description="Health check port")
    api_port: int = Field(default=8082, description="REST API server port")
    expected_stations: int = Field(default=100, description="Expected number of charging stations")
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")


class PriceFeederConfig(BaseModel):
    """Price feeder configuration (ENTSO-E day-ahead prices for European depots)."""

    enabled: bool = Field(default=True, description="Enable price feeder")
    entsoe_zones: List[str] = Field(
        default_factory=list,
        description=(
            "ENTSO-E bidding zone EIC codes for European depots "
            "(e.g., '10Y1001A1001A82H' for DE-LU, '10YLT-1001A0008Q' for LT)"
        ),
    )
    fetch_interval_seconds: int = Field(
        default=900, description="Price refresh interval in seconds"
    )
    lookahead_hours: int = Field(default=24, description="Forecast horizon in hours")


class OptimizationServiceConfig(BaseModel):
    """Optimization engine configuration."""

    enabled: bool = Field(default=True, description="Enable optimization service")
    horizon_hours: int = Field(default=4, description="Optimization horizon in hours")
    timestep_minutes: int = Field(default=60, description="Optimization timestep in minutes")
    soc_minimum: float = Field(
        default=0.2, description="Minimum allowed state of charge (fraction)"
    )
    soc_target: float = Field(default=0.8, description="Target state of charge before departure")
    charge_power_kw: float = Field(default=22.0, description="Default charge power limit in kW")
    discharge_power_kw: float = Field(
        default=10.0, description="Default discharge power limit in kW"
    )
    battery_capacity_kwh: float = Field(default=75.0, description="Default battery capacity in kWh")


class MainApiConfig(BaseModel):
    """Configuration for direct communication with the main API optimizer.

    When url is set the websocket_handler will:
    - Push significant OCPP events to /internal/ocpp-event for near-real-time triggers.
    - Read the service_heartbeat DB row to check main API liveness (primary).
    - Fall back to GET /health when the DB heartbeat is stale (secondary).
    - Only activate the heuristic optimizer when all three checks fail.
    """

    url: str = Field(default="", description="Base URL of the main API, e.g. http://api:8000")
    internal_token: str = Field(default="", description="Shared secret sent as X-Internal-Token")
    heartbeat_stale_seconds: int = Field(
        default=120, description="DB heartbeat age (s) before trying the HTTP health check"
    )
    push_timeout_seconds: float = Field(
        default=3.0, description="HTTP timeout for OCPP event pushes"
    )
    push_retry_attempts: int = Field(
        default=2, description="Extra retry attempts on push failure (0 = one try only)"
    )

    @property
    def enabled(self) -> bool:
        """True when a main API URL has been configured."""
        return bool(self.url)


class NotificationsConfig(BaseModel):
    """Alerts pipeline configuration (see docs/plans/alerts-pipeline.md)."""

    enabled: bool = Field(
        default=True,
        description="Enable the AlertDispatcher loop. False disables both LISTEN and polling.",
    )
    resend_api_key: str = Field(default="", description="Resend API bearer token")
    resend_from_address: str = Field(
        default="alerts@favonius.energy",
        description="Default sender address for outbound alert emails",
    )
    poll_interval_s: float = Field(
        default=30.0,
        description="Reconciliation poll cadence; safety net for dropped pg_notify events",
    )
    resend_interval_s: int = Field(
        default=3600,
        description="Minimum seconds between re-notifications for a still-active alert",
    )
    batch_size: int = Field(
        default=50,
        description="Maximum alerts processed per dispatcher tick",
    )


class VDV463Config(BaseModel):
    """VDV 463 transit operations configuration."""

    enabled: bool = Field(default=True, description="Enable VDV 463 support")
    validation_mode: str = Field(
        default="soft",
        description="Validation mode: 'soft' (warn only, default) or 'hard' (reject invalid)",
    )
    schema_dir: Optional[str] = Field(
        default=None, description="Directory containing VDV 463 JSON schemas"
    )
    default_depot_id: Optional[str] = Field(
        default=None, description="Default depot ID for VDV 463 connections"
    )


def _timescale_config_from_env(secrets_manager: SecretsManager) -> TimescaleConfig:
    """Build ``TimescaleConfig`` from env, parsing ``TIMESCALE_SERVICE_URL`` when needed."""
    _ts_secret_raw = secrets_manager.get_secret("TIMESCALE_SERVICE_URL") or os.getenv(
        "TIMESCALE_SERVICE_URL"
    )
    _ts_s = (_ts_secret_raw or "").strip()
    _env_ws = os.getenv("ENVIRONMENT", "development").strip().lower()
    _prodlike_ws = _env_ws in ("production", "staging")

    if _prodlike_ws and not _ts_s:
        raise ValueError(
            "TIMESCALE_SERVICE_URL must be set when ENVIRONMENT is production or staging "
            "for the WebSocket handler (TigerCloud favonius-timeseries). "
            "DATABASE_URL is reserved for Supabase static data."
        )

    _db_url_ws = (os.getenv("DATABASE_URL") or "").strip()
    _merge_url_candidate = _ts_s if _ts_s else ("" if _prodlike_ws else _db_url_ws)
    _merge_postgres_url = _merge_url_candidate if is_postgres_url(_merge_url_candidate) else None

    _pgport_raw = secrets_manager.get_secret("PGPORT") or os.getenv("PGPORT")
    _pgport_explicit = bool(_pgport_raw and str(_pgport_raw).strip())

    _host = secrets_manager.get_secret("PGHOST") or os.getenv("PGHOST")
    _port = _parse_int_value(_pgport_raw, 5432)
    _database = secrets_manager.get_secret("PGDATABASE") or os.getenv("PGDATABASE", "tsdb")
    _user = secrets_manager.get_secret("PGUSER") or os.getenv("PGUSER")
    _password = secrets_manager.get_secret("PGPASSWORD") or os.getenv("PGPASSWORD")
    _sslmode = secrets_manager.get_secret("PGSSLMODE") or os.getenv("PGSSLMODE", "require")

    h_m, port_m, d_m, u_m, pw_m, sm_m = merge_timescale_params_from_url(
        service_url=_merge_postgres_url,
        host=_host,
        port=_port,
        database=_database,
        user=_user,
        password=_password,
        sslmode=_sslmode or "require",
        pgport_explicit=_pgport_explicit,
    )

    if not h_m or not u_m or not pw_m:
        raise ValueError(
            "TimescaleDB connection is incomplete: set TIMESCALE_SERVICE_URL to a full "
            "postgres:// or postgresql:// URI, or set PGHOST, PGUSER, and PGPASSWORD "
            "(and optional PGPORT, PGDATABASE, PGSSLMODE)."
        )

    _service_url_field = _ts_s if _ts_s else (_merge_postgres_url or "")
    if not _service_url_field:
        _service_url_field = build_postgres_dsn(
            host=h_m,
            port=port_m,
            database=d_m,
            user=u_m,
            password=pw_m,
            sslmode=sm_m,
        )

    return TimescaleConfig(
        service_url=_service_url_field,
        host=h_m,
        port=port_m,
        database=d_m,
        user=u_m,
        password=pw_m,
        sslmode=sm_m,
        max_connections=int(
            secrets_manager.get_secret("TIMESCALE_MAX_CONNECTIONS")
            or os.getenv("TIMESCALE_MAX_CONNECTIONS", "100")
        ),
        pool_size=int(
            secrets_manager.get_secret("TIMESCALE_POOL_SIZE")
            or os.getenv("TIMESCALE_POOL_SIZE", "20")
        ),
        statement_timeout=int(
            secrets_manager.get_secret("TIMESCALE_STATEMENT_TIMEOUT")
            or os.getenv("TIMESCALE_STATEMENT_TIMEOUT", "30")
        ),
        idle_timeout=int(
            secrets_manager.get_secret("TIMESCALE_IDLE_TIMEOUT")
            or os.getenv("TIMESCALE_IDLE_TIMEOUT", "600")
        ),
        chunk_time_interval=secrets_manager.get_secret("TIMESCALE_CHUNK_INTERVAL")
        or os.getenv("TIMESCALE_CHUNK_INTERVAL", "1 day"),
        compression_after=secrets_manager.get_secret("TIMESCALE_COMPRESSION_AFTER")
        or os.getenv("TIMESCALE_COMPRESSION_AFTER", "7 days"),
        retention_period=secrets_manager.get_secret("TIMESCALE_RETENTION_PERIOD")
        or os.getenv("TIMESCALE_RETENTION_PERIOD", "2 years"),
    )


class Config(BaseModel):
    """Main application configuration."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    tls: TLSConfig = Field(default_factory=TLSConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    timescale: TimescaleConfig = Field(default_factory=TimescaleConfig)
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    price_feeder: PriceFeederConfig = Field(default_factory=PriceFeederConfig)
    optimization: OptimizationServiceConfig = Field(default_factory=OptimizationServiceConfig)
    vdv463: VDV463Config = Field(default_factory=VDV463Config)
    main_api: MainApiConfig = Field(default_factory=MainApiConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Environment-specific settings
    environment: str = Field(
        default="development", description="Environment (development/staging/production)"
    )
    debug: bool = Field(default=False, description="Debug mode")
    strict_startup_validation: bool = Field(
        default=True,
        description=(
            "Fail fast when startup dependency validation fails. "
            "Set to false to allow startup while external services are temporarily unavailable."
        ),
    )

    # Secrets management
    secrets_manager: Optional[SecretsManager] = None

    @validator("timescale", "supabase", pre=True, always=True)
    def validate_credentials(cls, v, values):
        """Validate that required credentials are present."""
        if hasattr(v, "password") and not v.password:
            raise ValueError("Database password is required")
        if hasattr(v, "service_key") and not v.service_key:
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
            fallback_to_env=os.getenv("FALLBACK_TO_ENV", "true").lower() == "true",
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
                port=_parse_int_env("WEBSOCKET_PORT", "PORT", 9000),
                host=os.getenv("WEBSOCKET_HOST", "0.0.0.0"),
                max_connections=int(os.getenv("MAX_CONNECTIONS", "100")),
                heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "30")),
                ping_interval=int(os.getenv("WEBSOCKET_PING_INTERVAL", "45")),
                ping_timeout=int(os.getenv("WEBSOCKET_PING_TIMEOUT", "30")),
                absolute_silence_seconds=int(os.getenv("OCPP_ABSOLUTE_SILENCE_SECONDS", "1800")),
                message_timeout=int(os.getenv("MESSAGE_TIMEOUT", "60")),
                max_message_size=int(os.getenv("MAX_MESSAGE_SIZE", "1048576")),
                rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "100")),
            ),
            timescale=_timescale_config_from_env(secrets_manager),
            supabase=SupabaseConfig(
                url=secrets_manager.get_secret("SUPABASE_URL") or os.getenv("SUPABASE_URL"),
                anon_key=secrets_manager.get_secret("SUPABASE_ANON_KEY")
                or os.getenv("SUPABASE_ANON_KEY"),
                service_key=secrets_manager.get_secret("SUPABASE_SERVICE_KEY")
                or os.getenv("SUPABASE_SERVICE_KEY"),
                db_host=secrets_manager.get_secret("SUPABASE_DB_HOST")
                or os.getenv("SUPABASE_DB_HOST"),
                db_port=_parse_int_value(
                    secrets_manager.get_secret("SUPABASE_DB_PORT") or os.getenv("SUPABASE_DB_PORT"),
                    6543,
                ),
                db_name=secrets_manager.get_secret("SUPABASE_DB_NAME")
                or os.getenv("SUPABASE_DB_NAME", "postgres"),
                db_user=secrets_manager.get_secret("SUPABASE_DB_USER")
                or os.getenv("SUPABASE_DB_USER"),
                db_password=secrets_manager.get_secret("SUPABASE_DB_PASSWORD")
                or os.getenv("SUPABASE_DB_PASSWORD"),
                max_connections=int(
                    secrets_manager.get_secret("SUPABASE_MAX_CONNECTIONS")
                    or os.getenv("SUPABASE_MAX_CONNECTIONS", "20")
                ),
                connection_timeout=int(
                    secrets_manager.get_secret("SUPABASE_CONNECTION_TIMEOUT")
                    or os.getenv("SUPABASE_CONNECTION_TIMEOUT", "30")
                ),
                enable_realtime=os.getenv("SUPABASE_ENABLE_REALTIME", "true").lower() == "true",
            ),
            monitoring=MonitoringConfig(
                metrics_port=_parse_int_env("METRICS_PORT", default=9090),
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                enable_telemetry=os.getenv("ENABLE_TELEMETRY", "true").lower() == "true",
                health_check_port=_parse_int_env("HEALTH_CHECK_PORT", default=8081),
                api_port=_parse_int_env("API_PORT", default=8082),
            ),
            price_feeder=PriceFeederConfig(
                enabled=os.getenv("PRICE_FEEDER_ENABLED", "true").lower() == "true",
                entsoe_zones=[
                    z.strip()
                    for z in os.getenv("PRICE_FEEDER_ENTSOE_ZONES", "").split(",")
                    if z.strip()
                ],
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
            notifications=NotificationsConfig(
                enabled=os.getenv("EMAIL_DELIVERY_ENABLED", "true").lower() == "true",
                resend_api_key=secrets_manager.get_secret("RESEND_API_KEY")
                or os.getenv("RESEND_API_KEY", ""),
                resend_from_address=os.getenv("RESEND_FROM_ADDRESS", "alerts@favonius.energy"),
                poll_interval_s=float(os.getenv("ALERT_DISPATCHER_POLL_INTERVAL_S", "30")),
                resend_interval_s=int(os.getenv("ALERT_NOTIFY_RESEND_INTERVAL_S", "3600")),
                batch_size=int(os.getenv("ALERT_DISPATCHER_BATCH_SIZE", "50")),
            ),
            main_api=MainApiConfig(
                url=os.getenv("MAIN_API_URL", ""),
                internal_token=os.getenv("MAIN_API_INTERNAL_TOKEN", ""),
                heartbeat_stale_seconds=_parse_int_env(
                    "MAIN_API_HEARTBEAT_STALE_SECONDS", default=120
                ),
                push_timeout_seconds=float(os.getenv("MAIN_API_PUSH_TIMEOUT_SECONDS", "3.0")),
                push_retry_attempts=_parse_int_env("MAIN_API_PUSH_RETRY_ATTEMPTS", default=2),
            ),
            environment=os.getenv("ENVIRONMENT", "development"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            strict_startup_validation=os.getenv("STRICT_STARTUP_VALIDATION", "true").lower()
            == "true",
            secrets_manager=secrets_manager,
        )
