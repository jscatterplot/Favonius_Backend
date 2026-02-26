"""Unit tests for configuration management."""

import os

# Import config
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import (
    Config,
    MonitoringConfig,
    OptimizationServiceConfig,
    PriceFeederConfig,
    SupabaseConfig,
    TimescaleConfig,
    TLSConfig,
    WebSocketConfig,
)


class TestTLSConfig:
    """Test TLSConfig functionality."""

    def test_tls_config_defaults(self):
        """Test TLS config defaults."""
        config = TLSConfig()

        assert config.cert_path is None
        assert config.key_path is None
        assert config.ca_path is None
        assert config.verify_client is False

    def test_tls_config_custom_values(self):
        """Test TLS config with custom values."""
        config = TLSConfig(
            cert_path="/path/to/cert.pem",
            key_path="/path/to/key.pem",
            ca_path="/path/to/ca.pem",
            verify_client=True,
        )

        assert config.cert_path == "/path/to/cert.pem"
        assert config.key_path == "/path/to/key.pem"
        assert config.ca_path == "/path/to/ca.pem"
        assert config.verify_client is True


class TestWebSocketConfig:
    """Test WebSocketConfig functionality."""

    def test_websocket_config_defaults(self):
        """Test WebSocket config defaults."""
        config = WebSocketConfig()

        assert config.port == 9000
        assert config.host == "0.0.0.0"
        assert config.max_connections == 100
        assert config.heartbeat_interval == 30
        assert config.message_timeout == 60
        assert config.max_message_size == 1048576
        assert config.rate_limit_per_minute == 100

    def test_websocket_config_custom_values(self):
        """Test WebSocket config with custom values."""
        config = WebSocketConfig(
            port=8080,
            host="127.0.0.1",
            max_connections=50,
            heartbeat_interval=60,
            message_timeout=120,
            max_message_size=32768,
            rate_limit_per_minute=200,
        )

        assert config.port == 8080
        assert config.host == "127.0.0.1"
        assert config.max_connections == 50
        assert config.heartbeat_interval == 60
        assert config.message_timeout == 120
        assert config.max_message_size == 32768
        assert config.rate_limit_per_minute == 200


class TestTimescaleConfig:
    """Test TimescaleConfig functionality."""

    def test_timescale_config_defaults(self):
        """Test Timescale config defaults."""
        config = TimescaleConfig(
            service_url="postgres://test:test@localhost:5432/test",
            host="localhost",
            user="test",
            password="test",
        )

        assert config.service_url == "postgres://test:test@localhost:5432/test"
        assert config.host == "localhost"
        assert config.port == 5432
        assert config.database == "tsdb"
        assert config.user == "test"
        assert config.password == "test"
        assert config.sslmode == "require"
        assert config.max_connections == 100
        assert config.pool_size == 20
        assert config.statement_timeout == 30
        assert config.idle_timeout == 600
        assert config.chunk_time_interval == "1 day"
        assert config.compression_after == "7 days"
        assert config.retention_period == "2 years"

    def test_timescale_config_custom_values(self):
        """Test Timescale config with custom values."""
        config = TimescaleConfig(
            service_url="postgres://custom:custom@custom:5433/custom",
            host="custom",
            port=5433,
            database="custom_db",
            user="custom",
            password="custom",
            sslmode="disable",
            max_connections=200,
            pool_size=50,
            statement_timeout=60,
            idle_timeout=1200,
            chunk_time_interval="2 days",
            compression_after="14 days",
            retention_period="5 years",
        )

        assert config.service_url == "postgres://custom:custom@custom:5433/custom"
        assert config.host == "custom"
        assert config.port == 5433
        assert config.database == "custom_db"
        assert config.user == "custom"
        assert config.password == "custom"
        assert config.sslmode == "disable"
        assert config.max_connections == 200
        assert config.pool_size == 50
        assert config.statement_timeout == 60
        assert config.idle_timeout == 1200
        assert config.chunk_time_interval == "2 days"
        assert config.compression_after == "14 days"
        assert config.retention_period == "5 years"


