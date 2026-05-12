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
