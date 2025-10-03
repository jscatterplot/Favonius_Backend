"""Health check and status endpoints."""

import asyncio
import json
import time
from aiohttp import web, web_response
from typing import Dict, Any

from .monitoring import health_checker, metrics_collector, get_logger


class HealthCheckServer:
    """HTTP server for health checks and status endpoints."""
    
    def __init__(self, port: int = 8081):
        """Initialize health check server."""
        self.port = port
        self.logger = get_logger(__name__)
        self.app = web.Application()
        self.runner = None
        self.site = None
        
        # Setup routes
        self._setup_routes()
    
    def _setup_routes(self) -> None:
        """Setup HTTP routes."""
        self.app.router.add_get("/health", self._health_check)
        self.app.router.add_get("/readiness", self._readiness_check) 
        self.app.router.add_get("/liveness", self._liveness_check)
        self.app.router.add_get("/status", self._status_endpoint)
        self.app.router.add_get("/metrics/summary", self._metrics_summary)
    
    async def start(self) -> None:
        """Start health check server."""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            
            self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
            await self.site.start()
            
            self.logger.info(f"Health check server started on port {self.port}")
            
        except Exception as e:
            self.logger.error(f"Failed to start health check server: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop health check server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        
        self.logger.info("Health check server stopped")
    
    async def _health_check(self, request: web.Request) -> web.Response:
        """Comprehensive health check endpoint."""
        try:
            results = await health_checker.run_checks()
            
            status_code = 200 if results["status"] == "healthy" else 503
            
            return web.json_response(results, status=status_code)
            
        except Exception as e:
            self.logger.error(f"Health check error: {e}")
            return web.json_response(
                {
                    "status": "error",
                    "error": str(e),
                    "timestamp": time.time()
                },
                status=500
            )
    
    async def _readiness_check(self, request: web.Request) -> web.Response:
        """Kubernetes readiness probe endpoint."""
        try:
            # Check if critical components are ready
            results = await health_checker.run_checks()
            
            critical_checks = ["connections"]  # Redis removed
            ready = all(
                results["checks"].get(check, {}).get("status") == "healthy"
                for check in critical_checks
            )
            
            if ready:
                return web.json_response({"status": "ready"})
            else:
                return web.json_response(
                    {
                        "status": "not_ready", 
                        "checks": {
                            k: v for k, v in results["checks"].items() 
                            if k in critical_checks
                        }
                    },
                    status=503
                )
                
        except Exception as e:
            return web.json_response(
                {"status": "error", "error": str(e)},
                status=500
            )
    
    async def _liveness_check(self, request: web.Request) -> web.Response:
        """Kubernetes liveness probe endpoint."""
        # Simple liveness check - just return OK if server is responding
        return web.json_response({"status": "alive", "timestamp": time.time()})
    
    async def _status_endpoint(self, request: web.Request) -> web.Response:
        """Detailed status information endpoint."""
        try:
            # Get health check results
            health_results = await health_checker.run_checks()
            
            # Get metrics summary
            metrics_summary = metrics_collector.get_summary_stats()
            
            # Get last health check results for non-blocking access
            last_results = health_checker.get_last_results()
            
            status_info = {
                "service": "websocket-handler",
                "version": "1.0.0",
                "timestamp": time.time(),
                "health": health_results,
                "metrics": metrics_summary,
                "last_health_checks": last_results,
                "uptime": time.time() - getattr(self, '_start_time', time.time())
            }
            
            return web.json_response(status_info)
            
        except Exception as e:
            self.logger.error(f"Status endpoint error: {e}")
            return web.json_response(
                {"error": str(e), "timestamp": time.time()},
                status=500
            )
    
    async def _metrics_summary(self, request: web.Request) -> web.Response:
        """Metrics summary endpoint (lighter than full Prometheus metrics)."""
        try:
            summary = metrics_collector.get_summary_stats()
            
            return web.json_response({
                "metrics": summary,
                "timestamp": time.time()
            })
            
        except Exception as e:
            return web.json_response(
                {"error": str(e)},
                status=500
            )
