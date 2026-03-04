"""Supabase client for static/reference data access."""

import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg
from supabase import Client, create_client

from .config import SupabaseConfig
from .monitoring import get_logger


class SupabaseClient:
    """Client for accessing Supabase static/reference data.

    This client provides access to:
    - Depot configurations
    - Vehicle metadata (battery capacity, max_charge_kw, id_tag)
    - Charger metadata (rated_kw, connector_type, status)
    - Charger-vehicle accessibility matrix
    - Stationary battery configuration
    - Route schedules (static operational data)
    - User/organization data for authentication
    """

    def __init__(self, config: SupabaseConfig):
        """Initialize Supabase client."""
        self.config = config
        self.logger = get_logger(__name__)
        self.client: Optional[Client] = None
        self.db_pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Connect to Supabase."""
        try:
            # Initialize Supabase Python client
            self.client = create_client(self.config.url, self.config.service_key)

            # Initialize direct database connection pool for async operations
            self.db_pool = await asyncpg.create_pool(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
                min_size=2,
                max_size=self.config.max_connections,
                command_timeout=self.config.connection_timeout,
                # Disable per-connection prepared statement caching so that
                # PgBouncer in transaction/statement mode can be used safely.
                # Prepared statements are not compatible with these modes.
                statement_cache_size=0,
                max_cached_statement_lifetime=0,
            )

            self.logger.info("Supabase client connected successfully")
        except Exception as e:
            self.logger.error(f"Failed to connect to Supabase: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Supabase."""
        if self.db_pool:
            await self.db_pool.close()
            self.db_pool = None
        self.client = None
        self.logger.info("Supabase client disconnected")

    async def close(self) -> None:
        """Alias for disconnect."""
        await self.disconnect()

    async def reconnect(self) -> None:
        """Reconnect to Supabase with retry logic and jitter."""
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                await self.disconnect()
                await self.connect()
                self.logger.info("Supabase client reconnected successfully")
                return
            except Exception as e:
                if attempt == max_retries - 1:
                    self.logger.error(
                        f"Failed to reconnect to Supabase after {max_retries} attempts: {e}"
                    )
                    raise

                # Calculate delay with exponential backoff
                delay = base_delay * (2**attempt)
                # Add jitter: 50-100% of base delay to prevent connection storms
                delay *= 0.5 + random.random() * 0.5

                self.logger.warning(
                    f"Supabase reconnection attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

    async def health_check(self) -> bool:
        """Check Supabase connection health."""
        try:
            if not self.db_pool:
                return False
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchval("SELECT 1")
                return result == 1
        except Exception as e:
            self.logger.error(f"Supabase health check failed: {e}")
            return False

    async def fetch_one(self, query: str, *args) -> Optional[Dict[str, Any]]:
        """Execute a query and return one row."""
        if not self.db_pool:
            raise RuntimeError("Database pool not initialized")
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def fetch_all(self, query: str, *args) -> List[Dict[str, Any]]:
        """Execute a query and return all rows."""
        if not self.db_pool:
            raise RuntimeError("Database pool not initialized")
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    # Static data access methods

    async def get_active_routes(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get active route schedules from Supabase (schedules table)."""
        query = "SELECT * FROM schedules WHERE departure_time > NOW()"
        if depot_id:
            query = (
                "SELECT s.* FROM schedules s"
                " JOIN vehicles v ON v.vehicle_id = s.vehicle_id"
                " WHERE v.depot_id = $1 AND s.departure_time > NOW()"
            )
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_vehicles(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get vehicles from Supabase."""
        query = "SELECT * FROM vehicles"
        if depot_id:
            query += " WHERE depot_id = $1"
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_chargers(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get chargers from Supabase."""
        query = "SELECT * FROM chargers"
        if depot_id:
            query += " WHERE depot_id = $1"
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_depot_config(self, depot_id: str) -> Optional[Dict[str, Any]]:
        """Get depot configuration from Supabase."""
        query = "SELECT * FROM depots WHERE depot_id = $1"
        return await self.fetch_one(query, depot_id)

    async def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        """Get organization by ID."""
        # Using Supabase client for table access
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("organizations").select("*").eq("id", org_id).execute()
        return response.data[0] if response.data else None

    async def update_organization(
        self, org_id: str, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Update organization."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("organizations").update(data).eq("id", org_id).execute()
        return response.data[0] if response.data else None

    async def get_vehicles_by_organization(self, org_id: str) -> List[Dict[str, Any]]:
        """Get vehicles by organization."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("vehicles").select("*").eq("organization_id", org_id).execute()
        return response.data if response.data else []

    async def create_vehicle(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a vehicle."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("vehicles").insert(data).execute()
        return response.data[0] if response.data else {}

    async def update_vehicle_status(self, vehicle_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Update vehicle status."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("vehicles").update({"status": status}).eq("id", vehicle_id).execute()
        )
        return response.data[0] if response.data else None

    async def create_charging_station(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a charging station."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("charging_stations").insert(data).execute()
        return response.data[0] if response.data else {}

    async def update_station_status(self, station_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Update station status."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("charging_stations")
            .update({"status": status})
            .eq("id", station_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_active_sessions(self, org_id: str) -> List[Dict[str, Any]]:
        """Get active charging sessions."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("charging_sessions_active")
            .select("*")
            .eq("organization_id", org_id)
            .execute()
        )
        return response.data if response.data else []

    async def update_session_status(self, session_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Update session status."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("charging_sessions_active")
            .update({"status": status})
            .eq("id", session_id)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_schedule_configs(self, org_id: str) -> List[Dict[str, Any]]:
        """Get schedule configurations."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("charging_schedules_config")
            .select("*")
            .eq("organization_id", org_id)
            .execute()
        )
        return response.data if response.data else []

    async def create_schedule_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create schedule configuration."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = self.client.table("charging_schedules_config").insert(data).execute()
        return response.data[0] if response.data else {}

    async def get_daily_energy_summary(
        self, org_id: str, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Get daily energy summary."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        response = (
            self.client.table("daily_energy_summary")
            .select("*")
            .eq("organization_id", org_id)
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .execute()
        )
        return response.data if response.data else []

    async def calculate_savings(
        self, org_id: str, start_date: datetime, end_date: datetime
    ) -> Dict[str, Any]:
        """Calculate savings."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        # Placeholder implementation
        return {"total_savings": 0.0, "energy_savings_kwh": 0.0, "cost_savings_usd": 0.0}

    async def sync_session_summaries(self, session_data: List[Dict[str, Any]]) -> None:
        """Sync session summaries to Supabase."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        if session_data:
            self.client.table("charging_sessions_summary").upsert(session_data).execute()

    async def sync_vehicle_states(self, state_data: List[Dict[str, Any]]) -> None:
        """Sync vehicle states to Supabase."""
        if not self.client:
            raise RuntimeError("Supabase client not initialized")
        if state_data:
            self.client.table("vehicle_states").upsert(state_data).execute()
