"""Monitoring and observability setup for WebSocket handler."""

import logging
import sys
import time
import asyncio
from typing import Dict, Any
import structlog
from prometheus_client import start_http_server, Counter, Histogram, Gauge, Info

from .config import MonitoringConfig


# Prometheus metrics
WEBSOCKET_CONNECTIONS = Gauge(
    "websocket_connections_active", 
    "Number of active WebSocket connections"
)

MESSAGES_RECEIVED_TOTAL = Counter(
    "websocket_messages_received_total",
    "Total number of WebSocket messages received"
)

MESSAGES_SENT_TOTAL = Counter(
    "websocket_messages_sent_total", 
    "Total number of WebSocket messages sent"
)

REDIS_OPERATION_DURATION = Histogram(
    "redis_operation_duration_seconds",
    "Duration of Redis operations"
)

REDIS_OPERATIONS_TOTAL = Counter(
    "redis_operations_total",
    "Total number of Redis operations"
)

# Note: Other metrics are defined in server.py to avoid duplication

# Redis metrics removed for simplification


APPLICATION_INFO = Info(
    "websocket_handler_info",
    "WebSocket handler application info"
)


def setup_monitoring(config: MonitoringConfig) -> None:
    """Setup monitoring and metrics collection."""
    
    # Start Prometheus metrics server
    if config.enable_telemetry:
        start_http_server(config.metrics_port)
        
        # Set application info
        APPLICATION_INFO.info({
            "version": "1.0.0",
            "log_level": config.log_level,
        })
    
    # Setup structured logging
    setup_logging(config)


def setup_logging(config: MonitoringConfig) -> None:
    """Setup structured logging with structlog."""
    
    # Configure logging level
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    
    # Setup structlog processors
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="ISO"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    
    # Add JSON formatter for production
    if config.log_level.upper() in ["INFO", "WARNING", "ERROR"]:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        context_class=dict,
        cache_logger_on_first_use=True,
    )
    
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


class MetricsCollector:
    """Collects and exposes application metrics."""
    
    def __init__(self):
        """Initialize metrics collector."""
        self.logger = get_logger(__name__)
        self._connection_count = 0
        self._message_counts = {}
        self._error_counts = {}
        
    def record_connection(self, connected: bool = True) -> None:
        """Record connection event."""
        if connected:
            self._connection_count += 1
            WEBSOCKET_CONNECTIONS.inc()
        else:
            self._connection_count -= 1
            WEBSOCKET_CONNECTIONS.dec()
    
    def record_message_received(self, station_id: str, message_type: str) -> None:
        """Record received message."""
        MESSAGES_RECEIVED_TOTAL.labels(
            station_id=station_id, 
            message_type=message_type
        ).inc()
        
        key = f"{station_id}:{message_type}:received"
        self._message_counts[key] = self._message_counts.get(key, 0) + 1
    
    def record_message_sent(self, station_id: str, message_type: str) -> None:
        """Record sent message."""
        MESSAGES_SENT_TOTAL.labels(
            station_id=station_id,
            message_type=message_type
        ).inc()
        
        key = f"{station_id}:{message_type}:sent"
        self._message_counts[key] = self._message_counts.get(key, 0) + 1
    
    def record_message_processing_time(self, message_type: str, duration: float) -> None:
        """Record message processing time."""
        # Use the metric from server.py instead
        pass  # This will be handled by the server's MESSAGE_PROCESSING_TIME metric
    
    def record_error(self, error_type: str, station_id: str = "unknown") -> None:
        """Record error event."""
        # Use the metric from server.py instead
        pass  # This will be handled by the server's ERRORS_TOTAL metric
        
        key = f"{error_type}:{station_id}"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1
    
    def record_redis_operation(self, operation: str, duration: float, success: bool = True) -> None:
        """Record Redis operation."""
        status = "success" if success else "error"
        
        REDIS_OPERATIONS_TOTAL.labels(
            operation=operation,
            status=status
        ).inc()
        
        REDIS_OPERATION_DURATION.labels(operation=operation).observe(duration)
    
    def record_kafka_send(self, topic: str, duration: float, success: bool = True) -> None:
        """Record external message send (placeholder for future use)."""
        pass
    
    def get_connection_count(self) -> int:
        """Get current connection count."""
        return self._connection_count
    
    def get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics."""
        return {
            "connections": self._connection_count,
            "messages": len(self._message_counts),
            "errors": len(self._error_counts),
            "timestamp": time.time()
        }


# Global metrics collector instance
metrics_collector = MetricsCollector()


class PerformanceTimer:
    """Context manager for timing operations."""
    
    def __init__(self, operation_name: str, callback=None):
        """Initialize performance timer."""
        self.operation_name = operation_name
        self.callback = callback
        self.start_time = 0
        self.duration = 0
    
    def __enter__(self):
        """Start timing."""
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timing and record."""
        self.duration = time.time() - self.start_time
        
        # Call custom callback if provided
        if self.callback:
            self.callback(self.operation_name, self.duration, exc_type is None)
        
        # Record in metrics (Redis removed for simplification)
        if "kafka" in self.operation_name.lower():
            metrics_collector.record_kafka_send(
                self.operation_name, self.duration, exc_type is None
            )


