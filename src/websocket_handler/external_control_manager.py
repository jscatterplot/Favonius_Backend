"""External Control Manager for EMS/SO/CSO limit signals."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class ExternalControlSource(Enum):
    """External control signal sources."""

    EMS = "EMS"  # Energy Management System
    SO = "SO"  # System Operator
    CSO = "CSO"  # Charging Station Operator
    OTHER = "Other"


class ExternalControlType(Enum):
    """External control signal types."""

    GRID_CRITICAL = "GridCritical"
    LOCAL_GENERATION = "LocalGeneration"
    DEMAND_RESPONSE = "DemandResponse"
    FREQUENCY_REGULATION = "FrequencyRegulation"
    VOLTAGE_REGULATION = "VoltageRegulation"
    EMERGENCY_SHUTDOWN = "EmergencyShutdown"


@dataclass
class ExternalControlSignal:
    """External control signal."""

    signal_id: str
    station_id: str
    evse_id: int
    source: ExternalControlSource
    control_type: ExternalControlType
    signal_value: float
    target_response_kw: float
    response_deadline: datetime
    compensation_rate_kwh: Optional[float] = None
    grid_operator: Optional[str] = None
    region: Optional[str] = None
    is_active: bool = True
    created_at: datetime = None
    expires_at: Optional[datetime] = None


class ExternalControlManager:
    """Manages external control signals from EMS/SO/CSO."""

    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Active external control signals per station
        self.active_signals: Dict[str, List[ExternalControlSignal]] = {}

        # External control limits
        self.max_signals_per_station = 10
        self.max_signals_per_evse = 3

    async def process_external_limit_signal(
        self, station_id: str, evse_id: int, signal_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Process external limit signal from EMS/SO/CSO."""
        try:
            self.logger.info(
                f"Processing external limit signal for station {station_id}, EVSE {evse_id}"
            )

            # Parse signal data
            signal = await self._parse_external_signal(station_id, evse_id, signal_data)
            if not signal:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "PropertyConstraintViolation",
                        "additionalInfo": "Invalid signal data",
                    },
                }

            # Validate signal
            validation_result = await self._validate_external_signal(signal)
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"],
                    },
                }

            # Check limits
            if not await self._check_external_signal_limits(station_id, evse_id):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "ExternalControlLimitExceeded",
                        "additionalInfo": "Maximum external control signals exceeded",
                    },
                }

            # Process signal based on type
            processing_result = await self._process_signal_by_type(signal)
            if not processing_result["success"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "ProcessingError",
                        "additionalInfo": processing_result["message"],
                    },
                }

            # Store signal
            await self._store_external_signal(signal)

            # Add to active signals
            await self._add_active_signal(signal)

            # Send NotifyChargingLimit if required
            if signal.control_type in [
                ExternalControlType.GRID_CRITICAL,
                ExternalControlType.EMERGENCY_SHUTDOWN,
            ]:
                await self._send_notify_charging_limit(signal)

            return {
                "status": "Accepted",
                "statusInfo": {"reasonCode": "NoError"},
                "signalId": signal.signal_id,
                "responseDeadline": signal.response_deadline.isoformat(),
                "targetResponseKw": signal.target_response_kw,
            }

        except Exception as e:
            self.logger.error(f"Error processing external limit signal: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def clear_external_limit_signal(self, station_id: str, signal_id: str) -> Dict[str, Any]:
        """Clear external limit signal."""
        try:
            self.logger.info(f"Clearing external limit signal {signal_id} for station {station_id}")

            # Find active signal
            signal = await self._find_active_signal(station_id, signal_id)
            if not signal:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownSignal",
                        "additionalInfo": f"Signal {signal_id} not found",
                    },
                }

            # Deactivate signal
            await self._deactivate_signal(signal)

            # Update database
            await self._update_signal_status(signal, False)

            # Send ClearedChargingLimit if required
            if signal.control_type in [
                ExternalControlType.GRID_CRITICAL,
                ExternalControlType.EMERGENCY_SHUTDOWN,
            ]:
                await self._send_cleared_charging_limit(signal)

            return {"status": "Accepted", "statusInfo": {"reasonCode": "NoError"}}

        except Exception as e:
            self.logger.error(f"Error clearing external limit signal: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def _parse_external_signal(
        self, station_id: str, evse_id: int, signal_data: Dict[str, Any]
    ) -> Optional[ExternalControlSignal]:
        """Parse external signal data."""
        try:
            signal_id = signal_data.get("signalId", f"ext_{int(datetime.now().timestamp())}")
            source_str = signal_data.get("source", "OTHER")
            control_type_str = signal_data.get("controlType", "DEMAND_RESPONSE")
            signal_value = float(signal_data.get("signalValue", 0.0))
            target_response_kw = float(signal_data.get("targetResponseKw", 0.0))
            response_deadline_str = signal_data.get("responseDeadline")
            compensation_rate_kwh = signal_data.get("compensationRateKwh")
            grid_operator = signal_data.get("gridOperator")
            region = signal_data.get("region")
            expires_at_str = signal_data.get("expiresAt")

            # Parse enums
            try:
                source = ExternalControlSource(source_str)
            except ValueError:
                source = ExternalControlSource.OTHER

            try:
                control_type = ExternalControlType(control_type_str)
            except ValueError:
                control_type = ExternalControlType.DEMAND_RESPONSE

            # Parse timestamps
            response_deadline = (
                datetime.fromisoformat(response_deadline_str.replace("Z", "+00:00"))
                if response_deadline_str
                else datetime.now(timezone.utc)
            )
            expires_at = (
                datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                if expires_at_str
                else None
            )

            return ExternalControlSignal(
                signal_id=signal_id,
                station_id=station_id,
                evse_id=evse_id,
                source=source,
                control_type=control_type,
                signal_value=signal_value,
                target_response_kw=target_response_kw,
                response_deadline=response_deadline,
                compensation_rate_kwh=compensation_rate_kwh,
                grid_operator=grid_operator,
                region=region,
                is_active=True,
                created_at=datetime.now(timezone.utc),
                expires_at=expires_at,
            )

        except Exception as e:
            self.logger.error(f"Error parsing external signal: {e}")
            return None

    async def _validate_external_signal(self, signal: ExternalControlSignal) -> Dict[str, Any]:
        """Validate external control signal."""
        try:
            # Check response deadline
            if signal.response_deadline <= datetime.now(timezone.utc):
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Response deadline must be in the future",
                }

            # Check target response power
            if signal.target_response_kw < 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Target response power must be non-negative",
                }

            # Check signal value constraints based on type
            if signal.control_type == ExternalControlType.GRID_CRITICAL:
                if signal.signal_value < 0 or signal.signal_value > 1:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "Grid critical signal value must be between 0 and 1",
                    }

            return {"valid": True}

        except Exception as e:
            return {
                "valid": False,
                "reason_code": "ValidationError",
                "message": f"Signal validation failed: {e}",
            }

    async def _check_external_signal_limits(self, station_id: str, evse_id: int) -> bool:
        """Check external signal limits."""
        # Check station limit
        station_count = len(self.active_signals.get(station_id, []))
        if station_count >= self.max_signals_per_station:
            return False

        # Check EVSE limit
        evse_count = sum(
            1 for signal in self.active_signals.get(station_id, []) if signal.evse_id == evse_id
        )
        if evse_count >= self.max_signals_per_evse:
            return False

        return True

    async def _process_signal_by_type(self, signal: ExternalControlSignal) -> Dict[str, Any]:
        """Process signal based on its type."""
        try:
            if signal.control_type == ExternalControlType.GRID_CRITICAL:
                return await self._process_grid_critical_signal(signal)
            elif signal.control_type == ExternalControlType.LOCAL_GENERATION:
                return await self._process_local_generation_signal(signal)
            elif signal.control_type == ExternalControlType.DEMAND_RESPONSE:
                return await self._process_demand_response_signal(signal)
            elif signal.control_type == ExternalControlType.FREQUENCY_REGULATION:
                return await self._process_frequency_regulation_signal(signal)
            elif signal.control_type == ExternalControlType.VOLTAGE_REGULATION:
                return await self._process_voltage_regulation_signal(signal)
            elif signal.control_type == ExternalControlType.EMERGENCY_SHUTDOWN:
                return await self._process_emergency_shutdown_signal(signal)
            else:
                return {"success": False, "message": f"Unknown control type: {signal.control_type}"}

        except Exception as e:
            return {"success": False, "message": f"Error processing signal: {e}"}

    async def _process_grid_critical_signal(self, signal: ExternalControlSignal) -> Dict[str, Any]:
        """Process grid critical signal."""
        try:
            # Grid critical signals require immediate response
            self.logger.warning(
                f"Grid critical signal received for station {signal.station_id}: {signal.signal_value}"
            )

            # Create charging limit profile
            await self._create_external_constraints_profile(signal, is_grid_critical=True)

            return {"success": True}

        except Exception as e:
            return {"success": False, "message": f"Error processing grid critical signal: {e}"}

    async def _process_local_generation_signal(
        self, signal: ExternalControlSignal
    ) -> Dict[str, Any]:
        """Process local generation signal."""
        try:
            # Local generation signals add capacity
            self.logger.info(
                f"Local generation signal received for station {signal.station_id}: {signal.signal_value}"
            )

            # Create local generation profile
            await self._create_external_constraints_profile(signal, is_local_generation=True)

            return {"success": True}

        except Exception as e:
            return {"success": False, "message": f"Error processing local generation signal: {e}"}

    async def _process_demand_response_signal(
        self, signal: ExternalControlSignal
    ) -> Dict[str, Any]:
        """Process demand response signal."""
        try:
            # Demand response signals adjust charging behavior
            self.logger.info(
                f"Demand response signal received for station {signal.station_id}: {signal.signal_value}"
            )

            # Create demand response profile
            await self._create_external_constraints_profile(signal, is_demand_response=True)

            return {"success": True}

        except Exception as e:
            return {"success": False, "message": f"Error processing demand response signal: {e}"}

    async def _process_frequency_regulation_signal(
        self, signal: ExternalControlSignal
    ) -> Dict[str, Any]:
        """Process frequency regulation signal."""
        try:
            # Frequency regulation signals adjust power based on grid frequency
            self.logger.info(
                f"Frequency regulation signal received for station {signal.station_id}: {signal.signal_value}"
            )

            # Create frequency regulation profile
            await self._create_external_constraints_profile(signal, is_frequency_regulation=True)

            return {"success": True}

        except Exception as e:
            return {
                "success": False,
                "message": f"Error processing frequency regulation signal: {e}",
            }

    async def _process_voltage_regulation_signal(
        self, signal: ExternalControlSignal
    ) -> Dict[str, Any]:
        """Process voltage regulation signal."""
        try:
            # Voltage regulation signals adjust reactive power
            self.logger.info(
                f"Voltage regulation signal received for station {signal.station_id}: {signal.signal_value}"
            )

            # Create voltage regulation profile
            await self._create_external_constraints_profile(signal, is_voltage_regulation=True)

            return {"success": True}

        except Exception as e:
            return {"success": False, "message": f"Error processing voltage regulation signal: {e}"}

    async def _process_emergency_shutdown_signal(
        self, signal: ExternalControlSignal
    ) -> Dict[str, Any]:
        """Process emergency shutdown signal."""
        try:
            # Emergency shutdown signals require immediate stop
            self.logger.critical(
                f"Emergency shutdown signal received for station {signal.station_id}"
            )

            # Create emergency shutdown profile
            await self._create_external_constraints_profile(signal, is_emergency_shutdown=True)

            return {"success": True}

        except Exception as e:
            return {"success": False, "message": f"Error processing emergency shutdown signal: {e}"}

    async def _create_external_constraints_profile(
        self, signal: ExternalControlSignal, **kwargs
    ) -> None:
        """Create external constraints charging profile."""
        try:
            # This would integrate with the charging profile manager
            # to create appropriate charging profiles based on the signal type

            profile_data = {
                "station_id": signal.station_id,
                "evse_id": signal.evse_id,
                "signal_id": signal.signal_id,
                "source": signal.source.value,
                "control_type": signal.control_type.value,
                "signal_value": signal.signal_value,
                "target_response_kw": signal.target_response_kw,
                "response_deadline": signal.response_deadline,
                "compensation_rate_kwh": signal.compensation_rate_kwh,
                "grid_operator": signal.grid_operator,
                "region": signal.region,
                "is_grid_critical": kwargs.get("is_grid_critical", False),
                "is_local_generation": kwargs.get("is_local_generation", False),
                "is_demand_response": kwargs.get("is_demand_response", False),
                "is_frequency_regulation": kwargs.get("is_frequency_regulation", False),
                "is_voltage_regulation": kwargs.get("is_voltage_regulation", False),
                "is_emergency_shutdown": kwargs.get("is_emergency_shutdown", False),
                "created_at": signal.created_at,
            }

            await self.timescale_client.store_external_charging_limit(profile_data)

        except Exception as e:
            self.logger.error(f"Error creating external constraints profile: {e}")

    async def _store_external_signal(self, signal: ExternalControlSignal) -> None:
        """Store external signal in database."""
        try:
            signal_data = {
                "signal_id": signal.signal_id,
                "station_id": signal.station_id,
                "evse_id": signal.evse_id,
                "source": signal.source.value,
                "control_type": signal.control_type.value,
                "signal_value": signal.signal_value,
                "target_response_kw": signal.target_response_kw,
                "response_deadline": signal.response_deadline,
                "compensation_rate_kwh": signal.compensation_rate_kwh,
                "grid_operator": signal.grid_operator,
                "region": signal.region,
                "is_active": signal.is_active,
                "created_at": signal.created_at,
                "expires_at": signal.expires_at,
            }

            await self.timescale_client.store_external_control_signal(signal_data)

        except Exception as e:
            self.logger.error(f"Error storing external signal: {e}")

    async def _add_active_signal(self, signal: ExternalControlSignal) -> None:
        """Add signal to active signals."""
        if signal.station_id not in self.active_signals:
            self.active_signals[signal.station_id] = []

        self.active_signals[signal.station_id].append(signal)

    async def _find_active_signal(
        self, station_id: str, signal_id: str
    ) -> Optional[ExternalControlSignal]:
        """Find active signal by ID."""
        if station_id not in self.active_signals:
            return None

        for signal in self.active_signals[station_id]:
            if signal.signal_id == signal_id and signal.is_active:
                return signal

        return None

    async def _deactivate_signal(self, signal: ExternalControlSignal) -> None:
        """Deactivate external signal."""
        signal.is_active = False

        # Remove from active signals
        if signal.station_id in self.active_signals:
            self.active_signals[signal.station_id] = [
                s for s in self.active_signals[signal.station_id] if s.signal_id != signal.signal_id
            ]

    async def _update_signal_status(self, signal: ExternalControlSignal, is_active: bool) -> None:
        """Update signal status in database."""
        try:
            await self.timescale_client.update_external_control_signal_status(
                signal.signal_id, is_active
            )
        except Exception as e:
            self.logger.error(f"Error updating signal status: {e}")

    async def _send_notify_charging_limit(self, signal: ExternalControlSignal) -> None:
        """Send NotifyChargingLimit to CSMS."""
        try:
            # This would integrate with the OCPP handler to send the message
            self.logger.info(f"Sending NotifyChargingLimit for signal {signal.signal_id}")

            # For now, just log the action
            # In a real implementation, this would call the OCPP handler

        except Exception as e:
            self.logger.error(f"Error sending NotifyChargingLimit: {e}")

    async def _send_cleared_charging_limit(self, signal: ExternalControlSignal) -> None:
        """Send ClearedChargingLimit to CSMS."""
        try:
            # This would integrate with the OCPP handler to send the message
            self.logger.info(f"Sending ClearedChargingLimit for signal {signal.signal_id}")

            # For now, just log the action
            # In a real implementation, this would call the OCPP handler

        except Exception as e:
            self.logger.error(f"Error sending ClearedChargingLimit: {e}")

    async def get_active_signals(self, station_id: str) -> List[ExternalControlSignal]:
        """Get all active external signals for a station."""
        return self.active_signals.get(station_id, [])

    async def cleanup_expired_signals(self) -> None:
        """Clean up expired external signals."""
        try:
            current_time = datetime.now(timezone.utc)

            for station_id, signals in self.active_signals.items():
                expired_signals = []

                for signal in signals:
                    if signal.expires_at and current_time > signal.expires_at:
                        expired_signals.append(signal)

                # Deactivate expired signals
                for signal in expired_signals:
                    await self._deactivate_signal(signal)
                    await self._update_signal_status(signal, False)

                    self.logger.info(
                        f"Expired external signal {signal.signal_id} for station {station_id}"
                    )

        except Exception as e:
            self.logger.error(f"Error cleaning up expired signals: {e}")

    async def get_external_control_summary(self, station_id: str) -> Dict[str, Any]:
        """Get external control summary for a station."""
        try:
            active_signals = await self.get_active_signals(station_id)

            summary = {
                "station_id": station_id,
                "active_signals_count": len(active_signals),
                "max_signals_per_station": self.max_signals_per_station,
                "max_signals_per_evse": self.max_signals_per_evse,
                "active_signals": [],
            }

            for signal in active_signals:
                summary["active_signals"].append(
                    {
                        "signal_id": signal.signal_id,
                        "evse_id": signal.evse_id,
                        "source": signal.source.value,
                        "control_type": signal.control_type.value,
                        "signal_value": signal.signal_value,
                        "target_response_kw": signal.target_response_kw,
                        "response_deadline": signal.response_deadline.isoformat(),
                        "compensation_rate_kwh": signal.compensation_rate_kwh,
                        "grid_operator": signal.grid_operator,
                        "region": signal.region,
                        "created_at": signal.created_at.isoformat(),
                        "expires_at": signal.expires_at.isoformat() if signal.expires_at else None,
                    }
                )

            return summary

        except Exception as e:
            self.logger.error(f"Error getting external control summary: {e}")
            return {"station_id": station_id, "error": str(e)}
