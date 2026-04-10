"""Diagnostics and firmware management for OCPP 2.0.1."""

import asyncio
import hashlib
import json
import os
import uuid as _uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import aiofiles
import aiohttp

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class LogType(Enum):
    """Log types for diagnostics."""

    DIAGNOSTICS_LOG = "DiagnosticsLog"
    SECURITY_LOG = "SecurityLog"
    FIRMWARE_STATUS_LOG = "FirmwareStatusLog"
    LOCAL_LIST_LOG = "LocalListLog"
    CUSTOM_LOG = "CustomLog"


class LogStatus(Enum):
    """Log status."""

    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    ACCEPTED_CANCELED = "AcceptedCanceled"


class FirmwareStatus(Enum):
    """Firmware status."""

    DOWNLOADED = "Downloaded"
    DOWNLOAD_FAILED = "DownloadFailed"
    DOWNLOADING = "Downloading"
    DOWNLOAD_SCHEDULED = "DownloadScheduled"
    DOWNLOAD_PAUSED = "DownloadPaused"
    IDLE = "Idle"
    INSTALLATION_FAILED = "InstallationFailed"
    INSTALLING = "Installing"
    INSTALLED = "Installed"
    INSTALL_REBOOTING = "InstallRebooting"
    INSTALL_SCHEDULED = "InstallScheduled"
    INSTALL_VERIFICATION_FAILED = "InstallVerificationFailed"
    INVALID_SIGNATURE = "InvalidSignature"
    SIGNATURE_VERIFIED = "SignatureVerified"


class EventType(Enum):
    """Event types for NotifyEvent."""

    HARD_RESET = "HardReset"
    SOFT_RESET = "SoftReset"
    FIRMWARE_UPDATE = "FirmwareUpdate"
    DIAGNOSTICS_STATUS_NOTIFICATION = "DiagnosticsStatusNotification"
    DATA_TRANSFER = "DataTransfer"
    TIMER = "Timer"
    SIGNED_UPDATE_FIRMWARE_STATUS_NOTIFICATION = "SignedUpdateFirmwareStatusNotification"
    SIGNED_FIRMWARE_STATUS_NOTIFICATION = "SignedFirmwareStatusNotification"
    LOG_STATUS_NOTIFICATION = "LogStatusNotification"
    SECURITY_EVENT = "SecurityEvent"
    V2G_CERTIFICATE_EXPIRED = "V2GCertificateExpired"
    V2G_CERTIFICATE_ABOUT_TO_EXPIRE = "V2GCertificateAboutToExpire"
    V2G_CERTIFICATE_INVALID = "V2GCertificateInvalid"
    V2G_CERTIFICATE_REVOKED = "V2GCertificateRevoked"
    V2G_CERTIFICATE_UNKNOWN = "V2GCertificateUnknown"
    V2G_CERTIFICATE_UPDATE_FAILED = "V2GCertificateUpdateFailed"
    V2G_CERTIFICATE_UPDATE_SUCCESS = "V2GCertificateUpdateSuccess"
    V2G_CERTIFICATE_UPDATE_REQUIRED = "V2GCertificateUpdateRequired"
    V2G_CERTIFICATE_UPDATE_NOT_REQUIRED = "V2GCertificateUpdateNotRequired"
    V2G_CERTIFICATE_UPDATE_PENDING = "V2GCertificateUpdatePending"
    V2G_CERTIFICATE_UPDATE_IN_PROGRESS = "V2GCertificateUpdateInProgress"


@dataclass
class LogEntry:
    """Log entry for diagnostics."""

    timestamp: datetime
    level: str
    message: str
    component: Optional[str] = None
    event_type: Optional[str] = None
    additional_info: Optional[Dict[str, Any]] = None


@dataclass
class FirmwareInfo:
    """Firmware information."""

    location: str
    retrieve_date_time: datetime
    install_date_time: Optional[datetime] = None
    signing_certificate: Optional[str] = None
    signature: Optional[str] = None
    signing_certificate_chain: Optional[List[str]] = None
    checksum: Optional[str] = None
    checksum_algorithm: Optional[str] = None


