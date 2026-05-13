"""Main application entry point for OCPP WebSocket handler."""

import asyncio
import signal
import sys
import time
from typing import Optional

import uvloop

from .analytics_service import AnalyticsService
from .api_server import APIServer
from .auth_manager import AuthManager
from .charging_profile_manager import ChargingCommandQueueConsumer
from src.notifications.dispatcher import AlertDispatcher
from src.notifications.email_client import EmailDeliveryClient, FakeEmailClient
from src.notifications.resend_client import ResendEmailClient
from .config import Config
from .config_validator import ConfigValidator
from .connection_monitor import ConnectionMonitor
from .data_sync import DataSyncService
from .database_schema import create_schema_from_config
from .health import HealthCheckServer
from .health_checks import create_health_checks, notify_websocket_ready
from .monitoring import (
    ACTIVE_TRANSACTIONS,
    get_logger,
    setup_logging,
    setup_monitoring,
)
from .optimization_engine import OptimizationEngine
from .price_feeder import PriceFeederService
from .resilience_manager import resilience_manager
from .server import OCPPWebSocketServer
from .supabase_client import SupabaseClient
from .timescale_client import TimescaleClient
from .timescale_schema import create_timescale_schema_from_config


class Application:
    """Main application orchestrator."""

    def __init__(self, config: Config):
        """Initialize application."""
        self.config = config
        self.logger = get_logger(__name__)

        # Core components
        self.websocket_server: Optional[OCPPWebSocketServer] = None
        self.health_server: Optional[HealthCheckServer] = None

        # Supabase components
        self.supabase_client: Optional[SupabaseClient] = None
        self.auth_manager: Optional[AuthManager] = None
        self.data_sync_service: Optional[DataSyncService] = None
        self.api_server: Optional[APIServer] = None

        # TimescaleDB components
        self.timescale_client: Optional[TimescaleClient] = None
        self.analytics_service: Optional[AnalyticsService] = None
        self.price_feeder: Optional[PriceFeederService] = None
        self.optimization_engine: Optional[OptimizationEngine] = None

        # Connection monitoring
        self.connection_monitor: Optional[ConnectionMonitor] = None

        # Queue-mediated dispatch consumer (session 3) — drains
        # charging_command_queue rows that the FastAPI optimizer enqueues.
        self.queue_consumer: Optional[ChargingCommandQueueConsumer] = None
        self._active_tx_reconciler_task: Optional[asyncio.Task] = None
        self._orphan_recovery_task: Optional[asyncio.Task] = None

        # Alerts pipeline (see docs/plans/alerts-pipeline.md). Optional —
        # disabled by default in test envs without a Resend key.
        self.alert_dispatcher: Optional[AlertDispatcher] = None
        self._email_client: Optional[EmailDeliveryClient] = None

        # State
        self.running = False
        self.start_time = time.time()

    async def start(self) -> None:
        """Start all application components."""
        self.logger.info("Starting EV Charging WebSocket Handler...")

        try:
            # Validate configuration first
            await self._validate_configuration()

            # Start Prometheus metrics HTTP server (logging is already configured
            # by main() before Application is created).
            setup_monitoring(self.config.monitoring)

            # Eagerly initialise geo-blocking so the GeoIP runtime download
            # and DB load happen before chargers connect. Without this, the
            # singleton lazy-inits on the first connection and a missing /
            # failed download silently fail-closes every subsequent request
            # with "GeoIP unavailable, fail-closed: blocking IP <X>" — which
            # is invisible at startup and looks identical to a hostile-country
            # block in the logs.
            await asyncio.to_thread(self._initialize_geo_blocking)

            # Initialize resilience manager
            await self._initialize_resilience()

            # Initialize Supabase components
            await self._initialize_with_retry(
                component_name="Supabase",
                initializer=self._initialize_supabase_components,
            )

            # Initialize TimescaleDB components
            await self._initialize_with_retry(
                component_name="TimescaleDB",
                initializer=self._initialize_timescale_components,
            )

            # Create WebSocket server and pass optimization engine for wiring
            self.websocket_server = OCPPWebSocketServer(
                self.config,
                self.timescale_client,
                self.optimization_engine,  # Pass optimization engine so it can be wired to connection manager
                supabase_client=self.supabase_client,
            )

            # Create health check server
            self.health_server = HealthCheckServer(self.config.monitoring.health_check_port)

            # Create API server. The OCPP debug endpoint needs the
            # TimescaleDB client (for the queue/connector rollup) and a
            # late-bound reference to the WebSocket server (for the
            # in-memory FleetChargePoint snapshot).
            self.api_server = APIServer(
                self.config.supabase,
                self.supabase_client,
                self.auth_manager,
                timescale_client=self.timescale_client,
                websocket_server=self.websocket_server,
            )

            # Validate no port conflicts before starting listeners
            self._check_port_conflicts()

            # Start components
            await self.health_server.start()
            await self.api_server.start(port=self.config.monitoring.api_port)

            # Start data sync service (skipped silently when Supabase was
            # unavailable — the service is then None and there is nothing
            # to sync).
            if self.data_sync_service is not None:
                await self.data_sync_service.start()

            # Initialize connection monitoring
            self.connection_monitor = ConnectionMonitor(
                timescale_client=self.timescale_client,
                supabase_client=self.supabase_client,
                check_interval=30,
            )
            await self.connection_monitor.start_monitoring()

            # Queue-mediated dispatch consumer. Picks up SetChargingProfile
            # rows enqueued by the FastAPI optimizer and pushes them to the
            # connected charger. Started before websocket_server.start()
            # blocks so the polling loop is alive immediately.
            self.queue_consumer = ChargingCommandQueueConsumer(
                timescale_client=self.timescale_client,
                cp_lookup=self._cp_lookup,
            )
            await self.queue_consumer.start()

            # Periodically reconcile per-station active_transactions gauge
            # from DB so a metrics consumer sees the truth even after a
            # mid-transaction handler restart.
            self._active_tx_reconciler_task = asyncio.create_task(
                self._active_tx_reconciler(), name="active_tx_reconciler"
            )

            # Orphan charging-session recovery (migration 038). Closes rows
            # whose StopTransaction never arrived using the running
            # last_meter_wh as a synthesized meter_stop_wh — see PR #186 /
            # CLAUDE.md energy tracking plan.
            self._orphan_recovery_task = asyncio.create_task(
                self._orphan_recovery_loop(), name="orphan_recovery"
            )

            # Alerts dispatcher (optional). Only starts when notifications
            # are enabled; the migration 022 trigger already produces
            # notification_alerts rows independently.
            await self._initialize_alert_dispatcher()

            # Start WebSocket server
            self.running = True

            self.logger.info(
                f"Application started successfully in {time.time() - self.start_time:.2f}s"
            )

            # Start WebSocket server (this blocks until shutdown)
            # Check running flag periodically for graceful shutdown
            while self.running:
                try:
                    # Start server and wait for it to complete
                    await self.websocket_server.start()
                    break  # Server completed normally
                except Exception as e:
                    if self.running:
                        self.logger.error(f"WebSocket server error: {e}")
                        await asyncio.sleep(1)  # Brief pause before retry
                    else:
                        break  # Shutdown requested

        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
            raise

    async def _validate_configuration(self) -> None:
        """Validate configuration before starting."""
        self.logger.info("Validating configuration...")

        if getattr(self.config, "environment", "") == "test":
            self.logger.info("Skipping configuration validation in test environment")
            return

        validator = ConfigValidator(self.config)
        is_valid = await validator.validate_all()

        if not is_valid:
            report = validator.get_validation_report()
            if self.config.strict_startup_validation:
                self.logger.error(f"Configuration validation failed: {report}")
                raise Exception("Configuration validation failed")

            self.logger.warning(
                "Configuration validation reported issues but strict startup validation is disabled: "
                f"{report}"
            )
            return

        self.logger.info("Configuration validation passed")

    def _check_port_conflicts(self) -> None:
        """Check for port conflicts between services and fail fast with a clear message."""
        ports = {
            "WebSocket server": self.config.websocket.port,
            "Health check server": self.config.monitoring.health_check_port,
            "API server": self.config.monitoring.api_port,
        }
        if self.config.monitoring.enable_telemetry:
            ports["Prometheus metrics server"] = self.config.monitoring.metrics_port

        seen: dict[int, str] = {}
        for name, port in ports.items():
            if port in seen:
                raise RuntimeError(
                    f"Port conflict: {name} and {seen[port]} are both configured "
                    f"to use port {port}. Set distinct ports via environment variables "
                    f"(WEBSOCKET_PORT, API_PORT, METRICS_PORT, HEALTH_CHECK_PORT)."
                )
            seen[port] = name

    async def _initialize_with_retry(self, component_name: str, initializer) -> None:
        """Initialize critical components with optional retry behavior."""
        if self.config.strict_startup_validation:
            await initializer()
            return

        attempt = 0
        delay_seconds = 2
        max_attempts = 10

        while attempt < max_attempts:
            attempt += 1
            try:
                await initializer()
                if attempt > 1:
                    self.logger.info(
                        f"{component_name} initialized successfully after {attempt} attempts"
                    )
                return
            except Exception as exc:
                if attempt >= max_attempts:
                    self.logger.error(
                        f"{component_name} initialization failed after {max_attempts} attempts: {exc}"
                    )
                    raise
                self.logger.warning(
                    f"{component_name} initialization attempt {attempt} failed: {exc}. "
                    f"Retrying in {delay_seconds}s because strict startup validation is disabled."
                )
                # Clean up partially initialized components before retrying
                await self._cleanup_component(component_name)
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30)

    def _initialize_geo_blocking(self) -> None:
        """Force GeoIP DB download and reader load before serving traffic.

        Runs synchronously inside ``asyncio.to_thread`` because the runtime
        download blocks for up to retries × backoff seconds. Import or init
        failures are logged here and do not abort startup (same policy as
        ``src.api.main`` lifespan).
        """
        try:
            from src.security.geo_block import initialize_geo_blocking

            initialize_geo_blocking()
        except ImportError:
            self.logger.warning(
                "Geo-blocking module not importable — Article 73-3 controls "
                "inactive on this service"
            )
        except Exception as exc:  # pragma: no cover — defensive: never block startup
            self.logger.warning("Geo-blocking eager init failed: %s", exc)

    async def _initialize_resilience(self) -> None:
        """Initialize resilience manager and error handling."""
        self.logger.info("Initializing resilience manager...")

        # Register placeholder health checks (no live clients yet).
        # _refresh_health_checks() upgrades them to pool-backed checks once
        # the clients are available.
        health_checks = create_health_checks(self.config)

        for health_check in health_checks:
            resilience_manager.add_health_check(health_check)

        # Add circuit breakers
        resilience_manager.add_circuit_breaker(
            "timescale", failure_threshold=5, recovery_timeout=60.0
        )
        resilience_manager.add_circuit_breaker(
            "supabase", failure_threshold=3, recovery_timeout=30.0
        )
        resilience_manager.add_circuit_breaker(
            "websocket", failure_threshold=10, recovery_timeout=120.0
        )

        # Start resilience monitoring
        await resilience_manager.start()

        self.logger.info("Resilience manager initialized")

    def _refresh_health_checks(self) -> None:
        """Upgrade health-check functions to use the live client pools.

        Called after both timescale_client and supabase_client are ready so
        subsequent health-check cycles probe the existing connections instead
        of spinning up disposable ones.
        """
        upgraded = create_health_checks(
            self.config,
            timescale_client=self.timescale_client,
            supabase_client=self.supabase_client,
        )
        for hc in upgraded:
            if hc.name in resilience_manager.health_checks:
                resilience_manager.health_checks[hc.name].check_func = hc.check_func

    def _cp_lookup(self, cp_id: str):
        """Resolve a connected charger handler for the queue consumer.

        Returns ``None`` when the charger is not currently connected; the
        consumer leaves the row pending in that case (boot replay path
        will retry on reconnect).
        """
        if not self.websocket_server:
            return None
        return self.websocket_server.get_charge_point(cp_id)

    async def _orphan_recovery_loop(self) -> None:
        """Periodically close charging_sessions rows whose StopTransaction never arrived.

        Sweep cadence and staleness threshold are tunable so accounting can
        be tightened in production without a code change:

          * ``ORPHAN_RECOVERY_INTERVAL_S`` (default 300s / 5 min) — time
            between sweeps.
          * ``ORPHAN_RECOVERY_THRESHOLD_S`` (default 1800s / 30 min) —
            minimum age of ``last_seen_at`` before a row is considered
            orphaned. Must be larger than any realistic legit pause.
          * ``ORPHAN_RECOVERY_BATCH_LIMIT`` (default 100) — max rows
            closed per sweep; the next sweep picks up the rest.

        All errors are caught and logged; this is best-effort housekeeping
        and must never propagate.
        """
        import os

        interval = int(os.environ.get("ORPHAN_RECOVERY_INTERVAL_S", "300"))
        threshold = int(os.environ.get("ORPHAN_RECOVERY_THRESHOLD_S", "1800"))
        batch_limit = int(os.environ.get("ORPHAN_RECOVERY_BATCH_LIMIT", "100"))

        # Initial delay: wait one full interval before the first sweep so
        # legitimate in-flight sessions during a handler restart don't get
        # closed before BootNotification reload (fetch_open_sessions) runs.
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        while self.running:
            try:
                closed = await self.timescale_client.recover_orphaned_sessions(
                    stale_after_seconds=threshold,
                    batch_limit=batch_limit,
                )
                if closed:
                    self.logger.warning(
                        "orphan_recovery_loop closed %d session(s); first=%s last=%s",
                        len(closed),
                        closed[0].get("station_id"),
                        closed[-1].get("station_id"),
                    )
            except Exception as exc:
                self.logger.error("orphan_recovery_loop sweep failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _active_tx_reconciler(self) -> None:
        """Refresh the ``active_transactions`` gauge from DB every 30 s.

        Increments / decrements happen inline in the OCPP handler on
        Start/StopTransaction; this loop is the safety net for restarts
        and dropped events.
        """
        # Track which stations had a non-zero gauge so we can zero them
        # out when their open count drops to 0 (Prometheus does not emit
        # series we never touch).
        seen_stations: set[str] = set()
        while self.running:
            try:
                counts = await self.timescale_client.count_active_transactions_by_station()
            except Exception as exc:
                self.logger.debug("active_tx reconcile failed: %s", exc)
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise
                continue

            for station_id, n in counts.items():
                ACTIVE_TRANSACTIONS.labels(station_id=station_id).set(n)
                seen_stations.add(station_id)

            for station_id in list(seen_stations):
                if station_id not in counts:
                    ACTIVE_TRANSACTIONS.labels(station_id=station_id).set(0)

            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        """Stop all application components gracefully."""
        self.logger.info("Shutting down application...")

        self.running = False

        # Stop components in reverse order
        stop_tasks = []

        # Stop queue consumer first so we don't push profiles to a charger
        # that is about to be disconnected.
        if self.queue_consumer:
            stop_tasks.append(self.queue_consumer.stop())
        if self._active_tx_reconciler_task:
            self._active_tx_reconciler_task.cancel()
            stop_tasks.append(self._active_tx_reconciler_task)
        if self._orphan_recovery_task:
            self._orphan_recovery_task.cancel()
            stop_tasks.append(self._orphan_recovery_task)

        # Stop alert dispatcher before tearing down the DB pool.
        if self.alert_dispatcher:
            stop_tasks.append(self.alert_dispatcher.stop())
        if isinstance(self._email_client, ResendEmailClient):
            stop_tasks.append(self._email_client.aclose())

        # Stop connection monitoring
        if self.connection_monitor:
            stop_tasks.append(self.connection_monitor.stop_monitoring())

        if self.api_server:
            stop_tasks.append(self.api_server.stop())

        if self.websocket_server:
            stop_tasks.append(self.websocket_server.stop())

        if self.health_server:
            stop_tasks.append(self.health_server.stop())

        # Wait for components that do not close the shared Timescale pool.
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        # DataSyncService shares timescale_client.pg_pool; its stop() cancels
        # the sync task but disconnect() must not close the pool until that
        # cancellation has finished (otherwise in-flight queries see InterfaceError).
        if self.data_sync_service:
            await asyncio.gather(self.data_sync_service.stop(), return_exceptions=True)

        db_teardown = []
        if self.timescale_client:
            db_teardown.append(self.timescale_client.disconnect())
        if self.supabase_client:
            db_teardown.append(self.supabase_client.disconnect())
        if db_teardown:
            await asyncio.gather(*db_teardown, return_exceptions=True)

        self.logger.info("Application shutdown complete")

    async def _cleanup_component(self, component_name: str) -> None:
        """Clean up partially initialized components before retry."""
        if component_name == "Supabase":
            if self.supabase_client:
                try:
                    await self.supabase_client.disconnect()
                except Exception:
                    pass
                self.supabase_client = None
            self.auth_manager = None
            self.data_sync_service = None
        elif component_name == "TimescaleDB":
            if self.price_feeder:
                try:
                    await self.price_feeder.stop()
                except Exception:
                    pass
                self.price_feeder = None
            if self.optimization_engine:
                try:
                    await self.optimization_engine.stop()
                except Exception:
                    pass
                self.optimization_engine = None
            if self.analytics_service:
                try:
                    # AnalyticsService may not have a stop method, but try to clean up if it does
                    if hasattr(self.analytics_service, "stop"):
                        await self.analytics_service.stop()
                except Exception:
                    pass
                self.analytics_service = None
            # DataSyncService now shares the TimescaleClient pool — it lives
            # in the TimescaleDB-init phase, so reset it here. It is safe to
            # null without calling .stop() because the pool we'd want to
            # close is owned by ``self.timescale_client`` (closed below).
            self.data_sync_service = None
            if self.timescale_client:
                try:
                    await self.timescale_client.disconnect()
                except Exception:
                    pass
                self.timescale_client = None

    async def _initialize_alert_dispatcher(self) -> None:
        """Wire up the alerts pipeline dispatcher.

        Skipped when notifications are disabled via EMAIL_DELIVERY_ENABLED=false
        or when the timescale pool isn't available. Without a Resend API key
        we still start the dispatcher with a FakeEmailClient — useful for
        staging/dev so alerts move through the pipeline and we can inspect
        notification_deliveries without sending real email.
        """
        notifications_cfg = self.config.notifications
        if not notifications_cfg.enabled:
            self.logger.info("alerts: dispatcher disabled (EMAIL_DELIVERY_ENABLED=false)")
            return

        if not self.timescale_client or not self.timescale_client.pg_pool:
            self.logger.warning("alerts: dispatcher not started; timescale pg_pool unavailable")
            return

        if notifications_cfg.resend_api_key:
            self._email_client = ResendEmailClient(
                api_key=notifications_cfg.resend_api_key,
                default_from=notifications_cfg.resend_from_address,
            )
            self.logger.info("alerts: using Resend for outbound email")
        else:
            self._email_client = FakeEmailClient()
            self.logger.warning(
                "alerts: RESEND_API_KEY missing; dispatcher running with "
                "FakeEmailClient — emails will NOT be delivered"
            )

        self.alert_dispatcher = AlertDispatcher(
            pool=self.timescale_client.pg_pool,
            email_client=self._email_client,
            default_from=notifications_cfg.resend_from_address,
            poll_interval_s=notifications_cfg.poll_interval_s,
            resend_interval_s=notifications_cfg.resend_interval_s,
            batch_size=notifications_cfg.batch_size,
        )
        await self.alert_dispatcher.start()

    async def _initialize_supabase_components(self) -> None:
        """Initialize Supabase components."""
        try:
            # In production the Supabase schema is managed via SQL migrations
            # (see migrations/ and scripts/run_migrations.py) and must not be
            # mutated by this service at runtime. In lower environments we keep
            # the convenience of creating the schema on startup.
            if self.config.environment != "production":
                try:
                    await create_schema_from_config(self.config.supabase)
                    self.logger.info("Database schema initialized")
                except Exception as e:
                    self.logger.warning(
                        "Schema initialization failed or already applied; "
                        f"continuing with existing schema: {e}"
                    )
            else:
                self.logger.info(
                    "Skipping Supabase schema initialization in production; "
                    "migrations/ and scripts/run_migrations.py are authoritative."
                )

            # Create Supabase client
            self.supabase_client = SupabaseClient(self.config.supabase)
            await self.supabase_client.connect()

            # Create auth manager
            self.auth_manager = AuthManager(self.config.supabase, self.supabase_client)

            # DataSyncService is constructed later in
            # _initialize_timescale_components so it can share the live
            # TimescaleClient pool instead of opening its own. Leaving the
            # attribute None here keeps the cleanup paths safe.
            self.data_sync_service = None

            self.logger.info("Supabase components initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize Supabase components: {e}")
            raise

    async def _initialize_timescale_components(self) -> None:
        """Initialize TimescaleDB components."""
        try:
            # In production the TimescaleDB schema is managed via SQL migrations
            # (see migrations/ and scripts/run_migrations.py) and must not be
            # mutated by this service at runtime. In lower environments we keep
            # the convenience of creating the schema on startup.
            if self.config.environment != "production":
                try:
                    await create_timescale_schema_from_config(self.config.timescale)
                    self.logger.info("TimescaleDB schema initialized")
                except Exception as e:
                    self.logger.warning(
                        "TimescaleDB schema initialization failed or already applied; "
                        f"continuing with existing schema: {e}"
                    )
            else:
                self.logger.info(
                    "Skipping TimescaleDB schema initialization in production; "
                    "migrations/ and scripts/run_migrations.py are authoritative."
                )

            # Create TimescaleDB client
            self.timescale_client = TimescaleClient(self.config.timescale)
            await self.timescale_client.connect()

            # RFID/vehicle/charging_stations rows live in Supabase, not in
            # TimescaleDB. Wire the Supabase pool into the timescale client so
            # ``lookup_id_tag`` queries the right database; without this the
            # lookup hits the dropped/renamed shadow tables and every
            # Authorize/StartTransaction is rejected as Invalid.
            if self.supabase_client is not None:
                self.timescale_client.set_supabase_client(self.supabase_client)

            # Create data sync service now that the TimescaleClient pool
            # exists; share its pool so we do not open a second 1-10
            # connection pool against the same Postgres.
            if self.supabase_client is not None:
                self.data_sync_service = DataSyncService(
                    self.config.supabase,
                    self.supabase_client,
                    self.config.timescale,
                    timescale_client=self.timescale_client,
                )

            # Initialize analytics service
            self.analytics_service = AnalyticsService(
                config=self.config.timescale,
                timescale_client=self.timescale_client,
            )
            await self.analytics_service.initialize()

            # Telemetry ingestion service removed for simplification

            # Initialize optimization engine
            if self.config.optimization.enabled:
                self.optimization_engine = OptimizationEngine(
                    config=self.config.optimization,
                    timescale_client=self.timescale_client,
                    supabase_client=self.supabase_client,
                    connection_manager=None,  # Will be set later after WebSocket server created
                    main_api_config=self.config.main_api,
                )
                await self.optimization_engine.start()

            # Initialize price feeder and link to optimization engine
            if self.config.price_feeder.enabled:
                self.price_feeder = PriceFeederService(
                    config=self.config.price_feeder, timescale_client=self.timescale_client
                )
                # Link price feeder to optimization engine
                if self.optimization_engine:
                    self.price_feeder.set_optimization_engine(self.optimization_engine)
                await self.price_feeder.start()

            self.logger.info("TimescaleDB components initialized successfully")

            # Both clients are now live — upgrade health checks to use their pools
            # so subsequent cycles don't create/destroy throwaway connections.
            self._refresh_health_checks()

        except Exception as e:
            self.logger.error(f"Failed to initialize TimescaleDB components: {e}")
            raise

    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating shutdown...")
            # Set flag instead of creating async task from signal handler
            self.running = False

        # Register signal handlers
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # On Unix systems, also handle SIGHUP for graceful restart
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal_handler)


