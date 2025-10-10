"""Advanced monitoring and alerting system for OCPP 2.0.1."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Set, Tuple
import numpy as np

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class MonitoringBase(Enum):
    """Monitoring base types."""
    CONFIGURATION_INVENTORY = "ConfigurationInventory"
    FULL_INVENTORY = "FullInventory"
    SUMMARY_INVENTORY = "SummaryInventory"


class MonitoringCriterion(Enum):
    """Monitoring criteria."""
    DELTA = "Delta"
    PERIODIC = "Periodic"
    THRESHOLD_MONITORING = "ThresholdMonitoring"


class Severity(Enum):
    """Alert severity levels."""
    INFO = "Info"
    WARNING = "Warning"
    ERROR = "Error"
    CRITICAL = "Critical"


class AlertStatus(Enum):
    """Alert status."""
    ACTIVE = "Active"
    ACKNOWLEDGED = "Acknowledged"
    RESOLVED = "Resolved"
    SUPPRESSED = "Suppressed"


@dataclass
class VariableMonitoring:
    """Variable monitoring configuration."""
    component: Dict[str, str]
    variable: Dict[str, str]
    monitoring_criterion: MonitoringCriterion
    severity: Severity
    threshold: Optional[float] = None
    delta: Optional[float] = None
    period: Optional[int] = None
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AlertRule:
    """Alert rule definition."""
    rule_id: str
    name: str
    description: str
    component_name: str
    variable_name: str
    condition: str  # e.g., ">", "<", "==", "!=", ">=", "<="
    threshold: float
    severity: Severity
    enabled: bool = True
    cooldown_minutes: int = 5
    notification_channels: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Alert:
    """Alert instance."""
    alert_id: str
    rule_id: str
    station_id: str
    component_name: str
    variable_name: str
    current_value: Any
    threshold_value: float
    severity: Severity
    status: AlertStatus
    message: str
    triggered_at: datetime
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    additional_info: Optional[Dict[str, Any]] = None


@dataclass
class MonitoringReport:
    """Monitoring report data."""
    station_id: str
    request_id: int
    generated_at: datetime
    tbc: bool
    seq_no: int
    report_data: List[Dict[str, Any]]
    component_variables: List[Dict[str, Any]] = field(default_factory=list)


class MonitoringAlertingManager:
    """Advanced monitoring and alerting system."""
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Variable monitoring cache
        self.variable_monitoring: Dict[str, List[VariableMonitoring]] = {}
        
        # Alert rules cache
        self.alert_rules: Dict[str, AlertRule] = {}
        
        # Active alerts cache
        self.active_alerts: Dict[str, Alert] = {}
        
        # Monitoring data cache
        self.monitoring_data_cache: Dict[str, Dict[str, Any]] = {}
        
        # Alert cooldown tracking
        self.alert_cooldowns: Dict[str, datetime] = {}
        
        # Background monitoring task
        self.monitoring_task: Optional[asyncio.Task] = None
        self.monitoring_running = False
    
    async def start_monitoring(self) -> None:
        """Start background monitoring task."""
        if self.monitoring_running:
            return
        
        self.monitoring_running = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        self.logger.info("Monitoring and alerting system started")
    
    async def stop_monitoring(self) -> None:
        """Stop background monitoring task."""
        self.monitoring_running = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Monitoring and alerting system stopped")
    
    async def get_monitoring_report(self, station_id: str, request_id: int,
                                  monitoring_base: MonitoringBase,
                                  monitoring_criteria: Optional[List[MonitoringCriterion]] = None,
                                  component_variable: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Handle GetMonitoringReport request."""
        try:
            self.logger.info(f"GetMonitoringReport for {station_id}: {monitoring_base.value}")
            
            # Generate monitoring report
            report = await self._generate_monitoring_report(
                station_id, request_id, monitoring_base, monitoring_criteria, component_variable
            )
            
            # Store monitoring report
            await self.timescale_client.store_monitoring_report({
                "station_id": station_id,
                "request_id": request_id,
                "monitoring_base": monitoring_base.value,
                "monitoring_criteria": json.dumps([c.value for c in monitoring_criteria]) if monitoring_criteria else None,
                "component_variable": json.dumps(component_variable) if component_variable else None,
                "generated_at": report.generated_at,
                "tbc": report.tbc,
                "seq_no": report.seq_no,
                "report_data": json.dumps(report.report_data),
                "created_at": datetime.now(timezone.utc)
            })
            
            return {
                "status": "Accepted",
                "request_id": request_id
            }
            
        except Exception as e:
            self.logger.error(f"Error handling GetMonitoringReport: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def set_variable_monitoring(self, station_id: str, monitoring_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Handle SetVariableMonitoring request."""
        try:
            self.logger.info(f"SetVariableMonitoring for {station_id}: {len(monitoring_data)} variables")
            
            # Process each monitoring configuration
            results = []
            for monitor_config in monitoring_data:
                result = await self._set_variable_monitoring(station_id, monitor_config)
                results.append(result)
            
            # Check if any failed
            failed_count = sum(1 for r in results if r["status"] != "Accepted")
            
            if failed_count == 0:
                return {"status": "Accepted"}
            else:
                return {
                    "status": "PartiallyAccepted",
                    "statusInfo": {
                        "reasonCode": "PartialFailure",
                        "additionalInfo": f"{failed_count} out of {len(results)} monitoring configurations failed"
                    }
                }
            
        except Exception as e:
            self.logger.error(f"Error handling SetVariableMonitoring: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def clear_variable_monitoring(self, station_id: str, 
                                      component_variable: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Handle ClearVariableMonitoring request."""
        try:
            self.logger.info(f"ClearVariableMonitoring for {station_id}")
            
            if component_variable:
                # Clear specific variables
                for var_config in component_variable:
                    await self._clear_specific_variable_monitoring(station_id, var_config)
            else:
                # Clear all variable monitoring for station
                await self._clear_all_variable_monitoring(station_id)
            
            return {"status": "Accepted"}
            
        except Exception as e:
            self.logger.error(f"Error handling ClearVariableMonitoring: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def handle_notify_monitoring_report(self, station_id: str, request_id: int,
                                            generated_at: str, tbc: bool, seq_no: int,
                                            report_data: List[Dict[str, Any]]) -> None:
        """Handle NotifyMonitoringReport."""
        try:
            self.logger.info(f"NotifyMonitoringReport from {station_id}: Request ID {request_id}")
            
            # Store monitoring report
            await self.timescale_client.store_notified_monitoring_report({
                "station_id": station_id,
                "request_id": request_id,
                "generated_at": datetime.fromisoformat(generated_at.replace('Z', '+00:00')),
                "tbc": tbc,
                "seq_no": seq_no,
                "report_data": json.dumps(report_data),
                "created_at": datetime.now(timezone.utc)
            })
            
            # Process monitoring data for alerts
            await self._process_monitoring_data_for_alerts(station_id, report_data)
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyMonitoringReport: {e}")
    
    async def add_alert_rule(self, rule: AlertRule) -> bool:
        """Add alert rule."""
        try:
            # Store alert rule
            await self.timescale_client.store_alert_rule({
                "rule_id": rule.rule_id,
                "name": rule.name,
                "description": rule.description,
                "component_name": rule.component_name,
                "variable_name": rule.variable_name,
                "condition": rule.condition,
                "threshold": rule.threshold,
                "severity": rule.severity.value,
                "enabled": rule.enabled,
                "cooldown_minutes": rule.cooldown_minutes,
                "notification_channels": json.dumps(rule.notification_channels),
                "created_at": rule.created_at
            })
            
            # Update cache
            self.alert_rules[rule.rule_id] = rule
            
            self.logger.info(f"Added alert rule: {rule.name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error adding alert rule: {e}")
            return False
    
    async def remove_alert_rule(self, rule_id: str) -> bool:
        """Remove alert rule."""
        try:
            # Remove from database
            await self.timescale_client.remove_alert_rule(rule_id)
            
            # Remove from cache
            self.alert_rules.pop(rule_id, None)
            
            self.logger.info(f"Removed alert rule: {rule_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error removing alert rule: {e}")
            return False
    
    async def get_active_alerts(self, station_id: Optional[str] = None,
                              severity: Optional[Severity] = None) -> List[Alert]:
        """Get active alerts."""
        try:
            alerts_data = await self.timescale_client.get_active_alerts(station_id, severity.value if severity else None)
            
            alerts = []
            for alert_data in alerts_data:
                alert = Alert(
                    alert_id=alert_data["alert_id"],
                    rule_id=alert_data["rule_id"],
                    station_id=alert_data["station_id"],
                    component_name=alert_data["component_name"],
                    variable_name=alert_data["variable_name"],
                    current_value=alert_data["current_value"],
                    threshold_value=alert_data["threshold_value"],
                    severity=Severity(alert_data["severity"]),
                    status=AlertStatus(alert_data["status"]),
                    message=alert_data["message"],
                    triggered_at=alert_data["triggered_at"],
                    acknowledged_at=alert_data.get("acknowledged_at"),
                    resolved_at=alert_data.get("resolved_at"),
                    additional_info=json.loads(alert_data["additional_info"]) if alert_data.get("additional_info") else None
                )
                alerts.append(alert)
            
            return alerts
            
        except Exception as e:
            self.logger.error(f"Error getting active alerts: {e}")
            return []
    
    async def acknowledge_alert(self, alert_id: str, acknowledged_by: str) -> bool:
        """Acknowledge alert."""
        try:
            # Update alert status
            await self.timescale_client.acknowledge_alert(alert_id, acknowledged_by, datetime.now(timezone.utc))
            
            # Update cache
            if alert_id in self.active_alerts:
                self.active_alerts[alert_id].status = AlertStatus.ACKNOWLEDGED
                self.active_alerts[alert_id].acknowledged_at = datetime.now(timezone.utc)
            
            self.logger.info(f"Alert {alert_id} acknowledged by {acknowledged_by}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error acknowledging alert: {e}")
            return False
    
    async def resolve_alert(self, alert_id: str, resolved_by: str) -> bool:
        """Resolve alert."""
        try:
            # Update alert status
            await self.timescale_client.resolve_alert(alert_id, resolved_by, datetime.now(timezone.utc))
            
            # Remove from active alerts cache
            self.active_alerts.pop(alert_id, None)
            
            self.logger.info(f"Alert {alert_id} resolved by {resolved_by}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error resolving alert: {e}")
            return False
    
    async def _monitoring_loop(self) -> None:
        """Background monitoring loop."""
        while self.monitoring_running:
            try:
                # Check all active variable monitoring
                await self._check_variable_monitoring()
                
                # Check alert rules
                await self._check_alert_rules()
                
                # Clean up old alerts
                await self._cleanup_old_alerts()
                
                # Wait before next check
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(10)
    
    async def _generate_monitoring_report(self, station_id: str, request_id: int,
                                        monitoring_base: MonitoringBase,
                                        monitoring_criteria: Optional[List[MonitoringCriterion]],
                                        component_variable: Optional[List[Dict[str, Any]]]) -> MonitoringReport:
        """Generate monitoring report."""
        try:
            # Get current time
            generated_at = datetime.now(timezone.utc)
            
            # Get monitoring data based on base type
            if monitoring_base == MonitoringBase.CONFIGURATION_INVENTORY:
                report_data = await self._get_configuration_inventory(station_id)
            elif monitoring_base == MonitoringBase.FULL_INVENTORY:
                report_data = await self._get_full_inventory(station_id)
            elif monitoring_base == MonitoringBase.SUMMARY_INVENTORY:
                report_data = await self._get_summary_inventory(station_id)
            else:
                report_data = []
            
            # Filter by monitoring criteria if specified
            if monitoring_criteria:
                report_data = await self._filter_by_monitoring_criteria(report_data, monitoring_criteria)
            
            # Filter by component variable if specified
            if component_variable:
                report_data = await self._filter_by_component_variable(report_data, component_variable)
            
            return MonitoringReport(
                station_id=station_id,
                request_id=request_id,
                generated_at=generated_at,
                tbc=False,  # To be continued
                seq_no=1,
                report_data=report_data
            )
            
        except Exception as e:
            self.logger.error(f"Error generating monitoring report: {e}")
            raise
    
    async def _get_configuration_inventory(self, station_id: str) -> List[Dict[str, Any]]:
        """Get configuration inventory."""
        try:
            # Get all configuration variables
            config_variables = await self.timescale_client.get_configuration_variables(station_id)
            
            report_data = []
            for var in config_variables:
                report_data.append({
                    "component": {"name": var["component_name"], "instance": var["component_instance"]},
                    "variable": {"name": var["variable_name"], "instance": var["variable_instance"]},
                    "variable_attributes": [
                        {"type": "Actual", "value": var["actual_value"]},
                        {"type": "Target", "value": var["target_value"]},
                        {"type": "Default", "value": var["default_value"]}
                    ]
                })
            
            return report_data
            
        except Exception as e:
            self.logger.error(f"Error getting configuration inventory: {e}")
            return []
    
    async def _get_full_inventory(self, station_id: str) -> List[Dict[str, Any]]:
        """Get full inventory."""
        try:
            # Get all variables
            all_variables = await self.timescale_client.get_all_variables(station_id)
            
            report_data = []
            for var in all_variables:
                report_data.append({
                    "component": {"name": var["component_name"], "instance": var["component_instance"]},
                    "variable": {"name": var["variable_name"], "instance": var["variable_instance"]},
                    "variable_attributes": [
                        {"type": "Actual", "value": var["actual_value"]},
                        {"type": "Target", "value": var["target_value"]},
                        {"type": "Default", "value": var["default_value"]},
                        {"type": "MinSet", "value": var["min_set_value"]},
                        {"type": "MaxSet", "value": var["max_set_value"]}
                    ]
                })
            
            return report_data
            
        except Exception as e:
            self.logger.error(f"Error getting full inventory: {e}")
            return []
    
    async def _get_summary_inventory(self, station_id: str) -> List[Dict[str, Any]]:
        """Get summary inventory."""
        try:
            # Get summary of components and variables
            summary = await self.timescale_client.get_inventory_summary(station_id)
            
            report_data = []
            for component in summary:
                report_data.append({
                    "component": {"name": component["component_name"], "instance": component["instance"]},
                    "variable_count": component["variable_count"],
                    "last_updated": component["last_updated"]
                })
            
            return report_data
            
        except Exception as e:
            self.logger.error(f"Error getting summary inventory: {e}")
            return []
    
    async def _filter_by_monitoring_criteria(self, report_data: List[Dict[str, Any]],
                                           monitoring_criteria: List[MonitoringCriterion]) -> List[Dict[str, Any]]:
        """Filter report data by monitoring criteria."""
        try:
            filtered_data = []
            
            for data in report_data:
                # Check if data matches any monitoring criteria
                for criterion in monitoring_criteria:
                    if await self._matches_monitoring_criterion(data, criterion):
                        filtered_data.append(data)
                        break
            
            return filtered_data
            
        except Exception as e:
            self.logger.error(f"Error filtering by monitoring criteria: {e}")
            return report_data
    
    async def _filter_by_component_variable(self, report_data: List[Dict[str, Any]],
                                          component_variable: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter report data by component variable."""
        try:
            filtered_data = []
            
            for data in report_data:
                component = data.get("component", {})
                variable = data.get("variable", {})
                
                # Check if data matches any component variable filter
                for filter_item in component_variable:
                    filter_component = filter_item.get("component", {})
                    filter_variable = filter_item.get("variable", {})
                    
                    if (component.get("name") == filter_component.get("name") and
                        component.get("instance") == filter_component.get("instance") and
                        variable.get("name") == filter_variable.get("name") and
                        variable.get("instance") == filter_variable.get("instance")):
                        filtered_data.append(data)
                        break
            
            return filtered_data
            
        except Exception as e:
            self.logger.error(f"Error filtering by component variable: {e}")
            return report_data
    
    async def _matches_monitoring_criterion(self, data: Dict[str, Any], criterion: MonitoringCriterion) -> bool:
        """Check if data matches monitoring criterion."""
        try:
            if criterion == MonitoringCriterion.DELTA:
                # Check if variable has delta monitoring
                return await self._has_delta_monitoring(data)
            elif criterion == MonitoringCriterion.PERIODIC:
                # Check if variable has periodic monitoring
                return await self._has_periodic_monitoring(data)
            elif criterion == MonitoringCriterion.THRESHOLD_MONITORING:
                # Check if variable has threshold monitoring
                return await self._has_threshold_monitoring(data)
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking monitoring criterion: {e}")
            return False
    
    async def _has_delta_monitoring(self, data: Dict[str, Any]) -> bool:
        """Check if variable has delta monitoring."""
        try:
            component = data.get("component", {})
            variable = data.get("variable", {})
            
            # Check if variable has delta monitoring configured
            monitoring_config = await self.timescale_client.get_variable_monitoring_config(
                component.get("name"), variable.get("name"), MonitoringCriterion.DELTA.value
            )
            
            return monitoring_config is not None
            
        except Exception as e:
            self.logger.error(f"Error checking delta monitoring: {e}")
            return False
    
    async def _has_periodic_monitoring(self, data: Dict[str, Any]) -> bool:
        """Check if variable has periodic monitoring."""
        try:
            component = data.get("component", {})
            variable = data.get("variable", {})
            
            # Check if variable has periodic monitoring configured
            monitoring_config = await self.timescale_client.get_variable_monitoring_config(
                component.get("name"), variable.get("name"), MonitoringCriterion.PERIODIC.value
            )
            
            return monitoring_config is not None
            
        except Exception as e:
            self.logger.error(f"Error checking periodic monitoring: {e}")
            return False
    
    async def _has_threshold_monitoring(self, data: Dict[str, Any]) -> bool:
        """Check if variable has threshold monitoring."""
        try:
            component = data.get("component", {})
            variable = data.get("variable", {})
            
            # Check if variable has threshold monitoring configured
            monitoring_config = await self.timescale_client.get_variable_monitoring_config(
                component.get("name"), variable.get("name"), MonitoringCriterion.THRESHOLD_MONITORING.value
            )
            
            return monitoring_config is not None
            
        except Exception as e:
            self.logger.error(f"Error checking threshold monitoring: {e}")
            return False
    
    async def _set_variable_monitoring(self, station_id: str, monitor_config: Dict[str, Any]) -> Dict[str, Any]:
        """Set variable monitoring configuration."""
        try:
            component = monitor_config.get("component", {})
            variable = monitor_config.get("variable", {})
            monitoring_criterion = monitor_config.get("monitoringCriterion")
            severity = monitor_config.get("severity")
            
            # Validate monitoring configuration
            if not await self._validate_monitoring_config(monitor_config):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "InvalidConfiguration",
                        "additionalInfo": "Invalid monitoring configuration"
                    }
                }
            
            # Store monitoring configuration
            await self.timescale_client.store_variable_monitoring({
                "station_id": station_id,
                "component_name": component.get("name"),
                "component_instance": component.get("instance", ""),
                "variable_name": variable.get("name"),
                "variable_instance": variable.get("instance", ""),
                "monitoring_criterion": monitoring_criterion,
                "severity": severity,
                "threshold": monitor_config.get("threshold"),
                "delta": monitor_config.get("delta"),
                "period": monitor_config.get("period"),
                "enabled": True,
                "created_at": datetime.now(timezone.utc)
            })
            
            # Update cache
            cache_key = f"{station_id}:{component.get('name')}:{variable.get('name')}"
            if cache_key not in self.variable_monitoring:
                self.variable_monitoring[cache_key] = []
            
            monitoring = VariableMonitoring(
                component=component,
                variable=variable,
                monitoring_criterion=MonitoringCriterion(monitoring_criterion),
                severity=Severity(severity),
                threshold=monitor_config.get("threshold"),
                delta=monitor_config.get("delta"),
                period=monitor_config.get("period")
            )
            
            self.variable_monitoring[cache_key].append(monitoring)
            
            return {"status": "Accepted"}
            
        except Exception as e:
            self.logger.error(f"Error setting variable monitoring: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def _clear_specific_variable_monitoring(self, station_id: str, var_config: Dict[str, Any]) -> None:
        """Clear specific variable monitoring."""
        try:
            component = var_config.get("component", {})
            variable = var_config.get("variable", {})
            
            # Remove from database
            await self.timescale_client.remove_variable_monitoring(
                station_id, component.get("name"), component.get("instance", ""),
                variable.get("name"), variable.get("instance", "")
            )
            
            # Remove from cache
            cache_key = f"{station_id}:{component.get('name')}:{variable.get('name')}"
            self.variable_monitoring.pop(cache_key, None)
            
        except Exception as e:
            self.logger.error(f"Error clearing specific variable monitoring: {e}")
    
    async def _clear_all_variable_monitoring(self, station_id: str) -> None:
        """Clear all variable monitoring for station."""
        try:
            # Remove from database
            await self.timescale_client.remove_all_variable_monitoring(station_id)
            
            # Remove from cache
            keys_to_remove = [key for key in self.variable_monitoring.keys() if key.startswith(f"{station_id}:")]
            for key in keys_to_remove:
                self.variable_monitoring.pop(key, None)
            
        except Exception as e:
            self.logger.error(f"Error clearing all variable monitoring: {e}")
    
    async def _check_variable_monitoring(self) -> None:
        """Check variable monitoring for all stations."""
        try:
            # Get all active variable monitoring
            all_monitoring = await self.timescale_client.get_all_active_variable_monitoring()
            
            for monitoring_data in all_monitoring:
                station_id = monitoring_data["station_id"]
                component_name = monitoring_data["component_name"]
                variable_name = monitoring_data["variable_name"]
                
                # Get current variable value
                current_value = await self.timescale_client.get_current_variable_value(
                    station_id, component_name, variable_name
                )
                
                if current_value is not None:
                    # Check monitoring criteria
                    await self._check_monitoring_criteria(monitoring_data, current_value)
            
        except Exception as e:
            self.logger.error(f"Error checking variable monitoring: {e}")
    
    async def _check_monitoring_criteria(self, monitoring_data: Dict[str, Any], current_value: Any) -> None:
        """Check monitoring criteria for a variable."""
        try:
            station_id = monitoring_data["station_id"]
            component_name = monitoring_data["component_name"]
            variable_name = monitoring_data["variable_name"]
            monitoring_criterion = monitoring_data["monitoring_criterion"]
            threshold = monitoring_data.get("threshold")
            delta = monitoring_data.get("delta")
            severity = monitoring_data["severity"]
            
            # Check based on monitoring criterion
            if monitoring_criterion == MonitoringCriterion.THRESHOLD_MONITORING.value:
                await self._check_threshold_monitoring(
                    station_id, component_name, variable_name, current_value, threshold, severity
                )
            elif monitoring_criterion == MonitoringCriterion.DELTA.value:
                await self._check_delta_monitoring(
                    station_id, component_name, variable_name, current_value, delta, severity
                )
            elif monitoring_criterion == MonitoringCriterion.PERIODIC.value:
                await self._check_periodic_monitoring(
                    station_id, component_name, variable_name, current_value, severity
                )
            
        except Exception as e:
            self.logger.error(f"Error checking monitoring criteria: {e}")
    
    async def _check_threshold_monitoring(self, station_id: str, component_name: str, variable_name: str,
                                        current_value: Any, threshold: float, severity: str) -> None:
        """Check threshold monitoring."""
        try:
            # Convert current value to float for comparison
            try:
                current_float = float(current_value)
            except (ValueError, TypeError):
                return  # Skip non-numeric values
            
            # Check if threshold is exceeded
            if current_float > threshold:
                await self._trigger_alert(
                    station_id, component_name, variable_name, current_value, threshold,
                    Severity(severity), f"Value {current_float} exceeds threshold {threshold}"
                )
            
        except Exception as e:
            self.logger.error(f"Error checking threshold monitoring: {e}")
    
    async def _check_delta_monitoring(self, station_id: str, component_name: str, variable_name: str,
                                    current_value: Any, delta: float, severity: str) -> None:
        """Check delta monitoring."""
        try:
            # Get previous value
            previous_value = await self.timescale_client.get_previous_variable_value(
                station_id, component_name, variable_name
            )
            
            if previous_value is None:
                return  # No previous value to compare
            
            # Convert values to float
            try:
                current_float = float(current_value)
                previous_float = float(previous_value)
            except (ValueError, TypeError):
                return  # Skip non-numeric values
            
            # Check if delta is exceeded
            value_delta = abs(current_float - previous_float)
            if value_delta > delta:
                await self._trigger_alert(
                    station_id, component_name, variable_name, current_value, delta,
                    Severity(severity), f"Value change {value_delta} exceeds delta {delta}"
                )
            
        except Exception as e:
            self.logger.error(f"Error checking delta monitoring: {e}")
    
    async def _check_periodic_monitoring(self, station_id: str, component_name: str, variable_name: str,
                                       current_value: Any, severity: str) -> None:
        """Check periodic monitoring."""
        try:
            # For periodic monitoring, we just log the current value
            # This could be extended to check for patterns or trends
            await self.timescale_client.store_periodic_monitoring_data({
                "station_id": station_id,
                "component_name": component_name,
                "variable_name": variable_name,
                "value": str(current_value),
                "timestamp": datetime.now(timezone.utc)
            })
            
        except Exception as e:
            self.logger.error(f"Error checking periodic monitoring: {e}")
    
    async def _check_alert_rules(self) -> None:
        """Check alert rules for all stations."""
        try:
            # Get all enabled alert rules
            rules_data = await self.timescale_client.get_enabled_alert_rules()
            
            for rule_data in rules_data:
                await self._check_alert_rule(rule_data)
            
        except Exception as e:
            self.logger.error(f"Error checking alert rules: {e}")
    
    async def _check_alert_rule(self, rule_data: Dict[str, Any]) -> None:
        """Check individual alert rule."""
        try:
            rule_id = rule_data["rule_id"]
            component_name = rule_data["component_name"]
            variable_name = rule_data["variable_name"]
            condition = rule_data["condition"]
            threshold = rule_data["threshold"]
            severity = rule_data["severity"]
            cooldown_minutes = rule_data["cooldown_minutes"]
            
            # Check cooldown
            cooldown_key = f"{rule_id}:{component_name}:{variable_name}"
            if cooldown_key in self.alert_cooldowns:
                if datetime.now(timezone.utc) - self.alert_cooldowns[cooldown_key] < timedelta(minutes=cooldown_minutes):
                    return  # Still in cooldown
            
            # Get current value for all stations with this component/variable
            stations_data = await self.timescale_client.get_stations_with_component_variable(
                component_name, variable_name
            )
            
            for station_data in stations_data:
                station_id = station_data["station_id"]
                current_value = station_data["value"]
                
                # Check condition
                if await self._evaluate_condition(current_value, condition, threshold):
                    await self._trigger_alert(
                        station_id, component_name, variable_name, current_value, threshold,
                        Severity(severity), f"Alert rule triggered: {rule_data['name']}"
                    )
                    
                    # Set cooldown
                    self.alert_cooldowns[cooldown_key] = datetime.now(timezone.utc)
            
        except Exception as e:
            self.logger.error(f"Error checking alert rule: {e}")
    
    async def _evaluate_condition(self, current_value: Any, condition: str, threshold: float) -> bool:
        """Evaluate alert condition."""
        try:
            # Convert current value to float
            try:
                current_float = float(current_value)
            except (ValueError, TypeError):
                return False  # Skip non-numeric values
            
            # Evaluate condition
            if condition == ">":
                return current_float > threshold
            elif condition == "<":
                return current_float < threshold
            elif condition == "==":
                return current_float == threshold
            elif condition == "!=":
                return current_float != threshold
            elif condition == ">=":
                return current_float >= threshold
            elif condition == "<=":
                return current_float <= threshold
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error evaluating condition: {e}")
            return False
    
    async def _trigger_alert(self, station_id: str, component_name: str, variable_name: str,
                           current_value: Any, threshold_value: float, severity: Severity, message: str) -> None:
        """Trigger alert."""
        try:
            # Generate alert ID
            alert_id = f"{station_id}:{component_name}:{variable_name}:{int(datetime.now().timestamp())}"
            
            # Create alert
            alert = Alert(
                alert_id=alert_id,
                rule_id="",  # Would be set for rule-based alerts
                station_id=station_id,
                component_name=component_name,
                variable_name=variable_name,
                current_value=current_value,
                threshold_value=threshold_value,
                severity=severity,
                status=AlertStatus.ACTIVE,
                message=message,
                triggered_at=datetime.now(timezone.utc)
            )
            
            # Store alert
            await self.timescale_client.store_alert({
                "alert_id": alert_id,
                "rule_id": alert.rule_id,
                "station_id": station_id,
                "component_name": component_name,
                "variable_name": variable_name,
                "current_value": str(current_value),
                "threshold_value": threshold_value,
                "severity": severity.value,
                "status": AlertStatus.ACTIVE.value,
                "message": message,
                "triggered_at": alert.triggered_at,
                "created_at": datetime.now(timezone.utc)
            })
            
            # Update cache
            self.active_alerts[alert_id] = alert
            
            # Send notifications
            await self._send_alert_notifications(alert)
            
            self.logger.warning(f"Alert triggered: {alert_id} - {message}")
            
        except Exception as e:
            self.logger.error(f"Error triggering alert: {e}")
    
    async def _send_alert_notifications(self, alert: Alert) -> None:
        """Send alert notifications."""
        try:
            # This would integrate with notification systems (email, SMS, Slack, etc.)
            # For now, just log the alert
            self.logger.critical(f"ALERT: {alert.severity.value} - {alert.message} (Station: {alert.station_id})")
            
        except Exception as e:
            self.logger.error(f"Error sending alert notifications: {e}")
    
    async def _process_monitoring_data_for_alerts(self, station_id: str, report_data: List[Dict[str, Any]]) -> None:
        """Process monitoring data for alerts."""
        try:
            # Update monitoring data cache
            cache_key = f"{station_id}:monitoring_data"
            self.monitoring_data_cache[cache_key] = {
                "data": report_data,
                "timestamp": datetime.now(timezone.utc)
            }
            
            # Check each variable in the report for alerts
            for data in report_data:
                component = data.get("component", {})
                variable = data.get("variable", {})
                variable_attributes = data.get("variable_attributes", [])
                
                # Get actual value
                actual_value = None
                for attr in variable_attributes:
                    if attr.get("type") == "Actual":
                        actual_value = attr.get("value")
                        break
                
                if actual_value is not None:
                    # Check for alerts
                    await self._check_variable_for_alerts(
                        station_id, component.get("name"), variable.get("name"), actual_value
                    )
            
        except Exception as e:
            self.logger.error(f"Error processing monitoring data for alerts: {e}")
    
    async def _check_variable_for_alerts(self, station_id: str, component_name: str,
                                       variable_name: str, current_value: Any) -> None:
        """Check variable for alerts."""
        try:
            # Get monitoring configuration for this variable
            monitoring_config = await self.timescale_client.get_variable_monitoring_config(
                component_name, variable_name
            )
            
            if monitoring_config:
                await self._check_monitoring_criteria(monitoring_config, current_value)
            
            # Check alert rules for this variable
            alert_rules = await self.timescale_client.get_alert_rules_for_variable(
                component_name, variable_name
            )
            
            for rule_data in alert_rules:
                if await self._evaluate_condition(current_value, rule_data["condition"], rule_data["threshold"]):
                    await self._trigger_alert(
                        station_id, component_name, variable_name, current_value,
                        rule_data["threshold"], Severity(rule_data["severity"]),
                        f"Alert rule triggered: {rule_data['name']}"
                    )
            
        except Exception as e:
            self.logger.error(f"Error checking variable for alerts: {e}")
    
    async def _cleanup_old_alerts(self) -> None:
        """Clean up old alerts."""
        try:
            # Remove alerts older than 30 days
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
            await self.timescale_client.cleanup_old_alerts(cutoff_date)
            
            # Clean up cache
            keys_to_remove = []
            for alert_id, alert in self.active_alerts.items():
                if alert.triggered_at < cutoff_date:
                    keys_to_remove.append(alert_id)
            
            for key in keys_to_remove:
                self.active_alerts.pop(key, None)
            
        except Exception as e:
            self.logger.error(f"Error cleaning up old alerts: {e}")
    
    async def _validate_monitoring_config(self, monitor_config: Dict[str, Any]) -> bool:
        """Validate monitoring configuration."""
        try:
            # Check required fields
            required_fields = ["component", "variable", "monitoringCriterion", "severity"]
            for field in required_fields:
                if field not in monitor_config:
                    return False
            
            # Check monitoring criterion
            monitoring_criterion = monitor_config["monitoringCriterion"]
            if monitoring_criterion == MonitoringCriterion.THRESHOLD_MONITORING.value:
                if "threshold" not in monitor_config:
                    return False
            elif monitoring_criterion == MonitoringCriterion.DELTA.value:
                if "delta" not in monitor_config:
                    return False
            elif monitoring_criterion == MonitoringCriterion.PERIODIC.value:
                if "period" not in monitor_config:
                    return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating monitoring config: {e}")
            return False
