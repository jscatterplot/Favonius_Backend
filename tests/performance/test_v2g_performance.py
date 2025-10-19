"""Performance tests for V2G operations and DER controls."""

import pytest
import asyncio
import time
import statistics
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any
import psutil
import os

from src.websocket_handler.der_control_manager import DERControlManager
from src.websocket_handler.timescale_client import TimescaleClient
from src.websocket_handler.config import Config, TimescaleConfig
from unittest.mock import AsyncMock


class PerformanceMetrics:
    """Collect and analyze performance metrics."""
    
    def __init__(self):
        self.response_times: List[float] = []
        self.throughput_rates: List[float] = []
        self.error_rates: List[float] = []
        self.memory_usage: List[float] = []
        self.cpu_usage: List[float] = []
    
    def add_response_time(self, response_time: float):
        """Add response time measurement."""
        self.response_times.append(response_time)
    
    def add_throughput(self, operations_per_second: float):
        """Add throughput measurement."""
        self.throughput_rates.append(operations_per_second)
    
    def add_error_rate(self, error_rate: float):
        """Add error rate measurement."""
        self.error_rates.append(error_rate)
    
    def add_system_metrics(self):
        """Add current system metrics."""
        self.memory_usage.append(psutil.virtual_memory().percent)
        self.cpu_usage.append(psutil.cpu_percent())
    
    def get_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        return {
            "response_times": {
                "mean": statistics.mean(self.response_times) if self.response_times else 0,
                "median": statistics.median(self.response_times) if self.response_times else 0,
                "p95": self._percentile(self.response_times, 95),
                "p99": self._percentile(self.response_times, 99),
                "max": max(self.response_times) if self.response_times else 0,
                "min": min(self.response_times) if self.response_times else 0
            },
            "throughput": {
                "mean": statistics.mean(self.throughput_rates) if self.throughput_rates else 0,
                "max": max(self.throughput_rates) if self.throughput_rates else 0
            },
            "error_rates": {
                "mean": statistics.mean(self.error_rates) if self.error_rates else 0,
                "max": max(self.error_rates) if self.error_rates else 0
            },
            "system_resources": {
                "memory_usage_mean": statistics.mean(self.memory_usage) if self.memory_usage else 0,
                "memory_usage_max": max(self.memory_usage) if self.memory_usage else 0,
                "cpu_usage_mean": statistics.mean(self.cpu_usage) if self.cpu_usage else 0,
                "cpu_usage_max": max(self.cpu_usage) if self.cpu_usage else 0
            }
        }
    
    def _percentile(self, data: List[float], percentile: int) -> float:
        """Calculate percentile."""
        if not data:
            return 0
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        return sorted_data[min(index, len(sorted_data) - 1)]


@pytest.fixture
def mock_timescale_client():
    """Mock TimescaleDB client for performance testing."""
    client = AsyncMock(spec=TimescaleClient)
    client.execute_query = AsyncMock()
    client.fetch_one = AsyncMock()
    client.fetch_all = AsyncMock()
    return client


@pytest.fixture
def der_control_manager(mock_timescale_client):
    """Create DER control manager for testing."""
    manager = DERControlManager(mock_timescale_client)
    
    # Mock all database operations
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