async def run_application(config: Config) -> None:
    """Run the application with proper setup."""
    # Use uvloop for better async performance on Unix systems
    if hasattr(uvloop, "install"):
        uvloop.install()

    # Create application
    app = Application(config)

    # Setup signal handlers
    app.setup_signal_handlers()

    try:
        # Start application
        await app.start()

    except KeyboardInterrupt:
        app.logger.info("Received keyboard interrupt")
    except Exception as e:
        app.logger.error(f"Application error: {e}")
        raise
    finally:
        # Ensure cleanup happens
        await app.stop()


def main() -> None:
    """Main entry point."""
    # Load configuration first so logging can be set up with the right level.
    config = Config.from_env()

    # Configure structlog immediately so ALL log output — including the lines
    # below — uses a single, consistent format (JSON in production).
    setup_logging(config.monitoring)

    logger = get_logger(__name__)

    try:
        logger.info(
            "Loading configuration...",
            ws_bind=f"{config.websocket.host}:{config.websocket.port}",
            api_port=config.monitoring.api_port,
            metrics_port=config.monitoring.metrics_port,
            environment=config.environment,
        )

        if config.debug:
            logger.warning("Debug mode is enabled")

        logger.info("Starting application...")
        asyncio.run(run_application(config))

    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error("Application startup failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
