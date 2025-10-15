"""Supabase client for database operations and real-time subscriptions."""

import asyncio
import json
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone
import asyncpg
from supabase import create_client, Client
from supabase._async.client import AsyncClient

from .config import SupabaseConfig
from .monitoring import get_logger


class SupabaseClient:
    """Supabase client for database operations."""
    
    def __init__(self, config: SupabaseConfig):
        """Initialize Supabase client."""
        self.config = config
        self.logger = get_logger(__name__)
        
        # Supabase client for API operations
        self.client: Optional[Client] = None
        self.async_client: Optional[AsyncClient] = None
        
        # Direct PostgreSQL connection for complex queries
        self.pg_pool: Optional[asyncpg.Pool] = None
        
        # Connection state
        self.connected = False
        
    async def connect(self) -> None:
        """Establish connections to Supabase with retry logic."""
        await self._connect_with_retry()
    
    async def _connect_with_retry(self, max_retries: int = 3, base_delay: float = 1.0) -> None:
        """Connect with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                await self._establish_connections()
                await self._test_connections()
                
                self.connected = True
                self.logger.info("Supabase client connected successfully")
                return
                
            except Exception as e:
                if attempt == max_retries - 1:
                    self.logger.error(f"Failed to connect to Supabase after {max_retries} attempts: {e}")
                    raise
                
                delay = base_delay * (2 ** attempt)
                self.logger.warning(f"Supabase connection attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s")
                await asyncio.sleep(delay)
    
    async def _establish_connections(self) -> None:
        """Establish the actual Supabase connections."""
        # Initialize Supabase client
        self.client = create_client(
            self.config.url,
            self.config.service_key
        )
        
        # Initialize async client for real-time subscriptions
        self.async_client = AsyncClient(
            self.config.url,
            self.config.service_key
        )
        
        # Create PostgreSQL connection pool
        self.pg_pool = await asyncpg.create_pool(
            host=self.config.db_host,
            port=self.config.db_port,
            database=self.config.db_name,
            user=self.config.db_user,
            password=self.config.db_password,
            min_size=1,
            max_size=self.config.max_connections,
            command_timeout=self.config.connection_timeout,
        )
    
    async def disconnect(self) -> None:
        """Close all connections."""
        if self.pg_pool:
            await self.pg_pool.close()
        
        if self.async_client:
            await self.async_client.close()
        
        self.connected = False
        self.logger.info("Supabase client disconnected")
    
    async def health_check(self) -> bool:
        """Check connection health."""
        try:
            if not self.connected or not self.pg_pool:
                return False
            
            # Test PostgreSQL connection
            async with self.pg_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            
            # Test Supabase REST client
            if self.client:
                # Simple test query
                response = self.client.table('_supabase_migrations').select('*').limit(1).execute()
                # If we get here without exception, connection is healthy
            
            return True
        except Exception as e:
            self.logger.warning(f"Supabase health check failed: {e}")
            return False
    
    async def reconnect(self) -> None:
        """Reconnect to Supabase."""
        self.logger.info("Attempting to reconnect to Supabase")
        await self.disconnect()
        await self.connect()
    
    async def _test_connections(self) -> None:
        """Test all connections."""
        # Test Supabase API
        if self.client:
            response = self.client.table('organizations').select('id').limit(1).execute()
            if response.data is None:
                raise Exception("Supabase API connection failed")
        
        # Test PostgreSQL connection
        if self.pg_pool:
            async with self.pg_pool.acquire() as conn:
                await conn.fetchval('SELECT 1')
    
    # Organization Management
    async def create_organization(self, org_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new organization."""
        try:
            response = self.client.table('organizations').insert(org_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create organization: {e}")
            raise
    
    async def get_organization(self, org_id: str) -> Optional[Dict[str, Any]]:
        """Get organization by ID."""
        try:
            response = self.client.table('organizations').select('*').eq('id', org_id).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            self.logger.error(f"Failed to get organization: {e}")
            raise
    
    async def update_organization(self, org_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update organization."""
        try:
            response = self.client.table('organizations').update(updates).eq('id', org_id).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to update organization: {e}")
            raise
    
    # Vehicle Management
    async def create_vehicle(self, vehicle_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new vehicle."""
        try:
            response = self.client.table('vehicles').insert(vehicle_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create vehicle: {e}")
            raise
    
    async def get_vehicles_by_organization(self, org_id: str) -> List[Dict[str, Any]]:
        """Get all vehicles for an organization."""
        try:
            response = self.client.table('vehicles').select('*').eq('organization_id', org_id).execute()
            return response.data or []
        except Exception as e:
            self.logger.error(f"Failed to get vehicles: {e}")
            raise
    
    async def update_vehicle_status(self, vehicle_id: str, status: str) -> Dict[str, Any]:
        """Update vehicle status."""
        try:
            updates = {
                'status': status,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            response = self.client.table('vehicles').update(updates).eq('id', vehicle_id).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to update vehicle status: {e}")
            raise
    
    # Charging Station Management
    async def create_charging_station(self, station_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new charging station."""
        try:
            response = self.client.table('charging_stations').insert(station_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create charging station: {e}")
            raise
    
    async def get_stations_by_site(self, site_id: str) -> List[Dict[str, Any]]:
        """Get all charging stations for a site."""
        try:
            response = self.client.table('charging_stations').select('*').eq('site_id', site_id).execute()
            return response.data or []
        except Exception as e:
            self.logger.error(f"Failed to get charging stations: {e}")
            raise
    
    async def update_station_status(self, station_id: str, status: str) -> Dict[str, Any]:
        """Update charging station status."""
        try:
            updates = {
                'status': status,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            response = self.client.table('charging_stations').update(updates).eq('id', station_id).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to update station status: {e}")
            raise
    
    # Charging Sessions
    async def create_charging_session(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new charging session."""
        try:
            response = self.client.table('charging_sessions_summary').insert(session_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create charging session: {e}")
            raise
    
    async def get_active_sessions(self, org_id: str) -> List[Dict[str, Any]]:
        """Get active charging sessions for an organization."""
        try:
            response = self.client.table('charging_sessions_active').select('*').eq('organization_id', org_id).execute()
            return response.data or []
        except Exception as e:
            self.logger.error(f"Failed to get active sessions: {e}")
            raise
    
    async def update_session_status(self, session_id: str, status: str, updates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Update charging session status."""
        try:
            update_data = {
                'status': status,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            if updates:
                update_data.update(updates)
            
            response = self.client.table('charging_sessions_active').update(update_data).eq('id', session_id).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to update session status: {e}")
            raise
    
    # Schedule Management
    async def create_schedule_config(self, config_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a charging schedule configuration."""
        try:
            response = self.client.table('charging_schedules_config').insert(config_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create schedule config: {e}")
            raise
    
    async def get_schedule_configs(self, org_id: str) -> List[Dict[str, Any]]:
        """Get all schedule configurations for an organization."""
        try:
            response = self.client.table('charging_schedules_config').select('*').eq('organization_id', org_id).execute()
            return response.data or []
        except Exception as e:
            self.logger.error(f"Failed to get schedule configs: {e}")
            raise
    
    async def create_vehicle_schedule(self, schedule_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a vehicle schedule."""
        try:
            response = self.client.table('vehicle_schedules').insert(schedule_data).execute()
            return response.data[0] if response.data else {}
        except Exception as e:
            self.logger.error(f"Failed to create vehicle schedule: {e}")
            raise
    
    # Analytics and Reporting
    async def get_daily_energy_summary(self, org_id: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Get daily energy summary for an organization."""
        try:
            response = self.client.table('daily_energy_summary').select('*').eq('organization_id', org_id).gte('date', start_date).lte('date', end_date).execute()
            return response.data or []
        except Exception as e:
            self.logger.error(f"Failed to get daily energy summary: {e}")
            raise
    
    async def calculate_savings(self, org_id: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """Calculate cost savings for an organization."""
        try:
            if not self.pg_pool:
                raise Exception("PostgreSQL connection not available")
            
            async with self.pg_pool.acquire() as conn:
                result = await conn.fetchrow(
                    "SELECT * FROM calculate_savings($1, $2, $3)",
                    org_id, start_date, end_date
                )
                return dict(result) if result else {}
        except Exception as e:
            self.logger.error(f"Failed to calculate savings: {e}")
            raise
    
    # Real-time Subscriptions
    async def subscribe_to_vehicle_updates(self, vehicle_id: str, callback) -> None:
        """Subscribe to real-time vehicle updates."""
        if not self.async_client or not self.config.enable_realtime:
            self.logger.warning("Real-time subscriptions not enabled")
            return
        
        try:
            channel = self.async_client.channel(f'vehicle:{vehicle_id}')
            
            channel.on(
                'postgres_changes',
                {
                    'event': 'UPDATE',
                    'schema': 'public',
                    'table': 'vehicle_realtime_state',
                    'filter': f'vehicle_id=eq.{vehicle_id}'
                },
                callback
            )
            
            await channel.subscribe()
            self.logger.info(f"Subscribed to vehicle updates: {vehicle_id}")
            
        except Exception as e:
            self.logger.error(f"Failed to subscribe to vehicle updates: {e}")
            raise
    
    async def subscribe_to_fleet_updates(self, org_id: str, callback) -> None:
        """Subscribe to fleet-wide updates."""
        if not self.async_client or not self.config.enable_realtime:
            self.logger.warning("Real-time subscriptions not enabled")
            return
        
        try:
            channel = self.async_client.channel(f'fleet:{org_id}')
            
            # Vehicle state changes
            channel.on(
                'postgres_changes',
                {
                    'event': '*',
                    'schema': 'public',
                    'table': 'vehicle_realtime_state',
                    'filter': f'organization_id=eq.{org_id}'
                },
                callback
            )
            
            # New charging sessions
            channel.on(
                'postgres_changes',
                {
                    'event': 'INSERT',
                    'schema': 'public',
                    'table': 'charging_sessions_active',
                    'filter': f'organization_id=eq.{org_id}'
                },
                callback
            )
            
            await channel.subscribe()
            self.logger.info(f"Subscribed to fleet updates: {org_id}")
            
        except Exception as e:
            self.logger.error(f"Failed to subscribe to fleet updates: {e}")
            raise
    
    # Data Synchronization
    async def sync_session_summaries(self, sessions: List[Dict[str, Any]]) -> None:
        """Sync completed sessions from TimescaleDB to Supabase."""
        if not sessions:
            return
        
        try:
            # Bulk upsert to Supabase
            response = self.client.table('charging_sessions_summary').upsert(sessions).execute()
            
            if response.data:
                self.logger.info(f"Synced {len(response.data)} session summaries")
            else:
                self.logger.warning("No data returned from session sync")
                
        except Exception as e:
            self.logger.error(f"Failed to sync session summaries: {e}")
            raise
    
    async def sync_vehicle_states(self, states: List[Dict[str, Any]]) -> None:
        """Sync vehicle states to Supabase."""
        if not states:
            return
        
        try:
            # Bulk upsert to Supabase
            response = self.client.table('vehicle_realtime_state').upsert(states).execute()
            
            if response.data:
                self.logger.info(f"Synced {len(response.data)} vehicle states")
            else:
                self.logger.warning("No data returned from vehicle state sync")
                
        except Exception as e:
            self.logger.error(f"Failed to sync vehicle states: {e}")
            raise
    
    # Health Check
    async def health_check(self) -> Dict[str, Any]:
        """Check Supabase connection health."""
        try:
            if not self.connected:
                return {"status": "disconnected", "error": "Not connected"}
            
            # Test Supabase API
            api_status = "healthy"
            try:
                self.client.table('organizations').select('id').limit(1).execute()
            except Exception as e:
                api_status = f"unhealthy: {str(e)}"
            
            # Test PostgreSQL connection
            pg_status = "healthy"
            try:
                if self.pg_pool:
                    async with self.pg_pool.acquire() as conn:
                        await conn.fetchval('SELECT 1')
                else:
                    pg_status = "no connection pool"
            except Exception as e:
                pg_status = f"unhealthy: {str(e)}"
            
            return {
                "status": "healthy" if api_status == "healthy" and pg_status == "healthy" else "degraded",
                "api": api_status,
                "postgresql": pg_status,
                "realtime_enabled": self.config.enable_realtime
            }
            
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
