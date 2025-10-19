"""Enhanced error handling with advanced resilience patterns."""

import asyncio
import time
import random
from typing import Dict, Any, Optional, Callable, Type, List, Union
from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from functools import wraps
import structlog

from .monitoring import get_logger


class ErrorSeverity(Enum):
    """Error severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RetryConfig:
    """Retry configuration."""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: List[Type[Exception]] = None
    
    def __post_init__(self):
        if self.retryable_exceptions is None:
            self.retryable_exceptions = [
                ConnectionError, TimeoutError, asyncio.TimeoutError,
                OSError, IOError
            ]


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 3
    expected_exceptions: List[Type[Exception]] = None
    
    def __post_init__(self):
        if self.expected_exceptions is None:
            self.expected_exceptions = [Exception]


@dataclass
class BulkheadConfig:
    """Bulkhead configuration for resource isolation."""
    max_concurrent: int = 10
    queue_size: int = 100
    timeout: float = 30.0


class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class BulkheadFullError(Exception):
    """Raised when bulkhead is full."""
    pass


class EnhancedCircuitBreaker:
    """Enhanced circuit breaker with half-open state management."""
    
    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.half_open_calls = 0
        self.logger = get_logger(f"circuit_breaker.{name}")
    
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        if self.state == CircuitBreakerState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitBreakerState.HALF_OPEN
                self.half_open_calls = 0
                self.logger.info(f"Circuit breaker {self.name} entering half-open state")
            else:
                raise CircuitBreakerOpenError(f"Circuit breaker {self.name} is OPEN")
        
        if self.state == CircuitBreakerState.HALF_OPEN:
            if self.half_open_calls >= self.config.half_open_max_calls:
                self.state = CircuitBreakerState.OPEN
                self.last_failure_time = datetime.now(timezone.utc)
                raise CircuitBreakerOpenError(f"Circuit breaker {self.name} exceeded half-open calls")
            
            self.half_open_calls += 1
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            if any(isinstance(e, exc_type) for exc_type in self.config.expected_exceptions):
                self._on_failure()
            raise e
    
    def _should_attempt_reset(self) -> bool:
        """Check if circuit breaker should attempt reset."""
        if not self.last_failure_time:
            return True
        
        time_since_failure = datetime.now(timezone.utc) - self.last_failure_time
        return time_since_failure.total_seconds() >= self.config.recovery_timeout
    
    def _on_success(self):
        """Handle successful call."""
        self.failure_count = 0
        self.success_count += 1
        
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.state = CircuitBreakerState.CLOSED
            self.half_open_calls = 0
            self.logger.info(f"Circuit breaker {self.name} reset to CLOSED")
    
    def _on_failure(self):
        """Handle failed call."""
        self.failure_count += 1
        self.success_count = 0
        self.last_failure_time = datetime.now(timezone.utc)
        
        if self.failure_count >= self.config.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            self.logger.warning(
                f"Circuit breaker {self.name} opened after {self.failure_count} failures"
            )


class Bulkhead:
    """Bulkhead pattern for resource isolation."""
    
    def __init__(self, name: str, config: BulkheadConfig):
        self.name = name
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent)
        self.queue = asyncio.Queue(maxsize=config.queue_size)
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.logger = get_logger(f"bulkhead.{name}")
    
    async def execute(self, task_id: str, func: Callable, *args, **kwargs) -> Any:
        """Execute function within bulkhead."""
        if task_id in self.active_tasks and not self.active_tasks[task_id].done():
            raise BulkheadFullError(f"Task {task_id} already executing in bulkhead {self.name}")
        
        try:
            async with self.semaphore:
                task = asyncio.create_task(
                    asyncio.wait_for(func(*args, **kwargs), timeout=self.config.timeout)
                )
                self.active_tasks[task_id] = task
                result = await task
                return result
        except asyncio.TimeoutError:
            self.logger.error(f"Task {task_id} timed out in bulkhead {self.name}")
            raise
        except Exception as e:
            self.logger.error(f"Task {task_id} failed in bulkhead {self.name}: {e}")
            raise
        finally:
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]


class EnhancedRetryManager:
    """Enhanced retry management with advanced patterns."""
    
    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self.logger = get_logger("retry_manager")
    
    async def execute_with_retry(
        self,
        func: Callable,
        *args,
        config: Optional[RetryConfig] = None,
        **kwargs
    ) -> Any:
        """Execute function with retry logic."""
        retry_config = config or self.config
        last_exception = None
        
        for attempt in range(retry_config.max_attempts):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                
                # Check if exception is retryable
                if not any(isinstance(e, exc_type) for exc_type in retry_config.retryable_exceptions):
                    self.logger.error(f"Non-retryable exception in {func.__name__}: {e}")
                    raise e
                
                if attempt == retry_config.max_attempts - 1:
                    self.logger.error(f"All {retry_config.max_attempts} retry attempts failed for {func.__name__}")
                    raise e
                
                # Calculate delay with exponential backoff
                delay = min(
                    retry_config.base_delay * (retry_config.exponential_base ** attempt),
                    retry_config.max_delay
                )
                
                if retry_config.jitter:
                    delay *= (0.5 + random.random() * 0.5)
                
                self.logger.warning(
                    f"Attempt {attempt + 1} failed for {func.__name__}: {e}. "
                    f"Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)


class ErrorHandler:
    """Centralized error handling with multiple resilience patterns."""
    
    def __init__(self):
        self.logger = get_logger("error_handler")
        self.circuit_breakers: Dict[str, EnhancedCircuitBreaker] = {}
        self.bulkheads: Dict[str, Bulkhead] = {}
        self.retry_manager = EnhancedRetryManager()
    
    def add_circuit_breaker(self, name: str, config: CircuitBreakerConfig) -> None:
        """Add a circuit breaker."""
        self.circuit_breakers[name] = EnhancedCircuitBreaker(name, config)
        self.logger.info(f"Added circuit breaker: {name}")
    
    def add_bulkhead(self, name: str, config: BulkheadConfig) -> None:
        """Add a bulkhead."""
        self.bulkheads[name] = Bulkhead(name, config)
        self.logger.info(f"Added bulkhead: {name}")
    
    async def execute_with_resilience(
        self,
        func: Callable,
        *args,
        circuit_breaker: Optional[str] = None,
        bulkhead: Optional[str] = None,
        retry_config: Optional[RetryConfig] = None,
        **kwargs
    ) -> Any:
        """Execute function with multiple resilience patterns."""
        # Apply bulkhead if specified
        if bulkhead and bulkhead in self.bulkheads:
            task_id = f"{func.__name__}_{int(time.time() * 1000)}"
            return await self.bulkheads[bulkhead].execute(task_id, func, *args, **kwargs)
        
        # Apply circuit breaker if specified
        if circuit_breaker and circuit_breaker in self.circuit_breakers:
            return await self.circuit_breakers[circuit_breaker].call(func, *args, **kwargs)
        
        # Apply retry logic
        return await self.retry_manager.execute_with_retry(func, *args, config=retry_config, **kwargs)
    
    def get_circuit_breaker_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Get circuit breaker status."""
        if name not in self.circuit_breakers:
            return None
        
        cb = self.circuit_breakers[name]
        return {
            "name": name,
            "state": cb.state.value,
            "failure_count": cb.failure_count,
            "success_count": cb.success_count,
            "last_failure_time": cb.last_failure_time.isoformat() if cb.last_failure_time else None,
            "half_open_calls": cb.half_open_calls
        }
    
    def get_bulkhead_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Get bulkhead status."""
        if name not in self.bulkheads:
            return None
        
        bulkhead = self.bulkheads[name]
        return {
            "name": name,
            "active_tasks": len(bulkhead.active_tasks),
            "max_concurrent": bulkhead.config.max_concurrent,
            "queue_size": bulkhead.queue.qsize(),
            "max_queue_size": bulkhead.config.queue_size
        }


# Decorators for easy application of resilience patterns
def with_circuit_breaker(name: str, config: Optional[CircuitBreakerConfig] = None):
    """Decorator to apply circuit breaker to a function."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            error_handler = ErrorHandler()
            if name not in error_handler.circuit_breakers:
                error_handler.add_circuit_breaker(name, config or CircuitBreakerConfig())
            
            return await error_handler.execute_with_resilience(
                func, *args, circuit_breaker=name, **kwargs
            )
        return wrapper
    return decorator


def with_retry(config: Optional[RetryConfig] = None):
    """Decorator to apply retry logic to a function."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            error_handler = ErrorHandler()
            return await error_handler.execute_with_resilience(
                func, *args, retry_config=config, **kwargs
            )
        return wrapper
    return decorator


def with_bulkhead(name: str, config: Optional[BulkheadConfig] = None):
    """Decorator to apply bulkhead to a function."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            error_handler = ErrorHandler()
            if name not in error_handler.bulkheads:
                error_handler.add_bulkhead(name, config or BulkheadConfig())
            
            return await error_handler.execute_with_resilience(
                func, *args, bulkhead=name, **kwargs
            )
        return wrapper
    return decorator


# Global error handler instance
error_handler = ErrorHandler()