@dataclass
class FirmwareUpdateRequest:
    """Firmware update request."""

    request_id: int
    station_id: str
    firmware_info: FirmwareInfo
    retry_interval: Optional[int] = None
    retries: Optional[int] = None
    retry_back_off_random_range: Optional[int] = None
    request_start_time: Optional[datetime] = None
    request_stop_time: Optional[datetime] = None


class DiagnosticsManager:
    """Manages diagnostics and logging for charging stations."""

    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Active log requests
        self.active_log_requests: Dict[str, Dict[str, Any]] = {}

        # Log storage
        self.log_storage_path = "/tmp/ocpp_logs"
        os.makedirs(self.log_storage_path, exist_ok=True)

    async def get_log(
        self,
        station_id: str,
        log_type: LogType,
        request_id: int,
        retry_count: Optional[int] = None,
        retry_interval: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle GetLog request."""
        try:
            self.logger.info(f"GetLog request for {station_id}: {log_type.value}")

            # Validate log type
            if not await self._is_log_type_supported(station_id, log_type):
                return {
                    "status": LogStatus.REJECTED.value,
                    "statusInfo": {
                        "reasonCode": "NotSupported",
                        "additionalInfo": f"Log type {log_type.value} not supported",
                    },
                }

            # Check if log request is already active
            if station_id in self.active_log_requests:
                return {
                    "status": LogStatus.REJECTED.value,
                    "statusInfo": {
                        "reasonCode": "Busy",
                        "additionalInfo": "Another log request is already active",
                    },
                }

            # Store log request
            log_request = {
                "station_id": station_id,
                "log_type": log_type.value,
                "request_id": request_id,
                "retry_count": retry_count or 3,
                "retry_interval": retry_interval or 60,
                "status": "Accepted",
                "created_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_log_request(log_request)
            self.active_log_requests[station_id] = log_request

            # Trigger log collection
            asyncio.create_task(self._collect_logs(station_id, log_type, request_id))

            return {"status": LogStatus.ACCEPTED.value}

        except Exception as e:
            self.logger.error(f"Error handling GetLog: {e}")
            return {
                "status": LogStatus.REJECTED.value,
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def handle_log_status_notification(
        self, station_id: str, request_id: int, status: str, additional_info: Optional[str] = None
    ) -> None:
        """Handle LogStatusNotification."""
        try:
            self.logger.info(f"LogStatusNotification from {station_id}: {status}")

            # Update log request status
            await self.timescale_client.update_log_request_status(
                station_id, request_id, status, additional_info
            )

            # Remove from active requests if completed
            if status in ["Uploaded", "UploadFailure", "UploadedAndVerified"]:
                self.active_log_requests.pop(station_id, None)

        except Exception as e:
            self.logger.error(f"Error handling LogStatusNotification: {e}")

    async def handle_notify_event(
        self,
        station_id: str,
        event_type: str,
        timestamp: str,
        tech_info: Optional[str] = None,
        additional_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Handle NotifyEvent."""
        try:
            self.logger.info(f"NotifyEvent from {station_id}: {event_type}")

            # Store event
            await self.timescale_client.store_notify_event(
                {
                    "station_id": station_id,
                    "event_type": event_type,
                    "timestamp": datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                    "tech_info": tech_info,
                    "additional_info": json.dumps(additional_info) if additional_info else None,
                }
            )

            # Handle specific event types
            if event_type == EventType.FIRMWARE_UPDATE.value:
                await self._handle_firmware_update_event(station_id, tech_info, additional_info)
            elif event_type == EventType.SECURITY_EVENT.value:
                await self._handle_security_event(station_id, tech_info, additional_info)
            elif event_type in [EventType.HARD_RESET.value, EventType.SOFT_RESET.value]:
                await self._handle_reset_event(station_id, event_type, tech_info)

        except Exception as e:
            self.logger.error(f"Error handling NotifyEvent: {e}")

    async def _collect_logs(self, station_id: str, log_type: LogType, request_id: int) -> None:
        """Collect logs from station."""
        try:
            # Security: use server-generated UUID to prevent path traversal via station_id
            safe_id = _uuid.uuid4().hex[:12]
            log_filename = (
                f"{safe_id}_{log_type.value}_{request_id}_{int(datetime.now().timestamp())}.log"
            )
            log_filepath = os.path.join(self.log_storage_path, log_filename)
            # Path confinement check
            if not os.path.commonpath(
                [os.path.abspath(log_filepath), os.path.abspath(self.log_storage_path)]
            ) == os.path.abspath(self.log_storage_path):
                self.logger.error(
                    "Path traversal attempt blocked for station %s", station_id
                )
                return

            # Collect logs based on type
            if log_type == LogType.DIAGNOSTICS_LOG:
                await self._collect_diagnostics_logs(station_id, log_filepath)
            elif log_type == LogType.SECURITY_LOG:
                await self._collect_security_logs(station_id, log_filepath)
            elif log_type == LogType.FIRMWARE_STATUS_LOG:
                await self._collect_firmware_status_logs(station_id, log_filepath)
            elif log_type == LogType.LOCAL_LIST_LOG:
                await self._collect_local_list_logs(station_id, log_filepath)

            # Compress log file
            zip_filepath = f"{log_filepath}.zip"
            await self._compress_log_file(log_filepath, zip_filepath)

            # Store log file info
            await self.timescale_client.store_log_file(
                {
                    "station_id": station_id,
                    "request_id": request_id,
                    "log_type": log_type.value,
                    "file_path": zip_filepath,
                    "file_size": os.path.getsize(zip_filepath),
                    "created_at": datetime.now(timezone.utc),
                }
            )

            # Clean up original log file
            os.remove(log_filepath)

        except Exception as e:
            self.logger.error(f"Error collecting logs: {e}")

    async def _collect_diagnostics_logs(self, station_id: str, log_filepath: str) -> None:
        """Collect diagnostics logs."""
        try:
            # Get diagnostic logs from database
            logs = await self.timescale_client.get_diagnostic_logs(station_id)

            async with aiofiles.open(log_filepath, "w") as f:
                for log in logs:
                    # Security: strip control characters to prevent log injection
                    import re

                    safe_message = re.sub(
                        r"[\x00-\x1f\x7f-\x9f]", "", str(log.get("message", ""))
                    )
                    safe_level = re.sub(
                        r"[\x00-\x1f\x7f-\x9f]", "", str(log.get("level", "INFO"))
                    )
                    log_line = f"{log['timestamp']} [{safe_level}] {safe_message}\n"
                    await f.write(log_line)

        except Exception as e:
            self.logger.error(f"Error collecting diagnostics logs: {e}")

    async def _collect_security_logs(self, station_id: str, log_filepath: str) -> None:
        """Collect security logs."""
        try:
            # Get security logs from database
            logs = await self.timescale_client.get_security_logs(station_id)

            async with aiofiles.open(log_filepath, "w") as f:
                for log in logs:
                    log_line = (
                        f"{log['timestamp']} [SECURITY] {log['event_type']}: {log['tech_info']}\n"
                    )
                    await f.write(log_line)

        except Exception as e:
            self.logger.error(f"Error collecting security logs: {e}")

    async def _collect_firmware_status_logs(self, station_id: str, log_filepath: str) -> None:
        """Collect firmware status logs."""
        try:
            # Get firmware status logs from database
            logs = await self.timescale_client.get_firmware_status_logs(station_id)

            async with aiofiles.open(log_filepath, "w") as f:
                for log in logs:
                    log_line = (
                        f"{log['timestamp']} [FIRMWARE] {log['status']}: {log['additional_info']}\n"
                    )
                    await f.write(log_line)

        except Exception as e:
            self.logger.error(f"Error collecting firmware status logs: {e}")

    async def _collect_local_list_logs(self, station_id: str, log_filepath: str) -> None:
        """Collect local list logs."""
        try:
            # Get local list logs from database
            logs = await self.timescale_client.get_local_list_logs(station_id)

            async with aiofiles.open(log_filepath, "w") as f:
                for log in logs:
                    log_line = (
                        f"{log['timestamp']} [LOCAL_LIST] {log['action']}: {log['id_token']}\n"
                    )
                    await f.write(log_line)

        except Exception as e:
            self.logger.error(f"Error collecting local list logs: {e}")

    async def _compress_log_file(self, log_filepath: str, zip_filepath: str) -> None:
        """Compress log file."""
        try:
            with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(log_filepath, os.path.basename(log_filepath))

        except Exception as e:
            self.logger.error(f"Error compressing log file: {e}")
            raise

    async def _is_log_type_supported(self, station_id: str, log_type: LogType) -> bool:
        """Check if log type is supported by station."""
        try:
            # Get supported log types from device model
            supported_types = await self.timescale_client.get_supported_log_types(station_id)
            return log_type.value in supported_types

        except Exception as e:
            self.logger.error(f"Error checking log type support: {e}")
            return False

    async def _handle_firmware_update_event(
        self, station_id: str, tech_info: Optional[str], additional_info: Optional[Dict[str, Any]]
    ) -> None:
        """Handle firmware update event."""
        try:
            # Store firmware update event
            await self.timescale_client.store_firmware_update_event(
                {
                    "station_id": station_id,
                    "tech_info": tech_info,
                    "additional_info": json.dumps(additional_info) if additional_info else None,
                    "timestamp": datetime.now(timezone.utc),
                }
            )

        except Exception as e:
            self.logger.error(f"Error handling firmware update event: {e}")

    async def _handle_security_event(
        self, station_id: str, tech_info: Optional[str], additional_info: Optional[Dict[str, Any]]
    ) -> None:
        """Handle security event."""
        try:
            # Store security event
            await self.timescale_client.store_security_event(
                {
                    "station_id": station_id,
                    "event_type": "SecurityEvent",
                    "timestamp": datetime.now(timezone.utc),
                    "tech_info": tech_info,
                    "additional_info": json.dumps(additional_info) if additional_info else None,
                }
            )

        except Exception as e:
            self.logger.error(f"Error handling security event: {e}")

    async def _handle_reset_event(
        self, station_id: str, event_type: str, tech_info: Optional[str]
    ) -> None:
        """Handle reset event."""
        try:
            # Store reset event
            await self.timescale_client.store_reset_event(
                {
                    "station_id": station_id,
                    "reset_type": event_type,
                    "tech_info": tech_info,
                    "timestamp": datetime.now(timezone.utc),
                }
            )

        except Exception as e:
            self.logger.error(f"Error handling reset event: {e}")


class FirmwareManager:
    """Manages firmware updates for charging stations."""

    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Active firmware update requests
        self.active_firmware_requests: Dict[str, FirmwareUpdateRequest] = {}

        # Firmware storage
        self.firmware_storage_path = "/tmp/ocpp_firmware"
        os.makedirs(self.firmware_storage_path, exist_ok=True)

    async def publish_firmware(
        self,
        station_id: str,
        location: str,
        retrieve_date_time: str,
        request_id: int,
        retry_interval: Optional[int] = None,
        retries: Optional[int] = None,
        retry_back_off_random_range: Optional[int] = None,
        checksum: Optional[str] = None,
        checksum_algorithm: Optional[str] = None,
        signing_certificate: Optional[str] = None,
        signature: Optional[str] = None,
        signing_certificate_chain: Optional[List[str]] = None,
        request_start_time: Optional[str] = None,
        request_stop_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle PublishFirmware request."""
        try:
            self.logger.info(f"PublishFirmware request for {station_id}: {location}")

            # Validate firmware location
            if not await self._validate_firmware_location(location):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "InvalidLocation",
                        "additionalInfo": "Invalid firmware location",
                    },
                }

            # Check if firmware update is already active
            if station_id in self.active_firmware_requests:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "Busy",
                        "additionalInfo": "Another firmware update is already active",
                    },
                }

            # Create firmware info
            firmware_info = FirmwareInfo(
                location=location,
                retrieve_date_time=datetime.fromisoformat(
                    retrieve_date_time.replace("Z", "+00:00")
                ),
                signing_certificate=signing_certificate,
                signature=signature,
                signing_certificate_chain=signing_certificate_chain,
                checksum=checksum,
                checksum_algorithm=checksum_algorithm,
            )

            # Create firmware update request
            firmware_request = FirmwareUpdateRequest(
                request_id=request_id,
                station_id=station_id,
                firmware_info=firmware_info,
                retry_interval=retry_interval,
                retries=retries,
                retry_back_off_random_range=retry_back_off_random_range,
                request_start_time=(
                    datetime.fromisoformat(request_start_time.replace("Z", "+00:00"))
                    if request_start_time
                    else None
                ),
                request_stop_time=(
                    datetime.fromisoformat(request_stop_time.replace("Z", "+00:00"))
                    if request_stop_time
                    else None
                ),
            )

            # Store firmware request
            await self.timescale_client.store_firmware_request(
                {
                    "station_id": station_id,
                    "request_id": request_id,
                    "location": location,
                    "retrieve_date_time": firmware_info.retrieve_date_time,
                    "retry_interval": retry_interval,
                    "retries": retries,
                    "retry_back_off_random_range": retry_back_off_random_range,
                    "checksum": checksum,
                    "checksum_algorithm": checksum_algorithm,
                    "signing_certificate": signing_certificate,
                    "signature": signature,
                    "signing_certificate_chain": (
                        json.dumps(signing_certificate_chain) if signing_certificate_chain else None
                    ),
                    "request_start_time": firmware_request.request_start_time,
                    "request_stop_time": firmware_request.request_stop_time,
                    "status": "Accepted",
                    "created_at": datetime.now(timezone.utc),
                }
            )

            # Store in active requests
            self.active_firmware_requests[station_id] = firmware_request

            # Start firmware download
            asyncio.create_task(self._download_firmware(firmware_request))

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Error handling PublishFirmware: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def handle_firmware_status_notification(
        self,
        station_id: str,
        status: str,
        request_id: Optional[int] = None,
        location: Optional[str] = None,
    ) -> None:
        """Handle FirmwareStatusNotification."""
        try:
            self.logger.info(f"FirmwareStatusNotification from {station_id}: {status}")

            # Update firmware request status
            await self.timescale_client.update_firmware_request_status(
                station_id, request_id, status, location
            )

            # Handle status-specific actions
            if status == FirmwareStatus.INSTALLED.value:
                await self._handle_firmware_installed(station_id, request_id)
            elif status == FirmwareStatus.INSTALLATION_FAILED.value:
                await self._handle_firmware_installation_failed(station_id, request_id)
            elif status == FirmwareStatus.DOWNLOAD_FAILED.value:
                await self._handle_firmware_download_failed(station_id, request_id)

            # Remove from active requests if completed
            if status in [
                FirmwareStatus.INSTALLED.value,
                FirmwareStatus.INSTALLATION_FAILED.value,
                FirmwareStatus.DOWNLOAD_FAILED.value,
            ]:
                self.active_firmware_requests.pop(station_id, None)

        except Exception as e:
            self.logger.error(f"Error handling FirmwareStatusNotification: {e}")

    async def unpublish_firmware(self, station_id: str, checksum: str) -> Dict[str, Any]:
        """Handle UnpublishFirmware request."""
        try:
            self.logger.info(f"UnpublishFirmware request for {station_id}: {checksum}")

            # Cancel active firmware request
            if station_id in self.active_firmware_requests:
                firmware_request = self.active_firmware_requests[station_id]
                if firmware_request.firmware_info.checksum == checksum:
                    # Cancel the request
                    await self.timescale_client.cancel_firmware_request(station_id, checksum)
                    self.active_firmware_requests.pop(station_id, None)

                    return {"status": "Accepted"}

            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "NotFound",
                    "additionalInfo": "Firmware request not found",
                },
            }

        except Exception as e:
            self.logger.error(f"Error handling UnpublishFirmware: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def update_firmware(
        self,
        station_id: str,
        location: str,
        retrieve_date_time: str,
        request_id: int,
        retry_interval: Optional[int] = None,
        retries: Optional[int] = None,
        retry_back_off_random_range: Optional[int] = None,
        checksum: Optional[str] = None,
        checksum_algorithm: Optional[str] = None,
        signing_certificate: Optional[str] = None,
        signature: Optional[str] = None,
        signing_certificate_chain: Optional[List[str]] = None,
        request_start_time: Optional[str] = None,
        request_stop_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle UpdateFirmware request."""
        try:
            self.logger.info(f"UpdateFirmware request for {station_id}: {location}")

            # Validate firmware location
            if not await self._validate_firmware_location(location):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "InvalidLocation",
                        "additionalInfo": "Invalid firmware location",
                    },
                }

            # Check if firmware update is already active
            if station_id in self.active_firmware_requests:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "Busy",
                        "additionalInfo": "Another firmware update is already active",
                    },
                }

            # Create firmware info
            firmware_info = FirmwareInfo(
                location=location,
                retrieve_date_time=datetime.fromisoformat(
                    retrieve_date_time.replace("Z", "+00:00")
                ),
                signing_certificate=signing_certificate,
                signature=signature,
                signing_certificate_chain=signing_certificate_chain,
                checksum=checksum,
                checksum_algorithm=checksum_algorithm,
            )

            # Create firmware update request
            firmware_request = FirmwareUpdateRequest(
                request_id=request_id,
                station_id=station_id,
                firmware_info=firmware_info,
                retry_interval=retry_interval,
                retries=retries,
                retry_back_off_random_range=retry_back_off_random_range,
                request_start_time=(
                    datetime.fromisoformat(request_start_time.replace("Z", "+00:00"))
                    if request_start_time
                    else None
                ),
                request_stop_time=(
                    datetime.fromisoformat(request_stop_time.replace("Z", "+00:00"))
                    if request_stop_time
                    else None
                ),
            )

            # Store firmware request
            await self.timescale_client.store_firmware_request(
                {
                    "station_id": station_id,
                    "request_id": request_id,
                    "location": location,
                    "retrieve_date_time": firmware_info.retrieve_date_time,
                    "retry_interval": retry_interval,
                    "retries": retries,
                    "retry_back_off_random_range": retry_back_off_random_range,
                    "checksum": checksum,
                    "checksum_algorithm": checksum_algorithm,
                    "signing_certificate": signing_certificate,
                    "signature": signature,
                    "signing_certificate_chain": (
                        json.dumps(signing_certificate_chain) if signing_certificate_chain else None
                    ),
                    "request_start_time": firmware_request.request_start_time,
                    "request_stop_time": firmware_request.request_stop_time,
                    "status": "Accepted",
                    "created_at": datetime.now(timezone.utc),
                }
            )

            # Store in active requests
            self.active_firmware_requests[station_id] = firmware_request

            # Start firmware download and installation
            asyncio.create_task(self._download_and_install_firmware(firmware_request))

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Error handling UpdateFirmware: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def _download_firmware(self, firmware_request: FirmwareUpdateRequest) -> None:
        """Download firmware from location."""
        try:
            self.logger.info(f"Downloading firmware for {firmware_request.station_id}")

            # Update status to downloading
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.DOWNLOADING.value,
            )

            # Security: validate firmware URL to prevent SSRF
            if not self._validate_firmware_url(firmware_request.firmware_info.location):
                self.logger.error(
                    "Firmware download blocked: invalid URL for station %s",
                    firmware_request.station_id,
                )
                await self.timescale_client.update_firmware_request_status(
                    firmware_request.station_id,
                    firmware_request.request_id,
                    FirmwareStatus.DOWNLOAD_FAILED.value,
                    "Firmware URL validation failed (SSRF protection)",
                )
                return

            # Download firmware
            firmware_data = await self._download_firmware_data(
                firmware_request.firmware_info.location
            )

            # Verify checksum if provided
            if firmware_request.firmware_info.checksum:
                if not await self._verify_firmware_checksum(
                    firmware_data, firmware_request.firmware_info
                ):
                    await self.timescale_client.update_firmware_request_status(
                        firmware_request.station_id,
                        firmware_request.request_id,
                        FirmwareStatus.DOWNLOAD_FAILED.value,
                        "Checksum verification failed",
                    )
                    return

            # Verify signature if provided
            if firmware_request.firmware_info.signature:
                if not await self._verify_firmware_signature(
                    firmware_data, firmware_request.firmware_info
                ):
                    await self.timescale_client.update_firmware_request_status(
                        firmware_request.station_id,
                        firmware_request.request_id,
                        FirmwareStatus.INVALID_SIGNATURE.value,
                        "Signature verification failed",
                    )
                    return

            # Security: use server-generated UUID to prevent path traversal via station_id
            safe_id = _uuid.uuid4().hex[:12]
            firmware_filename = f"{safe_id}_{firmware_request.request_id}.bin"
            firmware_filepath = os.path.join(self.firmware_storage_path, firmware_filename)
            # Path confinement check
            if not os.path.commonpath(
                [
                    os.path.abspath(firmware_filepath),
                    os.path.abspath(self.firmware_storage_path),
                ]
            ) == os.path.abspath(self.firmware_storage_path):
                self.logger.error(
                    "Path traversal attempt blocked for station %s",
                    firmware_request.station_id,
                )
                return

            async with aiofiles.open(firmware_filepath, "wb") as f:
                await f.write(firmware_data)

            # Update status to downloaded
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.DOWNLOADED.value,
            )

            self.logger.info(f"Firmware downloaded for {firmware_request.station_id}")

        except Exception as e:
            self.logger.error(f"Error downloading firmware: {e}")
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.DOWNLOAD_FAILED.value,
                str(e),
            )

    async def _download_and_install_firmware(self, firmware_request: FirmwareUpdateRequest) -> None:
        """Download and install firmware."""
        try:
            # First download the firmware
            await self._download_firmware(firmware_request)

            # Check if download was successful
            current_status = await self.timescale_client.get_firmware_request_status(
                firmware_request.station_id, firmware_request.request_id
            )

            if current_status != FirmwareStatus.DOWNLOADED.value:
                return  # Download failed

            # Update status to installing
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.INSTALLING.value,
            )

            # Install firmware (this would be handled by the station)
            # For now, we'll simulate the installation
            await asyncio.sleep(5)  # Simulate installation time

            # Update status to installed
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.INSTALLED.value,
            )

            self.logger.info(f"Firmware installed for {firmware_request.station_id}")

        except Exception as e:
            self.logger.error(f"Error installing firmware: {e}")
            await self.timescale_client.update_firmware_request_status(
                firmware_request.station_id,
                firmware_request.request_id,
                FirmwareStatus.INSTALLATION_FAILED.value,
                str(e),
            )

    def _validate_firmware_url(self, location: str) -> bool:
        """Validate firmware download URL to prevent SSRF attacks."""
        import ipaddress
        import socket
        from urllib.parse import urlparse as _urlparse

        try:
            parsed = _urlparse(location)

            # Only allow HTTPS
            if parsed.scheme not in ("https",):
                self.logger.warning(
                    "Firmware URL rejected: scheme '%s' not allowed", parsed.scheme
                )
                return False

            if not parsed.hostname:
                return False

            # Resolve hostname and block private/loopback IPs
            try:
                resolved_ips = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
                for family, _type, proto, canonname, sockaddr in resolved_ips:
                    ip = ipaddress.ip_address(sockaddr[0])
                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_reserved
                    ):
                        self.logger.warning(
                            "Firmware URL rejected: resolved to private/reserved IP %s",
                            ip,
                        )
                        return False
            except socket.gaierror:
                self.logger.warning(
                    "Firmware URL rejected: DNS resolution failed for %s",
                    parsed.hostname,
                )
                return False

            # Optional: check against allowlist
            allowed_hosts = os.getenv("ALLOWED_FIRMWARE_HOSTS", "").split(",")
            allowed_hosts = [h.strip() for h in allowed_hosts if h.strip()]
            if allowed_hosts and not any(
                parsed.hostname.endswith(h) for h in allowed_hosts
            ):
                self.logger.warning(
                    "Firmware URL rejected: host %s not in allowlist",
                    parsed.hostname,
                )
                return False

            return True
        except Exception as e:
            self.logger.warning("Firmware URL validation error: %s", e)
            return False

    async def _download_firmware_data(self, location: str) -> bytes:
        """Download firmware data from location."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(location) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        raise Exception(f"Failed to download firmware: HTTP {response.status}")

        except Exception as e:
            self.logger.error(f"Error downloading firmware data: {e}")
            raise

    async def _verify_firmware_checksum(
        self, firmware_data: bytes, firmware_info: FirmwareInfo
    ) -> bool:
        """Verify firmware checksum."""
        try:
            if not firmware_info.checksum or not firmware_info.checksum_algorithm:
                return True  # No checksum to verify

            # Calculate checksum
            if firmware_info.checksum_algorithm.lower() == "sha256":
                calculated_checksum = hashlib.sha256(firmware_data).hexdigest()
            elif firmware_info.checksum_algorithm.lower() == "sha1":
                calculated_checksum = hashlib.sha1(firmware_data).hexdigest()
            elif firmware_info.checksum_algorithm.lower() == "md5":
                calculated_checksum = hashlib.md5(firmware_data).hexdigest()
            else:
                self.logger.warning(
                    f"Unsupported checksum algorithm: {firmware_info.checksum_algorithm}"
                )
                return True  # Skip verification for unsupported algorithms

            return calculated_checksum.lower() == firmware_info.checksum.lower()

        except Exception as e:
            self.logger.error(f"Error verifying firmware checksum: {e}")
            return False

    async def _verify_firmware_signature(
        self, firmware_data: bytes, firmware_info: FirmwareInfo
    ) -> bool:
        """Verify firmware signature."""
        try:
            if not firmware_info.signature or not firmware_info.signing_certificate:
                return True  # No signature to verify

            # This would involve cryptographic signature verification
            # For now, we'll simulate verification
            self.logger.info("Firmware signature verification simulated")
            return True

        except Exception as e:
            self.logger.error(f"Error verifying firmware signature: {e}")
            return False

    async def _validate_firmware_location(self, location: str) -> bool:
        """Validate firmware location."""
        try:
            # Check if location is a valid URL
            if not location.startswith(("http://", "https://", "ftp://")):
                return False

            # Check if location is accessible
            async with aiohttp.ClientSession() as session:
                async with session.head(location) as response:
                    return response.status == 200

        except Exception as e:
            self.logger.error(f"Error validating firmware location: {e}")
            return False

    async def _handle_firmware_installed(self, station_id: str, request_id: Optional[int]) -> None:
        """Handle firmware installed event."""
        try:
            # Update station firmware version
            firmware_info = await self.timescale_client.get_firmware_request_info(
                station_id, request_id
            )
            if firmware_info:
                await self.timescale_client.update_station_firmware_version(
                    station_id, firmware_info.get("version", "Unknown")
                )

        except Exception as e:
            self.logger.error(f"Error handling firmware installed: {e}")

    async def _handle_firmware_installation_failed(
        self, station_id: str, request_id: Optional[int]
    ) -> None:
        """Handle firmware installation failed event."""
        try:
            # Log installation failure
            await self.timescale_client.store_firmware_failure_event(
                {
                    "station_id": station_id,
                    "request_id": request_id,
                    "failure_type": "InstallationFailed",
                    "timestamp": datetime.now(timezone.utc),
                }
            )

        except Exception as e:
            self.logger.error(f"Error handling firmware installation failed: {e}")

    async def _handle_firmware_download_failed(
        self, station_id: str, request_id: Optional[int]
    ) -> None:
        """Handle firmware download failed event."""
        try:
            # Log download failure
            await self.timescale_client.store_firmware_failure_event(
                {
                    "station_id": station_id,
                    "request_id": request_id,
                    "failure_type": "DownloadFailed",
                    "timestamp": datetime.now(timezone.utc),
                }
            )

        except Exception as e:
            self.logger.error(f"Error handling firmware download failed: {e}")
