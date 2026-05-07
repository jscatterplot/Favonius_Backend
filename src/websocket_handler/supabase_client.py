"""Supabase client for static/reference data access."""

import asyncio
import hmac
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

try:
    from supabase import Client, create_client
except ImportError:  # pragma: no cover - keep test imports working without optional deps
    Client = Any  # type: ignore[misc,assignment]

    def create_client(*args, **kwargs):  # type: ignore[no-redef]
        raise RuntimeError(
            "supabase package is unavailable. Install optional dependencies to enable Supabase access."
        )


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

    async def resolve_station_id(self, station_id: str) -> str:
        """Return the canonical station id for a path-supplied station or alias."""
        query = """
            SELECT canonical_station_id
            FROM ocpp_station_aliases
            WHERE alias_station_id = $1
              AND active = TRUE
            LIMIT 1
        """
        row = await self.fetch_one(query, station_id)
        return str(row["canonical_station_id"]) if row else station_id

    async def ensure_station_alias(
        self,
        alias_station_id: str,
        canonical_station_id: str,
        *,
        source: str = "auto-multipath",
        notes: str = "Auto-registered from OCPP multi-segment path",
    ) -> bool:
        """Best-effort upsert of an OCPP station alias.

        Mirror of ``TimescaleClient.ensure_station_alias`` for the Supabase-
        backed pool. Inserts ``(alias → canonical)`` only when the canonical
        id matches a real ``charging_stations.station_id`` row and no row
        already exists for the alias. Returns ``True`` iff a row was created.
        """
        if alias_station_id == canonical_station_id:
            return False
        if not self.db_pool:
            return False
        async with self.db_pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO ocpp_station_aliases
                    (alias_station_id, canonical_station_id, source, notes)
                SELECT $1, $2, $3, $4
                WHERE EXISTS (
                    SELECT 1 FROM charging_stations WHERE station_id = $2
                )
                ON CONFLICT (alias_station_id) DO NOTHING
                """,
                alias_station_id,
                canonical_station_id,
                source,
                notes,
            )
        return isinstance(result, str) and result.endswith(" 1")

    async def lookup_tenant_context(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Return ``{"organization_id", "depot_id"}`` for a charger or None.

        Used by the OCPP handlers to label time-series rows (e.g.
        ``connector_status`` after migration 029) with tenant context so the
        alerts trigger can route notifications without joining the dropped
        TimescaleDB shadow tables. Returns None when the station is not
        registered or has no site assigned — callers persist a NULL-context
        row in that case and the trigger silently bails.
        """
        query = """
            SELECT cs.site_id AS depot_id, s.organization_id
            FROM charging_stations cs
            LEFT JOIN sites s ON s.id = cs.site_id
            WHERE cs.station_id = $1
            LIMIT 1
        """
        row = await self.fetch_one(query, station_id)
        if not row:
            return None
        return {
            "organization_id": row.get("organization_id"),
            "depot_id": row.get("depot_id"),
        }

    async def lookup_charger_context(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Return the richer tenant + display context for a charger or None.

        Superset of ``lookup_tenant_context`` — also returns the charger UUID,
        a human-friendly charger label, and the site's display name. Used by
        ``security_manager`` to emit/resolve ``charger_auth_failure`` alerts
        with proper context fields. Replaces the legacy ``chargers JOIN
        depots`` query that was killed by migration 029.
        """
        query = """
            SELECT
                cs.id            AS charger_id,
                cs.site_id       AS depot_id,
                cs.station_id    AS ocpp_id,
                COALESCE(cs.display_name, cs.station_id) AS charger_name,
                s.organization_id,
                s.name           AS depot_name
            FROM charging_stations cs
            LEFT JOIN sites s ON s.id = cs.site_id
            WHERE cs.station_id = $1
            LIMIT 1
        """
        row = await self.fetch_one(query, station_id)
        if not row:
            return None
        return {
            "charger_id": row.get("charger_id"),
            "depot_id": row.get("depot_id"),
            "ocpp_id": row.get("ocpp_id"),
            "charger_name": row.get("charger_name"),
            "organization_id": row.get("organization_id"),
            "depot_name": row.get("depot_name"),
        }

    async def is_basic_auth_username_allowed(self, station_id: str, username: str) -> bool:
        """Return True when username is the canonical station id or an active alias."""
        if hmac.compare_digest(username, station_id):
            return True
        query = """
            SELECT 1
            FROM ocpp_station_aliases
            WHERE alias_station_id = $1
              AND canonical_station_id = $2
              AND active = TRUE
            LIMIT 1
        """
        return await self.fetch_one(query, username, station_id) is not None

    async def validate_basic_auth(self, station_id: str, username: str, password: str) -> bool:
        """Validate station Basic Auth credentials from Supabase static config."""
        query = """
            SELECT id, password_hash
            FROM station_credentials
            WHERE station_id = $1
              AND username IN ($1, $2)
              AND active = TRUE
            ORDER BY CASE WHEN username = $2 THEN 0 ELSE 1 END
            LIMIT 1
        """
        row = await self.fetch_one(query, station_id, username)
        if not row:
            return False

        import bcrypt

        password_ok = bcrypt.checkpw(
            password.encode("utf-8"),
            row["password_hash"].encode("utf-8"),
        )
        if password_ok and self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE station_credentials SET last_used = NOW() WHERE id = $1",
                        row["id"],
                    )
            except Exception as exc:
                self.logger.warning(
                    "Failed to update station_credentials.last_used for id %s: %s",
                    row["id"],
                    exc,
                )
        return password_ok

    async def station_requires_basic_auth(self, station_id: str) -> bool:
        """Return True when a provisioned charger requires Basic Auth."""
        query = """
            SELECT COALESCE(auth_required, FALSE) AS auth_required
            FROM charging_stations
            WHERE station_id = $1
        """
        row = await self.fetch_one(query, station_id)
        return bool(row["auth_required"]) if row else False

    # Static data access methods

    async def get_active_routes(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get active route schedules from Supabase (schedules table)."""
        query = """
            SELECT s.id AS schedule_id,
                   s.vehicle_id,
                   s.route_id,
                   s.departure_time,
                   s.return_time,
                   s.actual_return_time,
                   s.energy_kwh,
                   s.required_soc,
                   s.dest_site_id AS dest_depot_id
            FROM schedules s
            WHERE s.departure_time > NOW()
        """
        if depot_id:
            query = (
                "SELECT s.id AS schedule_id,"
                "       s.vehicle_id,"
                "       s.route_id,"
                "       s.departure_time,"
                "       s.return_time,"
                "       s.actual_return_time,"
                "       s.energy_kwh,"
                "       s.required_soc,"
                "       s.dest_site_id AS dest_depot_id"
                " FROM schedules s"
                " JOIN vehicles v ON v.id = s.vehicle_id"
                " WHERE v.site_id = $1 AND s.departure_time > NOW()"
            )
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_vehicles(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get vehicles from Supabase."""
        query = """
            SELECT id AS vehicle_id,
                   organization_id,
                   site_id AS depot_id,
                   vin,
                   external_id,
                   vehicle_type,
                   id_tag,
                   battery_capacity_kwh AS battery_kwh,
                   max_charge_rate_kw AS max_charge_kw,
                   max_discharge_rate_kw,
                   v2g_capable,
                   license_plate,
                   driver_id,
                   status
            FROM vehicles
        """
        if depot_id:
            query += " WHERE site_id = $1"
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_chargers(self, depot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get chargers from Supabase."""
        query = """
            SELECT id AS charger_id,
                   site_id AS depot_id,
                   station_id AS ocpp_id,
                   max_power_kw AS rated_kw,
                   efficiency,
                   auth_required,
                   connector_type,
                   display_name,
                   vendor,
                   connector_count,
                   connector_ids,
                   status
            FROM charging_stations
        """
        if depot_id:
            query += " WHERE site_id = $1"
            return await self.fetch_all(query, depot_id)
        return await self.fetch_all(query)

    async def get_depot_config(self, depot_id: str) -> Optional[Dict[str, Any]]:
        """Get depot configuration from Supabase."""
        query = """
            SELECT id AS depot_id,
                   name,
                   organization_id,
                   max_grid_kw,
                   demand_charge_rate_kw,
                   demand_charge_billing_period,
                   timezone,
                   currency,
                   utility_id,
                   address,
                   billing_metadata,
                   building_load_source,
                   building_load_assumption_kw,
                   access_mode,
                   charger_vehicle_access_default,
                   tariff_config,
                   latitude,
                   longitude
            FROM sites
            WHERE id = $1
        """
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
