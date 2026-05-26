"""REST API endpoints for user-facing operations."""

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from aiohttp import web

from .auth_manager import AuthManager, RateLimiter
from .config import SupabaseConfig
from .monitoring import get_logger
from .supabase_client import SupabaseClient
from .timescale_client import TimescaleClient


def _iso(value: Any) -> Optional[str]:
    """Render a TIMESTAMPTZ / datetime / None as ISO-8601 (UTC) or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class APIServer:
    """REST API server for user-facing operations."""

    def __init__(
        self,
        config: SupabaseConfig,
        supabase_client: SupabaseClient,
        auth_manager: AuthManager,
        *,
        timescale_client: Optional[TimescaleClient] = None,
        websocket_server: Optional[Any] = None,
    ):
        """Initialize API server.

        Args:
            timescale_client: Optional handle for the OCPP admin endpoint.
            websocket_server: Optional ``OCPPWebSocketServer`` reference so
                the admin endpoint can layer in-memory FleetChargePoint
                metadata (vendor, last_heartbeat) on top of the DB rollup.
        """
        self.config = config
        self.supabase_client = supabase_client
        self.auth_manager = auth_manager
        self.timescale_client = timescale_client
        self.websocket_server = websocket_server
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
        self.app.router.add_get("/auth/me", self._protected(self.get_current_user))

        # Organizations
        self.app.router.add_get("/organizations", self._protected(self.get_organizations))
        self.app.router.add_get(
            "/organizations/{org_id}",
            self._protected(self.get_organization, resource="organization", action="read"),
        )
        self.app.router.add_put(
            "/organizations/{org_id}",
            self._protected(self.update_organization, resource="organization", action="write"),
        )

        # Vehicles
        self.app.router.add_get(
            "/vehicles", self._protected(self.get_vehicles, resource="vehicles", action="read")
        )
        self.app.router.add_post(
            "/vehicles", self._protected(self.create_vehicle, resource="vehicles", action="write")
        )
        self.app.router.add_get(
            "/vehicles/{vehicle_id}",
            self._protected(self.get_vehicle, resource="vehicles", action="read"),
        )
        self.app.router.add_put(
            "/vehicles/{vehicle_id}",
            self._protected(self.update_vehicle, resource="vehicles", action="write"),
        )
        self.app.router.add_delete(
            "/vehicles/{vehicle_id}",
            self._protected(self.delete_vehicle, resource="vehicles", action="delete"),
        )

        # Charging Stations
        self.app.router.add_get("/stations", self._protected(self.get_stations))
        self.app.router.add_post("/stations", self._protected(self.create_station))
        self.app.router.add_get("/stations/{station_id}", self._protected(self.get_station))
        self.app.router.add_put("/stations/{station_id}", self._protected(self.update_station))

        # Charging Sessions
        self.app.router.add_get(
            "/sessions",
            self._protected(self.get_sessions, resource="charging_sessions", action="read"),
        )
        self.app.router.add_get(
            "/sessions/active",
            self._protected(self.get_active_sessions, resource="charging_sessions", action="read"),
        )
        self.app.router.add_post(
            "/sessions/{session_id}/stop",
            self._protected(self.stop_session, resource="charging_sessions", action="write"),
        )

        # Schedules
        self.app.router.add_get("/schedules", self._protected(self.get_schedules))
        self.app.router.add_post("/schedules", self._protected(self.create_schedule))
        self.app.router.add_put("/schedules/{schedule_id}", self._protected(self.update_schedule))
        self.app.router.add_delete(
            "/schedules/{schedule_id}",
            self._protected(self.delete_schedule),
        )

        # Analytics
        self.app.router.add_get(
            "/analytics/energy",
            self._protected(self.get_energy_analytics, resource="analytics", action="read"),
        )
        self.app.router.add_get(
            "/analytics/costs",
            self._protected(self.get_cost_analytics, resource="analytics", action="read"),
        )
        self.app.router.add_get(
            "/analytics/savings",
            self._protected(self.get_savings_analytics, resource="analytics", action="read"),
        )

        # Real-time subscriptions
        self.app.router.add_get("/realtime/subscribe", self._protected(self.subscribe_realtime))

        # System admin
        self.app.router.add_get(
            "/admin/sync-status",
            self._protected(self.get_sync_status, required_roles={"owner"}),
        )
        self.app.router.add_post(
            "/admin/sync",
            self._protected(self.force_sync, required_roles={"owner"}),
        )
        # Per-charger debug dump used to triage the pilot deployment.
        self.app.router.add_get(
            "/admin/ocpp/{cp_id}/state",
            self._protected(self.get_ocpp_state, required_roles={"owner"}),
        )

    def _protected(
        self,
        handler: Callable[[web.Request, Dict[str, Any]], Awaitable[web.Response]],
        *,
        resource: Optional[str] = None,
        action: str = "read",
        required_roles: Optional[set[str]] = None,
    ) -> Callable[[web.Request], Awaitable[web.Response]]:
        """Wrap aiohttp handlers with auth + optional authorization checks."""

        async def wrapped(request: web.Request) -> web.Response:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return web.json_response(
                    {"error": "Missing or invalid authorization header"},
                    status=401,
                )

            token = auth_header[7:]
            user = await self.auth_manager.authenticate_user(token)
            if not user:
                return web.json_response({"error": "Invalid or expired token"}, status=401)

            if required_roles and user.get("role") not in required_roles:
                return web.json_response({"error": "Insufficient permissions"}, status=403)

            if resource and not await self.auth_manager.authorize_action(user, resource, action):
                return web.json_response({"error": "Insufficient permissions"}, status=403)

            return await handler(request, user)

        return wrapped

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
        return web.json_response(
            {"error": "Endpoint removed. Use GET /depots/{id}/sessions on the FastAPI service."},
            status=410,
        )

    async def get_active_sessions(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get active charging sessions."""
        return web.json_response(
            {"error": "Endpoint removed. Use GET /depots/{id}/sessions/active on the FastAPI service."},
            status=410,
        )

    async def stop_session(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Stop charging session."""
        return web.json_response(
            {"error": "Endpoint removed. Use the FastAPI service for session management."},
            status=410,
        )

    # Schedule Endpoints

    async def get_schedules(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get charging schedules."""
        return web.json_response(
            {"error": "Endpoint removed. Schedules are managed via the FastAPI service."},
            status=410,
        )

    async def create_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Create charging schedule."""
        return web.json_response(
            {"error": "Endpoint removed. Schedules are managed via the FastAPI service."},
            status=410,
        )

    async def update_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Update charging schedule."""
        return web.json_response(
            {"error": "Endpoint removed. Schedules are managed via the FastAPI service."},
            status=410,
        )

    async def delete_schedule(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Delete charging schedule."""
        return web.json_response(
            {"error": "Endpoint removed. Schedules are managed via the FastAPI service."},
            status=410,
        )

    # Analytics Endpoints

    async def get_energy_analytics(
        self, request: web.Request, user: Dict[str, Any]
    ) -> web.Response:
        """Get energy analytics."""
        return web.json_response(
            {"error": "Endpoint removed. Use GET /depots/{id}/savings-summary on the FastAPI service."},
            status=410,
        )

    async def get_cost_analytics(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Get cost analytics."""
        return web.json_response(
            {"error": "Endpoint removed. Use GET /depots/{id}/savings-summary on the FastAPI service."},
            status=410,
        )

    async def get_savings_analytics(
        self, request: web.Request, user: Dict[str, Any]
    ) -> web.Response:
        """Get savings analytics."""
        return web.json_response(
            {"error": "Endpoint removed. Use GET /depots/{id}/savings-summary on the FastAPI service."},
            status=410,
        )

    # Real-time Endpoints

    async def subscribe_realtime(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Subscribe to real-time updates."""
        try:
            # This would typically be a WebSocket endpoint
            # Never return bearer tokens in URL query strings.
            return web.json_response(
                {
                    "subscription_url": "/realtime/ws",
                    "auth": {
                        "type": "bearer",
                        "transport": "Authorization header",
                    },
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
        return web.json_response(
            {"error": "Endpoint removed. The Supabase sync pipeline has been retired."},
            status=410,
        )

    async def force_sync(self, request: web.Request, user: Dict[str, Any]) -> web.Response:
        """Force data synchronization."""
        return web.json_response(
            {"error": "Endpoint removed. The Supabase sync pipeline has been retired."},
            status=410,
        )

    # ------------------------------------------------------------------
    # OCPP debug endpoint (session 3) — single charger state dump for ops.
    # ------------------------------------------------------------------
    async def get_ocpp_state(
        self, request: web.Request, user: Dict[str, Any]
    ) -> web.Response:
        """Aggregate live + DB state for one OCPP charge-point id.

        404 if the charger has neither an in-memory session nor any prior
        DB rows. Otherwise returns whatever we have: the in-memory part
        is None when the charger is currently disconnected.
        """
        cp_id = request.match_info.get("cp_id", "").strip()
        if not cp_id:
            return web.json_response({"error": "cp_id required"}, status=400)
        if len(cp_id) > 256:
            return web.json_response({"error": "cp_id too long"}, status=400)

        if self.timescale_client is None:
            return web.json_response(
                {"error": "TimescaleDB client not wired into APIServer"}, status=503
            )

        # 1. DB rollup (always queried — lets us 404 only when there is
        # genuinely no record of this charger).
        try:
            db_state = await self.timescale_client.fetch_admin_state(cp_id)
        except Exception as exc:
            self.logger.error("fetch_admin_state failed for cp=%s: %s", cp_id, exc)
            return web.json_response({"error": "DB query failed"}, status=500)

        # 2. In-memory snapshot from the OCPPWebSocketServer.
        cp = (
            self.websocket_server.get_charge_point(cp_id)
            if self.websocket_server is not None
            else None
        )

        connected = cp is not None
        subprotocol: Optional[str] = None
        vendor: Optional[str] = None
        model: Optional[str] = None
        last_boot_at: Optional[str] = None
        last_heartbeat_at: Optional[str] = None
        if cp is not None:
            # OCPP16Session wraps a FleetChargePoint in ``_cp``; the
            # FastAPI 2.0.1 path uses EnhancedOCPPChargePoint directly.
            inner = self._safe_attr(cp, "_cp") or cp
            subprotocol = self._safe_attr(cp, "_subprotocol") or self._safe_attr(
                inner, "subprotocol"
            )
            connection = self._safe_attr(inner, "_connection") or self._safe_attr(
                inner, "connection"
            )
            if subprotocol is None and connection is not None:
                subprotocol = self._safe_attr(connection, "subprotocol")
            vendor = self._safe_attr(inner, "vendor") or self._safe_attr(
                inner, "vendor_name"
            )
            model = self._safe_attr(inner, "model")
            last_boot_at = _iso(self._safe_attr(inner, "last_boot_at"))
            last_heartbeat_at = _iso(self._safe_attr(inner, "last_heartbeat_at"))
            # Older 2.0.1 handler stores ``last_heartbeat`` as a unix ts.
            if last_heartbeat_at is None:
                hb = self._safe_attr(inner, "last_heartbeat")
                if isinstance(hb, (int, float)) and hb > 0:
                    last_heartbeat_at = datetime.fromtimestamp(
                        hb, tz=timezone.utc
                    ).isoformat()

        # 404 only when DB has NO record AND no live session.
        if (
            not connected
            and not db_state["connectors"]
            and not db_state["active_transactions"]
            and not db_state["queue_counts"]
            and not db_state["last_command"]
        ):
            return web.json_response({"error": "Unknown charge_point_id"}, status=404)

        # Normalise queue rollup so every status appears (zero or otherwise).
        queue_counts = {
            status: int(db_state["queue_counts"].get(status, 0))
            for status in ("pending", "sent", "acked", "failed", "expired")
        }

        body: Dict[str, Any] = {
            "charge_point_id": cp_id,
            "connected": connected,
            "subprotocol": subprotocol,
            "vendor": vendor,
            "model": model,
            "last_boot_at": last_boot_at,
            "last_heartbeat_at": last_heartbeat_at,
            "connectors": [
                {
                    "id": int(c["connector_id"]),
                    "status": c["status"],
                    "error_code": c["error_code"],
                    "updated_at": _iso(c.get("updated_at")),
                }
                for c in db_state["connectors"]
            ],
            "active_transactions": [
                {
                    "transaction_id": int(t["transaction_id"]),
                    "connector_id": int(t["connector_id"])
                    if t.get("connector_id") is not None
                    else None,
                    "id_tag": t.get("id_token"),
                    "start_time": _iso(t.get("start_time")),
                }
                for t in db_state["active_transactions"]
            ],
            "queue": {
                **queue_counts,
                "last_command": (
                    {
                        "queue_id": int(db_state["last_command"]["queue_id"]),
                        "status": db_state["last_command"]["status"],
                        "enqueued_at": _iso(db_state["last_command"].get("enqueued_at")),
                        "sent_at": _iso(db_state["last_command"].get("sent_at")),
                        "acked_at": _iso(db_state["last_command"].get("acked_at")),
                        "last_error": db_state["last_command"].get("last_error"),
                    }
                    if db_state["last_command"]
                    else None
                ),
            },
        }
        return web.json_response(body)

    @staticmethod
    def _safe_attr(obj: Any, name: str) -> Any:
        try:
            return getattr(obj, name, None)
        except Exception:
            return None
