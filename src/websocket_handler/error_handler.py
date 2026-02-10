"""Backward-compat shim for error_handler (re-exports from enhanced_error_handler)."""

from typing import Optional

from .enhanced_error_handler import (
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitBreakerState,
    RetryConfig,
)
from .enhanced_error_handler import EnhancedCircuitBreaker
from .enhanced_error_handler import EnhancedRetryManager
from .enhanced_error_handler import ErrorHandler as _EnhancedErrorHandler


class CircuitBreaker:
    """Backward-compat wrapper: CircuitBreaker(name, failure_threshold=..., recovery_timeout=...)."""

    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0, _impl=None, **kwargs):
        if _impl is not None:
            self._impl = _impl
        else:
            config = CircuitBreakerConfig(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                **{k: v for k, v in kwargs.items() if k in ("half_open_max_calls", "expected_exceptions")},
            )
            self._impl = EnhancedCircuitBreaker(name, config)

    @property
    def state(self):
        return self._impl.state

    @state.setter
    def state(self, value):
        self._impl.state = value

    @property
    def name(self):
        return self._impl.name

    @property
    def failure_count(self):
        return self._impl.failure_count

    @failure_count.setter
    def failure_count(self, value):
        self._impl.failure_count = value

    @property
    def last_failure_time(self):
        return self._impl.last_failure_time

    @last_failure_time.setter
    def last_failure_time(self, value):
        if isinstance(value, (int, float)):
            from datetime import datetime, timezone
            value = datetime.fromtimestamp(value, tz=timezone.utc)
        self._impl.last_failure_time = value

    async def call(self, func, *args, **kwargs):
        return await self._impl.call(func, *args, **kwargs)


class DeadLetterQueue:
    """Placeholder DLQ for backward compat (no-op)."""

    def __init__(self, max_size: int = 100, *args, **kwargs):  # noqa: D401
        self.max_size = max_size
        self.queue: list[dict] = []

    async def put(self, item):  # noqa: D401
        await self.add_message(item, Exception("Queued"))

    async def get(self):  # noqa: D401
        if self.queue:
            return self.queue.pop(0)
        return None

    def __len__(self):
        return len(self.queue)

    async def add_message(self, message, error) -> None:
        """Add message to in-memory DLQ."""
        if len(self.queue) >= self.max_size:
            self.queue.pop(0)
        self.queue.append({"message": message, "error": str(error)})

    async def process_retryable_messages(self):
        """Return current retryable messages (no-op)."""
        return list(self.queue)


class _RetryManagerCompat:
    """Wrapper so execute_with_retry(..., max_retries=N) works."""

    def __init__(self, impl):
        self._impl = impl

    async def execute_with_retry(self, func, *args, max_retries=None, config=None, **kwargs):
        cfg = config
        if max_retries is not None and cfg is None:
            cfg = RetryConfig(max_attempts=max_retries, retryable_exceptions=[Exception])
        return await self._impl.execute_with_retry(func, *args, config=cfg, **kwargs)


class RetryManager:
    """Backward-compat retry manager wrapper."""

    def __init__(self, timescale_client: Optional[object] = None, config: Optional[RetryConfig] = None):
        self._client = timescale_client
        self._impl = EnhancedRetryManager(config)

    async def execute_with_retry(self, func, *args, max_retries=None, config=None, **kwargs):
        cfg = config
        if max_retries is not None and cfg is None:
            cfg = RetryConfig(max_attempts=max_retries, retryable_exceptions=[Exception])
        return await self._impl.execute_with_retry(func, *args, config=cfg, **kwargs)


class ErrorHandler:
    """Backward-compat: accepts optional timescale_client, exposes get_circuit_breaker and dlq."""

    def __init__(self, timescale_client: Optional[object] = None):
        self._client = timescale_client
        self._impl = _EnhancedErrorHandler()
        self._impl.add_circuit_breaker("database", CircuitBreakerConfig())
        self.retry_manager = _RetryManagerCompat(self._impl.retry_manager)
        self.dlq = DeadLetterQueue()
        self.circuit_breakers = self._impl.circuit_breakers

    def get_circuit_breaker(self, name: str) -> CircuitBreaker:
        if name not in self._impl.circuit_breakers:
            self._impl.add_circuit_breaker(name, CircuitBreakerConfig())
        cb = self._impl.circuit_breakers[name]
        return CircuitBreaker(name, _impl=cb)
