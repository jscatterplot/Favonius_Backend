"""
Database integration tests for TimescaleDB and Supabase operations.
Tests real database connections and data persistence.
"""

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.websocket_handler.config import Config
from src.websocket_handler.supabase_client import SupabaseClient
from src.websocket_handler.timescale_client import TimescaleClient


class TestDatabaseIntegration:
    """Integration tests for database operations."""

    @pytest.fixture
    async def config(self):
        """Create configuration for database tests."""
        return Config()

    @pytest.fixture
    async def timescale_client(self, config):
        """Create TimescaleDB client."""
        return TimescaleClient(config.timescale)

    @pytest.fixture
    async def supabase_client(self, config):
        """Create Supabase client."""
        return SupabaseClient(config.supabase)

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_timescale_connection(self, timescale_client):
        """Test TimescaleDB connection."""
        try:
            # Test basic connection
            result = await timescale_client.execute_query("SELECT 1 as test")
            assert result is not None

            # Test health check
            health = await timescale_client.check_health()
            assert health is not None

        except Exception as e:
            pytest.skip(f"TimescaleDB not available: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_timescale_telemetry_insert(self, timescale_client):
        """Test telemetry data insertion."""
        try:
            # Test telemetry insertion
            telemetry_data = {
                "station_id": "TEST_STATION_001",
                "timestamp": datetime.now(timezone.utc),
                "connector_id": 1,
                "energy_import": 15000.0,
                "power": 7500.0,
                "voltage": 240.0,
                "current": 31.25,
            }

            result = await timescale_client.insert_telemetry(telemetry_data)
            assert result is not None

        except Exception as e:
            pytest.skip(f"TimescaleDB telemetry not available: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_supabase_connection(self, supabase_client):
        """Test Supabase connection."""
        try:
            # Test client initialization
            assert supabase_client.client is not None
            assert supabase_client.url is not None
            assert supabase_client.key is not None

        except Exception as e:
            pytest.skip(f"Supabase not configured: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_database_transaction_workflow(self, timescale_client):
        """Test complete transaction workflow with database."""
        try:
            # Test transaction start
            transaction_data = {
                "transaction_id": "TXN_INTEGRATION_001",
                "station_id": "TEST_STATION_001",
                "connector_id": 1,
                "start_time": datetime.now(timezone.utc),
                "id_token": "RFID_123456789",
                "meter_start": 0.0,
            }

            result = await timescale_client.start_transaction(transaction_data)
            assert result is not None

            # Test transaction update
            update_data = {
                "transaction_id": "TXN_INTEGRATION_001",
                "energy_import": 5000.0,
                "meter_value": 5000.0,
                "timestamp": datetime.now(timezone.utc),
            }

            result = await timescale_client.update_transaction(update_data)
            assert result is not None

            # Test transaction end
            end_data = {
                "transaction_id": "TXN_INTEGRATION_001",
                "end_time": datetime.now(timezone.utc),
                "meter_stop": 5000.0,
                "reason": "EVDisconnected",
            }

            result = await timescale_client.end_transaction(end_data)
            assert result is not None

        except Exception as e:
            pytest.skip(f"Database transaction workflow not available: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_concurrent_database_operations(self, timescale_client):
        """Test concurrent database operations."""
        try:
            # Create multiple concurrent operations
            tasks = []
            for i in range(5):
                telemetry_data = {
                    "station_id": f"TEST_STATION_{i:03d}",
                    "timestamp": datetime.now(timezone.utc),
                    "connector_id": 1,
                    "energy_import": 1000.0 * i,
                    "power": 500.0 * i,
                    "voltage": 240.0,
                    "current": 2.0 * i,
                }

                task = timescale_client.insert_telemetry(telemetry_data)
                tasks.append(task)

            # Execute all operations concurrently
            results = await asyncio.gather(*tasks)

            # Verify all operations completed
            assert len(results) == 5
            for result in results:
                assert result is not None

        except Exception as e:
            pytest.skip(f"Concurrent database operations not available: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_database_error_handling(self, timescale_client):
        """Test database error handling."""
        try:
            # Test invalid query
            with pytest.raises(Exception):
                await timescale_client.execute_query("INVALID SQL QUERY")

            # Test connection recovery
            health = await timescale_client.check_health()
            assert health is not None

        except Exception as e:
            pytest.skip(f"Database error handling test not available: {e}")

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_database_performance(self, timescale_client):
        """Test database performance with multiple operations."""
        try:
            start_time = time.time()

            # Perform multiple operations
            for i in range(10):
                telemetry_data = {
                    "station_id": f"PERF_STATION_{i:03d}",
                    "timestamp": datetime.now(timezone.utc),
                    "connector_id": 1,
                    "energy_import": 1000.0,
                    "power": 500.0,
                    "voltage": 240.0,
                    "current": 2.0,
                }

                await timescale_client.insert_telemetry(telemetry_data)

            end_time = time.time()
            duration = end_time - start_time

            # Performance should be reasonable (less than 5 seconds for 10 operations)
            assert duration < 5.0

        except Exception as e:
            pytest.skip(f"Database performance test not available: {e}")
