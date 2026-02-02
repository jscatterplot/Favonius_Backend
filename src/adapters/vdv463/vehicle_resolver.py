"""Vehicle ID resolver for VDV 463 integration.

Maps external vehicleId (from transit systems) to internal vehicle_id UUIDs.
Per PRD Section 9.6: vehicles.external_id -> vehicles.vehicle_id.
"""

from __future__ import annotations

from typing import Optional, Any

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    import logging
    structlog = None  # type: ignore
    _HAS_STRUCTLOG = False


def _stdlib_log_adapter(logger_instance: Any) -> Any:
    """Wrap stdlib logger to accept structlog-style keyword args."""

    def _log(level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        if kwargs:
            extra = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
            msg = f"{msg} {extra}" if msg else extra
        getattr(logger_instance, level)(msg, *args)

    class Adapter:
        def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("warning", msg, *args, **kwargs)

        def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("info", msg, *args, **kwargs)

        def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("error", msg, *args, **kwargs)

        def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("debug", msg, *args, **kwargs)

    return Adapter()


def get_logger(name: str) -> Any:
    """Get a structured logger instance (structlog if available, else stdlib logging)."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _stdlib_log_adapter(logging.getLogger(name))


class VehicleResolver:
    """Resolves external vehicle IDs to internal vehicle UUIDs.

    When db_pool is set, queries vehicles by depot_id and external_id.
    Otherwise uses in-memory cache (testing).
    """

    def __init__(self, db_pool: Any = None):
        """Initialize vehicle resolver.

        Args:
            db_pool: asyncpg Pool for DB lookups. If None, uses in-memory cache only.
        """
        self.logger = get_logger(__name__)
        self.db_pool = db_pool
        self._cache: dict[str, str] = {}

    async def resolve_vehicle_id(
        self,
        vehicle_external_id: str,
        depot_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Resolve external vehicle ID to internal vehicle_id UUID.

        Resolution path: vdv.vehicleId -> vehicles.external_id -> vehicles.vehicle_id.

        Args:
            vehicle_external_id: External vehicle identifier from VDV 463 (e.g. 'bus_101')
            depot_id: Depot UUID string for scoping lookup

        Returns:
            Internal vehicle_id UUID string, or None if not found
        """
        cache_key = f"{depot_id or ''}:{vehicle_external_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if self.db_pool and depot_id:
            try:
                row = await self.db_pool.fetchrow(
                    """
                    SELECT vehicle_id FROM vehicles
                    WHERE depot_id = $1::uuid AND external_id = $2
                    """,
                    depot_id,
                    vehicle_external_id,
                )
                if row:
                    vehicle_id = str(row["vehicle_id"])
                    self._cache[cache_key] = vehicle_id
                    self.logger.debug(
                        "Resolved vehicle",
                        vehicle_external_id=vehicle_external_id,
                        vehicle_id=vehicle_id,
                    )
                    return vehicle_id
            except Exception as e:
                self.logger.warning(
                    "Vehicle resolution failed",
                    vehicle_external_id=vehicle_external_id,
                    depot_id=depot_id,
                    error=str(e),
                )

        self.logger.warning(
            "Vehicle ID not found",
            vehicle_external_id=vehicle_external_id,
            depot_id=depot_id,
        )
        return None

    def add_mapping(
        self,
        vehicle_external_id: str,
        vehicle_id: str,
        depot_id: Optional[str] = None,
    ) -> None:
        """
        Add a vehicle mapping (for testing/development).

        Args:
            vehicle_external_id: External vehicle identifier
            vehicle_id: Internal vehicle UUID string
            depot_id: Optional depot ID for cache key
        """
        cache_key = f"{depot_id or ''}:{vehicle_external_id}"
        self._cache[cache_key] = vehicle_id
        self.logger.debug(
            "Added vehicle mapping",
            vehicle_external_id=vehicle_external_id,
            vehicle_id=vehicle_id,
        )
