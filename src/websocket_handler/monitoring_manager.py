"""Monitoring and Alerting Manager for OCPP 2.0.1."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ocpp.v201.datatypes import ComponentType, MonitoringDataType, StatusInfoType, VariableType
from ocpp.v201.enums import GenericDeviceModelStatusEnumType

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class MonitoringCriterion(Enum):
    """Monitoring criteria types."""

    THRESHOLD_MONITORING = "ThresholdMonitoring"
    DELTA_MONITORING = "DeltaMonitoring"
    PERIODIC_MONITORING = "PeriodicMonitoring"


class AlertSeverity(Enum):
    """Alert severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MonitoringManager:
    """Manages monitoring and alerting for charging stations."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize MonitoringManager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}  # station_id -> monitoring_task

    async def get_monitoring_report(
        self,
        station_id: str,
        request_id: int,
        monitoring_base: str,
        monitoring_criterion: Optional[str] = None,
        component_name: Optional[str] = None,
        variable_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle GetMonitoringReport request."""
        self.logger.info(f"GetMonitoringReport for {station_id}, request_id: {request_id}")

        try:
            # Store monitoring report request
            report_data = {
                "station_id": station_id,
                "request_id": request_id,
                "monitoring_base": monitoring_base,
                "monitoring_criterion": monitoring_criterion,
                "component_name": component_name,
                "variable_name": variable_name,
                "created_at": datetime.now(timezone.utc),
            }
            await self.timescale_client.store_monitoring_report(report_data)

            # Get monitoring data based on criteria
            monitoring_data = await self._get_monitoring_data(
                station_id, monitoring_base, monitoring_criterion, component_name, variable_name
            )

            return {
                "status": GenericDeviceModelStatusEnumType.accepted,
                "monitoringData": monitoring_data,
            }

        except Exception as e:
            self.logger.error(f"Error getting monitoring report: {e}")
            return {
                "status": GenericDeviceModelStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def set_variable_monitoring(
        self,
        station_id: str,
        component_name: str,
        variable_name: str,
        monitoring_criterion: str,
        threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Set variable monitoring configuration."""
        self.logger.info(
            f"SetVariableMonitoring for {station_id}: {component_name}.{variable_name}"
        )

        try:
            # Validate monitoring criterion
            try:
                MonitoringCriterion(monitoring_criterion)
            except ValueError:
                return {
                    "status": GenericDeviceModelStatusEnumType.rejected,
                    "statusInfo": StatusInfoType(
                        reason_code="InvalidCriterion",
                        additional_info=f"Unknown monitoring criterion: {monitoring_criterion}",
                    ),
                }

            # Store monitoring configuration
            monitoring_data = {
                "station_id": station_id,
                "component_name": component_name,
                "variable_name": variable_name,
                "monitoring_criterion": monitoring_criterion,
                "threshold": threshold,
                "enabled": True,
                "created_at": datetime.now(timezone.utc),
            }
            await self.timescale_client.store_variable_monitoring(monitoring_data)

            # Start monitoring task if not already running
            await self._start_monitoring_task(station_id)

            return {"status": GenericDeviceModelStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error setting variable monitoring: {e}")
            return {
                "status": GenericDeviceModelStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def clear_variable_monitoring(
        self, station_id: str, component_name: str, variable_name: str, monitoring_criterion: str
    ) -> Dict[str, Any]:
        """Clear variable monitoring configuration."""
        self.logger.info(
            f"ClearVariableMonitoring for {station_id}: {component_name}.{variable_name}"
        )

        try:
            # Disable monitoring configuration
            monitoring_data = {
                "station_id": station_id,
                "component_name": component_name,
                "variable_name": variable_name,
                "monitoring_criterion": monitoring_criterion,
                "enabled": False,
                "created_at": datetime.now(timezone.utc),
            }
            await self.timescale_client.store_variable_monitoring(monitoring_data)

            return {"status": GenericDeviceModelStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error clearing variable monitoring: {e}")
            return {
                "status": GenericDeviceModelStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def notify_monitoring_report(
        self,
        station_id: str,
        request_id: int,
        monitoring_base: str,
        monitoring_criterion: Optional[str] = None,
        component_name: Optional[str] = None,
        variable_name: Optional[str] = None,
    ) -> None:
        """Handle NotifyMonitoringReport notification."""
        self.logger.info(f"NotifyMonitoringReport from {station_id}, request_id: {request_id}")

        try:
            # Store notified monitoring report
            report_data = {
                "station_id": station_id,
                "request_id": request_id,
                "monitoring_base": monitoring_base,
                "monitoring_criterion": monitoring_criterion,
                "component_name": component_name,
                "variable_name": variable_name,
                "created_at": datetime.now(timezone.utc),
            }
            await self.timescale_client.store_notified_monitoring_report(report_data)

        except Exception as e:
            self.logger.error(f"Error storing notified monitoring report: {e}")

    async def create_alert_rule(
        self,
        station_id: str,
        component_name: str,
        variable_name: str,
        monitoring_criterion: str,
        threshold: float,
        severity: str = "medium",
    ) -> str:
        """Create an alert rule."""
        rule_id = str(uuid.uuid4())

        rule_data = {
            "rule_id": rule_id,
            "station_id": station_id,
            "component_name": component_name,
            "variable_name": variable_name,
            "monitoring_criterion": monitoring_criterion,
            "threshold": threshold,
            "severity": severity,
            "enabled": True,
            "created_at": datetime.now(timezone.utc),
        }

        await self.timescale_client.store_alert_rule(rule_data)
        self.logger.info(
            f"Created alert rule {rule_id} for {station_id}: {component_name}.{variable_name}"
        )

        return rule_id

    async def trigger_alert(
        self,
        station_id: str,
        rule_id: str,
        component_name: str,
        variable_name: str,
        current_value: float,
        threshold: float,
        severity: str,
    ) -> str:
        """Trigger an alert."""
        alert_id = str(uuid.uuid4())

        alert_data = {
            "alert_id": alert_id,
            "station_id": station_id,
            "rule_id": rule_id,
            "component_name": component_name,
            "variable_name": variable_name,
            "current_value": current_value,
            "threshold": threshold,
            "severity": severity,
            "status": "active",
            "triggered_at": datetime.now(timezone.utc),
        }

        await self.timescale_client.store_alert(alert_data)
        self.logger.warning(
            f"Triggered alert {alert_id} for {station_id}: {component_name}.{variable_name} = {current_value}"
        )

        return alert_id

    async def _get_monitoring_data(
        self,
        station_id: str,
        monitoring_base: str,
        monitoring_criterion: Optional[str],
        component_name: Optional[str],
        variable_name: Optional[str],
    ) -> List[MonitoringDataType]:
        """Get monitoring data based on criteria."""
        monitoring_data = []

        if monitoring_base == "Configuration":
            # Get configuration data
            if component_name and variable_name:
                variables = await self.timescale_client.get_device_variables(
                    station_id, component_name, variable_name
                )
                for var in variables:
                    monitoring_data.append(
                        MonitoringDataType(
                            component=ComponentType(name=var["component_name"]),
                            variable=VariableType(name=var["variable_name"]),
                            variable_monitoring=[],
                        )
                    )
            else:
                # Get all configuration data
                variables = await self.timescale_client.get_all_device_variables(station_id)
                for var in variables:
                    monitoring_data.append(
                        MonitoringDataType(
                            component=ComponentType(name=var["component_name"]),
                            variable=VariableType(name=var["variable_name"]),
                            variable_monitoring=[],
                        )
                    )

        elif monitoring_base == "Operational":
            # Get operational data (meter values, status, etc.)
            if component_name and variable_name:
                # Get specific operational data
                operational_data = await self._get_operational_data(
                    station_id, component_name, variable_name
                )
                monitoring_data.extend(operational_data)
            else:
                # Get all operational data
                operational_data = await self._get_all_operational_data(station_id)
                monitoring_data.extend(operational_data)

        elif monitoring_base == "Security":
            # Get security-related data
            security_data = await self._get_security_data(station_id)
            monitoring_data.extend(security_data)

        return monitoring_data

    async def _get_operational_data(
        self, station_id: str, component_name: str, variable_name: str
    ) -> List[MonitoringDataType]:
        """Get operational data for specific component/variable."""
        # This would typically query meter values, status data, etc.
        # For now, return empty list
        return []

    async def _get_all_operational_data(self, station_id: str) -> List[MonitoringDataType]:
        """Get all operational data for station."""
        # This would typically query all meter values, status data, etc.
        # For now, return empty list
        return []

    async def _get_security_data(self, station_id: str) -> List[MonitoringDataType]:
        """Get security-related data."""
        # This would typically query security events, certificate status, etc.
        # For now, return empty list
        return []

    async def _start_monitoring_task(self, station_id: str) -> None:
        """Start monitoring task for station."""
        if station_id not in self.monitoring_tasks:
            task = asyncio.create_task(self._monitoring_loop(station_id))
            self.monitoring_tasks[station_id] = task
            self.logger.info(f"Started monitoring task for station {station_id}")

    async def _monitoring_loop(self, station_id: str) -> None:
        """Main monitoring loop for a station."""
        while True:
            try:
                await self._check_monitoring_rules(station_id)
                await asyncio.sleep(30)  # Check every 30 seconds
            except Exception as e:
                self.logger.error(f"Error in monitoring loop for {station_id}: {e}")
                await asyncio.sleep(60)  # Wait longer on error

    async def _check_monitoring_rules(self, station_id: str) -> None:
        """Check monitoring rules and trigger alerts if needed."""
        try:
            # Get alert rules for station
            rules = await self.timescale_client.get_alert_rules(station_id)

            for rule in rules:
                await self._check_rule(station_id, rule)

        except Exception as e:
            self.logger.error(f"Error checking monitoring rules for {station_id}: {e}")

    async def _check_rule(self, station_id: str, rule: Dict[str, Any]) -> None:
        """Check a single monitoring rule."""
        try:
            component_name = rule["component_name"]
            variable_name = rule["variable_name"]
            monitoring_criterion = rule["monitoring_criterion"]
            threshold = rule["threshold"]
            severity = rule["severity"]
            rule_id = rule["rule_id"]

            # Get current value
            variables = await self.timescale_client.get_device_variables(
                station_id, component_name, variable_name
            )

            if not variables:
                return

            current_value = float(variables[0]["actual_value"])

            # Check if alert should be triggered
            should_trigger = False

            if monitoring_criterion == "ThresholdMonitoring":
                if current_value > threshold:
                    should_trigger = True
            elif monitoring_criterion == "DeltaMonitoring":
                # Get previous value (simplified - would need proper delta calculation)
                previous_data = await self.timescale_client.get_periodic_monitoring_data(
                    station_id,
                    component_name,
                    variable_name,
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                    datetime.now(timezone.utc),
                )
                if previous_data:
                    previous_value = float(previous_data[0]["value"])
                    delta = abs(current_value - previous_value)
                    if delta > threshold:
                        should_trigger = True

            if should_trigger:
                # Check if alert already exists
                active_alerts = await self.timescale_client.get_active_alerts(station_id)
                existing_alert = any(
                    alert["rule_id"] == rule_id and alert["status"] == "active"
                    for alert in active_alerts
                )

                if not existing_alert:
                    await self.trigger_alert(
                        station_id,
                        rule_id,
                        component_name,
                        variable_name,
                        current_value,
                        threshold,
                        severity,
                    )

        except Exception as e:
            self.logger.error(f"Error checking rule {rule.get('rule_id', 'unknown')}: {e}")

    async def stop_monitoring(self, station_id: str) -> None:
        """Stop monitoring for station."""
        if station_id in self.monitoring_tasks:
            task = self.monitoring_tasks[station_id]
            task.cancel()
            del self.monitoring_tasks[station_id]
            self.logger.info(f"Stopped monitoring task for station {station_id}")

    async def cleanup(self) -> None:
        """Cleanup all monitoring tasks."""
        for station_id in list(self.monitoring_tasks.keys()):
            await self.stop_monitoring(station_id)
