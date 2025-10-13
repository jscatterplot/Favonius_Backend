"""Enhanced error handling and resilience for OCPP communication."""

import asyncio
import time
import logging
from enum import Enum
from typing import Any, Callable, Dict, Optional, List, Union
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import random
import backoff
from functools import wraps

from .monitoring import get_logger


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ErrorSeverity(Enum):
    """Error severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ErrorContext:
    """Context information for error handling."""
    error_type: str
    message: str
    station_id: Optional[str] = None
    action: Optional[str] = None
    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    timestamp: datetime = None
    retry_count: int = 0
    max_retries: int = 3
    backoff_factor: float = 2.0
    additional_info: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


class CircuitBreaker:
    """Circuit breaker implementation for service resilience."""
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitBreakerState.CLOSED
        self.logger = get_logger(f"circuit_breaker.{name}")
        
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        if self.state == CircuitBreakerState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitBreakerState.HALF_OPEN
                self.logger.info(f"Circuit breaker {self.name} entering half-open state")
            else:
                raise Exception(f"Circuit breaker {self.name} is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e
    
    def _should_attempt_reset(self) -> bool:
        """Check if circuit breaker should attempt reset."""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.recovery_timeout
    
    def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.state = CircuitBreakerState.CLOSED
            self.logger.info(f"Circuit breaker {self.name} reset to CLOSED")
    
    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            self.logger.warning(
                f"Circuit breaker {self.name} opened after {self.failure_count} failures"
            )


class RetryManager:
    """Advanced retry management with exponential backoff."""
    
    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.logger = get_logger("retry_manager")
    
    async def execute_with_retry(
        self,
        func: Callable,
        *args,
        max_retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
        **kwargs
    ) -> Any:
        """Execute function with retry logic."""
        retries = max_retries or self.max_retries
        delay = base_delay or self.base_delay
        
        last_exception = None
        
        for attempt in range(retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                
                if attempt == retries:
                    self.logger.error(f"Max retries ({retries}) exceeded for {func.__name__}")
                    raise e
                
                # Calculate delay with exponential backoff
                current_delay = min(
                    delay * (exponential_base ** attempt),
                    max_delay
                )
                
                # Add jitter to prevent thundering herd
                if jitter:
                    current_delay *= (0.5 + random.random() * 0.5)
                
                self.logger.warning(
                    f"Attempt {attempt + 1} failed for {func.__name__}: {e}. "
                    f"Retrying in {current_delay:.2f}s"
                )
                
                await asyncio.sleep(current_delay)
        
        raise last_exception


class DeadLetterQueue:
    """Dead letter queue for failed messages."""
    
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self.queue: List[Dict[str, Any]] = []
        self.logger = get_logger("dead_letter_queue")
    
    async def add_message(
        self,
        message: Dict[str, Any],
        error: Exception,
        retry_count: int = 0
    ) -> None:
        """Add message to dead letter queue."""
        if len(self.queue) >= self.max_size:
            # Remove oldest message
            self.queue.pop(0)
        
        dlq_entry = {
            "message": message,
            "error": str(error),
            "error_type": type(error).__name__,
            "retry_count": retry_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "id": f"dlq_{int(time.time())}_{len(self.queue)}"
        }
        
        self.queue.append(dlq_entry)
        self.logger.warning(f"Message added to DLQ: {dlq_entry['id']}")
    
    async def get_messages(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get messages from dead letter queue."""
        return self.queue[-limit:] if self.queue else []
    
    async def remove_message(self, message_id: str) -> bool:
        """Remove message from dead letter queue."""
        for i, entry in enumerate(self.queue):
            if entry["id"] == message_id:
                self.queue.pop(i)
                return True
        return False


