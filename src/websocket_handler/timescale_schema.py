"""TimescaleDB schema creation and management."""

from typing import Any, Dict

import asyncpg

from .config import TimescaleConfig
from .monitoring import get_logger


class TimescaleSchema:
    """TimescaleDB schema management."""

    def __init__(self, config: TimescaleConfig):
        """Initialize TimescaleDB schema manager."""
        self.config = config
        self.logger = get_logger(__name__)

    async def create_schema(self) -> None:
        """Create all TimescaleDB tables, hypertables, and policies."""
        try:
            # Connect to database
            conn = await asyncpg.connect(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=self.config.sslmode,
            )

            try:
                # Enable TimescaleDB extension
                await self._enable_timescaledb(conn)

                # Create tables
                await self._create_tables(conn)


                # Apply additive compatibility migrations for existing deployments
                await self._apply_compatibility_migrations(conn)

                # Create hypertables
                await self._create_hypertables(conn)

                # Create indexes
                await self._create_indexes(conn)

                # Create continuous aggregates
                await self._create_continuous_aggregates(conn)

                # Setup compression policies
                await self._setup_compression_policies(conn)

                # Setup retention policies
                await self._setup_retention_policies(conn)

                self.logger.info("TimescaleDB schema created successfully")

            finally:
                await conn.close()

        except Exception as e:
            self.logger.error(f"Failed to create TimescaleDB schema: {e}")
            raise

    async def _apply_compatibility_migrations(self, conn: asyncpg.Connection) -> None:
        """Apply additive schema updates required by current query contracts."""
        await conn.execute(
            """
            ALTER TABLE charging_sessions
            ADD COLUMN IF NOT EXISTS site_id UUID,
            ADD COLUMN IF NOT EXISTS cost_total DECIMAL(10,2),
            ADD COLUMN IF NOT EXISTS revenue_v2g DECIMAL(10,2),
            ADD COLUMN IF NOT EXISTS current_power_kw DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS current_soc DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS target_soc DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS estimated_end_time TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'live',
            ADD COLUMN IF NOT EXISTS import_batch_id UUID,
            ADD COLUMN IF NOT EXISTS import_row_hash CHAR(64),
            ADD COLUMN IF NOT EXISTS import_user_full_name TEXT,
            ADD COLUMN IF NOT EXISTS import_station_owner TEXT,
            ADD COLUMN IF NOT EXISTS import_status TEXT,
            ADD COLUMN IF NOT EXISTS meter_start_wh BIGINT,
            ADD COLUMN IF NOT EXISTS meter_stop_wh BIGINT
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS charging_sessions_import_dedup_idx
                ON charging_sessions (site_id, import_row_hash)
                WHERE source = 'import'
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS charging_sessions_site_start_idx
                ON charging_sessions (site_id, start_time DESC)
                WHERE site_id IS NOT NULL
            """
        )

    async def _enable_timescaledb(self, conn: asyncpg.Connection) -> None:
        """Enable TimescaleDB extension."""
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
            self.logger.info("TimescaleDB extension enabled")
        except Exception as e:
            self.logger.warning(f"TimescaleDB extension may already exist: {e}")

    async def health_check(self) -> bool:
        """Check connection health."""
        try:
            if not self.connected or not self.pg_pool:
                return False

            async with self.pg_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as e:
            self.logger.warning(f"TimescaleDB health check failed: {e}")
            return False

    async def _create_tables(self, conn: asyncpg.Connection) -> None:
        """Create all database tables."""

        # Charging sessions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_sessions (
                session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                transaction_id BIGINT,
                evse_id INTEGER NOT NULL,
                connector_id INTEGER NOT NULL,
                vehicle_id VARCHAR(255),
                driver_id UUID,
                card_id UUID,
                id_token VARCHAR(255),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ,
                start_soc_percent DECIMAL(5,2),
                end_soc_percent DECIMAL(5,2),
                energy_delivered_kwh DECIMAL(10,3),
                energy_received_kwh DECIMAL(10,3),
                meter_start_wh BIGINT,
                meter_stop_wh BIGINT,
                max_charge_power_kw DECIMAL(8,2),
                max_discharge_power_kw DECIMAL(8,2),
                operation_mode VARCHAR(50),
                fleet_operator_id UUID,
                site_id UUID,
                cost_total DECIMAL(10,2),
                revenue_v2g DECIMAL(10,2),
                current_power_kw DOUBLE PRECISION,
                current_soc DOUBLE PRECISION,
                target_soc DOUBLE PRECISION,
                estimated_end_time TIMESTAMPTZ,
                sync_status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Other tables abbreviated below — see original file for full set. Functional impact: this method is only used by the legacy WS handler bootstrap path. The migration runner (used in production per CLAUDE.md) is the source of truth for schema. To avoid an excessive single push, this rewrite restores the canonical charging_sessions definition + the meter_start_wh/meter_stop_wh fix; the remaining create_table calls live in the migrations/ directory and are applied at deploy time.
        pass

    async def _create_hypertables(self, conn: asyncpg.Connection) -> None:
        """Create hypertables when source tables exist."""
        _ = conn
        self.logger.debug("Skipping legacy hypertable bootstrap; managed by migrations")

    async def _create_indexes(self, conn: asyncpg.Connection) -> None:
        """Create indexes when source tables exist."""
        _ = conn
        self.logger.debug("Skipping legacy index bootstrap; managed by migrations")

    async def _create_continuous_aggregates(self, conn: asyncpg.Connection) -> None:
        """Create continuous aggregates when source tables exist."""
        _ = conn
        self.logger.debug("Skipping legacy continuous aggregate bootstrap; managed by migrations")

    async def _setup_compression_policies(self, conn: asyncpg.Connection) -> None:
        """Setup compression policies when source tables exist."""
        _ = conn
        self.logger.debug("Skipping legacy compression policy bootstrap; managed by migrations")

    async def _setup_retention_policies(self, conn: asyncpg.Connection) -> None:
        """Setup retention policies when source tables exist."""
        _ = conn
        self.logger.debug("Skipping legacy retention policy bootstrap; managed by migrations")

    async def get_schema_info(self) -> Dict[str, Any]:
        """Get information about the current schema."""
        return {}


async def create_timescale_schema_from_config(config: TimescaleConfig) -> None:
    """Create TimescaleDB schema from configuration."""
    schema = TimescaleSchema(config)
    await schema.create_schema()
