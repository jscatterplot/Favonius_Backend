"""Configuration validation and startup checks."""

import os
from typing import Any, Dict

import asyncpg

from src.db.postgres_url import ssl_context_for_postgres_sslmode
import structlog
from sqlalchemy import create_engine, text
try:
    from supabase import create_client
except ImportError:  # pragma: no cover - optional dependency during isolated test runs
    def create_client(*args, **kwargs):  # type: ignore[no-redef]
        raise RuntimeError(
            "supabase package is unavailable. Install optional dependencies to validate Supabase."
        )

from .config import Config


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    pass


class ConfigValidator:
    """Validates configuration and performs startup checks."""

    def __init__(self, config: Config):
        """Initialize config validator."""
        self.config = config
        self.logger = structlog.get_logger(__name__)
        self.validation_results: Dict[str, bool] = {}
        self.validation_details: Dict[str, str] = {}

    def _record_detail(self, component: str, detail: str) -> None:
        """Record human-readable validation detail for diagnostics."""
        self.validation_details[component] = detail

    def _is_placeholder(self, value: Any) -> bool:
        """Check whether a value looks like an unresolved template."""
        if value is None:
            return True
        text = str(value).strip()
        return text == "" or text.startswith("$")

    def _missing_fields(self, component: str, mapping: Dict[str, Any]) -> bool:
        """Validate required fields and record missing values with env var names."""
        missing = [name for name, value in mapping.items() if self._is_placeholder(value)]
        if missing:
            self._record_detail(component, f"Missing required configuration: {', '.join(missing)}")
            self.logger.error(
                f"{component.capitalize()} validation failed: missing required configuration values: {', '.join(missing)}"
            )
            return True
        return False

    async def validate_all(self) -> bool:
        """Validate all configuration components."""
        self.logger.info("Starting configuration validation")

        validations = [
            ("secrets", self._validate_secrets),
            ("timescale", self._validate_timescale),
            ("supabase", self._validate_supabase),
            ("websocket", self._validate_websocket),
            ("tls", self._validate_tls),
            ("monitoring", self._validate_monitoring),
        ]

        all_valid = True
        for name, validation_func in validations:
            try:
                result = await validation_func()
                self.validation_results[name] = result
                if not result:
                    all_valid = False
                    self.logger.error(f"Validation failed for {name}")
                else:
                    # Only record generic success if no specific detail was already recorded
                    if name not in self.validation_details:
                        self._record_detail(name, "Validation passed")
                    self.logger.info(f"Validation passed for {name}")
            except Exception as e:
                self.logger.error(f"Validation error for {name}: {e}")
                self._record_detail(name, f"Validation error: {e}")
                self.validation_results[name] = False
                all_valid = False

        if all_valid:
            self.logger.info("All configuration validations passed")
        else:
            self.logger.error("Some configuration validations failed")

        return all_valid

    async def _validate_secrets(self) -> bool:
        """Validate secrets management."""
        try:
            if not self.config.secrets_manager:
                self.logger.error("Secrets manager not initialized")
                return False

            # Test secret retrieval
            test_secret = self.config.secrets_manager.get_secret("TEST_SECRET", "default")
            if test_secret is None:
                self.logger.error("Failed to retrieve test secret")
                return False

            self.logger.info("Secrets management validation passed")
            return True
        except Exception as e:
            self.logger.error(f"Secrets validation failed: {e}")
            return False

    async def _validate_timescale(self) -> bool:
        """Validate TimescaleDB connection and schema."""
        try:
            if self._missing_fields(
                "timescale",
                {
                    "PGHOST": self.config.timescale.host,
                    "PGPORT": self.config.timescale.port,
                    "PGDATABASE": self.config.timescale.database,
                    "PGUSER": self.config.timescale.user,
                    "PGPASSWORD": self.config.timescale.password,
                },
            ):
                return False

            if (
                self.config.environment == "production"
                and str(self.config.timescale.host).strip().lower() in {"localhost", "127.0.0.1", "::1"}
            ):
                message = (
                    "Timescale host points to localhost in production. "
                    "Set TIMESCALE_SERVICE_URL to your TigerCloud / Timescale DSN, or set "
                    "PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD to your external TimescaleDB instance."
                )
                self._record_detail("timescale", message)
                self.logger.error(message)
                return False

            # Test connection
            conn = await asyncpg.connect(
                host=self.config.timescale.host,
                port=self.config.timescale.port,
                database=self.config.timescale.database,
                user=self.config.timescale.user,
                password=self.config.timescale.password,
                ssl=ssl_context_for_postgres_sslmode(str(self.config.timescale.sslmode)),
            )

            # Test TimescaleDB extension
            result = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            if not result:
                detail = "TimescaleDB extension not found. Ensure target database has timescaledb extension enabled."
                self._record_detail("timescale", detail)
                self.logger.error("TimescaleDB extension not found")
                await conn.close()
                return False

            # Test basic query
            await conn.fetchval("SELECT 1")

            await conn.close()
            self._record_detail("timescale", "Connected and query checks passed")
            self.logger.info("TimescaleDB validation passed")
            return True
        except Exception as e:
            detail = f"Connection failed: {e}"
            if "Connect call failed" in str(e):
                detail += "; verify PGHOST/PGPORT are reachable from Railway"
            self._record_detail("timescale", detail)
            self.logger.error(f"TimescaleDB validation failed: {e}")
            return False

    async def _validate_supabase(self) -> bool:
        """Validate Supabase connection."""
        try:
            if self._missing_fields(
                "supabase",
                {
                    "SUPABASE_URL": self.config.supabase.url,
                    "SUPABASE_SERVICE_KEY": self.config.supabase.service_key,
                    "SUPABASE_DB_HOST": self.config.supabase.db_host,
                    "SUPABASE_DB_PORT": self.config.supabase.db_port,
                    "SUPABASE_DB_NAME": self.config.supabase.db_name,
                    "SUPABASE_DB_USER": self.config.supabase.db_user,
                    "SUPABASE_DB_PASSWORD": self.config.supabase.db_password,
                },
            ):
                return False

            # Test Supabase client
            create_client(self.config.supabase.url, self.config.supabase.service_key)

            # Test database connection
            engine = create_engine(
                f"postgresql://{self.config.supabase.db_user}:{self.config.supabase.db_password}@"
                f"{self.config.supabase.db_host}:{self.config.supabase.db_port}/{self.config.supabase.db_name}"
            )

            with engine.connect() as conn:
                result = conn.execute(text("SELECT 1")).fetchone()
                if not result:
                    detail = "Supabase query returned no rows"
                    self._record_detail("supabase", detail)
                    self.logger.error("Supabase database query failed")
                    return False

            self._record_detail("supabase", "Connected and query checks passed")
            self.logger.info("Supabase validation passed")
            return True
        except Exception as e:
            detail = f"Connection failed: {e}"
            if "Tenant or user not found" in str(e):
                detail += "; verify SUPABASE_DB_USER/SUPABASE_DB_PASSWORD and pooler host"
            self._record_detail("supabase", detail)
            self.logger.error(f"Supabase validation failed: {e}")
            return False

    async def _validate_websocket(self) -> bool:
        """Validate WebSocket configuration."""
        try:
            if self.config.websocket.port <= 0 or self.config.websocket.port > 65535:
                self.logger.error(f"Invalid WebSocket port: {self.config.websocket.port}")
                return False

            if self.config.websocket.max_connections <= 0:
                self.logger.error(
                    f"Invalid max connections: {self.config.websocket.max_connections}"
                )
                return False

            if self.config.websocket.heartbeat_interval <= 0:
                self.logger.error(
                    f"Invalid heartbeat interval: {self.config.websocket.heartbeat_interval}"
                )
                return False

            self.logger.info("WebSocket configuration validation passed")
            return True
        except Exception as e:
            self.logger.error(f"WebSocket validation failed: {e}")
            return False

    async def _validate_tls(self) -> bool:
        """Validate TLS configuration."""
        try:
            if self.config.tls.cert_path and not os.path.exists(self.config.tls.cert_path):
                self.logger.error(f"TLS certificate file not found: {self.config.tls.cert_path}")
                return False

            if self.config.tls.key_path and not os.path.exists(self.config.tls.key_path):
                self.logger.error(f"TLS key file not found: {self.config.tls.key_path}")
                return False

            if self.config.tls.ca_path and not os.path.exists(self.config.tls.ca_path):
                self.logger.error(f"TLS CA file not found: {self.config.tls.ca_path}")
                return False

            self.logger.info("TLS configuration validation passed")
            return True
        except Exception as e:
            self.logger.error(f"TLS validation failed: {e}")
            return False

    async def _validate_monitoring(self) -> bool:
        """Validate monitoring configuration."""
        try:
            if (
                self.config.monitoring.metrics_port <= 0
                or self.config.monitoring.metrics_port > 65535
            ):
                self.logger.error(f"Invalid metrics port: {self.config.monitoring.metrics_port}")
                return False

            if (
                self.config.monitoring.health_check_port <= 0
                or self.config.monitoring.health_check_port > 65535
            ):
                self.logger.error(
                    f"Invalid health check port: {self.config.monitoring.health_check_port}"
                )
                return False

            valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if self.config.monitoring.log_level.upper() not in valid_log_levels:
                self.logger.error(f"Invalid log level: {self.config.monitoring.log_level}")
                return False

            self.logger.info("Monitoring configuration validation passed")
            return True
        except Exception as e:
            self.logger.error(f"Monitoring validation failed: {e}")
            return False

    def get_validation_report(self) -> Dict[str, Any]:
        """Get detailed validation report."""
        return {
            "overall_status": all(self.validation_results.values()),
            "validation_results": self.validation_results,
            "validation_details": self.validation_details,
            "environment": self.config.environment,
            "debug_mode": self.config.debug,
        }
