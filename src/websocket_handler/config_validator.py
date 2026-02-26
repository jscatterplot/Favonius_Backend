"""Configuration validation and startup checks."""

import os
from typing import Any, Dict

import asyncpg
import structlog
from sqlalchemy import create_engine, text
from supabase import create_client

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
                    self.logger.info(f"Validation passed for {name}")
            except Exception as e:
                self.logger.error(f"Validation error for {name}: {e}")
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
            # Test connection
            conn = await asyncpg.connect(
                host=self.config.timescale.host,
                port=self.config.timescale.port,
                database=self.config.timescale.database,
                user=self.config.timescale.user,
                password=self.config.timescale.password,
                ssl=self.config.timescale.sslmode,
            )

            # Test TimescaleDB extension
            result = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            if not result:
                self.logger.error("TimescaleDB extension not found")
                await conn.close()
                return False

            # Test basic query
            await conn.fetchval("SELECT 1")

            await conn.close()
            self.logger.info("TimescaleDB validation passed")
            return True
        except Exception as e:
            self.logger.error(f"TimescaleDB validation failed: {e}")
            return False

    async def _validate_supabase(self) -> bool:
        """Validate Supabase connection."""
        try:
            if not self.config.supabase.url or not self.config.supabase.service_key:
                self.logger.error("Supabase URL or service key not configured")
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
                    self.logger.error("Supabase database query failed")
                    return False

            self.logger.info("Supabase validation passed")
            return True
        except Exception as e:
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
            "environment": self.config.environment,
            "debug_mode": self.config.debug,
        }
