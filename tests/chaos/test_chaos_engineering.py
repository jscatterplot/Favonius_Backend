"""Chaos engineering tests for V2G system resilience."""

import pytest
import asyncio
import random
import time
from typing import List, Dict, Any
from unittest.mock import AsyncMock, patch
import psutil
import os
import signal

from src.websocket_handler.der_control_manager import DERControlManager
from src.websocket_handler.timescale_client import TimescaleClient
from src.websocket_handler.enhanced_error_handler import error_handler, CircuitBreakerConfig, RetryConfig
from src.websocket_handler.resilience_manager import resilience_manager


class ChaosInjector:
    """Inject various types of failures and chaos into the system."""
    
    def __init__(self):
        self.active_failures: List[str] = []
        self.original_methods: Dict[str, Any] = {}
    
    def inject_database_failure(self, failure_rate: float = 0.3, duration: float = 10.0):
        """Inject database connection failures."""
        async def failing_execute_query(*args, **kwargs):
            if random.random() < failure_rate:
                raise ConnectionError("Simulated database connection failure")
            return None
        
        async def failing_fetch_one(*args, **kwargs):
            if random.random() < failure_rate:
                raise ConnectionError("Simulated database query failure")
            return None
        
        async def failing_fetch_all(*args, **kwargs):
            if random.random() < failure_rate:
                raise ConnectionError("Simulated database query failure")
            return []
        
        return {
            "execute_query": failing_execute_query,
            "fetch_one": failing_fetch_one,
            "fetch_all": failing_fetch_all
        }
    
    def inject_network_latency(self, base_latency: float = 0.1, jitter: float = 0.05):
        """Inject network latency into operations."""
        async def delayed_operation(operation, *args, **kwargs):
            delay = base_latency + random.uniform(-jitter, jitter)
            await asyncio.sleep(delay)
            return await operation(*args, **kwargs)
        
        return delayed_operation
    
    def inject_memory_pressure(self, target_usage: float = 0.8):
        """Simulate memory pressure by allocating memory."""
        memory_blocks = []
        
        def allocate_memory():
            # Allocate memory to increase usage
            block_size = 1024 * 1024  # 1MB blocks
            while psutil.virtual_memory().percent < target_usage * 100:
                try:
                    memory_blocks.append(bytearray(block_size))
                except MemoryError:
                    break
        
        def release_memory():
            memory_blocks.clear()
        
        return allocate_memory, release_memory
    
    def inject_cpu_stress(self, duration: float = 5.0):
        """Simulate CPU stress."""
        def cpu_stress():
            end_time = time.time() + duration
            while time.time() < end_time:
                # CPU-intensive operation
                sum(i * i for i in range(10000))
        
        return cpu_stress
    
    def inject_random_exceptions(self, exception_rate: float = 0.2):
        """Inject random exceptions into operations."""
        exceptions = [
            ConnectionError("Network connection lost"),
            TimeoutError("Operation timed out"),
            OSError("System resource unavailable"),
            RuntimeError("Unexpected runtime error"),
            ValueError("Invalid input value")
        ]
        
        async def exception_injector(operation, *args, **kwargs):
            if random.random() < exception_rate:
                exception = random.choice(exceptions)
                raise exception
            return await operation(*args, **kwargs)
        
        return exception_injector


@pytest.fixture
def chaos_injector():
    """Create chaos injector for testing."""
    return ChaosInjector()


@pytest.fixture
def mock_timescale_client_with_failures(chaos_injector):
    """Mock TimescaleDB client with failure injection."""
    client = AsyncMock(spec=TimescaleClient)
    
    # Inject database failures
    failure_methods = chaos_injector.inject_database_failure(failure_rate=0.2)
    client.execute_query = failure_methods["execute_query"]
    client.fetch_one = failure_methods["fetch_one"]
    client.fetch_all = failure_methods["fetch_all"]
    
    return client


