"""Simplified working tests for the Favonius Energy system."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timezone

# Import managers
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.error_handler import CircuitBreaker, RetryManager, DeadLetterQueue, ErrorHandler, CircuitBreakerOpenError


class TestCircuitBreaker:
    """Test CircuitBreaker functionality."""

    @pytest.fixture
    def circuit_breaker(self):
        """Create CircuitBreaker instance."""
        return CircuitBreaker("test_service", failure_threshold=3, timeout_seconds=999999)

    @pytest.mark.asyncio
    async def test_circuit_breaker_closed_state(self, circuit_breaker):
        """Test circuit breaker in closed state."""
        async def success_func():
            return "success"

        result = await circuit_breaker.call(success_func)
        assert result == "success"
        assert circuit_breaker.state.value == "closed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_failures(self, circuit_breaker):
        """Test circuit breaker opens after threshold failures."""
        async def failing_func():
            raise Exception("Test failure")

        # Should fail 3 times before opening
        for i in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)

        # Circuit should now be open
        assert circuit_breaker.state.value == "open"

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_when_open(self, circuit_breaker):
        """Test circuit breaker blocks calls when open."""
        # Force circuit to open state and set failure time
        circuit_breaker.state = circuit_breaker.state.__class__("open")
        circuit_breaker.failure_count = 5
        circuit_breaker.last_failure_time = 0  # Set to past time to prevent reset

        async def any_func():
            return "should not be called"

        with pytest.raises(CircuitBreakerOpenError):
            await circuit_breaker.call(any_func)


class TestRetryManager:
    """Test RetryManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_retry_attempt = AsyncMock()
        return client

    @pytest.fixture
    def retry_manager(self, mock_timescale_client):
        """Create RetryManager instance."""
        return RetryManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self, retry_manager):
        """Test retry manager succeeds on first attempt."""
        async def success_func():
            return "success"

        result = await retry_manager.execute_with_retry(success_func, max_retries=3)
        assert result == "success"

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_failures(self, retry_manager):
        """Test retry manager succeeds after some failures."""
        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary failure")
            return "success"

        result = await retry_manager.execute_with_retry(flaky_func, max_retries=3)
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausts_attempts(self, retry_manager):
        """Test retry manager exhausts all attempts."""
        async def always_failing_func():
            raise Exception("Permanent failure")

        with pytest.raises(Exception):
            await retry_manager.execute_with_retry(always_failing_func, max_retries=2)


class TestDeadLetterQueue:
    """Test DeadLetterQueue functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_dead_letter_message = AsyncMock()
        client.get_retryable_dlq_messages = AsyncMock()
        client.update_dlq_message_retry = AsyncMock()
        client.mark_dlq_message_processed = AsyncMock()
        return client

    @pytest.fixture
    def dead_letter_queue(self, mock_timescale_client):
        """Create DeadLetterQueue instance."""
        return DeadLetterQueue(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_add_message(self, dead_letter_queue, mock_timescale_client):
        """Test adding message to dead letter queue."""
        original_message = {"test": "data"}
        error_message = "Test error"
        error_type = "TestError"

        message_id = await dead_letter_queue.add_message(
            original_message, error_message, error_type
        )

        assert message_id is not None
        mock_timescale_client.store_dead_letter_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_retryable_messages(self, dead_letter_queue, mock_timescale_client):
        """Test processing retryable messages."""
        mock_timescale_client.get_retryable_dlq_messages.return_value = []

        await dead_letter_queue.process_retryable_messages()

        mock_timescale_client.get_retryable_dlq_messages.assert_called_once()


class TestErrorHandler:
    """Test ErrorHandler functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_circuit_breaker_state = AsyncMock()
        client.store_dead_letter_message = AsyncMock()
        client.store_retry_attempt = AsyncMock()
        client.store_health_check_result = AsyncMock()
        client.get_degradation_rules = AsyncMock()
        return client

    @pytest.fixture
    def error_handler(self, mock_timescale_client):
        """Create ErrorHandler instance."""
        return ErrorHandler(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_get_circuit_breaker(self, error_handler):
        """Test getting circuit breaker."""
        circuit_breaker = error_handler.get_circuit_breaker("test_service")
        
        assert circuit_breaker is not None
        assert circuit_breaker.service_name == "test_service"

    @pytest.mark.asyncio
    async def test_initialize(self, error_handler, mock_timescale_client):
        """Test error handler initialization."""
        mock_timescale_client.get_degradation_rules.return_value = []

        await error_handler.initialize()

        mock_timescale_client.get_degradation_rules.assert_called_once()


class TestSystemIntegration:
    """Test system integration."""

    @pytest.mark.asyncio
    async def test_error_handler_components(self):
        """Test that error handler components work together."""
        mock_timescale_client = Mock()
        mock_timescale_client.store_circuit_breaker_state = AsyncMock()
        mock_timescale_client.store_dead_letter_message = AsyncMock()
        mock_timescale_client.store_retry_attempt = AsyncMock()
        mock_timescale_client.store_health_check_result = AsyncMock()
        mock_timescale_client.get_degradation_rules = AsyncMock()
        mock_timescale_client.get_retryable_dlq_messages = AsyncMock()

        error_handler = ErrorHandler(mock_timescale_client)
        
        # Test circuit breaker
        circuit_breaker = error_handler.get_circuit_breaker("test_service")
        assert circuit_breaker.service_name == "test_service"

        # Test retry manager
        retry_manager = error_handler.retry_manager
        assert retry_manager is not None

        # Test dead letter queue
        dead_letter_queue = error_handler.dead_letter_queue
        assert dead_letter_queue is not None

        # Test health checker
        health_checker = error_handler.health_checker
        assert health_checker is not None

        # Test degradation manager
        degradation_manager = error_handler.degradation_manager
        assert degradation_manager is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_with_retry(self):
        """Test circuit breaker with retry logic."""
        mock_timescale_client = Mock()
        mock_timescale_client.store_circuit_breaker_state = AsyncMock()
        mock_timescale_client.store_retry_attempt = AsyncMock()

        error_handler = ErrorHandler(mock_timescale_client)
        circuit_breaker = error_handler.get_circuit_breaker("test_service")
        retry_manager = error_handler.retry_manager

        # Test successful call with retry
        async def success_func():
            return "success"

        result = await retry_manager.execute_with_retry(success_func, max_retries=3)
        assert result == "success"

        # Test circuit breaker with failing function
        async def failing_func():
            raise Exception("Test failure")

        with pytest.raises(Exception):
            await circuit_breaker.call(failing_func)

        assert circuit_breaker.failure_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


