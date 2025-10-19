"""Advanced monitoring and observability for V2G operations."""

import asyncio
import time
import psutil
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from enum import Enum
import structlog
from prometheus_client import Counter, Histogram, Gauge, Info, CollectorRegistry

from .monitoring import get_logger


class MetricType(Enum):
    """Types of metrics."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    INFO = "info"


@dataclass
class MetricDefinition:
    """Definition of a custom metric."""
    name: str
    description: str
    metric_type: MetricType
    labels: List[str] = field(default_factory=list)
    buckets: Optional[List[float]] = None


class V2GMetrics:
    """V2G-specific metrics collection."""
    
    def __init__(self, registry: Optional[CollectorRegistry] = None):
        """Initialize V2G metrics."""
        self.registry = registry or CollectorRegistry()
        self.logger = get_logger(__name__)
        
        # DER Control metrics
        self.der_controls_total = Counter(
            'v2g_der_controls_total',
            'Total number of DER controls processed',
            ['station_id', 'control_type', 'status'],
            registry=self.registry
        )
        
        self.der_control_duration = Histogram(
            'v2g_der_control_duration_seconds',
            'Time spent processing DER controls',
            ['station_id', 'control_type'],
            buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
            registry=self.registry
        )
        
        self.active_der_controls = Gauge(
            'v2g_active_der_controls',
            'Number of active DER controls',
            ['station_id'],
            registry=self.registry
        )
        
        # V2G Workflow metrics
        self.v2g_workflows_total = Counter(
            'v2g_workflows_total',
            'Total number of V2G workflows',
            ['workflow_type', 'status'],
            registry=self.registry
        )
        
        self.v2g_workflow_duration = Histogram(
            'v2g_workflow_duration_seconds',
            'Time spent executing V2G workflows',
            ['workflow_type'],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
            registry=self.registry
        )
        
        # Power flow metrics
        self.power_flow_watts = Gauge(
            'v2g_power_flow_watts',
            'Current power flow in watts',
            ['station_id', 'direction'],
            registry=self.registry
        )
        
        self.energy_transferred_kwh = Counter(
            'v2g_energy_transferred_kwh_total',
            'Total energy transferred in kWh',
            ['station_id', 'direction'],
            registry=self.registry
        )
        
        # Grid support metrics
        self.grid_frequency_hz = Gauge(
            'v2g_grid_frequency_hz',
            'Grid frequency in Hz',
            ['station_id'],
            registry=self.registry
        )
        
        self.grid_voltage_v = Gauge(
            'v2g_grid_voltage_v',
            'Grid voltage in volts',
            ['station_id', 'phase'],
            registry=self.registry
        )
        
        self.reactive_power_var = Gauge(
            'v2g_reactive_power_var',
            'Reactive power in VAR',
            ['station_id', 'phase'],
            registry=self.registry
        )
        
        # System health metrics
        self.circuit_breaker_state = Gauge(
            'v2g_circuit_breaker_state',
            'Circuit breaker state (0=closed, 1=open, 2=half-open)',
            ['service_name'],
            registry=self.registry
        )
        
        self.resilience_score = Gauge(
            'v2g_resilience_score',
            'System resilience score (0-100)',
            registry=self.registry
        )
        
        # Custom metrics storage
        self.custom_metrics: Dict[str, Any] = {}
    
    def record_der_control(self, station_id: str, control_type: str, status: str, duration: float):
        """Record DER control metric."""
        self.der_controls_total.labels(
            station_id=station_id,
            control_type=control_type,
            status=status
        ).inc()
        
        self.der_control_duration.labels(
            station_id=station_id,
            control_type=control_type
        ).observe(duration)
    
    def record_v2g_workflow(self, workflow_type: str, status: str, duration: float):
        """Record V2G workflow metric."""
        self.v2g_workflows_total.labels(
            workflow_type=workflow_type,
            status=status
        ).inc()
        
        self.v2g_workflow_duration.labels(
            workflow_type=workflow_type
        ).observe(duration)
    
    def update_power_flow(self, station_id: str, direction: str, power_watts: float):
        """Update power flow metric."""
        self.power_flow_watts.labels(
            station_id=station_id,
            direction=direction
        ).set(power_watts)
    
    def record_energy_transfer(self, station_id: str, direction: str, energy_kwh: float):
        """Record energy transfer metric."""
        self.energy_transferred_kwh.labels(
            station_id=station_id,
            direction=direction
        ).inc(energy_kwh)
    
    def update_grid_metrics(self, station_id: str, frequency_hz: float, 
                           voltage_v: Dict[str, float], reactive_power_var: Dict[str, float]):
        """Update grid metrics."""
        self.grid_frequency_hz.labels(station_id=station_id).set(frequency_hz)
        
        for phase, voltage in voltage_v.items():
            self.grid_voltage_v.labels(
                station_id=station_id,
                phase=phase
            ).set(voltage)
        
        for phase, reactive in reactive_power_var.items():
            self.reactive_power_var.labels(
                station_id=station_id,
                phase=phase
            ).set(reactive)
    
    def update_circuit_breaker_state(self, service_name: str, state: str):
        """Update circuit breaker state metric."""
        state_value = {"closed": 0, "open": 1, "half_open": 2}.get(state, -1)
        self.circuit_breaker_state.labels(service_name=service_name).set(state_value)
    
    def update_resilience_score(self, score: float):
        """Update resilience score metric."""
        self.resilience_score.set(score)


class SystemHealthMonitor:
    """Advanced system health monitoring."""
    
    def __init__(self, v2g_metrics: V2GMetrics):
        """Initialize system health monitor."""
        self.v2g_metrics = v2g_metrics
        self.logger = get_logger(__name__)
        self.health_history: List[Dict[str, Any]] = []
        self.alert_thresholds = {
            "cpu_usage": 80.0,
            "memory_usage": 85.0,
            "disk_usage": 90.0,
            "response_time_p95": 1.0,
            "error_rate": 0.05
        }
    
    async def collect_system_metrics(self) -> Dict[str, Any]:
        """Collect comprehensive system metrics."""
        try:
            # CPU metrics
            cpu_percent = psutil.cpu_percent(interval=1)
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()
            
            # Memory metrics
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            
            # Disk metrics
            disk = psutil.disk_usage('/')
            disk_io = psutil.disk_io_counters()
            
            # Network metrics
            network_io = psutil.net_io_counters()
            
            # Process metrics
            process = psutil.Process()
            process_memory = process.memory_info()
            process_cpu = process.cpu_percent()
            
            metrics = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cpu": {
                    "usage_percent": cpu_percent,
                    "count": cpu_count,
                    "frequency_mhz": cpu_freq.current if cpu_freq else None,
                    "process_usage_percent": process_cpu
                },
                "memory": {
                    "total_gb": memory.total / (1024**3),
                    "available_gb": memory.available / (1024**3),
                    "used_gb": memory.used / (1024**3),
                    "usage_percent": memory.percent,
                    "process_memory_mb": process_memory.rss / (1024**2),
                    "swap_total_gb": swap.total / (1024**3),
                    "swap_used_gb": swap.used / (1024**3),
                    "swap_usage_percent": swap.percent
                },
                "disk": {
                    "total_gb": disk.total / (1024**3),
                    "used_gb": disk.used / (1024**3),
                    "free_gb": disk.free / (1024**3),
                    "usage_percent": (disk.used / disk.total) * 100,
                    "read_bytes": disk_io.read_bytes if disk_io else 0,
                    "write_bytes": disk_io.write_bytes if disk_io else 0
                },
                "network": {
                    "bytes_sent": network_io.bytes_sent,
                    "bytes_recv": network_io.bytes_recv,
                    "packets_sent": network_io.packets_sent,
                    "packets_recv": network_io.packets_recv
                }
            }
            
            # Store in history
            self.health_history.append(metrics)
            
            # Keep only last 1000 entries
            if len(self.health_history) > 1000:
                self.health_history = self.health_history[-1000:]
            
            return metrics
            
        except Exception as e:
            self.logger.error(f"Error collecting system metrics: {e}")
            return {}
    
    def check_health_alerts(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check for health alerts based on thresholds."""
        alerts = []
        
        if not metrics:
            return alerts
        
        # CPU alert
        cpu_usage = metrics.get("cpu", {}).get("usage_percent", 0)
        if cpu_usage > self.alert_thresholds["cpu_usage"]:
            alerts.append({
                "type": "cpu_high",
                "severity": "warning" if cpu_usage < 95 else "critical",
                "message": f"CPU usage is {cpu_usage:.1f}% (threshold: {self.alert_thresholds['cpu_usage']}%)",
                "value": cpu_usage,
                "threshold": self.alert_thresholds["cpu_usage"]
            })
        
        # Memory alert
        memory_usage = metrics.get("memory", {}).get("usage_percent", 0)
        if memory_usage > self.alert_thresholds["memory_usage"]:
            alerts.append({
                "type": "memory_high",
                "severity": "warning" if memory_usage < 95 else "critical",
                "message": f"Memory usage is {memory_usage:.1f}% (threshold: {self.alert_thresholds['memory_usage']}%)",
                "value": memory_usage,
                "threshold": self.alert_thresholds["memory_usage"]
            })
        
        # Disk alert
        disk_usage = metrics.get("disk", {}).get("usage_percent", 0)
        if disk_usage > self.alert_thresholds["disk_usage"]:
            alerts.append({
                "type": "disk_high",
                "severity": "warning" if disk_usage < 98 else "critical",
                "message": f"Disk usage is {disk_usage:.1f}% (threshold: {self.alert_thresholds['disk_usage']}%)",
                "value": disk_usage,
                "threshold": self.alert_thresholds["disk_usage"]
            })
        
        return alerts
    
    def calculate_resilience_score(self, metrics: Dict[str, Any]) -> float:
        """Calculate system resilience score (0-100)."""
        if not metrics:
            return 0.0
        
        score = 100.0
        
        # CPU penalty
        cpu_usage = metrics.get("cpu", {}).get("usage_percent", 0)
        if cpu_usage > 80:
            score -= (cpu_usage - 80) * 0.5
        
        # Memory penalty
        memory_usage = metrics.get("memory", {}).get("usage_percent", 0)
        if memory_usage > 80:
            score -= (memory_usage - 80) * 0.5
        
        # Disk penalty
        disk_usage = metrics.get("disk", {}).get("usage_percent", 0)
        if disk_usage > 85:
            score -= (disk_usage - 85) * 0.3
        
        # Ensure score is between 0 and 100
        return max(0.0, min(100.0, score))
    
    def get_health_summary(self) -> Dict[str, Any]:
        """Get comprehensive health summary."""
        if not self.health_history:
            return {"status": "unknown", "message": "No health data available"}
        
        latest_metrics = self.health_history[-1]
        alerts = self.check_health_alerts(latest_metrics)
        resilience_score = self.calculate_resilience_score(latest_metrics)
        
        # Determine overall status
        critical_alerts = [a for a in alerts if a["severity"] == "critical"]
        warning_alerts = [a for a in alerts if a["severity"] == "warning"]
        
        if critical_alerts:
            status = "critical"
        elif warning_alerts:
            status = "warning"
        elif resilience_score < 70:
            status = "degraded"
        else:
            status = "healthy"
        
        return {
            "status": status,
            "resilience_score": resilience_score,
            "alerts": alerts,
            "critical_alerts": len(critical_alerts),
            "warning_alerts": len(warning_alerts),
            "latest_metrics": latest_metrics,
            "history_length": len(self.health_history)
        }