@pytest.fixture
def der_control_manager_with_failures(mock_timescale_client_with_failures):
    """Create DER control manager with failure injection."""
    manager = DERControlManager(mock_timescale_client_with_failures)
    
    # Mock other operations
    manager._get_station_der_capabilities = AsyncMock(return_value={
        "modesSupported": ["FixedPFInject", "VoltVar", "WattVar", "FreqDroop", "LimitMaxDischarge"]
    })
    manager._get_active_der_controls = AsyncMock(return_value=[])
    manager._store_der_control = AsyncMock()
    manager._update_control_cache = AsyncMock()
    manager._get_der_control_by_id = AsyncMock(return_value=None)
    manager._clear_der_control_by_id = AsyncMock(return_value=True)
    manager._clear_all_der_controls = AsyncMock(return_value=0)
    manager._store_reported_der_controls = AsyncMock()
    manager._store_der_alarm_event = AsyncMock()
    manager._store_der_start_stop_event = AsyncMock()
    manager._activate_der_control = AsyncMock()
    manager._deactivate_der_control = AsyncMock()
    
    return manager


class TestDatabaseFailureResilience:
    """Test system resilience to database failures."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_database_connection_failures(self, der_control_manager_with_failures):
        """Test system behavior under database connection failures."""
        success_count = 0
        failure_count = 0
        total_operations = 100
        
        for i in range(total_operations):
            control_data = {
                "controlId": i,
                "controlType": "FixedPFInject",
                "priority": i % 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            
            try:
                result = await der_control_manager_with_failures.set_der_control(
                    f"station_{i % 10}", control_data
                )
                if result["status"] == "Accepted":
                    success_count += 1
                else:
                    failure_count += 1
            except Exception:
                failure_count += 1
        
        success_rate = success_count / total_operations
        failure_rate = failure_count / total_operations
        
        print(f"\nDatabase failure resilience test:")
        print(f"Total operations: {total_operations}")
        print(f"Successful: {success_count} ({success_rate:.1%})")
        print(f"Failed: {failure_count} ({failure_rate:.1%})")
        
        # System should still handle some operations successfully
        assert success_rate > 0.5, f"Success rate too low: {success_rate:.1%}"
        # Some failures are expected due to injected chaos
        assert failure_rate > 0.1, f"Failure rate too low: {failure_rate:.1%}"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_circuit_breaker_activation(self, der_control_manager_with_failures):
        """Test circuit breaker activation under high failure rates."""
        # Configure circuit breaker for testing
        error_handler.add_circuit_breaker("test_service", CircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout=10.0
        ))
        
        # Inject high failure rate
        failure_count = 0
        circuit_breaker_activated = False
        
        for i in range(20):
            control_data = {
                "controlId": i,
                "controlType": "FixedPFInject",
                "priority": i % 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            
            try:
                result = await error_handler.execute_with_resilience(
                    der_control_manager_with_failures.set_der_control,
                    f"station_{i % 5}", control_data,
                    circuit_breaker="test_service"
                )
                if result["status"] != "Accepted":
                    failure_count += 1
            except Exception as e:
                failure_count += 1
                if "Circuit breaker" in str(e):
                    circuit_breaker_activated = True
                    break
        
        print(f"\nCircuit breaker test:")
        print(f"Failures before activation: {failure_count}")
        print(f"Circuit breaker activated: {circuit_breaker_activated}")
        
        # Circuit breaker should activate after threshold
        assert circuit_breaker_activated, "Circuit breaker should have activated"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_retry_mechanism_under_failures(self, der_control_manager_with_failures):
        """Test retry mechanism under intermittent failures."""
        retry_config = RetryConfig(
            max_attempts=3,
            base_delay=0.1,
            max_delay=1.0
        )
        
        success_count = 0
        retry_count = 0
        total_operations = 50
        
        for i in range(total_operations):
            control_data = {
                "controlId": i,
                "controlType": "VoltVar",
                "priority": i % 10,
                "curve": {
                    "curveType": "VoltVar",
                    "curvePoints": [
                        {"x": 0.95, "y": 0.2},
                        {"x": 1.0, "y": 0.0},
                        {"x": 1.05, "y": -0.2}
                    ],
                    "curveUnitX": "p.u.",
                    "curveUnitY": "p.u."
                }
            }
            
            try:
                result = await error_handler.execute_with_resilience(
                    der_control_manager_with_failures.set_der_control,
                    f"station_{i % 8}", control_data,
                    retry_config=retry_config
                )
                if result["status"] == "Accepted":
                    success_count += 1
            except Exception as e:
                if "retry" in str(e).lower():
                    retry_count += 1
        
        success_rate = success_count / total_operations
        
        print(f"\nRetry mechanism test:")
        print(f"Total operations: {total_operations}")
        print(f"Successful: {success_count} ({success_rate:.1%})")
        print(f"Retry attempts: {retry_count}")
        
        # Retry mechanism should improve success rate
        assert success_rate > 0.6, f"Success rate too low: {success_rate:.1%}"


class TestResourceExhaustionResilience:
    """Test system resilience to resource exhaustion."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_memory_pressure_resilience(self, der_control_manager, chaos_injector):
        """Test system behavior under memory pressure."""
        # Inject memory pressure
        allocate_memory, release_memory = chaos_injector.inject_memory_pressure(target_usage=0.85)
        
        try:
            # Allocate memory to create pressure
            allocate_memory()
            
            initial_memory = psutil.virtual_memory().percent
            print(f"Initial memory usage: {initial_memory:.1f}%")
            
            # Run operations under memory pressure
            success_count = 0
            total_operations = 50
            
            for i in range(total_operations):
                control_data = {
                    "controlId": i,
                    "controlType": "FixedPFInject",
                    "priority": i % 10,
                    "startTime": "2024-01-01T00:00:00Z",
                    "duration": 3600
                }
                
                try:
                    result = await der_control_manager.set_der_control(
                        f"station_{i % 10}", control_data
                    )
                    if result["status"] == "Accepted":
                        success_count += 1
                except MemoryError:
                    # System should handle memory errors gracefully
                    pass
                except Exception:
                    # Other errors are acceptable under memory pressure
                    pass
            
            final_memory = psutil.virtual_memory().percent
            success_rate = success_count / total_operations
            
            print(f"Final memory usage: {final_memory:.1f}%")
            print(f"Operations under pressure: {total_operations}")
            print(f"Successful: {success_count} ({success_rate:.1%})")
            
            # System should still function under memory pressure
            assert success_rate > 0.3, f"Success rate too low under memory pressure: {success_rate:.1%}"
            
        finally:
            # Release allocated memory
            release_memory()
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_cpu_stress_resilience(self, der_control_manager, chaos_injector):
        """Test system behavior under CPU stress."""
        # Inject CPU stress
        cpu_stress = chaos_injector.inject_cpu_stress(duration=10.0)
        
        # Run CPU stress in background
        stress_task = asyncio.create_task(asyncio.to_thread(cpu_stress))
        
        try:
            initial_cpu = psutil.cpu_percent()
            print(f"Initial CPU usage: {initial_cpu:.1f}%")
            
            # Run operations under CPU stress
            success_count = 0
            total_operations = 30
            
            for i in range(total_operations):
                control_data = {
                    "controlId": i,
                    "controlType": "FreqDroop",
                    "priority": i % 10,
                    "overFreq": 50.2,
                    "underFreq": 49.8,
                    "overDroop": 0.05,
                    "underDroop": 0.05,
                    "responseTime": 5
                }
                
                try:
                    result = await der_control_manager.set_der_control(
                        f"station_{i % 8}", control_data
                    )
                    if result["status"] == "Accepted":
                        success_count += 1
                except Exception:
                    # Some failures are expected under CPU stress
                    pass
            
            final_cpu = psutil.cpu_percent()
            success_rate = success_count / total_operations
            
            print(f"Final CPU usage: {final_cpu:.1f}%")
            print(f"Operations under stress: {total_operations}")
            print(f"Successful: {success_count} ({success_rate:.1%})")
            
            # System should still function under CPU stress
            assert success_rate > 0.4, f"Success rate too low under CPU stress: {success_rate:.1%}"
            
        finally:
            # Cancel stress task
            stress_task.cancel()
            try:
                await stress_task
            except asyncio.CancelledError:
                pass


