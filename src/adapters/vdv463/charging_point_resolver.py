"""VDV 463 charging point ID resolution.

Maps VDV 463 chargingPointId strings to internal charger_id UUIDs.
Per PRD Section 9.6: validate charging point IDs against registered chargers.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

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

    return Adapter()


def get_logger(name: str) -> Any:
    """Get a structured logger instance (structlog if available, else stdlib logging)."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _stdlib_log_adapter(logging.getLogger(name))


logger = get_logger(__name__)


class ChargingPointResolver:
    """Resolves VDV 463 chargingPointId to internal charger_id UUID.

    chargingPointId may be ocpp_id (e.g. 'charger_01') or charger_id UUID string.
    """

    def __init__(self, db_pool=None):
        """Initialize resolver.

        Args:
            db_pool: asyncpg Pool for DB lookups. If None, resolve_charging_point_id returns None.
        """
        self.db_pool = db_pool
        self.logger = get_logger(__name__)

    async def resolve_charging_point_id(
        self,
        charging_point_id: str,
        depot_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Resolve VDV 463 chargingPointId to internal charger_id UUID string.

        Lookup order: (1) by charger_id UUID, (2) by ocpp_id.

        Args:
            charging_point_id: VDV 463 chargingPointId (opaque string, often ocpp_id or UUID)
            depot_id: Depot UUID string for scoping

        Returns:
            charger_id UUID string, or None if not found or pool not set
        """
        if not self.db_pool or not charging_point_id or not depot_id:
            return None

        try:
            # Try as UUID first
            try:
                uid = UUID(charging_point_id)
            except ValueError:
                uid = None

            if uid:
                row = await self.db_pool.fetchrow(
                    """
                    SELECT charger_id FROM chargers
                    WHERE depot_id = $1::uuid AND charger_id = $2
                    """,
                    depot_id,
                    uid,
                )
            else:
                row = await self.db_pool.fetchrow(
                    """
                    SELECT charger_id FROM chargers
                    WHERE depot_id = $1::uuid AND ocpp_id = $2
                    """,
                    depot_id,
                    charging_point_id,
                )

            if row:
                self.logger.debug(
                    "Resolved charging point",
                    charging_point_id=charging_point_id,
                    charger_id=str(row["charger_id"]),
                )
                return str(row["charger_id"])
            return None
        except Exception as e:
            self.logger.warning(
                "Charging point resolution failed",
                charging_point_id=charging_point_id,
                depot_id=depot_id,
                error=str(e),
            )
            return None