class TestDERControlPerformance:
    """Performance tests for DER control operations."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_concurrent_der_control_operations(self, der_control_manager):
        """Test concurrent DER control operations performance."""
        metrics = PerformanceMetrics()
        
        async def set_der_control_operation(station_id: str, control_id: int):
            """Single DER control operation."""
            start_time = time.time()
            
            control_data = {
                "controlId": control_id,
                "controlType": "FixedPFInject",
                "priority": control_id % 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            
            try:
                result = await der_control_manager.set_der_control(station_id, control_data)
                response_time = time.time() - start_time
                metrics.add_response_time(response_time)
                return result["status"] == "Accepted"
            except Exception as e:
                metrics.add_error_rate(1.0)
                return False
        
        # Test with different concurrency levels
        concurrency_levels = [10, 50, 100, 200]
        
        for concurrency in concurrency_levels:
            print(f"\nTesting with {concurrency} concurrent operations...")
            
            # Reset metrics
            metrics = PerformanceMetrics()
            
            # Create tasks
            tasks = []
            for i in range(concurrency):
                station_id = f"station_{i % 10}"  # 10 different stations
                control_id = i
                tasks.append(set_der_control_operation(station_id, control_id))
            
            # Execute concurrently
            start_time = time.time()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total_time = time.time() - start_time
            
            # Calculate metrics
            successful_operations = sum(1 for r in results if r is True)
            throughput = successful_operations / total_time
            error_rate = (concurrency - successful_operations) / concurrency
            
            metrics.add_throughput(throughput)
            metrics.add_error_rate(error_rate)
            metrics.add_system_metrics()
            
            print(f"Concurrency: {concurrency}")
            print(f"Total time: {total_time:.2f}s")
            print(f"Throughput: {throughput:.2f} ops/s")
            print(f"Error rate: {error_rate:.2%}")
            print(f"Successful operations: {successful_operations}/{concurrency}")
            
            # Performance assertions
            assert throughput > 10, f"Throughput too low: {throughput} ops/s"
            assert error_rate < 0.1, f"Error rate too high: {error_rate:.2%}"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_der_control_priority_handling_performance(self, der_control_manager):
        """Test performance of DER control priority handling."""
        metrics = PerformanceMetrics()
        
        # Create controls with different priorities
        controls = []
        for i in range(100):
            control_data = {
                "controlId": i,
                "controlType": "FixedPFInject",
                "priority": i % 20,  # 20 different priority levels
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            controls.append(control_data)
        
        # Mock existing controls for priority testing
        existing_controls = []
        for i in range(50):
            from src.websocket_handler.der_control_manager import DERControlType, DERControlEnumType
            control = DERControlType(
                control_id=i,
                control_type=DERControlEnumType.FIXED_PF_INJECT,
                priority=i % 15,
                is_superseded=False
            )
            existing_controls.append(control)
        
        der_control_manager._get_active_der_controls = AsyncMock(return_value=existing_controls)
        der_control_manager._update_der_control = AsyncMock()
        
        # Test priority handling performance
        start_time = time.time()
        
        for control_data in controls:
            operation_start = time.time()
            result = await der_control_manager.set_der_control("station_001", control_data)
            operation_time = time.time() - operation_start
            
            metrics.add_response_time(operation_time)
            assert result["status"] == "Accepted"
        
        total_time = time.time() - start_time
        throughput = len(controls) / total_time
        
        metrics.add_throughput(throughput)
        metrics.add_system_metrics()
        
        summary = metrics.get_summary()
        print(f"\nPriority handling performance:")
        print(f"Total operations: {len(controls)}")
        print(f"Total time: {total_time:.2f}s")
        print(f"Throughput: {throughput:.2f} ops/s")
        print(f"Mean response time: {summary['response_times']['mean']:.3f}s")
        print(f"P95 response time: {summary['response_times']['p95']:.3f}s")
        
        # Performance assertions
        assert throughput > 50, f"Throughput too low: {throughput} ops/s"
        assert summary['response_times']['mean'] < 0.1, f"Mean response time too high: {summary['response_times']['mean']:.3f}s"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_curve_based_control_performance(self, der_control_manager):
        """Test performance of curve-based DER controls."""
        metrics = PerformanceMetrics()
        
        # Create curve-based controls with different complexities
        curve_controls = []
        for i in range(50):
            # Create curve with varying number of points
            num_points = 5 + (i % 20)  # 5 to 24 points
            curve_points = []
            for j in range(num_points):
                x = 0.9 + (j / (num_points - 1)) * 0.2  # 0.9 to 1.1
                y = (j / (num_points - 1)) * 0.4 - 0.2  # -0.2 to 0.2
                curve_points.append({"x": x, "y": y})
            
            control_data = {
                "controlId": i,
                "controlType": "VoltVar",
                "priority": i % 10,
                "curve": {
                    "curveType": "VoltVar",
                    "curvePoints": curve_points,
                    "curveUnitX": "p.u.",
                    "curveUnitY": "p.u."
                }
            }
            curve_controls.append(control_data)
        
        # Test curve parsing and processing performance
        start_time = time.time()
        
        for control_data in curve_controls:
            operation_start = time.time()
            result = await der_control_manager.set_der_control("station_001", control_data)
            operation_time = time.time() - operation_start
            
            metrics.add_response_time(operation_time)
            assert result["status"] == "Accepted"
        
        total_time = time.time() - start_time
        throughput = len(curve_controls) / total_time
        
        metrics.add_throughput(throughput)
        metrics.add_system_metrics()
        
        summary = metrics.get_summary()
        print(f"\nCurve-based control performance:")
        print(f"Total operations: {len(curve_controls)}")
        print(f"Total time: {total_time:.2f}s")
        print(f"Throughput: {throughput:.2f} ops/s")
        print(f"Mean response time: {summary['response_times']['mean']:.3f}s")
        print(f"P95 response time: {summary['response_times']['p95']:.3f}s")
        
        # Performance assertions
        assert throughput > 20, f"Throughput too low: {throughput} ops/s"
        assert summary['response_times']['mean'] < 0.2, f"Mean response time too high: {summary['response_times']['mean']:.3f}s"


class TestV2GWorkflowPerformance:
    """Performance tests for complete V2G workflows."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_bidirectional_charging_workflow_performance(self, der_control_manager):
        """Test performance of complete bidirectional charging workflow."""
        metrics = PerformanceMetrics()
        
        async def bidirectional_workflow(station_id: str, workflow_id: int):
            """Complete bidirectional charging workflow."""
            start_time = time.time()
            
            try:
                # Step 1: Set discharge limit
                discharge_control = {
                    "controlId": workflow_id * 3 + 1,
                    "controlType": "LimitMaxDischarge",
                    "priority": 10,
                    "pctMaxDischargePower": 0.8
                }
                result1 = await der_control_manager.set_der_control(station_id, discharge_control)
                
                # Step 2: Set power factor control
                pf_control = {
                    "controlId": workflow_id * 3 + 2,
                    "controlType": "FixedPFInject",
                    "priority": 5,
                    "startTime": "2024-01-01T12:00:00Z",
                    "duration": 3600
                }
                result2 = await der_control_manager.set_der_control(station_id, pf_control)
                
                # Step 3: Report controls
                result3 = await der_control_manager.report_der_control(
                    station_id, [discharge_control, pf_control]
                )
                
                # Step 4: Start controls
                result4 = await der_control_manager.notify_der_start_stop(
                    station_id, workflow_id * 3 + 1, True
                )
                result5 = await der_control_manager.notify_der_start_stop(
                    station_id, workflow_id * 3 + 2, True
                )
                
                workflow_time = time.time() - start_time
                metrics.add_response_time(workflow_time)
                
                return all([
                    result1["status"] == "Accepted",
                    result2["status"] == "Accepted",
                    result3["status"] == "Accepted",
                    result4["status"] == "Accepted",
                    result5["status"] == "Accepted"
                ])
                
            except Exception as e:
                metrics.add_error_rate(1.0)
                return False
        
        # Test with multiple concurrent workflows
        num_workflows = 20
        tasks = []
        
        for i in range(num_workflows):
            station_id = f"station_{i % 5}"  # 5 different stations
            tasks.append(bidirectional_workflow(station_id, i))
        
        start_time = time.time()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_time = time.time() - start_time
        
        successful_workflows = sum(1 for r in results if r is True)
        throughput = successful_workflows / total_time
        error_rate = (num_workflows - successful_workflows) / num_workflows
        
        metrics.add_throughput(throughput)
        metrics.add_error_rate(error_rate)
        metrics.add_system_metrics()
        
        summary = metrics.get_summary()
        print(f"\nBidirectional charging workflow performance:")
        print(f"Total workflows: {num_workflows}")
        print(f"Total time: {total_time:.2f}s")
        print(f"Throughput: {throughput:.2f} workflows/s")
        print(f"Error rate: {error_rate:.2%}")
        print(f"Mean workflow time: {summary['response_times']['mean']:.3f}s")
        
        # Performance assertions
        assert throughput > 2, f"Throughput too low: {throughput} workflows/s"
        assert error_rate < 0.1, f"Error rate too high: {error_rate:.2%}"
        assert summary['response_times']['mean'] < 1.0, f"Mean workflow time too high: {summary['response_times']['mean']:.3f}s"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_frequency_response_workflow_performance(self, der_control_manager):
        """Test performance of frequency response workflow."""
        metrics = PerformanceMetrics()
        
        async def frequency_response_workflow(station_id: str, workflow_id: int):
            """Frequency response workflow."""
            start_time = time.time()
            
            try:
                # Step 1: Set frequency droop control
                freq_control = {
                    "controlId": workflow_id * 2 + 1,
                    "controlType": "FreqDroop",
                    "priority": 15,
                    "overFreq": 50.2,
                    "underFreq": 49.8,
                    "overDroop": 0.05,
                    "underDroop": 0.05,
                    "responseTime": 5
                }
                result1 = await der_control_manager.set_der_control(station_id, freq_control)
                
                # Step 2: Simulate frequency alarm
                result2 = await der_control_manager.notify_der_alarm(
                    station_id, "FreqDroop", False, "OverFrequency", "2024-01-01T12:00:00Z"
                )
                
                # Step 3: Start frequency response
                result3 = await der_control_manager.notify_der_start_stop(
                    station_id, workflow_id * 2 + 1, True
                )
                
                workflow_time = time.time() - start_time
                metrics.add_response_time(workflow_time)
                
                return all([
                    result1["status"] == "Accepted",
                    result2["status"] == "Accepted",
                    result3["status"] == "Accepted"
                ])
                
            except Exception as e:
                metrics.add_error_rate(1.0)
                return False
        
        # Test with multiple concurrent workflows
        num_workflows = 30
        tasks = []
        
        for i in range(num_workflows):
            station_id = f"station_{i % 8}"  # 8 different stations
            tasks.append(frequency_response_workflow(station_id, i))
        
        start_time = time.time()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_time = time.time() - start_time
        
        successful_workflows = sum(1 for r in results if r is True)
        throughput = successful_workflows / total_time
        error_rate = (num_workflows - successful_workflows) / num_workflows
        
        metrics.add_throughput(throughput)
        metrics.add_error_rate(error_rate)
        metrics.add_system_metrics()
        
        summary = metrics.get_summary()
        print(f"\nFrequency response workflow performance:")
        print(f"Total workflows: {num_workflows}")
        print(f"Total time: {total_time:.2f}s")
        print(f"Throughput: {throughput:.2f} workflows/s")
        print(f"Error rate: {error_rate:.2%}")
        print(f"Mean workflow time: {summary['response_times']['mean']:.3f}s")
        
        # Performance assertions
        assert throughput > 3, f"Throughput too low: {throughput} workflows/s"
        assert error_rate < 0.1, f"Error rate too high: {error_rate:.2%}"
        assert summary['response_times']['mean'] < 0.5, f"Mean workflow time too high: {summary['response_times']['mean']:.3f}s"


