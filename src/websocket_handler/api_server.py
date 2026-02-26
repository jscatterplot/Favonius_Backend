"""REST API endpoints for user-facing operations."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from aiohttp import web

from .auth_manager import AuthManager, RateLimiter
from .config import SupabaseConfig
from .monitoring import get_logger
from .supabase_client import SupabaseClient


class APIServer:
    """REST API server for user-facing operations."""

    def __init__(
        self, config: SupabaseConfig, supabase_client: SupabaseClient, auth_manager: AuthManager
    ):
        """Initialize API server."""
        self.config = config
        self.supabase_client = supabase_client
        self.auth_manager = auth_manager
        self.logger = get_logger(__name__)

        # Rate limiter
        self.rate_limiter = RateLimiter()

        # Web application
        self.app = web.Application()
        self._setup_routes()

        # Server
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    def _setup_routes(self) -> None:
        """Setup API routes."""

        # Health check
        self.app.router.add_get("/health", self.health_check)

        # Authentication
        self.app.router.add_post("/auth/login", self.login)
        self.app.router.add_post("/auth/refresh", self.refresh_token)
        self.app.router.add_get("/auth/me", self.get_current_user)

        # Organizations
        self.app.router.add_get("/organizations", self.get_organizations)
        self.app.router.add_get("/organizations/{org_id}", self.get_organization)
        self.app.router.add_put("/organizations/{org_id}", self.update_organization)

        # Vehicles
        self.app.router.add_get("/vehicles", self.get_vehicles)
        self.app.router.add_post("/vehicles", self.create_vehicle)
        self.app.router.add_get("/vehicles/{vehicle_id}", self.get_vehicle)
        self.app.router.add_put("/vehicles/{vehicle_id}", self.update_vehicle)
        self.app.router.add_delete("/vehicles/{vehicle_id}", self.delete_vehicle)

        # Charging Stations
        self.app.router.add_get("/stations", self.get_stations)
        self.app.router.add_post("/stations", self.create_station)
        self.app.router.add_get("/stations/{station_id}", self.get_station)
        self.app.router.add_put("/stations/{station_id}", self.update_station)

        # Charging Sessions
        self.app.router.add_get("/sessions", self.get_sessions)
        self.app.router.add_get("/sessions/active", self.get_active_sessions)
        self.app.router.add_post("/sessions/{session_id}/stop", self.stop_session)

        # Schedules
        self.app.router.add_get("/schedules", self.get_schedules)
        self.app.router.add_post("/schedules", self.create_schedule)
        self.app.router.add_put("/schedules/{schedule_id}", self.update_schedule)
        self.app.router.add_delete("/schedules/{schedule_id}", self.delete_schedule)

        # Analytics
        self.app.router.add_get("/analytics/energy", self.get_energy_analytics)
        self.app.router.add_get("/analytics/costs", self.get_cost_analytics)
        self.app.router.add_get("/analytics/savings", self.get_savings_analytics)

        # Real-time subscriptions
        self.app.router.add_get("/realtime/subscribe", self.subscribe_realtime)

        # System admin
        self.app.router.add_get("/admin/sync-status", self.get_sync_status)
        self.app.router.add_post("/admin/sync", self.force_sync)

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the API server."""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(self.runner, host, port)
            await self.site.start()

            self.logger.info(f"API server started on {host}:{port}")

        except Exception as e:
            self.logger.error(f"Failed to start API server: {e}")
            raise

    async def stop(self) -> None:
        """Stop the API server."""
        if self.site:
            await self.site.stop()

        if self.runner:
            await self.runner.cleanup()

        self.logger.info("API server stopped")

    # Health Check
    async def health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        try:
            supabase_health = await self.supabase_client.health_check()

            return web.json_response(
                {
                    "status": "healthy",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "services": {"supabase": supabase_health},
                }
            )
        except Exception as e:
            return web.json_response({"status": "unhealthy", "error": str(e)}, status=500)

    # Authentication Endpoints
    async def login(self, request: web.Request) -> web.Response:
        """User login endpoint."""
        try:
            data = await request.json()
            email = data.get("email")
            password = data.get("password")

            if not email or not password:
                return web.json_response({"error": "Email and password required"}, status=400)

            # Authenticate with Supabase
            response = self.supabase_client.client.auth.sign_in_with_password(
                {"email": email, "password": password}
            )

            if response.user:
                # Generate JWT token
                token = self.auth_manager.generate_token(
                    response.user.id,
                    response.user.user_metadata.get("organization_id", ""),
                    response.user.user_metadata.get("role", "viewer"),
                )

                return web.json_response(
                    {
                        "access_token": token,
                        "user": {
                            "id": response.user.id,
                            "email": response.user.email,
                            "role": response.user.user_metadata.get("role", "viewer"),
                        },
                    }
                )
            else:
                return web.json_response({"error": "Invalid credentials"}, status=401)

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return web.json_response({"error": "Login failed"}, status=500)

    async def refresh_token(self, request: web.Request) -> web.Response:
        """Refresh JWT token."""
        try:
            data = await request.json()
            refresh_token = data.get("refresh_token")

            if not refresh_token:
                return web.json_response({"error": "Refresh token required"}, status=400)

            # Refresh with Supabase
            response = self.supabase_client.client.auth.refresh_session(refresh_token)

            if response.user:
                token = self.auth_manager.generate_token(
                    response.user.id,
                    response.user.user_metadata.get("organization_id", ""),
                    response.user.user_metadata.get("role", "viewer"),
                )

                return web.json_response({"access_token": token})
            else:
                return web.json_response({"error": "Invalid refresh token"}, status=401)

        except Exception as e:
            self.logger.error(f"Token refresh error: {e}")
            return web.json_response({"error": "Token refresh failed"}, status=500)

    async def get_current_user(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get current user information."""
        return web.json_response({"user": user})

    # Organization Endpoints

    async def get_organizations(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get user's organizations."""
        try:
            organizations = await self.auth_manager.get_user_organizations(user["id"])
            return web.json_response({"organizations": organizations})
        except Exception as e:
            self.logger.error(f"Get organizations error: {e}")
            return web.json_response({"error": "Failed to get organizations"}, status=500)

    async def get_organization(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get organization details."""
        try:
            org_id = request.match_info["org_id"]
            organization = await self.supabase_client.get_organization(org_id)

            if not organization:
                return web.json_response({"error": "Organization not found"}, status=404)

            return web.json_response({"organization": organization})
        except Exception as e:
            self.logger.error(f"Get organization error: {e}")
            return web.json_response({"error": "Failed to get organization"}, status=500)

    async def update_organization(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Update organization."""
        try:
            org_id = request.match_info["org_id"]
            data = await request.json()

            organization = await self.supabase_client.update_organization(org_id, data)
            return web.json_response({"organization": organization})
        except Exception as e:
            self.logger.error(f"Update organization error: {e}")
            return web.json_response({"error": "Failed to update organization"}, status=500)

    # Vehicle Endpoints

    async def get_vehicles(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get vehicles for organization."""
        try:
            org_id = user["organization_id"]
            vehicles = await self.supabase_client.get_vehicles_by_organization(org_id)
            return web.json_response({"vehicles": vehicles})
        except Exception as e:
            self.logger.error(f"Get vehicles error: {e}")
            return web.json_response({"error": "Failed to get vehicles"}, status=500)

    async def create_vehicle(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Create new vehicle."""
        try:
            data = await request.json()
            data["organization_id"] = user["organization_id"]

            vehicle = await self.supabase_client.create_vehicle(data)
            return web.json_response({"vehicle": vehicle}, status=201)
        except Exception as e:
            self.logger.error(f"Create vehicle error: {e}")
            return web.json_response({"error": "Failed to create vehicle"}, status=500)

    async def get_vehicle(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get vehicle details."""
        try:
            vehicle_id = request.match_info["vehicle_id"]

            # Get vehicle from organization's vehicles
            vehicles = await self.supabase_client.get_vehicles_by_organization(
                user["organization_id"]
            )
            vehicle = next((v for v in vehicles if v["id"] == vehicle_id), None)

            if not vehicle:
                return web.json_response({"error": "Vehicle not found"}, status=404)

            return web.json_response({"vehicle": vehicle})
        except Exception as e:
            self.logger.error(f"Get vehicle error: {e}")
            return web.json_response({"error": "Failed to get vehicle"}, status=500)

    async def update_vehicle(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Update vehicle."""
        try:
            vehicle_id = request.match_info["vehicle_id"]
            data = await request.json()

            vehicle = await self.supabase_client.update_vehicle_status(
                vehicle_id, data.get("status", "active")
            )
            return web.json_response({"vehicle": vehicle})
        except Exception as e:
            self.logger.error(f"Update vehicle error: {e}")
            return web.json_response({"error": "Failed to update vehicle"}, status=500)

    async def delete_vehicle(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Delete vehicle."""
        try:
            vehicle_id = request.match_info["vehicle_id"]

            # Soft delete by updating status
            await self.supabase_client.update_vehicle_status(vehicle_id, "deleted")

            return web.json_response({"message": "Vehicle deleted"})
        except Exception as e:
            self.logger.error(f"Delete vehicle error: {e}")
            return web.json_response({"error": "Failed to delete vehicle"}, status=500)

    # Charging Station Endpoints

    async def get_stations(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get charging stations for organization."""
        try:
            # Get sites for organization first
            sites_response = (
                self.supabase_client.client.table("sites")
                .select("id")
                .eq("organization_id", user["organization_id"])
                .execute()
            )
            site_ids = [site["id"] for site in sites_response.data]

            if not site_ids:
                return web.json_response({"stations": []})

            # Get stations for sites
            stations_response = (
                self.supabase_client.client.table("charging_stations")
                .select("*")
                .in_("site_id", site_ids)
                .execute()
            )

            return web.json_response({"stations": stations_response.data or []})
        except Exception as e:
            self.logger.error(f"Get stations error: {e}")
            return web.json_response({"error": "Failed to get stations"}, status=500)

    async def create_station(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Create new charging station."""
        try:
            data = await request.json()
            station = await self.supabase_client.create_charging_station(data)
            return web.json_response({"station": station}, status=201)
        except Exception as e:
            self.logger.error(f"Create station error: {e}")
            return web.json_response({"error": "Failed to create station"}, status=500)

    async def get_station(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get charging station details."""
        try:
            station_id = request.match_info["station_id"]

            station_response = (
                self.supabase_client.client.table("charging_stations")
                .select("*")
                .eq("id", station_id)
                .execute()
            )

            if not station_response.data:
                return web.json_response({"error": "Station not found"}, status=404)

            return web.json_response({"station": station_response.data[0]})
        except Exception as e:
            self.logger.error(f"Get station error: {e}")
            return web.json_response({"error": "Failed to get station"}, status=500)

    async def update_station(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Update charging station."""
        try:
            station_id = request.match_info["station_id"]
            data = await request.json()

            station = await self.supabase_client.update_station_status(
                station_id, data.get("status", "operational")
            )
            return web.json_response({"station": station})
        except Exception as e:
            self.logger.error(f"Update station error: {e}")
            return web.json_response({"error": "Failed to update station"}, status=500)

    # Charging Session Endpoints

    async def get_sessions(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get charging sessions."""
        try:
            org_id = user["organization_id"]
            start_date = request.query.get("start_date")
            end_date = request.query.get("end_date")

            query = (
                self.supabase_client.client.table("charging_sessions_summary")
                .select("*")
                .eq("organization_id", org_id)
            )

            if start_date:
                query = query.gte("start_time", start_date)
            if end_date:
                query = query.lte("start_time", end_date)

            response = query.execute()
            return web.json_response({"sessions": response.data or []})
        except Exception as e:
            self.logger.error(f"Get sessions error: {e}")
            return web.json_response({"error": "Failed to get sessions"}, status=500)

    async def get_active_sessions(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get active charging sessions."""
        try:
            org_id = user["organization_id"]
            sessions = await self.supabase_client.get_active_sessions(org_id)
            return web.json_response({"sessions": sessions})
        except Exception as e:
            self.logger.error(f"Get active sessions error: {e}")
            return web.json_response({"error": "Failed to get active sessions"}, status=500)

    async def stop_session(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Stop charging session."""
        try:
            session_id = request.match_info["session_id"]

            # Update session status
            session = await self.supabase_client.update_session_status(session_id, "stopped")
            return web.json_response({"session": session})
        except Exception as e:
            self.logger.error(f"Stop session error: {e}")
            return web.json_response({"error": "Failed to stop session"}, status=500)

    # Schedule Endpoints

    async def get_schedules(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get charging schedules."""
        try:
            org_id = user["organization_id"]
            schedules = await self.supabase_client.get_schedule_configs(org_id)
            return web.json_response({"schedules": schedules})
        except Exception as e:
            self.logger.error(f"Get schedules error: {e}")
            return web.json_response({"error": "Failed to get schedules"}, status=500)

    async def create_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Create charging schedule."""
        try:
            data = await request.json()
            data["organization_id"] = user["organization_id"]

            schedule = await self.supabase_client.create_schedule_config(data)
            return web.json_response({"schedule": schedule}, status=201)
        except Exception as e:
            self.logger.error(f"Create schedule error: {e}")
            return web.json_response({"error": "Failed to create schedule"}, status=500)

    async def update_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Update charging schedule."""
        try:
            schedule_id = request.match_info["schedule_id"]
            data = await request.json()

            schedule_response = (
                self.supabase_client.client.table("charging_schedules_config")
                .update(data)
                .eq("id", schedule_id)
                .execute()
            )
            return web.json_response(
                {"schedule": schedule_response.data[0] if schedule_response.data else {}}
            )
        except Exception as e:
            self.logger.error(f"Update schedule error: {e}")
            return web.json_response({"error": "Failed to update schedule"}, status=500)

    async def delete_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Delete charging schedule."""
        try:
            schedule_id = request.match_info["schedule_id"]

            self.supabase_client.client.table("charging_schedules_config").delete().eq(
                "id", schedule_id
            ).execute()
            return web.json_response({"message": "Schedule deleted"})
        except Exception as e:
            self.logger.error(f"Delete schedule error: {e}")
            return web.json_response({"error": "Failed to delete schedule"}, status=500)

    # Analytics Endpoints

    async def get_energy_analytics(
        self, request: web.Request, user: Dict[str, Any]
    ) -> web.Response:
        """Get energy analytics."""
        try:
            org_id = user["organization_id"]
            start_date = request.query.get(
                "start_date", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            )
            end_date = request.query.get("end_date", datetime.now(timezone.utc).isoformat())

            analytics = await self.supabase_client.get_daily_energy_summary(
                org_id, start_date, end_date
            )
            return web.json_response({"analytics": analytics})
        except Exception as e:
            self.logger.error(f"Get energy analytics error: {e}")
            return web.json_response({"error": "Failed to get energy analytics"}, status=500)

    async def get_cost_analytics(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get cost analytics."""
        try:
            org_id = user["organization_id"]
            start_date = request.query.get(
                "start_date", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            )
            end_date = request.query.get("end_date", datetime.now(timezone.utc).isoformat())

            # Get cost data from sessions
            response = (
                self.supabase_client.client.table("charging_sessions_summary")
                .select("start_time, cost_total, revenue_v2g")
                .eq("organization_id", org_id)
                .gte("start_time", start_date)
                .lte("start_time", end_date)
                .execute()
            )

            return web.json_response({"analytics": response.data or []})
        except Exception as e:
            self.logger.error(f"Get cost analytics error: {e}")
            return web.json_response({"error": "Failed to get cost analytics"}, status=500)

    async def get_savings_analytics(
        self, request: web.Request, user: Dict[str, Any]
    ) -> web.Response:
        """Get savings analytics."""
        try:
            org_id = user["organization_id"]
            start_date = request.query.get(
                "start_date", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            )
            end_date = request.query.get("end_date", datetime.now(timezone.utc).isoformat())

            savings = await self.supabase_client.calculate_savings(org_id, start_date, end_date)
            return web.json_response({"savings": savings})
        except Exception as e:
            self.logger.error(f"Get savings analytics error: {e}")
            return web.json_response({"error": "Failed to get savings analytics"}, status=500)

    # Real-time Endpoints

    async def subscribe_realtime(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Subscribe to real-time updates."""
        try:
            # This would typically be a WebSocket endpoint
            # For now, return subscription info
            return web.json_response(
                {
                    "subscription_url": f'/realtime/ws?token={request.headers.get("Authorization", "").replace("Bearer ", "")}',
                    "channels": ["fleet_updates", "vehicle_updates", "session_updates"],
                }
            )
        except Exception as e:
            self.logger.error(f"Subscribe realtime error: {e}")
            return web.json_response(
                {"error": "Failed to subscribe to real-time updates"}, status=500
            )

    # Admin Endpoints

    async def get_sync_status(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get data sync status."""
        try:
            # This would get status from the data sync service
            return web.json_response(
                {"sync_status": "running", "last_sync": datetime.now(timezone.utc).isoformat()}
            )
        except Exception as e:
            self.logger.error(f"Get sync status error: {e}")
            return web.json_response({"error": "Failed to get sync status"}, status=500)

    async def force_sync(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Force data synchronization."""
        try:
            data = await request.json()
            data_type = data.get("type", "all")

            # This would trigger sync in the data sync service
            return web.json_response(
                {
                    "message": f"Sync triggered for {data_type}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as e:
            self.logger.error(f"Force sync error: {e}")
            return web.json_response({"error": "Failed to force sync"}, status=500)