class ErrorHandler:
    """Centralized error handling and resilience management."""
    
    def __init__(self, timescale_client=None):
        self.timescale_client = timescale_client
        self.logger = get_logger("error_handler")
        
        # Circuit breakers for different services
        self.circuit_breakers = {
            "database": CircuitBreaker("database", failure_threshold=3, recovery_timeout=30),
            "websocket": CircuitBreaker("websocket", failure_threshold=5, recovery_timeout=60),
            "external_api": CircuitBreaker("external_api", failure_threshold=3, recovery_timeout=120),
            "ocpp_message": CircuitBreaker("ocpp_message", failure_threshold=10, recovery_timeout=30),
        }
        
        # Retry manager
        self.retry_manager = RetryManager(max_retries=3, base_delay=1.0)
        
        # Dead letter queue
        self.dlq = DeadLetterQueue(max_size=1000)
        
        # Error statistics
        self.error_stats = {
            "total_errors": 0,
            "errors_by_type": {},
            "errors_by_station": {},
            "circuit_breaker_trips": 0,
        }
    
    def get_circuit_breaker(self, service_name: str) -> CircuitBreaker:
        """Get circuit breaker for service."""
        return self.circuit_breakers.get(service_name, self.circuit_breakers["ocpp_message"])
    
    async def handle_error(
        self,
        error: Exception,
        context: ErrorContext,
        retry_func: Optional[Callable] = None
    ) -> Any:
        """Handle error with appropriate strategy."""
        self.error_stats["total_errors"] += 1
        
        # Update error statistics
        error_type = type(error).__name__
        self.error_stats["errors_by_type"][error_type] = \
            self.error_stats["errors_by_type"].get(error_type, 0) + 1
        
        if context.station_id:
            self.error_stats["errors_by_station"][context.station_id] = \
                self.error_stats["errors_by_station"].get(context.station_id, 0) + 1
        
        # Log error
        self.logger.error(
            f"Error in {context.action or 'unknown'}: {error}",
            extra={
                "error_type": error_type,
                "station_id": context.station_id,
                "action": context.action,
                "severity": context.severity.value,
                "retry_count": context.retry_count,
            }
        )
        
        # Store error in database if available
        if self.timescale_client:
            await self._store_error_in_db(error, context)
        
        # Handle based on severity
        if context.severity == ErrorSeverity.CRITICAL:
            await self._handle_critical_error(error, context)
        elif context.severity == ErrorSeverity.HIGH:
            await self._handle_high_severity_error(error, context, retry_func)
        else:
            await self._handle_standard_error(error, context, retry_func)
    
    async def _handle_critical_error(self, error: Exception, context: ErrorContext):
        """Handle critical errors."""
        self.logger.critical(f"Critical error: {error}")
        
        # Alert operations team
        await self._send_alert("CRITICAL", f"Critical error: {error}", context)
        
        # Add to dead letter queue
        await self.dlq.add_message(
            {"action": context.action, "station_id": context.station_id},
            error,
            context.retry_count
        )
    
    async def _handle_high_severity_error(
        self, 
        error: Exception, 
        context: ErrorContext, 
        retry_func: Optional[Callable]
    ):
        """Handle high severity errors with retry."""
        if retry_func and context.retry_count < context.max_retries:
            try:
                # Use retry manager
                return await self.retry_manager.execute_with_retry(
                    retry_func,
                    max_retries=context.max_retries - context.retry_count
                )
            except Exception as retry_error:
                self.logger.error(f"Retry failed: {retry_error}")
                await self.dlq.add_message(
                    {"action": context.action, "station_id": context.station_id},
                    retry_error,
                    context.retry_count + 1
                )
        else:
            await self.dlq.add_message(
                {"action": context.action, "station_id": context.station_id},
                error,
                context.retry_count
            )
    
    async def _handle_standard_error(
        self, 
        error: Exception, 
        context: ErrorContext, 
        retry_func: Optional[Callable]
    ):
        """Handle standard errors."""
        if retry_func and context.retry_count < context.max_retries:
            # Simple retry with exponential backoff
            delay = context.backoff_factor ** context.retry_count
            await asyncio.sleep(delay)
            
            try:
                return await retry_func()
            except Exception as retry_error:
                context.retry_count += 1
                await self.handle_error(retry_error, context, retry_func)
    
    async def _store_error_in_db(self, error: Exception, context: ErrorContext):
        """Store error information in database."""
        try:
            error_data = {
                "timestamp": context.timestamp,
                "station_id": context.station_id,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "action": context.action,
                "severity": context.severity.value,
                "retry_count": context.retry_count,
                "additional_info": context.additional_info or {},
            }
            
            # Store in error_logs table (would need to be created)
            await self.timescale_client.execute_query(
                """
                INSERT INTO error_logs (
                    timestamp, station_id, error_type, error_message, 
                    action, severity, retry_count, additional_info
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    error_data["timestamp"],
                    error_data["station_id"],
                    error_data["error_type"],
                    error_data["error_message"],
                    error_data["action"],
                    error_data["severity"],
                    error_data["retry_count"],
                    error_data["additional_info"],
                )
            )
        except Exception as e:
            self.logger.error(f"Failed to store error in database: {e}")
    
    async def _send_alert(self, level: str, message: str, context: ErrorContext):
        """Send alert to operations team."""
        # In a real implementation, this would integrate with alerting systems
        # like PagerDuty, Slack, email, etc.
        self.logger.warning(f"ALERT [{level}]: {message}")
    
    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics."""
        return self.error_stats.copy()
    
    async def get_dlq_messages(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get dead letter queue messages."""
        return await self.dlq.get_messages(limit)
    
    async def reprocess_dlq_message(self, message_id: str, retry_func: Callable) -> bool:
        """Reprocess a message from dead letter queue."""
        messages = await self.dlq.get_messages(1000)  # Get all messages
        message = next((m for m in messages if m["id"] == message_id), None)
        
        if not message:
            return False
        
        try:
            await retry_func(message["message"])
            await self.dlq.remove_message(message_id)
            self.logger.info(f"Successfully reprocessed DLQ message {message_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reprocess DLQ message {message_id}: {e}")
            return False


def with_error_handling(
    error_handler: ErrorHandler,
    action: str,
    severity: ErrorSeverity = ErrorSeverity.MEDIUM,
    retry_on_failure: bool = True
):
    """Decorator for automatic error handling."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            context = ErrorContext(
                error_type="Unknown",
                message=f"Error in {func.__name__}",
                action=action,
                severity=severity,
                station_id=kwargs.get("station_id"),
            )
            
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                retry_func = None
                if retry_on_failure:
                    retry_func = lambda: func(*args, **kwargs)
                
                await error_handler.handle_error(e, context, retry_func)
                raise e
        
        return wrapper
    return decorator


def with_circuit_breaker(service_name: str, error_handler: ErrorHandler):
    """Decorator for circuit breaker protection."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            circuit_breaker = error_handler.get_circuit_breaker(service_name)
            return await circuit_breaker.call(func, *args, **kwargs)
        
        return wrapper
    return decorator


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True
):
    """Decorator for retry logic."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retry_manager = RetryManager(max_retries, base_delay)
            return await retry_manager.execute_with_retry(
                func,
                *args,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                jitter=jitter,
                **kwargs
            )
        
        return wrapper
    return decorator