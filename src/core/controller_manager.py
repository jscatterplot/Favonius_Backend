"""Controller manager for multiple depot controllers.

Reference: Development plan Step 5.2
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import asyncpg

from .controller import DepotController
from .controller_config import ControllerConfig
from .state.assembler import StateAssembler

if TYPE_CHECKING:
    from ..adapters.ocpp.server import OCPPServer

logger = logging.getLogger(__name__)


class ControllerManager:
    """Manages multiple DepotController instances.

    Handles lifecycle of controllers for all active depots, including
    startup, shutdown, health monitoring, and configuration management.

    Reference: Development plan Step 5.2
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        ocpp_server: Optional['OCPPServer'] = None,
        controller_config: Optional[ControllerConfig] = None,
    ):
        """Initialize controller manager.

        Args:
            pool: Database connection pool
            ocpp_server: Optional OCPP server for charger communication
            controller_config: Optional controller configuration
        """
        self.pool = pool
        self.ocpp_server = ocpp_server
        self.controller_config = controller_config or ControllerConfig.from_env()
        self.controllers: dict[str, DepotController] = {}
        self._running = False
        self._controller_tasks: dict[str, asyncio.Task] = {}

        logger.info("Initialized ControllerManager")

    async def start_all_controllers(self) -> None:
        """Start controllers for all active depots.

        Queries database for all depots and starts a controller for each.
        """
        if self._running:
            logger.warning("Controller manager already running")
            return

        self._running = True
        logger.info("Starting controllers for all active depots")

        try:
            # Query all depots from database
            query = """
            SELECT depot_id::text
            FROM depots
            ORDER BY created_at
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)

            depot_ids = [row['depot_id'] for row in rows]

            if not depot_ids:
                logger.warning("No depots found in database")
                return

            logger.info(f"Found {len(depot_ids)} depots, starting controllers...")

            # Start controller for each depot
            for depot_id in depot_ids:
                try:
                    await self.add_controller(depot_id)
                except Exception as e:
                    logger.error(
                        f"Failed to start controller for depot {depot_id}: {e}",
                        exc_info=True
                    )

            logger.info(
                f"Started {len(self.controllers)} controllers "
                f"out of {len(depot_ids)} depots"
            )

        except Exception as e:
            logger.error(f"Error starting controllers: {e}", exc_info=True)
            raise

    async def stop_all_controllers(self) -> None:
        """Stop all controllers gracefully."""
        if not self._running:
            return

        logger.info(f"Stopping {len(self.controllers)} controllers...")
        self._running = False

        # Stop all controllers
        stop_tasks = []
        for depot_id, controller in self.controllers.items():
            stop_tasks.append(
                self._stop_controller_async(depot_id, controller)
            )

        # Wait for all to stop (with timeout)
        if stop_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*stop_tasks, return_exceptions=True),
                    timeout=self.controller_config.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Some controllers did not stop within timeout, "
                    "cancelling tasks"
                )
                for task in stop_tasks:
                    if not task.done():
                        task.cancel()

        self.controllers.clear()
        self._controller_tasks.clear()
        logger.info("All controllers stopped")

    async def _stop_controller_async(
        self, depot_id: str, controller: DepotController
    ) -> None:
        """Stop a single controller asynchronously.

        Args:
            depot_id: Depot identifier
            controller: Controller instance to stop
        """
        try:
            await controller.stop_async(
                timeout=self.controller_config.shutdown_timeout_seconds
            )
        except Exception as e:
            logger.error(
                f"Error stopping controller for depot {depot_id}: {e}",
                exc_info=True
            )

    async def add_controller(
        self, depot_id: str | UUID
    ) -> DepotController:
        """Add and start a controller for a depot.

        Args:
            depot_id: Depot identifier
            config: Optional pre-configured controller (for testing)

        Returns:
            DepotController instance

        Raises:
            ValueError: If depot not found or configuration invalid
        """
        depot_id_str = str(depot_id)

        if depot_id_str in self.controllers:
            logger.debug(f"Controller already exists for depot {depot_id_str}")
            return self.controllers[depot_id_str]

        try:
            # Load depot configuration
            depot_config, _ = await StateAssembler.load_depot_config(
                self.pool, depot_id_str
            )

            # Create controller
            controller = DepotController(
                pool=self.pool,
                depot_id=depot_id_str,
                config=depot_config,
                ocpp_server=self.ocpp_server,
                controller_config=self.controller_config,
            )

            # Start controller in background
            if self._running:
                task = asyncio.create_task(controller.run())
                self._controller_tasks[depot_id_str] = task

            self.controllers[depot_id_str] = controller
            logger.info(f"Added controller for depot {depot_id_str}")

            return controller

        except ValueError as e:
            logger.error(f"Failed to add controller for depot {depot_id_str}: {e}")
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error adding controller for depot {depot_id_str}: {e}",
                exc_info=True
            )
            raise

    async def remove_controller(self, depot_id: str | UUID) -> None:
        """Remove and stop a controller for a depot.

        Args:
            depot_id: Depot identifier
        """
        depot_id_str = str(depot_id)

        if depot_id_str not in self.controllers:
            logger.warning(f"Controller not found for depot {depot_id_str}")
            return

        controller = self.controllers[depot_id_str]
        await self._stop_controller_async(depot_id_str, controller)

        # Cancel task if exists
        if depot_id_str in self._controller_tasks:
            task = self._controller_tasks[depot_id_str]
            if not task.done():
                task.cancel()
            del self._controller_tasks[depot_id_str]

        del self.controllers[depot_id_str]
        logger.info(f"Removed controller for depot {depot_id_str}")

    def get_controller(
        self, depot_id: str | UUID
    ) -> Optional[DepotController]:
        """Get controller instance for a depot.

        Args:
            depot_id: Depot identifier

        Returns:
            DepotController instance or None if not found
        """
        return self.controllers.get(str(depot_id))

    async def get_or_create_controller(
        self, depot_id: str | UUID
    ) -> DepotController:
        """Get existing controller or create new one.

        Args:
            depot_id: Depot identifier

        Returns:
            DepotController instance
        """
        depot_id_str = str(depot_id)
        controller = self.get_controller(depot_id_str)

        if controller is None:
            controller = await self.add_controller(depot_id_str)

        return controller

    def list_controllers(self) -> list[str]:
        """List all active depot IDs with controllers.

        Returns:
            List of depot IDs
        """
        return list(self.controllers.keys())

    async def health_check(self) -> dict[str, dict]:
        """Check health of all controllers.

        Returns:
            Dictionary mapping depot_id to health status
        """
        health_status = {}

        for depot_id, controller in self.controllers.items():
            try:
                # Check if controller is running
                is_running = controller._running
                last_run = controller.last_run_time
                failures = controller._optimization_failures
                circuit_breaker_open = controller._circuit_breaker_open

                health_status[depot_id] = {
                    'running': is_running,
                    'last_run_time': last_run.isoformat() if last_run else None,
                    'optimization_failures': failures,
                    'circuit_breaker_open': circuit_breaker_open,
                    'status': 'healthy' if is_running and not circuit_breaker_open else 'degraded',
                }
            except Exception as e:
                logger.error(
                    f"Error checking health for depot {depot_id}: {e}",
                    exc_info=True
                )
                health_status[depot_id] = {
                    'status': 'error',
                    'error': str(e),
                }

        return health_status

