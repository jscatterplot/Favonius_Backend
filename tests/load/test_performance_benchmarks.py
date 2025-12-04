"""
Performance benchmarks and stress tests for OCPP WebSocket server.
Tests system limits and performance characteristics.
"""

import pytest
import pytest_asyncio
import asyncio
import time
import json
import statistics
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock

from src.websocket_handler.server import OCPPWebSocketServer
from src.websocket_handler.connection_manager import ConnectionManager
from src.websocket_handler.message_handler import MessageHandler
from src.websocket_handler.config import Config


class TestPerformanceBenchmarks:
    """Performance benchmark tests."""
    
    @pytest_asyncio.fixture
    async def config(self):
        """Create test configuration."""
        return Config(
            timescale={
                "service_url": "postgresql://test:test@localhost:5432/test",
                "host": "localhost",
                "user": "test",
                "password": "test",
                "database": "test",
                "port": 5432
            },
            supabase={
                "url": "https://test.supabase.co",
                "anon_key": "test_anon_key",
                "service_key": "test_service_key",
                "db_host": "localhost",
                "db_port": 5432,
                "db_name": "postgres",
                "db_user": "test",
                "db_password": "test"
            }
        )
    
    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)
    
    @pytest_asyncio.fixture
    async def message_handler(self, connection_manager, config):
        """Create message handler."""
        return MessageHandler(
            connection_manager=connection_manager,
            config=config,
            timescale_client=None
        )
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_message_processing_performance(self, message_handler):
        """Test message processing performance."""
        # Test BootNotification processing speed
        boot_message = {
            "chargingStation": {
                "model": "Test Model",
                "vendorName": "Test Vendor"
            },
            "reason": "PowerUp"
        }
        
        # Measure processing time for multiple messages
        times = []
        for i in range(100):
            start_time = time.time()
            await message_handler.handle_message(
                station_id=f"TEST_STATION_{i}",
                message_type_id=2,
                unique_id=f"unique_{i}",
                action="BootNotification",
                payload=boot_message
            )
            end_time = time.time()
            times.append(end_time - start_time)
        
        # Calculate performance metrics
        avg_time = statistics.mean(times)
        max_time = max(times)
        min_time = min(times)
        p95_time = statistics.quantiles(times, n=20)[18]  # 95th percentile
        
        # Performance assertions
        assert avg_time < 0.1, f"Average processing time too high: {avg_time:.4f}s"
        assert max_time < 0.5, f"Maximum processing time too high: {max_time:.4f}s"
        assert p95_time < 0.2, f"95th percentile too high: {p95_time:.4f}s"
        
        print(f"BootNotification Performance:")
        print(f"  Average: {avg_time:.4f}s")
        print(f"  Min: {min_time:.4f}s")
        print(f"  Max: {max_time:.4f}s")
        print(f"  95th percentile: {p95_time:.4f}s")
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_concurrent_message_processing(self, message_handler):
        """Test concurrent message processing performance."""
        # Create multiple concurrent message processing tasks
        async def process_message(station_id):
            boot_message = {
                "chargingStation": {
                    "model": f"Model_{station_id}",
                    "vendorName": "Test Vendor"
                },
                "reason": "PowerUp"
            }
            
            start_time = time.time()
            await message_handler.handle_message(
                station_id=station_id,
                message_type_id=2,
                unique_id=f"unique_{station_id}",
                action="BootNotification",
                payload=boot_message
            )
            end_time = time.time()
            return end_time - start_time
        
        # Process 50 messages concurrently
        tasks = [
            process_message(f"CONCURRENT_STATION_{i}")
            for i in range(50)
        ]
        
        start_time = time.time()
        times = await asyncio.gather(*tasks)
        total_time = time.time() - start_time
        
        # Calculate metrics
        avg_processing_time = statistics.mean(times)
        total_throughput = len(tasks) / total_time
        
        # Performance assertions
        assert avg_processing_time < 0.1, f"Average concurrent processing time too high: {avg_processing_time:.4f}s"
        assert total_throughput > 10, f"Throughput too low: {total_throughput:.2f} messages/second"
        
        print(f"Concurrent Processing Performance:")
        print(f"  Total time: {total_time:.4f}s")
        print(f"  Average processing time: {avg_processing_time:.4f}s")
        print(f"  Throughput: {total_throughput:.2f} messages/second")
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_connection_manager_performance(self, connection_manager):
        """Test connection manager performance."""
        # Test connection registration performance
        registration_times = []
        for i in range(100):
            connection_id = f"perf_conn_{i}"
            station_id = f"PERF_STATION_{i}"
            
            start_time = time.time()
            await connection_manager.register_connection(
                station_id=station_id,
                connection_id=connection_id,
                client_ip="127.0.0.1",
                websocket=Mock()
            )
            end_time = time.time()
            registration_times.append(end_time - start_time)
        
        avg_registration_time = statistics.mean(registration_times)
        max_registration_time = max(registration_times)
        
        # Test connection lookup performance
        lookup_times = []
        for i in range(100):
            connection_id = f"perf_conn_{i}"
            
            start_time = time.time()
            connection = connection_manager.get_connection(connection_id)
            end_time = time.time()
            lookup_times.append(end_time - start_time)
        
        avg_lookup_time = statistics.mean(lookup_times)
        max_lookup_time = max(lookup_times)
        
        # Performance assertions
        assert avg_registration_time < 0.01, f"Average registration time too high: {avg_registration_time:.4f}s"
        assert max_registration_time < 0.05, f"Maximum registration time too high: {max_registration_time:.4f}s"
        assert avg_lookup_time < 0.001, f"Average lookup time too high: {avg_lookup_time:.4f}s"
        assert max_lookup_time < 0.01, f"Maximum lookup time too high: {max_lookup_time:.4f}s"
        
        print(f"Connection Manager Performance:")
        print(f"  Registration - Average: {avg_registration_time:.4f}s, Max: {max_registration_time:.4f}s")
        print(f"  Lookup - Average: {avg_lookup_time:.4f}s, Max: {max_lookup_time:.4f}s")
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_memory_usage_under_load(self, message_handler):
        """Test memory usage under sustained load."""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        # Process many messages to test memory usage
        for i in range(1000):
            boot_message = {
                "chargingStation": {
                    "model": f"MemoryTestModel_{i}",
                    "vendorName": "Memory Test Vendor"
                },
                "reason": "PowerUp"
            }
            
            await message_handler.handle_message(
                station_id=f"MEMORY_STATION_{i}",
                message_type_id=2,
                unique_id=f"memory_unique_{i}",
                action="BootNotification",
                payload=boot_message
            )
            
            # Check memory every 100 messages
            if i % 100 == 0:
                current_memory = process.memory_info().rss / 1024 / 1024  # MB
                memory_increase = current_memory - initial_memory
                
                # Memory should not increase excessively
                assert memory_increase < 100, f"Memory usage increased too much: {memory_increase:.2f}MB"
        
        final_memory = process.memory_info().rss / 1024 / 1024  # MB
        total_memory_increase = final_memory - initial_memory
        
        print(f"Memory Usage Test:")
        print(f"  Initial memory: {initial_memory:.2f}MB")
        print(f"  Final memory: {final_memory:.2f}MB")
        print(f"  Total increase: {total_memory_increase:.2f}MB")
        
        # Final memory assertion
        assert total_memory_increase < 50, f"Total memory increase too high: {total_memory_increase:.2f}MB"