def timer(operation_name: str):
    """Decorator for timing function execution."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            with PerformanceTimer(operation_name):
                return func(*args, **kwargs)
        return wrapper
    return decorator


class AsyncTimer:
    """Async context manager for timing operations."""
    
    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time = None
    
    async def __aenter__(self):
        self.start_time = time.time()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration = time.time() - self.start_time
            if "message" in self.operation_name.lower():
                message_type = self.operation_name.split("_")[-1] if "_" in self.operation_name else "unknown"
                metrics_collector.record_message_processing_time(message_type, duration)


def async_timer(operation_name: str):
    """Create async timer context manager."""
    return AsyncTimer(operation_name)


class HealthChecker:
    """Health check utilities."""
    
    def __init__(self):
        """Initialize health checker."""
        self.logger = get_logger(__name__)
        self.checks = {}
    
    def register_check(self, name: str, check_func, critical: bool = True) -> None:
        """Register a health check function."""
        self.checks[name] = {
            "func": check_func,
            "critical": critical,
            "last_result": None,
            "last_check": None
        }
    
    async def run_checks(self) -> Dict[str, Any]:
        """Run all health checks."""
        results = {
            "status": "healthy",
            "timestamp": time.time(),
            "checks": {}
        }
        
        overall_healthy = True
        
        for check_name, check_info in self.checks.items():
            try:
                start_time = time.time()
                
                # Run the check
                if asyncio.iscoroutinefunction(check_info["func"]):
                    check_result = await check_info["func"]()
                else:
                    check_result = check_info["func"]()
                
                duration = time.time() - start_time
                
                # Update check info
                check_info["last_result"] = check_result
                check_info["last_check"] = time.time()
                
                # Add to results
                results["checks"][check_name] = {
                    "status": check_result.get("status", "unknown"),
                    "duration": duration,
                    "details": check_result
                }
                
                # Check if this affects overall health
                if check_info["critical"] and check_result.get("status") != "healthy":
                    overall_healthy = False
                
            except Exception as e:
                self.logger.error(f"Health check {check_name} failed: {e}")
                
                results["checks"][check_name] = {
                    "status": "error",
                    "error": str(e)
                }
                
                if check_info["critical"]:
                    overall_healthy = False
        
        results["status"] = "healthy" if overall_healthy else "unhealthy"
        return results
    
    def get_last_results(self) -> Dict[str, Any]:
        """Get last health check results without running checks."""
        return {
            check_name: check_info["last_result"]
            for check_name, check_info in self.checks.items()
            if check_info["last_result"] is not None
        }


# Global health checker instance
health_checker = HealthChecker()


def setup_health_checks(connection_manager=None, timescale_client=None) -> None:
    """Setup standard health checks."""
    
    # Connection manager health check
    if connection_manager:
        health_checker.register_check(
            "connections",
            connection_manager.get_health_status,
            critical=True
        )
    
    # TimescaleDB health check
    if timescale_client:
        health_checker.register_check(
            "timescale",
            timescale_client.health_check,
            critical=True
        )
    
    # System resource check
    def system_check():
        import psutil
        
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        return {
            "status": "healthy" if cpu_percent < 90 and memory.percent < 90 else "warning",
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "disk_percent": disk.percent,
        }
    
    health_checker.register_check("system", system_check, critical=False)
