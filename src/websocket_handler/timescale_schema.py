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
            ADD COLUMN IF NOT EXISTS import_status TEXT
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

    async def _create_tables(self, conn: asyncpg.Connection) -> None:
        """Create all database tables."""

        # Charging sessions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_sessions (
                session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                connector_id INTEGER NOT NULL,
                vehicle_id VARCHAR(255),
                id_token VARCHAR(255),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ,
                start_soc_percent DECIMAL(5,2),
                end_soc_percent DECIMAL(5,2),
                energy_delivered_kwh DECIMAL(10,3),
                energy_received_kwh DECIMAL(10,3),
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

        # Telemetry data table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_data (
                time TIMESTAMPTZ NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                connector_id INTEGER NOT NULL,
                session_id UUID,
                power_kw DECIMAL(8,2),
                energy_kwh DECIMAL(10,3),
                voltage_v DECIMAL(6,2),
                current_a DECIMAL(8,2),
                frequency_hz DECIMAL(5,2),
                soc_percent DECIMAL(5,2),
                temperature_c DECIMAL(5,2),
                grid_frequency_mhz INTEGER,
                reactive_power_kvar DECIMAL(8,2),
                power_factor DECIMAL(3,2)
            );
        """)

        # Optimization decisions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS optimization_decisions (
                decision_id UUID NOT NULL DEFAULT gen_random_uuid(),
                time TIMESTAMPTZ NOT NULL,
                optimization_window_start TIMESTAMPTZ NOT NULL,
                optimization_window_end TIMESTAMPTZ NOT NULL,
                fleet_operator_id UUID,
                site_id UUID,
                algorithm_version VARCHAR(50),
                objective_function VARCHAR(100),
                objective_value DECIMAL(12,2),
                computation_time_ms INTEGER,
                constraints_satisfied BOOLEAN,
                decision_payload JSONB,
                sync_status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (decision_id, time)
            );
        """)

        # Vehicle routes table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_routes (
                route_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                vehicle_id VARCHAR(255) NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                departure_time TIMESTAMPTZ NOT NULL,
                arrival_time TIMESTAMPTZ,
                destination TEXT,
                route_distance_km DECIMAL(8,2),
                required_soc_percent DECIMAL(5,2) NOT NULL,
                actual_soc_percent DECIMAL(5,2),
                route_status VARCHAR(20) DEFAULT 'scheduled',
                route_priority INTEGER DEFAULT 1,
                estimated_duration_hours DECIMAL(4,2),
                override_type VARCHAR(20),
                override_reason TEXT,
                operator_id VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Vehicle fleet table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_fleet (
                vehicle_id VARCHAR(255) PRIMARY KEY,
                station_id VARCHAR(255) NOT NULL,
                battery_capacity_kwh DECIMAL(8,2) NOT NULL,
                max_charge_rate_kw DECIMAL(8,2) NOT NULL,
                max_discharge_rate_kw DECIMAL(8,2) NOT NULL,
                current_soc_kwh DECIMAL(8,2),
                min_soc_kwh DECIMAL(8,2) DEFAULT 15.0,
                charge_efficiency DECIMAL(3,2) DEFAULT 0.95,
                discharge_efficiency DECIMAL(3,2) DEFAULT 0.90,
                vehicle_type VARCHAR(50),
                make VARCHAR(100),
                model VARCHAR(100),
                year INTEGER,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Price forecasts table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS price_forecasts (
                forecast_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                forecast_time TIMESTAMPTZ NOT NULL,
                horizon_start TIMESTAMPTZ NOT NULL,
                horizon_end TIMESTAMPTZ NOT NULL,
                node_id VARCHAR(100) NOT NULL,
                market_type VARCHAR(20) NOT NULL,
                forecast_prices JSONB NOT NULL,
                confidence_intervals JSONB,
                model_version VARCHAR(50),
                accuracy_score DECIMAL(5,4),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Demand forecasts table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS demand_forecasts (
                forecast_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                forecast_time TIMESTAMPTZ NOT NULL,
                horizon_start TIMESTAMPTZ NOT NULL,
                horizon_end TIMESTAMPTZ NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                forecast_demand JSONB NOT NULL,
                confidence_intervals JSONB,
                model_version VARCHAR(50),
                accuracy_score DECIMAL(5,4),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Charging schedules table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_schedules (
                schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                decision_id UUID,
                profile_id VARCHAR(255) NOT NULL,
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ NOT NULL,
                schedule_periods JSONB NOT NULL,
                priority INTEGER DEFAULT 0,
                stacking_level INTEGER DEFAULT 0,
                purpose VARCHAR(50),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                executed_at TIMESTAMPTZ,
                execution_status VARCHAR(50)
            );
        """)

        # Electricity prices table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS electricity_prices (
                time TIMESTAMPTZ NOT NULL,
                node_id VARCHAR(100) NOT NULL,
                market_type VARCHAR(50) NOT NULL,
                lmp_price_mwh DECIMAL(10,2),
                energy_component_mwh DECIMAL(10,2),
                congestion_component_mwh DECIMAL(10,2),
                loss_component_mwh DECIMAL(10,2),
                ghg_adder_mwh DECIMAL(8,2),
                price_confidence DECIMAL(3,2),
                forecast_horizon_minutes INTEGER
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS electricity_price_forecasts (
                time TIMESTAMPTZ NOT NULL,
                node_id VARCHAR(100) NOT NULL,
                market_type VARCHAR(50) NOT NULL,
                forecast_start TIMESTAMPTZ NOT NULL,
                forecast_end TIMESTAMPTZ NOT NULL,
                forecast_interval_minutes INTEGER NOT NULL,
                lmp_price_mwh DECIMAL(10,2),
                confidence_low DECIMAL(10,2),
                confidence_high DECIMAL(10,2),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Grid signals table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_signals (
                time TIMESTAMPTZ NOT NULL,
                signal_type VARCHAR(50) NOT NULL,
                signal_value DECIMAL(10,4),
                target_response_kw DECIMAL(10,2),
                response_deadline TIMESTAMPTZ,
                compensation_rate_kwh DECIMAL(8,2),
                grid_operator VARCHAR(100),
                region VARCHAR(100)
            );
        """)

        # Vehicle telemetry table (for real-time vehicle state)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_telemetry (
                time TIMESTAMPTZ NOT NULL,
                vehicle_id UUID NOT NULL,
                organization_id UUID,
                current_soc DECIMAL(5,2),
                current_power_kw DECIMAL(8,2),
                charging_status VARCHAR(50),
                location_lat DECIMAL(10,8),
                location_lon DECIMAL(11,8),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # V2G-specific tables
        await self._create_v2g_tables(conn)

        self.logger.info("All tables created successfully")

    async def _create_v2g_tables(self, conn: asyncpg.Connection) -> None:
        """Create V2G-specific tables."""

        # Enhanced charging profiles table with V2G fields
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_profiles_v2g (
                profile_id INTEGER PRIMARY KEY,
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                stack_level INTEGER NOT NULL,
                purpose VARCHAR(50) NOT NULL,
                kind VARCHAR(50) NOT NULL,
                schedule JSONB NOT NULL,
                valid_from TIMESTAMPTZ,
                valid_to TIMESTAMPTZ,
                transaction_id VARCHAR(255),
                discharging_limit DECIMAL(8,2),
                setpoint DECIMAL(8,2),
                setpoint_reactive DECIMAL(8,2),
                operation_mode VARCHAR(50),
                v2x_freq_watt_curve JSONB,
                v2x_signal_watt_curve JSONB,
                v2x_baseline DECIMAL(8,2),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Enhanced EV charging needs table with V2G parameters
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ev_charging_needs_v2g (
                need_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                transaction_id VARCHAR(255),
                ev_maximum_discharge_power DECIMAL(8,2),
                ev_minimum_discharge_power DECIMAL(8,2),
                ev_maximum_charge_power DECIMAL(8,2),
                ev_minimum_charge_power DECIMAL(8,2),
                ev_target_energy_request DECIMAL(10,3),
                ev_maximum_energy_request DECIMAL(10,3),
                ev_minimum_energy_request DECIMAL(10,3),
                ev_present_active_power DECIMAL(8,2),
                ev_present_reactive_power DECIMAL(8,2),
                soc DECIMAL(5,2),
                capacity DECIMAL(10,3),
                control_mode VARCHAR(50),
                v2x_charging_parameters JSONB,
                der_charging_parameters JSONB,
                timestamp TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # DER controls table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS der_controls (
                control_id INTEGER PRIMARY KEY,
                station_id VARCHAR(255) NOT NULL,
                control_type VARCHAR(50) NOT NULL,
                priority INTEGER NOT NULL,
                start_time TIMESTAMPTZ NOT NULL,
                duration INTEGER,
                is_superseded BOOLEAN DEFAULT FALSE,
                is_default BOOLEAN DEFAULT FALSE,
                control_data JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # DER events table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS der_events (
                event_id UUID NOT NULL DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                control_id INTEGER,
                grid_event_fault VARCHAR(50),
                timestamp TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (event_id, timestamp)
            );
        """)

        # External charging limits table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS external_charging_limits (
                limit_id UUID NOT NULL DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                source VARCHAR(50) NOT NULL,
                is_grid_critical BOOLEAN DEFAULT FALSE,
                is_local_generation BOOLEAN DEFAULT FALSE,
                schedule_data JSONB,
                timestamp TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (limit_id, timestamp)
            );
        """)

        # Enhanced transaction events table with V2G fields
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transaction_events_v2g (
                event_id UUID NOT NULL DEFAULT gen_random_uuid(),
                transaction_id VARCHAR(255) NOT NULL,
                event_type VARCHAR(50) NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                connector_id INTEGER NOT NULL,
                charging_state VARCHAR(50),
                stopped_reason VARCHAR(50),
                remote_start_id VARCHAR(255),
                trigger_reason VARCHAR(50),
                is_v2g_event BOOLEAN DEFAULT FALSE,
                operation_mode VARCHAR(50),
                v2g_meter_data JSONB,
                offline BOOLEAN DEFAULT FALSE,
                number_of_phases_used INTEGER,
                cable_max_current DECIMAL(8,2),
                reservation_id VARCHAR(255),
                evse JSONB,
                id_token JSONB,
                certificate TEXT,
                iso15118_certificate_hash_data JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (event_id, timestamp)
            );
        """)

        # Enhanced telemetry data table with V2G measurands
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_data_v2g (
                time TIMESTAMPTZ NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                evse_id INTEGER NOT NULL,
                connector_id INTEGER NOT NULL,
                session_id UUID,
                power_kw DECIMAL(8,2),
                power_discharge_kw DECIMAL(8,2),
                power_setpoint_kw DECIMAL(8,2),
                power_residual_kw DECIMAL(8,2),
                energy_kwh DECIMAL(10,3),
                energy_discharged_kwh DECIMAL(10,3),
                voltage_v DECIMAL(6,2),
                current_a DECIMAL(8,2),
                current_discharge_a DECIMAL(8,2),
                frequency_hz DECIMAL(5,2),
                soc_percent DECIMAL(5,2),
                temperature_c DECIMAL(5,2),
                reactive_power_import_kvar DECIMAL(8,2),
                reactive_power_export_kvar DECIMAL(8,2),
                power_factor DECIMAL(3,2),
                operation_mode VARCHAR(50)
            );
        """)

        # Device model variables table for V2G components
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS device_model_variables_v2g (
                variable_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                component_name VARCHAR(100) NOT NULL,
                variable_name VARCHAR(100) NOT NULL,
                variable_type VARCHAR(50) NOT NULL,
                variable_access VARCHAR(50) NOT NULL,
                default_value TEXT,
                required BOOLEAN DEFAULT FALSE,
                value TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(station_id, component_name, variable_name)
            );
        """)

        # ISO 15118 certificates table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iso15118_certificates (
                certificate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                certificate_type VARCHAR(50) NOT NULL,
                certificate_data TEXT NOT NULL,
                certificate_chain JSONB,
                issuer_name VARCHAR(500),
                subject_name VARCHAR(500),
                serial_number VARCHAR(100),
                valid_from TIMESTAMPTZ,
                valid_to TIMESTAMPTZ,
                status VARCHAR(20) DEFAULT 'Valid',
                installation_date TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Authorization records table for Plug & Charge
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS authorization_records (
                auth_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                station_id VARCHAR(255) NOT NULL,
                emaid VARCHAR(255) NOT NULL,
                contract_certificate TEXT NOT NULL,
                charging_needs JSONB,
                authorization_time TIMESTAMPTZ DEFAULT NOW(),
                status VARCHAR(20) DEFAULT 'authorized',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        self.logger.info("V2G tables created successfully")

    async def _create_hypertable_safe(
        self, conn: asyncpg.Connection, table: str, sql: str
    ) -> None:
        """Attempt to create a hypertable, logging failures without aborting the rest."""
        try:
            await conn.execute(sql)
        except Exception as e:
            # For legacy deployments, certain tables may already have unique
            # indexes that are incompatible with Timescale hypertables. In
            # particular, optimization_decisions might have a unique index
            # that does not include the partitioning column (time), which
            # causes create_hypertable to fail with:
            # \"cannot create a unique index without the column \\\"time\\\"\".
            # In that case we log a clear warning but do not abort schema
            # creation; the table will still function as a regular table.
            if (
                table == "optimization_decisions"
                and "cannot create a unique index without the column \"time\"" in str(e)
            ):
                self.logger.warning(
                    "Skipping hypertable for 'optimization_decisions' because an existing "
                    "unique index does not include the partitioning column 'time'. "
                    "Consider dropping or altering the legacy unique index so that it "
                    "includes 'time' before enabling this hypertable."
                )
            else:
                self.logger.warning(f"Skipping hypertable for '{table}': {e}")

    async def _create_hypertables(self, conn: asyncpg.Connection) -> None:
        """Create TimescaleDB hypertables."""

        await self._create_hypertable_safe(conn, "telemetry_data", f"""
            SELECT create_hypertable('telemetry_data', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "optimization_decisions", f"""
            SELECT create_hypertable('optimization_decisions', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "electricity_prices", f"""
            SELECT create_hypertable('electricity_prices', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "electricity_price_forecasts", """
            SELECT create_hypertable('electricity_price_forecasts', 'time',
                create_default_indexes => FALSE,
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "grid_signals", f"""
            SELECT create_hypertable('grid_signals', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "vehicle_telemetry", f"""
            SELECT create_hypertable('vehicle_telemetry', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        # Create V2G hypertables
        await self._create_v2g_hypertables(conn)

        self.logger.info("All hypertables created successfully")

    async def _create_v2g_hypertables(self, conn: asyncpg.Connection) -> None:
        """Create V2G-specific hypertables."""

        await self._create_hypertable_safe(conn, "telemetry_data_v2g", f"""
            SELECT create_hypertable('telemetry_data_v2g', 'time',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "transaction_events_v2g", f"""
            SELECT create_hypertable('transaction_events_v2g', 'timestamp',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "der_events", f"""
            SELECT create_hypertable('der_events', 'timestamp',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        await self._create_hypertable_safe(conn, "external_charging_limits", f"""
            SELECT create_hypertable('external_charging_limits', 'timestamp',
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)

        self.logger.info("V2G hypertables created successfully")

    async def _create_indexes(self, conn: asyncpg.Connection) -> None:
        """Create database indexes for performance."""

        # Charging sessions indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_station_time 
            ON charging_sessions(station_id, start_time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_fleet 
            ON charging_sessions(fleet_operator_id, start_time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_sync_status 
            ON charging_sessions(sync_status, created_at);
        """)

        # Telemetry data indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_station 
            ON telemetry_data(station_id, time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_session 
            ON telemetry_data(session_id, time DESC);
        """)

        # Optimization decisions indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_optimization_fleet 
            ON optimization_decisions(fleet_operator_id, time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_optimization_sync_status 
            ON optimization_decisions(sync_status, created_at);
        """)

        # Vehicle routes indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_routes_vehicle 
            ON vehicle_routes(vehicle_id, departure_time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_routes_station 
            ON vehicle_routes(station_id, departure_time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_routes_status 
            ON vehicle_routes(route_status, created_at);
        """)

        # Vehicle fleet indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fleet_station 
            ON vehicle_fleet(station_id, is_active);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fleet_active 
            ON vehicle_fleet(is_active, updated_at DESC);
        """)

        # Price forecasts indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_forecasts_time 
            ON price_forecasts(forecast_time DESC, horizon_start);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_forecasts_node 
            ON price_forecasts(node_id, forecast_time DESC);
        """)

        # Demand forecasts indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_demand_forecasts_time 
            ON demand_forecasts(forecast_time DESC, horizon_start);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_demand_forecasts_station 
            ON demand_forecasts(station_id, forecast_time DESC);
        """)

        # Charging schedules indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_schedules_station 
            ON charging_schedules(station_id, start_time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_schedules_decision 
            ON charging_schedules(decision_id);
        """)

        # Electricity prices indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prices_node 
            ON electricity_prices(node_id, time DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_price_forecasts_node_time
            ON electricity_price_forecasts (node_id, time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prices_market 
            ON electricity_prices(market_type, time DESC);
        """)

        # Grid signals indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_signals_type 
            ON grid_signals(signal_type, time DESC);
        """)

        # Vehicle telemetry indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_vehicle 
            ON vehicle_telemetry(vehicle_id, time DESC);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_org 
            ON vehicle_telemetry(organization_id, time DESC);
        """)

        # V2G-specific indexes
        await self._create_v2g_indexes(conn)

        self.logger.info("All indexes created successfully")

    async def _create_v2g_indexes(self, conn: asyncpg.Connection) -> None:
        """Create V2G-specific indexes."""

        # Charging profiles V2G indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_charging_profiles_v2g_station 
            ON charging_profiles_v2g(station_id, evse_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_charging_profiles_v2g_purpose 
            ON charging_profiles_v2g(purpose, stack_level);
        """)

        # EV charging needs V2G indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ev_charging_needs_v2g_station 
            ON ev_charging_needs_v2g(station_id, evse_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ev_charging_needs_v2g_timestamp 
            ON ev_charging_needs_v2g(timestamp DESC);
        """)

        # DER controls indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_der_controls_station 
            ON der_controls(station_id, control_type);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_der_controls_priority 
            ON der_controls(priority, start_time);
        """)

        # DER events indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_der_events_station 
            ON der_events(station_id, event_type);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_der_events_timestamp 
            ON der_events(timestamp DESC);
        """)

        # External charging limits indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_external_limits_station 
            ON external_charging_limits(station_id, evse_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_external_limits_source 
            ON external_charging_limits(source, is_grid_critical);
        """)

        # Transaction events V2G indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_events_v2g_station 
            ON transaction_events_v2g(station_id, evse_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_events_v2g_transaction 
            ON transaction_events_v2g(transaction_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_events_v2g_v2g 
            ON transaction_events_v2g(is_v2g_event, trigger_reason);
        """)

        # Telemetry data V2G indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_v2g_station 
            ON telemetry_data_v2g(station_id, evse_id, time DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_v2g_session 
            ON telemetry_data_v2g(session_id, time DESC);
        """)

        # Device model variables V2G indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_model_v2g_station 
            ON device_model_variables_v2g(station_id, component_name);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_model_v2g_variable 
            ON device_model_variables_v2g(component_name, variable_name);
        """)

        # ISO 15118 certificates indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_certificates_station 
            ON iso15118_certificates(station_id, certificate_type);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_certificates_status 
            ON iso15118_certificates(status, valid_to);
        """)

        # Authorization records indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_auth_records_station 
            ON authorization_records(station_id, emaid);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_auth_records_time 
            ON authorization_records(authorization_time DESC);
        """)

        self.logger.info("V2G indexes created successfully")

    async def _create_continuous_aggregates(self, conn: asyncpg.Connection) -> None:
        """Create continuous aggregates for performance."""

        # Hourly energy aggregates
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_energy_aggregates
            WITH (timescaledb.continuous) AS
            SELECT 
                time_bucket('1 hour', time) AS hour,
                station_id,
                AVG(power_kw) AS avg_power_kw,
                MAX(power_kw) AS max_power_kw,
                MIN(power_kw) AS min_power_kw,
                SUM(CASE WHEN power_kw > 0 THEN power_kw * 1/120 ELSE 0 END) AS energy_charged_kwh,
                SUM(CASE WHEN power_kw < 0 THEN ABS(power_kw) * 1/120 ELSE 0 END) AS energy_discharged_kwh,
                AVG(soc_percent) AS avg_soc,
                COUNT(*) AS sample_count
            FROM telemetry_data
            GROUP BY hour, station_id;
        """)

        # Add continuous aggregate policy (idempotent)
        await conn.execute("""
            DO $$
            BEGIN
                PERFORM add_continuous_aggregate_policy('hourly_energy_aggregates',
                    start_offset => INTERVAL '3 hours',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour');
            EXCEPTION
                WHEN unique_violation THEN
                    NULL;
                WHEN duplicate_object THEN
                    NULL;
            END
            $$;
        """)

        # Daily fleet metrics
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS daily_fleet_metrics
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 day', cs.start_time) AS day,
                cs.fleet_operator_id,
                COUNT(DISTINCT cs.station_id) AS active_stations,
                COUNT(DISTINCT cs.session_id) AS total_sessions,
                SUM(cs.energy_delivered_kwh) AS total_energy_charged_kwh,
                SUM(cs.energy_received_kwh) AS total_energy_discharged_kwh,
                AVG(EXTRACT(EPOCH FROM (cs.end_time - cs.start_time))/3600) AS avg_session_duration_hours
            FROM charging_sessions cs
            WHERE cs.end_time IS NOT NULL
            GROUP BY day, cs.fleet_operator_id;
        """)

        # Add continuous aggregate policy for daily metrics (idempotent)
        await conn.execute("""
            DO $$
            BEGIN
                PERFORM add_continuous_aggregate_policy('daily_fleet_metrics',
                    start_offset => INTERVAL '3 days',
                    end_offset => INTERVAL '1 day',
                    schedule_interval => INTERVAL '1 day');
            EXCEPTION
                WHEN unique_violation THEN
                    NULL;
                WHEN duplicate_object THEN
                    NULL;
            END
            $$;
        """)

        # Hourly optimization performance
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_optimization_performance
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 hour', time) AS hour,
                fleet_operator_id,
                COUNT(*) AS total_decisions,
                AVG(computation_time_ms) AS avg_computation_time_ms,
                COUNT(CASE WHEN constraints_satisfied THEN 1 END) AS successful_decisions,
                AVG(objective_value) AS avg_objective_value
            FROM optimization_decisions
            GROUP BY hour, fleet_operator_id;
        """)

        # Add continuous aggregate policy for optimization performance (idempotent)
        await conn.execute("""
            DO $$
            BEGIN
                PERFORM add_continuous_aggregate_policy('hourly_optimization_performance',
                    start_offset => INTERVAL '3 hours',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour');
            EXCEPTION
                WHEN unique_violation THEN
                    NULL;
                WHEN duplicate_object THEN
                    NULL;
            END
            $$;
        """)

        self.logger.info("All continuous aggregates created successfully")

    async def _setup_compression_policies(self, conn: asyncpg.Connection) -> None:
        """Setup compression policies for older data."""

        # Enable compression on telemetry data
        await conn.execute("""
            ALTER TABLE telemetry_data SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'station_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)

        # Add compression policy
        await conn.execute(f"""
            SELECT add_compression_policy('telemetry_data', INTERVAL '{self.config.compression_after}');
        """)

        # Enable compression on electricity prices
        await conn.execute("""
            ALTER TABLE electricity_prices SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'node_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)

        # Add compression policy for prices
        await conn.execute("""
            SELECT add_compression_policy('electricity_prices', INTERVAL '30 days');
        """)
        await conn.execute("""
            SELECT add_compression_policy('electricity_price_forecasts', INTERVAL '30 days')
            ON CONFLICT DO NOTHING;
        """)

        # Enable compression on grid signals
        await conn.execute("""
            ALTER TABLE grid_signals SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'signal_type',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)

        # Add compression policy for grid signals
        await conn.execute("""
            SELECT add_compression_policy('grid_signals', INTERVAL '30 days');
        """)

        # Enable compression on vehicle telemetry
        await conn.execute("""
            ALTER TABLE vehicle_telemetry SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'vehicle_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)

        # Add compression policy for vehicle telemetry
        await conn.execute(f"""
            SELECT add_compression_policy('vehicle_telemetry', INTERVAL '{self.config.compression_after}');
        """)

        self.logger.info("All compression policies created successfully")

    async def _setup_retention_policies(self, conn: asyncpg.Connection) -> None:
        """Setup retention policies for data lifecycle management."""

        # Retention policy for telemetry data
        await conn.execute(f"""
            SELECT add_retention_policy('telemetry_data', INTERVAL '{self.config.retention_period}');
        """)

        # Retention policy for grid signals
        await conn.execute("""
            SELECT add_retention_policy('grid_signals', INTERVAL '1 year');
        """)

        # Retention policy for vehicle telemetry
        await conn.execute(f"""
            SELECT add_retention_policy('vehicle_telemetry', INTERVAL '{self.config.retention_period}');
        """)

        # Retention policy for electricity prices
        await conn.execute("""
            SELECT add_retention_policy('electricity_prices', INTERVAL '2 years');
        """)
        await conn.execute("""
            SELECT add_retention_policy('electricity_price_forecasts', INTERVAL '2 years')
            ON CONFLICT DO NOTHING;
        """)

        # Keep optimization decisions indefinitely for model training
        # Keep charging sessions indefinitely for billing and analytics
        # Keep electricity prices indefinitely for historical analysis

        self.logger.info("All retention policies created successfully")

    async def get_schema_info(self) -> Dict[str, Any]:
        """Get information about the current schema."""
        try:
            conn = await asyncpg.connect(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=self.config.sslmode,
            )

            try:
                # Get hypertables
                hypertables = await conn.fetch("""
                    SELECT hypertable_name, num_dimensions, num_chunks
                    FROM timescaledb_information.hypertables;
                """)

                # Get continuous aggregates
                continuous_aggregates = await conn.fetch("""
                    SELECT view_name, materialized_only, finalized
                    FROM timescaledb_information.continuous_aggregates;
                """)

                # Get compression policies
                compression_policies = await conn.fetch("""
                    SELECT hypertable_name, compress_after
                    FROM timescaledb_information.jobs
                    WHERE proc_name = 'policy_compression';
                """)

                # Get retention policies
                retention_policies = await conn.fetch("""
                    SELECT hypertable_name, drop_after
                    FROM timescaledb_information.jobs
                    WHERE proc_name = 'policy_retention';
                """)

                return {
                    "hypertables": [dict(row) for row in hypertables],
                    "continuous_aggregates": [dict(row) for row in continuous_aggregates],
                    "compression_policies": [dict(row) for row in compression_policies],
                    "retention_policies": [dict(row) for row in retention_policies],
                }

            finally:
                await conn.close()

        except Exception as e:
            self.logger.error(f"Failed to get schema info: {e}")
            raise


async def create_timescale_schema_from_config(config: TimescaleConfig) -> None:
    """Create TimescaleDB schema from configuration."""
    schema = TimescaleSchema(config)
    await schema.create_schema()


async def get_timescale_schema_info(config: TimescaleConfig) -> Dict[str, Any]:
    """Get TimescaleDB schema information."""
    schema = TimescaleSchema(config)
    return await schema.get_schema_info()


async def create_tables(client) -> None:
    """Create tables for testing - simplified version."""
    # This is a simplified version for testing
    # In real usage, use create_timescale_schema_from_config
    try:
        await client.connect()
        # Basic table creation for testing
        await client.execute("""
            CREATE TABLE IF NOT EXISTS charging_stations (
                id SERIAL PRIMARY KEY,
                station_id VARCHAR(255) UNIQUE NOT NULL,
                vendor_name VARCHAR(255),
                model VARCHAR(255),
                serial_number VARCHAR(255),
                firmware_version VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS charging_transactions (
                id SERIAL PRIMARY KEY,
                transaction_id VARCHAR(255) UNIQUE NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                connector_id INTEGER,
                start_time TIMESTAMPTZ,
                end_time TIMESTAMPTZ,
                energy_delivered DECIMAL(10,3),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    except Exception as e:
        print(f"Error creating tables: {e}")
        raise
