"""Display Message Manager for OCPP 2.0.1."""

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ocpp.v201.datatypes import MessageContentType, MessageInfoType, StatusInfoType
from ocpp.v201.enums import (
    DisplayMessageStatusEnumType,
    GenericStatusEnumType,
    MessagePriorityEnumType,
)

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class DisplayMessageType(Enum):
    """Display message types."""

    NORMAL = "Normal"
    INFO = "Info"
    WARNING = "Warning"
    ERROR = "Error"


class DisplayMessageState(Enum):
    """Display message states."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPIRED = "expired"


class DisplayManager:
    """Manages display messages for charging stations."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize DisplayManager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.active_messages: Dict[str, Dict[str, Any]] = {}  # message_id -> message_data

    async def set_display_message(
        self,
        station_id: str,
        message_info: MessageInfoType,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle SetDisplayMessage request."""
        self.logger.info(f"SetDisplayMessage for {station_id}: {message_info.id}")

        try:
            # Validate message info
            if not self._validate_message_info(message_info):
                return {
                    "status": DisplayMessageStatusEnumType.rejected,
                    "statusInfo": StatusInfoType(
                        reason_code="InvalidMessage", additional_info="Message validation failed"
                    ),
                }

            # Generate message ID if not provided
            message_id = message_info.id or str(uuid.uuid4())

            # Store display message
            message_data = {
                "message_id": message_id,
                "station_id": station_id,
                "evse_id": evse_id,
                "connector_id": connector_id,
                "message_type": message_info.priority.value,
                "message_content": self._format_message_content(message_info.message),
                "language": message_info.message.language or "en",
                "priority": self._get_priority_value(message_info.priority),
                "state": DisplayMessageState.ACTIVE.value,
                "valid_from": message_info.display_message.start_date_time,
                "valid_to": message_info.display_message.end_date_time,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_display_message(message_data)

            # Store history
            await self._store_message_history(
                station_id,
                message_id,
                "created",
                {
                    "message_info": message_info.to_json(),
                    "evse_id": evse_id,
                    "connector_id": connector_id,
                },
            )

            # Cache active message
            self.active_messages[message_id] = message_data

            self.logger.info(f"Display message {message_id} set for {station_id}")
            return {"status": DisplayMessageStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error setting display message: {e}")
            return {
                "status": DisplayMessageStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def clear_display_message(
        self,
        station_id: str,
        message_id: Optional[str] = None,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle ClearDisplayMessage request."""
        self.logger.info(f"ClearDisplayMessage for {station_id}: {message_id}")

        try:
            if message_id:
                # Clear specific message
                await self.timescale_client.clear_display_message(message_id)

                # Store history
                await self._store_message_history(
                    station_id, message_id, "cleared", {"cleared_by": "system"}
                )

                # Remove from cache
                if message_id in self.active_messages:
                    del self.active_messages[message_id]

                self.logger.info(f"Display message {message_id} cleared for {station_id}")
            else:
                # Clear all messages for station/EVSE/connector
                await self.timescale_client.clear_all_display_messages(
                    station_id, evse_id, connector_id
                )

                # Store history
                await self._store_message_history(
                    station_id,
                    "all",
                    "cleared",
                    {"evse_id": evse_id, "connector_id": connector_id, "cleared_by": "system"},
                )

                # Remove from cache
                messages_to_remove = []
                for msg_id, msg_data in self.active_messages.items():
                    if (
                        msg_data["station_id"] == station_id
                        and (evse_id is None or msg_data.get("evse_id") == evse_id)
                        and (connector_id is None or msg_data.get("connector_id") == connector_id)
                    ):
                        messages_to_remove.append(msg_id)

                for msg_id in messages_to_remove:
                    del self.active_messages[msg_id]

                self.logger.info(f"All display messages cleared for {station_id}")

            return {"status": GenericStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error clearing display message: {e}")
            return {
                "status": GenericStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def get_display_messages(
        self, station_id: str, evse_id: Optional[int] = None, connector_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get active display messages for station."""
        try:
            messages = await self.timescale_client.get_display_messages(
                station_id, evse_id, connector_id, "active"
            )
            return messages
        except Exception as e:
            self.logger.error(f"Error getting display messages: {e}")
            return []

    async def get_display_message_history(
        self,
        station_id: str,
        message_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get display message history."""
        try:
            history = await self.timescale_client.get_display_message_history(
                station_id, message_id, start_time, end_time
            )
            return history
        except Exception as e:
            self.logger.error(f"Error getting display message history: {e}")
            return []

    async def cleanup_expired_messages(self) -> None:
        """Clean up expired display messages."""
        try:
            await self.timescale_client.cleanup_expired_display_messages()

            # Remove expired messages from cache
            expired_messages = []
            for message_id, message_data in self.active_messages.items():
                if message_data.get("state") == "expired":
                    expired_messages.append(message_id)

            for message_id in expired_messages:
                del self.active_messages[message_id]

        except Exception as e:
            self.logger.error(f"Error cleaning up expired messages: {e}")

    def _validate_message_info(self, message_info: MessageInfoType) -> bool:
        """Validate message info."""
        if not message_info.message:
            return False

        if not message_info.message.content:
            return False

        if message_info.display_message and message_info.display_message.start_date_time:
            if (
                message_info.display_message.end_date_time
                and message_info.display_message.end_date_time
                <= message_info.display_message.start_date_time
            ):
                return False

        return True

    def _format_message_content(self, message_content: MessageContentType) -> str:
        """Format message content for display."""
        if message_content.content:
            return message_content.content

        # Fallback to format if content is not provided
        if message_content.format:
            return f"[{message_content.format.value}] Message"

        return "Display Message"

    def _get_priority_value(self, priority: MessagePriorityEnumType) -> int:
        """Get priority value for sorting."""
        priority_map = {
            MessagePriorityEnumType.always_front: 100,
            MessagePriorityEnumType.inFront: 75,
            MessagePriorityEnumType.normalCycle: 50,
            MessagePriorityEnumType.cyclic: 25,
        }
        return priority_map.get(priority, 50)

    async def _store_message_history(
        self, station_id: str, message_id: str, action: str, details: Dict[str, Any]
    ) -> None:
        """Store message history."""
        try:
            history_data = {
                "message_id": message_id,
                "station_id": station_id,
                "action": action,
                "timestamp": datetime.now(timezone.utc),
                "details": details,
            }
            await self.timescale_client.store_display_message_history(history_data)
        except Exception as e:
            self.logger.error(f"Error storing message history: {e}")

    async def create_system_message(
        self,
        station_id: str,
        message_type: str,
        content: str,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
        priority: int = 50,
        valid_duration: Optional[timedelta] = None,
    ) -> str:
        """Create a system display message."""
        message_id = str(uuid.uuid4())

        valid_from = datetime.now(timezone.utc)
        valid_to = None
        if valid_duration:
            valid_to = valid_from + valid_duration

        message_data = {
            "message_id": message_id,
            "station_id": station_id,
            "evse_id": evse_id,
            "connector_id": connector_id,
            "message_type": message_type,
            "message_content": content,
            "language": "en",
            "priority": priority,
            "state": DisplayMessageState.ACTIVE.value,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "created_at": valid_from,
            "updated_at": valid_from,
        }

        await self.timescale_client.store_display_message(message_data)
        await self._store_message_history(
            station_id,
            message_id,
            "created",
            {
                "system_message": True,
                "message_type": message_type,
                "evse_id": evse_id,
                "connector_id": connector_id,
            },
        )

        self.active_messages[message_id] = message_data
        self.logger.info(f"System message {message_id} created for {station_id}")

        return message_id

    async def create_charging_message(
        self,
        station_id: str,
        evse_id: int,
        connector_id: int,
        message: str,
        message_type: str = "Info",
    ) -> str:
        """Create a charging-related display message."""
        return await self.create_system_message(
            station_id, message_type, message, evse_id, connector_id, 75
        )

    async def create_error_message(
        self,
        station_id: str,
        error_message: str,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
    ) -> str:
        """Create an error display message."""
        return await self.create_system_message(
            station_id, "Error", error_message, evse_id, connector_id, 100
        )

    async def create_warning_message(
        self,
        station_id: str,
        warning_message: str,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
    ) -> str:
        """Create a warning display message."""
        return await self.create_system_message(
            station_id, "Warning", warning_message, evse_id, connector_id, 80
        )
