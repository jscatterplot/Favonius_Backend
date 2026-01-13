"""Resilience and fault tolerance management."""

import asyncio
import time
from typing import Dict, Any, Optional, Callable, List
from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import structlog

from .enhanced_error_handler import CircuitBreakerState, CircuitBreakerOpenError


class HealthStatus(Enum):
    """Health status enumeration."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheck:
    """Health check configuration."""
    name: str
    check_func: Callable[[], bool]
    timeout: float = 5.0
    interval: float = 30.0
    failure_threshold: int = 3
    recovery_threshold: int = 2
    enabled: bool = True


@dataclass
class ServiceHealth:
    """Service health information."""
    name: str
    status: HealthStatus
    last_check: datetime
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_error: Optional[str] = None
    response_time_ms: Optional[float] = None


class ResilienceManager:
    """Manages system resilience and fault tolerance."""
    
    def __init__(self):
        """Initialize resilience manager."""
        self.logger = structlog.get_logger(__name__)
        self.health_checks: Dict[str, HealthCheck] = {}
        self.service_health: Dict[str, ServiceHealth] = {}
        self.circuit_breakers: Dict[str, CircuitBreakerState] = {}
        self.bulkhead_tasks: Dict[str, asyncio.Task] = {}
        self.monitoring_task: Optional[asyncio.Task] = None
        self.is_running = False
    
    async def start(self) -> None:
        """Start resilience monitoring."""
        if self.is_running:
            return
        
        self.is_running = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        self.logger.info("Resilience manager started")
    
    async def stop(self) -> None:
        """Stop resilience monitoring."""
        self.is_running = False
        
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all bulkhead tasks
        for task in self.bulkhead_tasks.values():
            if not task.done():
                task.cancel()
        
        self.logger.info("Resilience manager stopped")
    
    def add_health_check(self, health_check: HealthCheck) -> None:
        """Add a health check."""
        self.health_checks[health_check.name] = health_check
        self.service_health[health_check.name] = ServiceHealth(
            name=health_check.name,
            status=HealthStatus.UNKNOWN,
            last_check=datetime.now(timezone.utc)
        )
        self.logger.info(f"Added health check: {health_check.name}")
    
    def add_circuit_breaker(self, service_name: str, failure_threshold: int = 5, 
                           recovery_timeout: float = 60.0) -> None:
        """Add a circuit breaker for a service."""
        self.circuit_breakers[service_name] = CircuitBreakerState(
            failure_count=0,
            last_failure_time=None,
            state="CLOSED",
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout
        )
        self.logger.info(f"Added circuit breaker for service: {service_name}")
    
    async def execute_with_resilience(self, service_name: str, operation: Callable, 
                                    *args, **kwargs) -> Any:
        """Execute operation with resilience patterns."""
        # Check circuit breaker
        if service_name in self.circuit_breakers:
            circuit_breaker = self.circuit_breakers[service_name]
            if circuit_breaker.state == "OPEN":
                if self._should_attempt_reset(circuit_breaker):
                    circuit_breaker.state = "HALF_OPEN"
                else:
                    raise CircuitBreakerOpenError(f"Circuit breaker open for {service_name}")
        
        try:
            # Execute operation with timeout
            result = await asyncio.wait_for(operation(*args, **kwargs), timeout=30.0)
            
            # Record success
            if service_name in self.circuit_breakers:
                self._record_success(service_name)
            
            return result
            
        except asyncio.TimeoutError:
            self._record_failure(service_name, "Operation timeout")
            raise
        except Exception as e:
            self._record_failure(service_name, str(e))
            raise
    
    async def execute_in_bulkhead(self, bulkhead_name: str, operation: Callable, 
                                 *args, **kwargs) -> Any:
        """Execute operation in a bulkhead (isolated execution context)."""
        if bulkhead_name in self.bulkhead_tasks and not self.bulkhead_tasks[bulkhead_name].done():
            raise Exception(f"Bulkhead {bulkhead_name} is already executing")
        
        try:
            self.bulkhead_tasks[bulkhead_name] = asyncio.create_task(
                operation(*args, **kwargs)
            )
            result = await self.bulkhead_tasks[bulkhead_name]
            return result
        except Exception as e:
            self.logger.error(f"Bulkhead {bulkhead_name} execution failed: {e}")
            raise
        finally:
            if bulkhead_name in self.bulkhead_tasks:
                del self.bulkhead_tasks[bulkhead_name]
    
    def get_service_health(self, service_name: str) -> Optional[ServiceHealth]:
        """Get health status for a service."""
        return self.service_health.get(service_name)
    
    def get_overall_health(self) -> Dict[str, Any]:
        """Get overall system health."""
        healthy_services = sum(1 for h in self.service_health.values() 
                              if h.status == HealthStatus.HEALTHY)
        total_services = len(self.service_health)
        
        overall_status = HealthStatus.HEALTHY
        if healthy_services == 0:
            overall_status = HealthStatus.UNHEALTHY
        elif healthy_services < total_services:
            overall_status = HealthStatus.DEGRADED
        
        return {
            "overall_status": overall_status.value,
            "healthy_services": healthy_services,
            "total_services": total_services,
            "services": {name: {
                "status": health.status.value,
                "last_check": health.last_check.isoformat(),
                "consecutive_failures": health.consecutive_failures,
                "response_time_ms": health.response_time_ms,
                "last_error": health.last_error
            } for name, health in self.service_health.items()},
            "circuit_breakers": {name: {
                "state": cb.state,
                "failure_count": cb.failure_count,
                "last_failure": cb.last_failure_time.isoformat() if cb.last_failure_time else None
            } for name, cb in self.circuit_breakers.items()}
        }
    
    async def _monitoring_loop(self) -> None:
        """Main monitoring loop."""
        while self.is_running:
            try:
                await self._run_health_checks()
                await asyncio.sleep(10)  # Check every 10 seconds
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(30)  # Wait longer on error
    
    async def _run_health_checks(self) -> None:
        """Run all health checks."""
        for name, health_check in self.health_checks.items():
            if not health_check.enabled:
                continue
            
            try:
                start_time = time.time()
                
                # Run health check with timeout
                result = await asyncio.wait_for(
                    asyncio.to_thread(health_check.check_func),
                    timeout=health_check.timeout
                )
                
                response_time = (time.time() - start_time) * 1000
                
                if result:
                    self._record_health_success(name, response_time)
                else:
                    self._record_health_failure(name, "Health check returned False")
                
            except asyncio.TimeoutError:
                self._record_health_failure(name, "Health check timeout")
            except Exception as e:
                self._record_health_failure(name, str(e))
    
    def _record_health_success(self, service_name: str, response_time_ms: float) -> None:
        """Record successful health check."""
        if service_name not in self.service_health:
            return
        
        health = self.service_health[service_name]
        health.consecutive_successes += 1
        health.consecutive_failures = 0
        health.last_check = datetime.now(timezone.utc)
        health.response_time_ms = response_time_ms
        health.last_error = None
        
        # Update status based on consecutive successes
        if health.consecutive_successes >= 2:  # Recovery threshold
            health.status = HealthStatus.HEALTHY
        
        self.logger.debug(f"Health check success for {service_name}: {response_time_ms:.2f}ms")
    
    def _record_health_failure(self, service_name: str, error: str) -> None:
        """Record failed health check."""
        if service_name not in self.service_health:
            return
        
        health = self.service_health[service_name]
        health.consecutive_failures += 1
        health.consecutive_successes = 0
        health.last_check = datetime.now(timezone.utc)
        health.last_error = error
        
        # Update status based on consecutive failures
        if health.consecutive_failures >= 3:  # Failure threshold
            health.status = HealthStatus.UNHEALTHY
        elif health.consecutive_failures >= 1:
            health.status = HealthStatus.DEGRADED
        
        self.logger.warning(f"Health check failure for {service_name}: {error}")
    
    def _record_success(self, service_name: str) -> None:
        """Record successful operation."""
        if service_name in self.circuit_breakers:
            circuit_breaker = self.circuit_breakers[service_name]
            if circuit_breaker.state == "HALF_OPEN":
                circuit_breaker.state = "CLOSED"
                circuit_breaker.failure_count = 0
                self.logger.info(f"Circuit breaker closed for {service_name}")
    
    def _record_failure(self, service_name: str, error: str) -> None:
        """Record failed operation."""
        if service_name in self.circuit_breakers:
            circuit_breaker = self.circuit_breakers[service_name]
            circuit_breaker.failure_count += 1
            circuit_breaker.last_failure_time = datetime.now(timezone.utc)
            
            if circuit_breaker.failure_count >= circuit_breaker.failure_threshold:
                circuit_breaker.state = "OPEN"
                self.logger.warning(f"Circuit breaker opened for {service_name}")
    
    def _should_attempt_reset(self, circuit_breaker: CircuitBreakerState) -> bool:
        """Check if circuit breaker should attempt reset."""
        if not circuit_breaker.last_failure_time:
            return True
        
        time_since_failure = datetime.now(timezone.utc) - circuit_breaker.last_failure_time
        return time_since_failure.total_seconds() >= circuit_breaker.recovery_timeout


# Global resilience manager instance
resilience_manager = ResilienceManager()