class TestNetworkFailureResilience:
    """Test system resilience to network failures."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_network_latency_resilience(self, der_control_manager, chaos_injector):
        """Test system behavior under network latency."""
        # Inject network latency
        delayed_operation = chaos_injector.inject_network_latency(
            base_latency=0.5, jitter=0.2
        )
        
        # Apply latency to database operations
        original_methods = {
            "execute_query": der_control_manager.timescale_client.execute_query,
            "fetch_one": der_control_manager.timescale_client.fetch_one,
            "fetch_all": der_control_manager.timescale_client.fetch_all
        }
        
        der_control_manager.timescale_client.execute_query = lambda *args, **kwargs: delayed_operation(
            original_methods["execute_query"], *args, **kwargs
        )
        der_control_manager.timescale_client.fetch_one = lambda *args, **kwargs: delayed_operation(
            original_methods["fetch_one"], *args, **kwargs
        )
        der_control_manager.timescale_client.fetch_all = lambda *args, **kwargs: delayed_operation(
            original_methods["fetch_all"], *args, **kwargs
        )
        
        try:
            # Run operations with network latency
            response_times = []
            success_count = 0
            total_operations = 20
            
            for i in range(total_operations):
                control_data = {
                    "controlId": i,
                    "controlType": "VoltVar",
                    "priority": i % 10,
                    "curve": {
                        "curveType": "VoltVar",
                        "curvePoints": [
                            {"x": 0.95, "y": 0.2},
                            {"x": 1.0, "y": 0.0},
                            {"x": 1.05, "y": -0.2}
                        ],
                        "curveUnitX": "p.u.",
                        "curveUnitY": "p.u."
                    }
                }
                
                start_time = time.time()
                try:
                    result = await der_control_manager.set_der_control(
                        f"station_{i % 5}", control_data
                    )
                    response_time = time.time() - start_time
                    response_times.append(response_time)
                    
                    if result["status"] == "Accepted":
                        success_count += 1
                except Exception:
                    pass
            
            avg_response_time = sum(response_times) / len(response_times) if response_times else 0
            success_rate = success_count / total_operations
            
            print(f"\nNetwork latency resilience test:")
            print(f"Total operations: {total_operations}")
            print(f"Successful: {success_count} ({success_rate:.1%})")
            print(f"Average response time: {avg_response_time:.3f}s")
            print(f"Response times: {response_times}")
            
            # System should handle latency gracefully
            assert success_rate > 0.7, f"Success rate too low under latency: {success_rate:.1%}"
            assert avg_response_time > 0.3, f"Response time too low (latency not applied): {avg_response_time:.3f}s"
            
        finally:
            # Restore original methods
            der_control_manager.timescale_client.execute_query = original_methods["execute_query"]
            der_control_manager.timescale_client.fetch_one = original_methods["fetch_one"]
            der_control_manager.timescale_client.fetch_all = original_methods["fetch_all"]


class TestCascadingFailureResilience:
    """Test system resilience to cascading failures."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_cascading_database_failures(self, der_control_manager_with_failures):
        """Test system behavior under cascading database failures."""
        # Simulate cascading failures with increasing failure rate
        failure_rates = [0.1, 0.3, 0.5, 0.7, 0.9]
        results = []
        
        for failure_rate in failure_rates:
            # Update failure rate
            chaos_injector = ChaosInjector()
            failure_methods = chaos_injector.inject_database_failure(failure_rate=failure_rate)
            
            der_control_manager_with_failures.timescale_client.execute_query = failure_methods["execute_query"]
            der_control_manager_with_failures.timescale_client.fetch_one = failure_methods["fetch_one"]
            der_control_manager_with_failures.timescale_client.fetch_all = failure_methods["fetch_all"]
            
            # Run operations with current failure rate
            success_count = 0
            total_operations = 20
            
            for i in range(total_operations):
                control_data = {
                    "controlId": i,
                    "controlType": "FixedPFInject",
                    "priority": i % 10,
                    "startTime": "2024-01-01T00:00:00Z",
                    "duration": 3600
                }
                
                try:
                    result = await der_control_manager_with_failures.set_der_control(
                        f"station_{i % 5}", control_data
                    )
                    if result["status"] == "Accepted":
                        success_count += 1
                except Exception:
                    pass
            
            success_rate = success_count / total_operations
            results.append((failure_rate, success_rate))
            
            print(f"Failure rate: {failure_rate:.1%}, Success rate: {success_rate:.1%}")
        
        # Analyze results
        print(f"\nCascading failure analysis:")
        for failure_rate, success_rate in results:
            print(f"  {failure_rate:.1%} failure rate -> {success_rate:.1%} success rate")
        
        # System should degrade gracefully
        assert results[0][1] > results[-1][1], "Success rate should decrease with higher failure rates"
        assert results[-1][1] > 0.1, "System should not completely fail even at high failure rates"


