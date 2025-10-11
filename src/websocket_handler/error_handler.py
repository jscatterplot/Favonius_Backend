"""Error Handling and Resilience Management for OCPP 2.0.1."""

import asyncio
import uuid
import time
from typing import Any, Dict, List, Optional, Callable, TypeVar, Union
from datetime import datetime, timezone, timedelta
from enum import Enum
from functools import wraps
import json

from .monitoring import get_logger
from .timescale_client import TimescaleClient

T = TypeVar('T')


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HealthStatus(Enum):
    """Health check status."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


class DegradationAction(Enum):
    """Degradation actions."""
    DISABLE_FEATURE = "disable_feature"
    USE_FALLBACK = "use_fallback"
    REDUCE_FUNCTIONALITY = "reduce_functionality"


class CircuitBreaker:
    """Circuit breaker implementation."""

    def __init__(self, service_name: str, failure_threshold: int = 5,
                 timeout_seconds: int = 60, timescale_client: Optional[TimescaleClient] = None):
        """Initialize circuit breaker."""
        self.service_name = service_name
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # In-memory state (will be synced with database)
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.last_success_time = None

    async def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute function with circuit breaker protection."""
        if self.state == CircuitBreakerState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitBreakerState.HALF_OPEN
                self.logger.info(f"Circuit breaker {self.service_name} transitioning to HALF_OPEN")
            else:
                raise CircuitBreakerOpenError(f"Circuit breaker {self.service_name} is OPEN")

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure(e)
            raise

    def _should_attempt_reset(self) -> bool:
        """Check if circuit breaker should attempt reset."""
        if self.last_failure_time is None:
            return True
        
        time_since_failure = time.time() - self.last_failure_time
        return time_since_failure >= self.timeout_seconds

    async def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        self.last_success_time = time.time()
        
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.state = CircuitBreakerState.CLOSED
            self.logger.info(f"Circuit breaker {self.service_name} reset to CLOSED")
        
        # Update database state
        if self.timescale_client:
            await self._update_circuit_breaker_state()

    async def _on_failure(self, error: Exception):
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            self.logger.warning(f"Circuit breaker {self.service_name} opened after {self.failure_count} failures")
        
        # Update database state
        if self.timescale_client:
            await self._update_circuit_breaker_state()

    async def _update_circuit_breaker_state(self):
        """Update circuit breaker state in database."""
        try:
            state_data = {
                "service_name": self.service_name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "last_failure_time": datetime.fromtimestamp(self.last_failure_time, timezone.utc) if self.last_failure_time else None,
                "last_success_time": datetime.fromtimestamp(self.last_success_time, timezone.utc) if self.last_success_time else None,
                "failure_threshold": self.failure_threshold,
                "timeout_seconds": self.timeout_seconds,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_circuit_breaker_state(state_data)
        except Exception as e:
            self.logger.error(f"Failed to update circuit breaker state: {e}")


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class RetryManager:
    """Manages retry logic with exponential backoff."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize retry manager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

    async def execute_with_retry(self, func: Callable[..., T], *args, 
                               max_retries: int = 3, base_delay: float = 1.0,
                               max_delay: float = 60.0, backoff_factor: float = 2.0,
                               **kwargs) -> T:
        """Execute function with retry logic."""
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                
                if attempt == max_retries:
                    # Log final failure
                    await self._log_retry_attempt(
                        str(uuid.uuid4()), attempt + 1, str(e), False
                    )
                    break
                
                # Calculate delay with exponential backoff
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                
                # Log retry attempt
                await self._log_retry_attempt(
                    str(uuid.uuid4()), attempt + 1, str(e), False
                )
                
                self.logger.warning(f"Attempt {attempt + 1} failed, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
        
        raise last_exception

    async def _log_retry_attempt(self, message_id: str, attempt_number: int,
                               error_message: str, success: bool):
        """Log retry attempt."""
        try:
            attempt_data = {
                "attempt_id": str(uuid.uuid4()),
                "message_id": message_id,
                "attempt_number": attempt_number,
                "error_message": error_message,
                "attempt_time": datetime.now(timezone.utc),
                "success": success,
                "created_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_retry_attempt(attempt_data)
        except Exception as e:
            self.logger.error(f"Failed to log retry attempt: {e}")


class DeadLetterQueue:
    """Manages dead letter queue for failed messages."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize dead letter queue."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

    async def add_message(self, original_message: Dict[str, Any], error_message: str,
                         error_type: str, max_retries: int = 3) -> str:
        """Add message to dead letter queue."""
        message_id = str(uuid.uuid4())
        
        # Calculate next retry time (exponential backoff)
        next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        dlq_data = {
            "message_id": message_id,
            "original_message": original_message,
            "error_message": error_message,
            "error_type": error_type,
            "retry_count": 0,
            "max_retries": max_retries,
            "next_retry_at": next_retry_at,
            "created_at": datetime.now(timezone.utc)
        }
        
        await self.timescale_client.store_dead_letter_message(dlq_data)
        self.logger.warning(f"Added message {message_id} to dead letter queue: {error_message}")
        
        return message_id

    async def process_retryable_messages(self) -> None:
        """Process messages that are ready for retry."""
        try:
            messages = await self.timescale_client.get_retryable_dlq_messages()
            
            for message in messages:
                await self._retry_message(message)
                
        except Exception as e:
            self.logger.error(f"Error processing retryable messages: {e}")

    async def _retry_message(self, message: Dict[str, Any]) -> None:
        """Retry a dead letter queue message."""
        try:
            # Increment retry count
            new_retry_count = message["retry_count"] + 1
            
            if new_retry_count > message["max_retries"]:
                # Message exceeded max retries, mark as processed
                await self.timescale_client.mark_dlq_message_processed(message["message_id"])
                self.logger.error(f"Message {message['message_id']} exceeded max retries, giving up")
                return
            
            # Calculate next retry time
            delay_minutes = min(5 * (2 ** (new_retry_count - 1)), 60)  # Max 1 hour
            next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
            
            # Update message
            await self.timescale_client.update_dlq_message_retry(
                message["message_id"], new_retry_count, next_retry_at
            )
            
            self.logger.info(f"Retry {new_retry_count} scheduled for message {message['message_id']}")
            
        except Exception as e:
            self.logger.error(f"Error retrying message {message['message_id']}: {e}")


class HealthChecker:
    """Manages health checks for system components."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize health checker."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.health_checks: Dict[str, Callable] = {}

    def register_health_check(self, service_name: str, check_func: Callable):
        """Register a health check function."""
        self.health_checks[service_name] = check_func

    async def run_health_checks(self) -> Dict[str, Dict[str, Any]]:
        """Run all registered health checks."""
        results = {}
        
        for service_name, check_func in self.health_checks.items():
            try:
                start_time = time.time()
                result = await check_func()
                response_time = int((time.time() - start_time) * 1000)
                
                if result:
                    status = HealthStatus.HEALTHY
                    error_message = None
                else:
                    status = HealthStatus.UNHEALTHY
                    error_message = "Health check failed"
                
                results[service_name] = {
                    "status": status.value,
                    "response_time_ms": response_time,
                    "error_message": error_message
                }
                
                # Store result in database
                await self._store_health_check_result(
                    service_name, "system", status.value, response_time, error_message
                )
                
            except Exception as e:
                results[service_name] = {
                    "status": HealthStatus.UNHEALTHY.value,
                    "response_time_ms": None,
                    "error_message": str(e)
                }
                
                # Store result in database
                await self._store_health_check_result(
                    service_name, "system", HealthStatus.UNHEALTHY.value, None, str(e)
                )
                
                self.logger.error(f"Health check failed for {service_name}: {e}")
        
        return results

    async def _store_health_check_result(self, service_name: str, check_type: str,
                                       status: str, response_time_ms: Optional[int],
                                       error_message: Optional[str]):
        """Store health check result."""
        try:
            result_data = {
                "check_id": str(uuid.uuid4()),
                "service_name": service_name,
                "check_type": check_type,
                "status": status,
                "response_time_ms": response_time_ms,
                "error_message": error_message,
                "check_time": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.store_health_check_result(result_data)
        except Exception as e:
            self.logger.error(f"Failed to store health check result: {e}")


class GracefulDegradationManager:
    """Manages graceful degradation when services fail."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize degradation manager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.degradation_rules: Dict[str, Dict[str, Any]] = {}

    async def load_degradation_rules(self) -> None:
        """Load degradation rules from database."""
        try:
            rules = await self.timescale_client.get_degradation_rules()
            
            for rule in rules:
                if rule["enabled"]:
                    self.degradation_rules[rule["service_name"]] = rule
                    
            self.logger.info(f"Loaded {len(self.degradation_rules)} degradation rules")
            
        except Exception as e:
            self.logger.error(f"Failed to load degradation rules: {e}")

    async def check_degradation(self, service_name: str, condition: str) -> Optional[Dict[str, Any]]:
        """Check if service should be degraded."""
        if service_name not in self.degradation_rules:
            return None
        
        rule = self.degradation_rules[service_name]
        
        # Simple condition matching (can be enhanced)
        if condition in rule["trigger_condition"]:
            self.logger.warning(f"Degradation triggered for {service_name}: {condition}")
            return rule
        
        return None

    async def apply_degradation(self, service_name: str, action: str,
                              fallback_config: Optional[Dict[str, Any]] = None) -> None:
        """Apply degradation action."""
        self.logger.info(f"Applying degradation for {service_name}: {action}")
        
        if action == DegradationAction.DISABLE_FEATURE.value:
            # Disable feature
            pass
        elif action == DegradationAction.USE_FALLBACK.value:
            # Use fallback configuration
            if fallback_config:
                # Apply fallback config
                pass
        elif action == DegradationAction.REDUCE_FUNCTIONALITY.value:
            # Reduce functionality
            pass


class ErrorHandler:
    """Main error handling and resilience manager."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize error handler."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Initialize components
        self.retry_manager = RetryManager(timescale_client)
        self.dead_letter_queue = DeadLetterQueue(timescale_client)
        self.health_checker = HealthChecker(timescale_client)
        self.degradation_manager = GracefulDegradationManager(timescale_client)
        
        # Circuit breakers for different services
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}

    def get_circuit_breaker(self, service_name: str, failure_threshold: int = 5,
                          timeout_seconds: int = 60) -> CircuitBreaker:
        """Get or create circuit breaker for service."""
        if service_name not in self.circuit_breakers:
            self.circuit_breakers[service_name] = CircuitBreaker(
                service_name, failure_threshold, timeout_seconds, self.timescale_client
            )
        
        return self.circuit_breakers[service_name]

    async def initialize(self) -> None:
        """Initialize error handling system."""
        try:
            # Load degradation rules
            await self.degradation_manager.load_degradation_rules()
            
            # Register default health checks
            self._register_default_health_checks()
            
            self.logger.info("Error handling system initialized")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize error handling system: {e}")

    def _register_default_health_checks(self) -> None:
        """Register default health checks."""
        # Database health check
        self.health_checker.register_health_check("database", self._check_database_health)
        
        # WebSocket health check
        self.health_checker.register_health_check("websocket", self._check_websocket_health)

    async def _check_database_health(self) -> bool:
        """Check database health."""
        try:
            # Simple query to check database connectivity
            await self.timescale_client.pg_pool.fetch("SELECT 1")
            return True
        except Exception:
            return False

    async def _check_websocket_health(self) -> bool:
        """Check WebSocket health."""
        try:
            # Check if connection manager is available and has active connections
            if hasattr(self, 'connection_manager') and self.connection_manager:
                health_status = await self.connection_manager.get_health_status()
                return health_status.get('total_connections', 0) >= 0
            return True
        except Exception:
            return False

    async def cleanup_expired_data(self) -> None:
        """Clean up expired error handling data."""
        try:
            # Clean up old health check results (keep last 24 hours)
            cutoff_date = datetime.now(timezone.utc) - timedelta(hours=24)
            await self.timescale_client.cleanup_old_health_check_results(cutoff_date)
            
            # Clean up old retry attempts (keep last 7 days)
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=7)
            await self.timescale_client.cleanup_old_retry_attempts(cutoff_date)
            
            self.logger.info("Cleaned up expired error handling data")
            
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")


# Decorator for automatic error handling
def with_error_handling(error_handler: ErrorHandler, service_name: str,
                       max_retries: int = 3, use_circuit_breaker: bool = True):
    """Decorator to add error handling to functions."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                if use_circuit_breaker:
                    circuit_breaker = error_handler.get_circuit_breaker(service_name)
                    return await circuit_breaker.call(func, *args, **kwargs)
                else:
                    return await error_handler.retry_manager.execute_with_retry(
                        func, *args, max_retries=max_retries, **kwargs
                    )
            except Exception as e:
                # Add to dead letter queue if retries exhausted
                await error_handler.dead_letter_queue.add_message(
                    {"function": func.__name__, "args": str(args), "kwargs": str(kwargs)},
                    str(e), type(e).__name__
                )
                raise
        
        return wrapper
    return decorator