class TestStressTests:
    """Stress tests to find system limits."""
    
    @pytest_asyncio.fixture
    async def config(self):
        """Create test configuration."""
        return Config(
            timescale={
                "service_url": "postgresql://test:test@localhost:5432/test",
                "host": "localhost",
                "user": "test",
                "password": "test",
                "database": "test",
                "port": 5432
            },
            supabase={
                "url": "https://test.supabase.co",
                "anon_key": "test_anon_key",
                "service_key": "test_service_key",
                "db_host": "localhost",
                "db_port": 5432,
                "db_name": "postgres",
                "db_user": "test",
                "db_password": "test"
            }
        )
    
    @pytest_asyncio.fixture
    async def connection_manager(self, config):
        """Create connection manager."""
        return ConnectionManager(config)
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_max_concurrent_connections(self, connection_manager):
        """Test maximum concurrent connections."""
        max_connections = 0
        successful_connections = 0
        
        try:
            # Try to create as many connections as possible
            for i in range(10000):  # Try up to 10,000 connections
                connection_id = f"stress_conn_{i}"
                station_id = f"STRESS_STATION_{i}"
                
                try:
                    await connection_manager.register_connection(
                        station_id=station_id,
                        connection_id=connection_id,
                        client_ip="127.0.0.1",
                        websocket=Mock()
                    )
                    successful_connections += 1
                    max_connections = max(max_connections, successful_connections)
                except Exception as e:
                    print(f"Failed to create connection {i}: {e}")
                    break
                    
        except Exception as e:
            print(f"Stress test failed: {e}")
        
        print(f"Maximum concurrent connections: {max_connections}")
        
        # Should be able to handle at least 1000 connections
        assert max_connections >= 1000, f"Maximum connections too low: {max_connections}"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_message_processing_under_stress(self, connection_manager, config):
        """Test message processing under stress conditions."""
        message_handler = MessageHandler(
            connection_manager=connection_manager,
            config=config,
            timescale_client=None
        )
        
        # Create many connections first
        for i in range(100):
            connection_id = f"stress_msg_conn_{i}"
            station_id = f"STRESS_MSG_STATION_{i}"
            await connection_manager.register_connection(
                station_id=station_id,
                connection_id=connection_id,
                client_ip="127.0.0.1",
                websocket=Mock()
            )
        
        # Process many messages rapidly
        start_time = time.time()
        tasks = []
        
        for i in range(500):  # 500 concurrent messages
            boot_message = {
                "chargingStation": {
                    "model": f"StressModel_{i}",
                    "vendorName": "Stress Vendor"
                },
                "reason": "PowerUp"
            }
            
            task = message_handler.handle_message(
                station_id=f"STRESS_MSG_STATION_{i % 100}",
                message_type_id=2,
                unique_id=f"stress_unique_{i}",
                action="BootNotification",
                payload=boot_message
            )
            tasks.append(task)
        
        # Wait for all messages to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.time()
        
        # Count successful and failed messages
        successful = sum(1 for r in results if not isinstance(r, Exception))
        failed = sum(1 for r in results if isinstance(r, Exception))
        total_time = end_time - start_time
        throughput = len(tasks) / total_time
        
        print(f"Stress Test Results:")
        print(f"  Total messages: {len(tasks)}")
        print(f"  Successful: {successful}")
        print(f"  Failed: {failed}")
        print(f"  Total time: {total_time:.4f}s")
        print(f"  Throughput: {throughput:.2f} messages/second")
        
        # Should handle stress reasonably well
        success_rate = successful / len(tasks)
        assert success_rate > 0.95, f"Success rate too low: {success_rate:.2%}"
        assert throughput > 50, f"Throughput too low: {throughput:.2f} messages/second"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_error_recovery_performance(self, connection_manager, config):
        """Test error recovery performance."""
        message_handler = MessageHandler(
            connection_manager=connection_manager,
            config=config,
            timescale_client=None
        )
        
        # Test error handling performance
        error_times = []
        for i in range(100):
            # Send malformed message
            malformed_payload = {"invalid": "data", "missing": "required_fields"}
            
            start_time = time.time()
            try:
                await message_handler.handle_message(
                    station_id=f"ERROR_STATION_{i}",
                    message_type_id=2,
                    unique_id=f"error_unique_{i}",
                    action="BootNotification",
                    payload=malformed_payload
                )
            except Exception:
                pass  # Expected to fail
            end_time = time.time()
            error_times.append(end_time - start_time)
        
        avg_error_time = statistics.mean(error_times)
        max_error_time = max(error_times)
        
        print(f"Error Recovery Performance:")
        print(f"  Average error handling time: {avg_error_time:.4f}s")
        print(f"  Maximum error handling time: {max_error_time:.4f}s")
        
        # Error handling should be fast
        assert avg_error_time < 0.05, f"Average error handling time too high: {avg_error_time:.4f}s"
        assert max_error_time < 0.2, f"Maximum error handling time too high: {max_error_time:.4f}s"