class TestRecoveryResilience:
    """Test system recovery from failures."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_system_recovery_after_failures(self, der_control_manager, chaos_injector):
        """Test system recovery after failures are resolved."""
        # Phase 1: Inject failures
        print("Phase 1: Injecting failures...")
        failure_methods = chaos_injector.inject_database_failure(failure_rate=0.8)
        
        der_control_manager.timescale_client.execute_query = failure_methods["execute_query"]
        der_control_manager.timescale_client.fetch_one = failure_methods["fetch_one"]
        der_control_manager.timescale_client.fetch_all = failure_methods["fetch_all"]
        
        # Run operations during failure period
        failure_success_count = 0
        total_operations = 20
        
        for i in range(total_operations):
            control_data = {
                "controlId": i,
                "controlType": "FixedPFInject",
                "priority": i % 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            
            try:
                result = await der_control_manager.set_der_control(
                    f"station_{i % 5}", control_data
                )
                if result["status"] == "Accepted":
                    failure_success_count += 1
            except Exception:
                pass
        
        failure_success_rate = failure_success_count / total_operations
        print(f"Success rate during failures: {failure_success_rate:.1%}")
        
        # Phase 2: Restore normal operation
        print("Phase 2: Restoring normal operation...")
        
        # Restore normal database operations
        der_control_manager.timescale_client.execute_query = AsyncMock()
        der_control_manager.timescale_client.fetch_one = AsyncMock()
        der_control_manager.timescale_client.fetch_all = AsyncMock()
        
        # Wait for system to recover
        await asyncio.sleep(1.0)
        
        # Run operations after recovery
        recovery_success_count = 0
        total_operations = 20
        
        for i in range(total_operations):
            control_data = {
                "controlId": i + 100,  # Different control IDs
                "controlType": "VoltVar",
                "priority": i % 10,
                "curve": {
                    "curveType": "VoltVar",
                    "curvePoints": [
                        {"x": 0.95, "y": 0.2},
                        {"x": 1.0, "y": 0.0},
                        {"x": 1.05, "y": -0.2}
                    ],
                    "curveUnitX": "p.u.",
                    "curveUnitY": "p.u."
                }
            }
            
            try:
                result = await der_control_manager.set_der_control(
                    f"station_{i % 5}", control_data
                )
                if result["status"] == "Accepted":
                    recovery_success_count += 1
            except Exception:
                pass
        
        recovery_success_rate = recovery_success_count / total_operations
        print(f"Success rate after recovery: {recovery_success_rate:.1%}")
        
        # System should recover and perform better
        assert recovery_success_rate > failure_success_rate, "System should recover after failures"
        assert recovery_success_rate > 0.8, f"Recovery success rate too low: {recovery_success_rate:.1%}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])