class TestSupabaseConfig:
    """Test SupabaseConfig functionality."""

    def test_supabase_config_defaults(self):
        """Test Supabase config defaults."""
        config = SupabaseConfig(
            url="https://test.supabase.co",
            anon_key="test_anon_key",
            service_key="test_service_key",
            db_host="test.db.host",
            db_user="test_user",
            db_password="test_password",
        )

        assert config.url == "https://test.supabase.co"
        assert config.anon_key == "test_anon_key"
        assert config.service_key == "test_service_key"
        assert config.db_host == "test.db.host"
        assert config.db_port == 5432
        assert config.db_name == "postgres"
        assert config.db_user == "test_user"
        assert config.db_password == "test_password"
        assert config.max_connections == 20
        assert config.connection_timeout == 30
        assert config.enable_realtime is True

    def test_supabase_config_custom_values(self):
        """Test Supabase config with custom values."""
        config = SupabaseConfig(
            url="https://custom.supabase.co",
            anon_key="custom_anon_key",
            service_key="custom_service_key",
            db_host="custom.db.host",
            db_port=5433,
            db_name="custom_db",
            db_user="custom_user",
            db_password="custom_password",
            max_connections=50,
            connection_timeout=60,
            enable_realtime=False,
        )

        assert config.url == "https://custom.supabase.co"
        assert config.anon_key == "custom_anon_key"
        assert config.service_key == "custom_service_key"
        assert config.db_host == "custom.db.host"
        assert config.db_port == 5433
        assert config.db_name == "custom_db"
        assert config.db_user == "custom_user"
        assert config.db_password == "custom_password"
        assert config.max_connections == 50
        assert config.connection_timeout == 60
        assert config.enable_realtime is False


class TestMonitoringConfig:
    """Test MonitoringConfig functionality."""

    def test_monitoring_config_defaults(self):
        """Test monitoring config defaults."""
        config = MonitoringConfig()

        assert config.metrics_port == 8080
        assert config.log_level == "INFO"
        assert config.enable_telemetry is True
        assert config.health_check_port == 8081

    def test_monitoring_config_custom_values(self):
        """Test monitoring config with custom values."""
        config = MonitoringConfig(
            metrics_port=9090, log_level="DEBUG", enable_telemetry=False, health_check_port=9091
        )

        assert config.metrics_port == 9090
        assert config.log_level == "DEBUG"
        assert config.enable_telemetry is False
        assert config.health_check_port == 9091


class TestPriceFeederConfig:
    """Test PriceFeederConfig functionality."""

    def test_price_feeder_config_defaults(self):
        """Test price feeder config defaults."""
        config = PriceFeederConfig()

        assert config.enabled is True
        assert config.base_url == "https://oasis.caiso.com/oasisapi/SingleZip"
        assert config.nodes == ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"]
        assert config.fetch_interval_seconds == 900
        assert config.lookahead_hours == 24

    def test_price_feeder_config_custom_values(self):
        """Test price feeder config with custom values."""
        config = PriceFeederConfig(
            enabled=False,
            base_url="https://custom.api.com",
            nodes=["CUSTOM_NODE_1", "CUSTOM_NODE_2"],
            fetch_interval_seconds=1800,
            lookahead_hours=48,
        )

        assert config.enabled is False
        assert config.base_url == "https://custom.api.com"
        assert config.nodes == ["CUSTOM_NODE_1", "CUSTOM_NODE_2"]
        assert config.fetch_interval_seconds == 1800
        assert config.lookahead_hours == 48


class TestOptimizationServiceConfig:
    """Test OptimizationServiceConfig functionality."""

    def test_optimization_config_defaults(self):
        """Test optimization config defaults."""
        config = OptimizationServiceConfig()

        assert config.enabled is True
        assert config.horizon_hours == 4
        assert config.timestep_minutes == 60
        assert config.soc_minimum == 0.2
        assert config.soc_target == 0.8
        assert config.charge_power_kw == 22.0
        assert config.discharge_power_kw == 10.0
        assert config.battery_capacity_kwh == 75.0

    def test_optimization_config_custom_values(self):
        """Test optimization config with custom values."""
        config = OptimizationServiceConfig(
            enabled=False,
            horizon_hours=8,
            timestep_minutes=30,
            soc_minimum=0.1,
            soc_target=0.9,
            charge_power_kw=50.0,
            discharge_power_kw=25.0,
            battery_capacity_kwh=100.0,
        )

        assert config.enabled is False
        assert config.horizon_hours == 8
        assert config.timestep_minutes == 30
        assert config.soc_minimum == 0.1
        assert config.soc_target == 0.9
        assert config.charge_power_kw == 50.0
        assert config.discharge_power_kw == 25.0
        assert config.battery_capacity_kwh == 100.0


