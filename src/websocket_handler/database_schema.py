"""Database schema creation and migration scripts for Supabase."""

import json
from typing import Any, Dict

import asyncpg

from .config import SupabaseConfig
from .monitoring import get_logger


class DatabaseSchema:
    """Database schema management for Supabase."""

    def __init__(self, config: SupabaseConfig):
        """Initialize database schema manager."""
        self.config = config
        self.logger = get_logger(__name__)

    async def create_schema(self) -> None:
        """Create all database tables and functions."""
        try:
            # Connect to database
            conn = await asyncpg.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
            )

            try:
                # Create tables
                await self._create_tables(conn)

                # Create indexes
                await self._create_indexes(conn)

                # Create functions
                await self._create_functions(conn)

                # Create views
                await self._create_views(conn)

                # Setup RLS policies
                await self._setup_rls_policies(conn)

                self.logger.info("Database schema created successfully")

            finally:
                await conn.close()

        except Exception as e:
            self.logger.error(f"Failed to create database schema: {e}")
            raise

    async def _create_tables(self, conn: asyncpg.Connection) -> None:
        """Create all database tables."""

        # Enable PostGIS for geographic types used in sites and vehicle_realtime_state
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        except Exception as e:
            self.logger.warning(f"PostGIS extension unavailable, geography columns will fail: {e}")

        # Organizations table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS organizations (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                type VARCHAR(50) CHECK (type IN ('fleet_operator', 'site_owner', 'utility')),
                billing_address JSONB,
                primary_contact JSONB,
                subscription_tier VARCHAR(50) DEFAULT 'starter',
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # User organizations junction table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_organizations (
                user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
                organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
                role VARCHAR(50) CHECK (role IN ('owner', 'admin', 'operator', 'viewer')),
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, organization_id)
            );
        """)

        # Sites table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sites (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                address JSONB NOT NULL,
                location GEOGRAPHY(POINT, 4326),
                utility_account_number VARCHAR(255),
                rate_schedule VARCHAR(100),
                max_power_kw DECIMAL(10,2),
                caiso_node_id VARCHAR(100),
                metadata JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Vehicles table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
                vin VARCHAR(17) UNIQUE NOT NULL,
                make VARCHAR(50),
                model VARCHAR(50),
                year INTEGER,
                battery_capacity_kwh DECIMAL(6,2),
                max_charge_rate_kw DECIMAL(6,2),
                max_discharge_rate_kw DECIMAL(6,2),
                v2g_capable BOOLEAN DEFAULT false,
                license_plate VARCHAR(20),
                driver_id UUID REFERENCES auth.users(id),
                status VARCHAR(50) DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Charging stations table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_stations (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                site_id UUID REFERENCES sites(id) ON DELETE CASCADE,
                station_id VARCHAR(255) UNIQUE NOT NULL,
                manufacturer VARCHAR(100),
                model VARCHAR(100),
                serial_number VARCHAR(255),
                firmware_version VARCHAR(50),
                ocpp_version VARCHAR(20),
                max_power_kw DECIMAL(8,2),
                connector_type VARCHAR(50),
                bidirectional BOOLEAN DEFAULT false,
                installation_date DATE,
                last_maintenance_date DATE,
                status VARCHAR(50) DEFAULT 'operational',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Charging schedules configuration
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_schedules_config (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                schedule_type VARCHAR(50) CHECK (schedule_type IN ('smart', 'time_of_use', 'solar_match', 'manual')),
                parameters JSONB NOT NULL,
                is_default BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Vehicle schedules
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_schedules (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
                schedule_config_id UUID REFERENCES charging_schedules_config(id),
                departure_time TIME,
                departure_soc_requirement DECIMAL(5,2),
                days_of_week INTEGER[],
                priority INTEGER DEFAULT 5,
                override_until TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Charging sessions summary (synced from TimescaleDB)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_sessions_summary (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                session_id VARCHAR(255) UNIQUE NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                vehicle_id UUID REFERENCES vehicles(id),
                organization_id UUID REFERENCES organizations(id),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ,
                energy_delivered_kwh DECIMAL(10,3),
                energy_received_kwh DECIMAL(10,3),
                session_duration_minutes INTEGER,
                cost_total DECIMAL(10,2),
                revenue_v2g DECIMAL(10,2),
                status VARCHAR(50) DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Active charging sessions (real-time state)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_sessions_active (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                session_id VARCHAR(255) UNIQUE NOT NULL,
                station_id VARCHAR(255) NOT NULL,
                vehicle_id UUID REFERENCES vehicles(id),
                organization_id UUID REFERENCES organizations(id),
                start_time TIMESTAMPTZ NOT NULL,
                current_power_kw DECIMAL(8,2),
                current_soc DECIMAL(5,2),
                target_soc DECIMAL(5,2),
                estimated_end_time TIMESTAMPTZ,
                status VARCHAR(50) DEFAULT 'charging',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Vehicle real-time state
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_realtime_state (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
                organization_id UUID REFERENCES organizations(id),
                current_soc DECIMAL(5,2),
                current_power_kw DECIMAL(8,2),
                charging_status VARCHAR(50),
                location GEOGRAPHY(POINT, 4326),
                last_seen TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # API usage tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                organization_id UUID REFERENCES organizations(id),
                endpoint VARCHAR(255),
                method VARCHAR(10),
                response_code INTEGER,
                response_time_ms INTEGER,
                timestamp TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        self.logger.info("All tables created successfully")

    async def _create_indexes(self, conn: asyncpg.Connection) -> None:
        """Create database indexes for performance."""

        # Spatial index for sites
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sites_location 
            ON sites USING GIST(location);
        """)

        # Organization indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicles_org 
            ON vehicles(organization_id, status);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stations_site 
            ON charging_stations(site_id, status);
        """)

        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stations_ocpp_id 
            ON charging_stations(station_id);
        """)

        # Schedule indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_schedules 
            ON vehicle_schedules(vehicle_id, days_of_week);
        """)

        # Session indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_org_time 
            ON charging_sessions_summary(organization_id, start_time);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_sessions_org 
            ON charging_sessions_active(organization_id, status);
        """)

        # Real-time state indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_state_org 
            ON vehicle_realtime_state(organization_id, last_seen);
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_state_location 
            ON vehicle_realtime_state USING GIST(location);
        """)

        # API usage indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_usage_org_time 
            ON api_usage(organization_id, timestamp);
        """)

        self.logger.info("All indexes created successfully")

    async def _create_functions(self, conn: asyncpg.Connection) -> None:
        """Create database functions."""

        # Function to calculate cost savings
        await conn.execute("""
            CREATE OR REPLACE FUNCTION calculate_savings(
                org_id UUID,
                start_date DATE,
                end_date DATE
            ) RETURNS TABLE (
                total_saved DECIMAL,
                v2g_revenue DECIMAL,
                demand_charge_reduction DECIMAL,
                tou_optimization_savings DECIMAL
            ) AS $$
            BEGIN
                RETURN QUERY
                SELECT 
                    COALESCE(SUM(baseline_cost - actual_cost), 0) as total_saved,
                    COALESCE(SUM(v2g_revenue), 0) as v2g_revenue,
                    COALESCE(SUM(demand_charge_baseline - demand_charge_actual), 0) as demand_charge_reduction,
                    COALESCE(SUM(tou_savings), 0) as tou_optimization_savings
                FROM (
                    SELECT 
                        CASE 
                            WHEN energy_delivered_kwh > 0 THEN energy_delivered_kwh * 0.15 -- baseline rate
                            ELSE 0 
                        END as baseline_cost,
                        COALESCE(cost_total, 0) as actual_cost,
                        COALESCE(revenue_v2g, 0) as v2g_revenue,
                        0 as demand_charge_baseline,
                        0 as demand_charge_actual,
                        0 as tou_savings
                    FROM charging_sessions_summary
                    WHERE organization_id = org_id
                        AND DATE(start_time) BETWEEN start_date AND end_date
                ) cost_analysis;
            END;
            $$ LANGUAGE plpgsql SECURITY DEFINER;
        """)

        # Function to update timestamps
        await conn.execute("""
            CREATE OR REPLACE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)

        # Create triggers for updated_at
        tables = [
            "organizations",
            "sites",
            "vehicles",
            "charging_stations",
            "charging_schedules_config",
            "vehicle_schedules",
            "charging_sessions_summary",
            "charging_sessions_active",
            "vehicle_realtime_state",
        ]

        for table in tables:
            await conn.execute(f"""
                DROP TRIGGER IF EXISTS update_{table}_updated_at ON {table};
                CREATE TRIGGER update_{table}_updated_at
                    BEFORE UPDATE ON {table}
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();
            """)

        self.logger.info("All functions and triggers created successfully")

    async def _create_views(self, conn: asyncpg.Connection) -> None:
        """Create database views."""

        # Daily energy summary view
        await conn.execute("""
            CREATE OR REPLACE VIEW daily_energy_summary AS
            SELECT 
                o.id as organization_id,
                o.name as organization_name,
                DATE(s.start_time) as date,
                COUNT(DISTINCT s.vehicle_id) as vehicles_charged,
                SUM(s.energy_delivered_kwh) as total_energy_charged,
                SUM(s.energy_received_kwh) as total_energy_discharged,
                AVG(s.session_duration_minutes) as avg_session_duration,
                SUM(s.cost_total) as total_cost,
                SUM(s.revenue_v2g) as total_v2g_revenue
            FROM organizations o
            LEFT JOIN charging_sessions_summary s ON o.id = s.organization_id
            GROUP BY o.id, o.name, DATE(s.start_time);
        """)

        # Fleet overview view
        await conn.execute("""
            CREATE OR REPLACE VIEW fleet_overview AS
            SELECT 
                o.id as organization_id,
                o.name as organization_name,
                COUNT(DISTINCT v.id) as total_vehicles,
                COUNT(DISTINCT CASE WHEN v.status = 'active' THEN v.id END) as active_vehicles,
                COUNT(DISTINCT CASE WHEN v.v2g_capable THEN v.id END) as v2g_capable_vehicles,
                COUNT(DISTINCT cs.id) as total_stations,
                COUNT(DISTINCT CASE WHEN cs.status = 'operational' THEN cs.id END) as operational_stations,
                COUNT(DISTINCT CASE WHEN csa.status = 'charging' THEN csa.id END) as active_sessions
            FROM organizations o
            LEFT JOIN vehicles v ON o.id = v.organization_id
            LEFT JOIN charging_stations cs ON o.id = cs.site_id
            LEFT JOIN charging_sessions_active csa ON o.id = csa.organization_id
            GROUP BY o.id, o.name;
        """)

        # Grant access to views
        await conn.execute("""
            GRANT SELECT ON daily_energy_summary TO authenticated;
            GRANT SELECT ON fleet_overview TO authenticated;
        """)

        self.logger.info("All views created successfully")

    async def _setup_rls_policies(self, conn: asyncpg.Connection) -> None:
        """Setup Row Level Security policies."""

        # Enable RLS on all tables
        tables = [
            "organizations",
            "user_organizations",
            "sites",
            "vehicles",
            "charging_stations",
            "charging_schedules_config",
            "vehicle_schedules",
            "charging_sessions_summary",
            "charging_sessions_active",
            "vehicle_realtime_state",
            "api_usage",
        ]

        for table in tables:
            await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")

        # Organizations policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Users can view their organization" ON organizations;
            CREATE POLICY "Users can view their organization"
                ON organizations FOR SELECT
                USING (id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        # User organizations policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Users can view their memberships" ON user_organizations;
            CREATE POLICY "Users can view their memberships"
                ON user_organizations FOR SELECT
                USING (user_id = auth.uid());
        """)

        # Sites policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view sites" ON sites;
            CREATE POLICY "Organization members can view sites"
                ON sites FOR SELECT
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        # Vehicles policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view vehicles" ON vehicles;
            CREATE POLICY "Organization members can view vehicles"
                ON vehicles FOR SELECT
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        await conn.execute("""
            DROP POLICY IF EXISTS "Admins can manage vehicles" ON vehicles;
            CREATE POLICY "Admins can manage vehicles"
                ON vehicles FOR ALL
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid() AND role IN ('owner', 'admin')
                ));
        """)

        # Charging stations policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view stations" ON charging_stations;
            CREATE POLICY "Organization members can view stations"
                ON charging_stations FOR SELECT
                USING (site_id IN (
                    SELECT s.id FROM sites s
                    JOIN user_organizations uo ON s.organization_id = uo.organization_id
                    WHERE uo.user_id = auth.uid()
                ));
        """)

        # Session policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view sessions" ON charging_sessions_summary;
            CREATE POLICY "Organization members can view sessions"
                ON charging_sessions_summary FOR SELECT
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view active sessions" ON charging_sessions_active;
            CREATE POLICY "Organization members can view active sessions"
                ON charging_sessions_active FOR SELECT
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        # Vehicle state policies
        await conn.execute("""
            DROP POLICY IF EXISTS "Organization members can view vehicle state" ON vehicle_realtime_state;
            CREATE POLICY "Organization members can view vehicle state"
                ON vehicle_realtime_state FOR SELECT
                USING (organization_id IN (
                    SELECT organization_id FROM user_organizations 
                    WHERE user_id = auth.uid()
                ));
        """)

        self.logger.info("All RLS policies created successfully")

    async def migrate_data(self, migration_data: Dict[str, Any]) -> None:
        """Migrate data from external sources."""
        try:
            conn = await asyncpg.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
            )

            try:
                # Migrate organizations
                if "organizations" in migration_data:
                    for org in migration_data["organizations"]:
                        await conn.execute(
                            """
                            INSERT INTO organizations (id, name, type, billing_address, primary_contact, subscription_tier)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (id) DO UPDATE SET
                                name = EXCLUDED.name,
                                type = EXCLUDED.type,
                                billing_address = EXCLUDED.billing_address,
                                primary_contact = EXCLUDED.primary_contact,
                                subscription_tier = EXCLUDED.subscription_tier,
                                updated_at = NOW();
                        """,
                            org["id"],
                            org["name"],
                            org["type"],
                            json.dumps(org.get("billing_address", {})),
                            json.dumps(org.get("primary_contact", {})),
                            org.get("subscription_tier", "starter"),
                        )

                # Migrate vehicles
                if "vehicles" in migration_data:
                    for vehicle in migration_data["vehicles"]:
                        await conn.execute(
                            """
                            INSERT INTO vehicles (id, organization_id, vin, make, model, year, 
                                                battery_capacity_kwh, max_charge_rate_kw, max_discharge_rate_kw, 
                                                v2g_capable, license_plate, status)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                            ON CONFLICT (vin) DO UPDATE SET
                                make = EXCLUDED.make,
                                model = EXCLUDED.model,
                                year = EXCLUDED.year,
                                battery_capacity_kwh = EXCLUDED.battery_capacity_kwh,
                                max_charge_rate_kw = EXCLUDED.max_charge_rate_kw,
                                max_discharge_rate_kw = EXCLUDED.max_discharge_rate_kw,
                                v2g_capable = EXCLUDED.v2g_capable,
                                license_plate = EXCLUDED.license_plate,
                                status = EXCLUDED.status,
                                updated_at = NOW();
                        """,
                            vehicle["id"],
                            vehicle["organization_id"],
                            vehicle["vin"],
                            vehicle.get("make"),
                            vehicle.get("model"),
                            vehicle.get("year"),
                            vehicle.get("battery_capacity_kwh"),
                            vehicle.get("max_charge_rate_kw"),
                            vehicle.get("max_discharge_rate_kw"),
                            vehicle.get("v2g_capable", False),
                            vehicle.get("license_plate"),
                            vehicle.get("status", "active"),
                        )

                self.logger.info("Data migration completed successfully")

            finally:
                await conn.close()

        except Exception as e:
            self.logger.error(f"Failed to migrate data: {e}")
            raise


async def create_schema_from_config(config: SupabaseConfig) -> None:
    """Create database schema from configuration."""
    schema = DatabaseSchema(config)
    await schema.create_schema()


async def migrate_data_from_config(config: SupabaseConfig, data: Dict[str, Any]) -> None:
    """Migrate data from configuration."""
    schema = DatabaseSchema(config)
    await schema.migrate_data(data)