class TestSystemResourceUsage:
    """Test system resource usage during V2G operations."""
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_memory_usage_under_load(self, der_control_manager):
        """Test memory usage under high load."""
        initial_memory = psutil.virtual_memory().percent
        memory_samples = [initial_memory]
        
        # Run high load operations
        num_operations = 1000
        tasks = []
        
        for i in range(num_operations):
            control_data = {
                "controlId": i,
                "controlType": "FixedPFInject",
                "priority": i % 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            tasks.append(der_control_manager.set_der_control(f"station_{i % 20}", control_data))
        
        # Sample memory during execution
        async def memory_monitor():
            for _ in range(10):
                await asyncio.sleep(0.1)
                memory_samples.append(psutil.virtual_memory().percent)
        
        # Run operations and memory monitoring concurrently
        monitor_task = asyncio.create_task(memory_monitor())
        await asyncio.gather(*tasks)
        await monitor_task
        
        final_memory = psutil.virtual_memory().percent
        max_memory = max(memory_samples)
        memory_increase = max_memory - initial_memory
        
        print(f"\nMemory usage under load:")
        print(f"Initial memory: {initial_memory:.1f}%")
        print(f"Final memory: {final_memory:.1f}%")
        print(f"Max memory: {max_memory:.1f}%")
        print(f"Memory increase: {memory_increase:.1f}%")
        
        # Memory usage assertions
        assert memory_increase < 20, f"Memory increase too high: {memory_increase:.1f}%"
        assert max_memory < 90, f"Max memory usage too high: {max_memory:.1f}%"
    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_cpu_usage_under_load(self, der_control_manager):
        """Test CPU usage under high load."""
        initial_cpu = psutil.cpu_percent()
        cpu_samples = [initial_cpu]
        
        # Run high load operations
        num_operations = 500
        tasks = []
        
        for i in range(num_operations):
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
            tasks.append(der_control_manager.set_der_control(f"station_{i % 15}", control_data))
        
        # Sample CPU during execution
        async def cpu_monitor():
            for _ in range(20):
                await asyncio.sleep(0.05)
                cpu_samples.append(psutil.cpu_percent())
        
        # Run operations and CPU monitoring concurrently
        monitor_task = asyncio.create_task(cpu_monitor())
        await asyncio.gather(*tasks)
        await monitor_task
        
        final_cpu = psutil.cpu_percent()
        max_cpu = max(cpu_samples)
        avg_cpu = statistics.mean(cpu_samples)
        
        print(f"\nCPU usage under load:")
        print(f"Initial CPU: {initial_cpu:.1f}%")
        print(f"Final CPU: {final_cpu:.1f}%")
        print(f"Max CPU: {max_cpu:.1f}%")
        print(f"Average CPU: {avg_cpu:.1f}%")
        
        # CPU usage assertions
        assert max_cpu < 95, f"Max CPU usage too high: {max_cpu:.1f}%"
        assert avg_cpu < 80, f"Average CPU usage too high: {avg_cpu:.1f}%"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])