class TestConfig:
    """Test main Config functionality."""

    def test_config_defaults(self):
        """Test config defaults."""
        config = Config(
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="test.db.host",
                db_user="test_user",
                db_password="test_password",
            ),
        )

        assert isinstance(config.tls, TLSConfig)
        assert isinstance(config.websocket, WebSocketConfig)
        assert isinstance(config.timescale, TimescaleConfig)
        assert isinstance(config.supabase, SupabaseConfig)
        assert isinstance(config.monitoring, MonitoringConfig)
        assert isinstance(config.price_feeder, PriceFeederConfig)
        assert isinstance(config.optimization, OptimizationServiceConfig)
        assert config.environment == "development"
        assert config.debug is False

    def test_config_custom_values(self):
        """Test config with custom values."""
        config = Config(
            environment="production",
            debug=True,
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="test.db.host",
                db_user="test_user",
                db_password="test_password",
            ),
        )

        assert config.environment == "production"
        assert config.debug is True

    @patch.dict(
        os.environ,
        {
            "WEBSOCKET_PORT": "8080",
            "WEBSOCKET_HOST": "127.0.0.1",
            "MAX_CONNECTIONS": "50",
            "HEARTBEAT_INTERVAL": "60",
            "LOG_LEVEL": "DEBUG",
            "ENVIRONMENT": "staging",
            "DEBUG": "true",
            "TLS_CERT_PATH": "/path/to/cert.pem",
            "TLS_KEY_PATH": "/path/to/key.pem",
            "TLS_VERIFY_CLIENT": "true",
            "TIMESCALE_SERVICE_URL": "postgres://test:test@localhost:5432/test",
            "PGHOST": "localhost",
            "PGPORT": "5432",
            "PGDATABASE": "test_db",
            "PGUSER": "test_user",
            "PGPASSWORD": "test_password",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_ANON_KEY": "test_anon_key",
            "SUPABASE_SERVICE_KEY": "test_service_key",
            "SUPABASE_DB_HOST": "test.db.host",
            "SUPABASE_DB_USER": "test_user",
            "SUPABASE_DB_PASSWORD": "test_password",
            "METRICS_PORT": "9090",
            "HEALTH_CHECK_PORT": "9091",
            "PRICE_FEEDER_ENABLED": "false",
            "PRICE_FEEDER_NODES": "NODE1,NODE2",
            "PRICE_FEEDER_FETCH_INTERVAL": "1800",
            "PRICE_FEEDER_LOOKAHEAD_HOURS": "48",
            "OPTIMIZATION_ENABLED": "false",
            "OPTIMIZATION_HORIZON_HOURS": "8",
            "OPTIMIZATION_TIMESTEP_MINUTES": "30",
            "OPTIMIZATION_SOC_MIN": "0.1",
            "OPTIMIZATION_SOC_TARGET": "0.9",
            "OPTIMIZATION_CHARGE_POWER_KW": "50.0",
            "OPTIMIZATION_DISCHARGE_POWER_KW": "25.0",
            "OPTIMIZATION_BATTERY_CAPACITY_KWH": "100.0",
        },
    )
    def test_config_from_env(self):
        """Test config creation from environment variables."""
        config = Config.from_env()

        # Test WebSocket config
        assert config.websocket.port == 8080
        assert config.websocket.host == "127.0.0.1"
        assert config.websocket.max_connections == 50
        assert config.websocket.heartbeat_interval == 60

        # Test TLS config
        assert config.tls.cert_path == "/path/to/cert.pem"
        assert config.tls.key_path == "/path/to/key.pem"
        assert config.tls.verify_client is True

        # Test Timescale config
        assert config.timescale.service_url == "postgres://test:test@localhost:5432/test"
        assert config.timescale.host == "localhost"
        assert config.timescale.port == 5432
        assert config.timescale.database == "test_db"
        assert config.timescale.user == "test_user"
        assert config.timescale.password == "test_password"

        # Test Supabase config
        assert config.supabase.url == "https://test.supabase.co"
        assert config.supabase.anon_key == "test_anon_key"
        assert config.supabase.service_key == "test_service_key"
        assert config.supabase.db_host == "test.db.host"
        assert config.supabase.db_user == "test_user"
        assert config.supabase.db_password == "test_password"

        # Test monitoring config
        assert config.monitoring.metrics_port == 9090
        assert config.monitoring.log_level == "DEBUG"
        assert config.monitoring.health_check_port == 9091

        # Test price feeder config
        assert config.price_feeder.enabled is False
        assert config.price_feeder.nodes == ["NODE1", "NODE2"]
        assert config.price_feeder.fetch_interval_seconds == 1800
        assert config.price_feeder.lookahead_hours == 48

        # Test optimization config
        assert config.optimization.enabled is False
        assert config.optimization.horizon_hours == 8
        assert config.optimization.timestep_minutes == 30
        assert config.optimization.soc_minimum == 0.1
        assert config.optimization.soc_target == 0.9
        assert config.optimization.charge_power_kw == 50.0
        assert config.optimization.discharge_power_kw == 25.0
        assert config.optimization.battery_capacity_kwh == 100.0

        # Test main config
        assert config.environment == "staging"
        assert config.debug is True

    @patch.dict(
        os.environ,
        {
            "TIMESCALE_SERVICE_URL": "postgres://test:test@localhost:5432/test",
            "PGHOST": "localhost",
            "PGUSER": "test",
            "PGPASSWORD": "test",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "anon",
            "SUPABASE_SERVICE_KEY": "service",
            "SUPABASE_DB_HOST": "db.host",
            "SUPABASE_DB_USER": "user",
            "SUPABASE_DB_PASSWORD": "pass",
        },
        clear=True,
    )
    def test_config_from_env_defaults(self):
        """Test config creation from environment with defaults."""
        config = Config.from_env()

        # Should use defaults when env vars are not set
        assert config.websocket.port == 9000
        assert config.websocket.host == "0.0.0.0"
        assert config.tls.verify_client is False
        assert config.monitoring.log_level == "INFO"
        assert config.price_feeder.enabled is True
        assert config.optimization.enabled is True
        assert config.environment == "development"
        assert config.debug is False

    def test_config_validation(self):
        """Test config validation."""
        # Test valid config
        config = Config(
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="test.db.host",
                db_user="test_user",
                db_password="test_password",
            ),
        )
        assert config.environment in ["development", "staging", "production"]

        # Test invalid port (should still work, validation happens at runtime)
        config.websocket.port = -1
        assert config.websocket.port == -1

    def test_config_nested_access(self):
        """Test nested config access."""
        config = Config(
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="test.db.host",
                db_user="test_user",
                db_password="test_password",
            ),
        )

        # Test accessing nested configs
        assert config.websocket.port == 9000
        assert config.tls.verify_client is False
        assert config.monitoring.metrics_port == 8080
        assert config.price_feeder.nodes == ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"]
        assert config.optimization.horizon_hours == 4

    def test_config_immutability(self):
        """Test that config values can be modified."""
        config = Config(
            timescale=TimescaleConfig(
                service_url="postgres://test:test@localhost:5432/test",
                host="localhost",
                user="test",
                password="test",
            ),
            supabase=SupabaseConfig(
                url="https://test.supabase.co",
                anon_key="test_anon_key",
                service_key="test_service_key",
                db_host="test.db.host",
                db_user="test_user",
                db_password="test_password",
            ),
        )

        # Should be able to modify values
        config.websocket.port = 8080
        assert config.websocket.port == 8080

        config.environment = "production"
        assert config.environment == "production"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