class V2GEventLogger:
    """Structured event logging for V2G operations."""
    
    def __init__(self):
        """Initialize V2G event logger."""
        self.logger = structlog.get_logger("v2g_events")
    
    def log_der_control_event(self, event_type: str, station_id: str, control_id: int, 
                             control_type: str, **kwargs):
        """Log DER control event."""
        self.logger.info(
            "DER control event",
            event_type=event_type,
            station_id=station_id,
            control_id=control_id,
            control_type=control_type,
            **kwargs
        )
    
    def log_v2g_workflow_event(self, workflow_type: str, station_id: str, 
                              status: str, duration: float, **kwargs):
        """Log V2G workflow event."""
        self.logger.info(
            "V2G workflow event",
            workflow_type=workflow_type,
            station_id=station_id,
            status=status,
            duration=duration,
            **kwargs
        )
    
    def log_power_flow_event(self, station_id: str, direction: str, power_watts: float,
                            energy_kwh: float, **kwargs):
        """Log power flow event."""
        self.logger.info(
            "Power flow event",
            station_id=station_id,
            direction=direction,
            power_watts=power_watts,
            energy_kwh=energy_kwh,
            **kwargs
        )
    
    def log_grid_event(self, event_type: str, station_id: str, frequency_hz: float,
                      voltage_v: float, **kwargs):
        """Log grid event."""
        self.logger.info(
            "Grid event",
            event_type=event_type,
            station_id=station_id,
            frequency_hz=frequency_hz,
            voltage_v=voltage_v,
            **kwargs
        )
    
    def log_system_event(self, event_type: str, severity: str, message: str, **kwargs):
        """Log system event."""
        log_level = {
            "info": "info",
            "warning": "warning", 
            "error": "error",
            "critical": "critical"
        }.get(severity, "info")
        
        getattr(self.logger, log_level)(
            "System event",
            event_type=event_type,
            severity=severity,
            message=message,
            **kwargs
        )


class AdvancedMonitoringManager:
    """Manages advanced monitoring and observability."""
    
    def __init__(self, registry: Optional[CollectorRegistry] = None):
        """Initialize advanced monitoring manager."""
        self.v2g_metrics = V2GMetrics(registry)
        self.health_monitor = SystemHealthMonitor(self.v2g_metrics)
        self.event_logger = V2GEventLogger()
        self.logger = get_logger(__name__)
        self.monitoring_task: Optional[asyncio.Task] = None
        self.is_running = False
    
    async def start(self, interval: float = 30.0):
        """Start advanced monitoring."""
        if self.is_running:
            return
        
        self.is_running = True
        self.monitoring_task = asyncio.create_task(
            self._monitoring_loop(interval)
        )
        self.logger.info("Advanced monitoring started")
    
    async def stop(self):
        """Stop advanced monitoring."""
        self.is_running = False
        
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        
        self.logger.info("Advanced monitoring stopped")
    
    async def _monitoring_loop(self, interval: float):
        """Main monitoring loop."""
        while self.is_running:
            try:
                # Collect system metrics
                metrics = await self.health_monitor.collect_system_metrics()
                
                # Update resilience score
                resilience_score = self.health_monitor.calculate_resilience_score(metrics)
                self.v2g_metrics.update_resilience_score(resilience_score)
                
                # Check for alerts
                alerts = self.health_monitor.check_health_alerts(metrics)
                for alert in alerts:
                    self.event_logger.log_system_event(
                        "health_alert",
                        alert["severity"],
                        alert["message"],
                        alert_type=alert["type"],
                        value=alert["value"],
                        threshold=alert["threshold"]
                    )
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(interval)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get metrics summary."""
        return {
            "v2g_metrics": {
                "der_controls_total": self.v2g_metrics.der_controls_total._value.sum(),
                "v2g_workflows_total": self.v2g_metrics.v2g_workflows_total._value.sum(),
                "active_der_controls": self.v2g_metrics.active_der_controls._value.sum(),
                "resilience_score": self.v2g_metrics.resilience_score._value
            },
            "health_summary": self.health_monitor.get_health_summary()
        }


# Global monitoring manager instance
monitoring_manager = AdvancedMonitoringManager()