"""Load tests for concurrent connections and performance."""

import pytest
import asyncio
import time
import statistics
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, AsyncMock

# Import OCPP handler
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
from websocket_handler.timescale_client import TimescaleClient


class TestLoadPerformance:
    """Load tests for system performance."""

    @pytest.fixture
    async def timescale_client(self):
        """Create TimescaleClient for load tests."""
        client = TimescaleClient(
            host="localhost",
            port=5432,
            database="test_favonius",
            user="postgres",
            password="password"
        )
        await client.initialize()
        yield client
        await client.close()

    @pytest.mark.asyncio
    async def test_concurrent_boot_notifications(self, timescale_client):
        """Test concurrent boot notifications."""
        num_stations = 100
        stations = []
        
        # Create multiple station handlers
        for i in range(num_stations):
            station_id = f"LOAD_TEST_STATION_{i:03d}"
            handler = EnhancedOCPPChargePoint(station_id, timescale_client)
            stations.append(handler)

        # Execute boot notifications concurrently
        start_time = time.time()
        
        tasks = []
        for i, handler in enumerate(stations):
            from ocpp.v21.datatypes import ChargingStationType
            
            charging_station = ChargingStationType(
                model=f"LoadTestModel_{i}",
                vendor_name="LoadTestVendor",
                serial_number=f"SN{i:06d}",
                firmware_version="1.0.0"
            )
            
            task = handler.on_boot_notification(
                charging_station, "1.6", "2023-01-01T00:00:00Z"
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = time.time()

        # Verify all boot notifications succeeded
        assert len(results) == num_stations
        for result in results:
            assert result["status"] == "Accepted"

        # Performance metrics
        total_time = end_time - start_time
        throughput = num_stations / total_time
        
        print(f"Concurrent Boot Notifications:")
        print(f"  Stations: {num_stations}")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Throughput: {throughput:.2f} stations/second")
        print(f"  Average per station: {total_time/num_stations*1000:.2f}ms")

        # Performance assertions
        assert throughput > 10  # At least 10 stations per second
        assert total_time < 30   # Complete within 30 seconds

    @pytest.mark.asyncio
    async def test_concurrent_meter_values(self, timescale_client):
        """Test concurrent meter value processing."""
        num_stations = 50
        stations = []
        
        # Create station handlers
        for i in range(num_stations):
            station_id = f"METER_TEST_STATION_{i:03d}"
            handler = EnhancedOCPPChargePoint(station_id, timescale_client)
            stations.append(handler)

        # Execute meter values concurrently
        start_time = time.time()
        
        tasks = []
        for i, handler in enumerate(stations):
            from ocpp.v21.datatypes import MeterValueType, SampledValueType
            from ocpp.v21.enums import ReadingContextEnumType, MeasurandEnumType, UnitOfMeasureEnumType

            sampled_value = SampledValueType(
                value=str(22.5 + i),
                context=ReadingContextEnumType.sample_periodic,
                format="Raw",
                measurand=MeasurandEnumType.energy_active_import_register,
                unit_of_measure=UnitOfMeasureEnumType.kwh
            )

            meter_value = MeterValueType(
                timestamp="2023-01-01T00:00:00Z",
                sampled_value=[sampled_value]
            )

            task = handler.on_meter_values(
                evse_id=1,
                meter_value=[meter_value],
                transaction_id=f"TXN{i:06d}"
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = time.time()

        # Verify all meter values processed
        assert len(results) == num_stations
        # MeterValues has no response, so results should be None
        for result in results:
            assert result is None

        # Performance metrics
        total_time = end_time - start_time
        throughput = num_stations / total_time
        
        print(f"Concurrent Meter Values:")
        print(f"  Stations: {num_stations}")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Throughput: {throughput:.2f} meter values/second")

        # Performance assertions
        assert throughput > 20  # At least 20 meter values per second
        assert total_time < 10   # Complete within 10 seconds

    @pytest.mark.asyncio
    async def test_concurrent_transaction_events(self, timescale_client):
        """Test concurrent transaction event processing."""
        num_transactions = 30
        stations = []
        
        # Create station handlers
        for i in range(num_transactions):
            station_id = f"TXN_TEST_STATION_{i:03d}"
            handler = EnhancedOCPPChargePoint(station_id, timescale_client)
            stations.append(handler)

        # Execute transaction events concurrently
        start_time = time.time()
        
        tasks = []
        for i, handler in enumerate(stations):
            from ocpp.v21.datatypes import IdTokenType
            from ocpp.v21.enums import IdTokenEnumType, TransactionEventEnumType, TriggerReasonEnumType

            id_token = IdTokenType(id_token=f"AUTH{i:06d}", type=IdTokenEnumType.key_code)

            task = handler.on_transaction_event(
                event_type=TransactionEventEnumType.started,
                timestamp="2023-01-01T00:00:00Z",
                trigger_reason=TriggerReasonEnumType.authorized,
                seq_no=1,
                transaction_info={
                    "transactionId": f"TXN{i:06d}",
                    "chargingState": "Charging"
                },
                id_token=id_token
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = time.time()

        # Verify all transaction events processed
        assert len(results) == num_transactions
        for result in results:
            assert result["status"] == "Accepted"

        # Performance metrics
        total_time = end_time - start_time
        throughput = num_transactions / total_time
        
        print(f"Concurrent Transaction Events:")
        print(f"  Transactions: {num_transactions}")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Throughput: {throughput:.2f} events/second")

        # Performance assertions
        assert throughput > 5   # At least 5 events per second
        assert total_time < 15  # Complete within 15 seconds

    @pytest.mark.asyncio
    async def test_mixed_message_load(self, timescale_client):
        """Test mixed message types under load."""
        num_stations = 20
        stations = []
        
        # Create station handlers
        for i in range(num_stations):
            station_id = f"MIXED_TEST_STATION_{i:03d}"
            handler = EnhancedOCPPChargePoint(station_id, timescale_client)
            stations.append(handler)

        # Execute mixed messages concurrently
        start_time = time.time()
        
        tasks = []
        for i, handler in enumerate(stations):
            # Mix of different message types
            if i % 4 == 0:
                # Boot notification
                from ocpp.v21.datatypes import ChargingStationType
                charging_station = ChargingStationType(
                    model=f"MixedTestModel_{i}",
                    vendor_name="MixedTestVendor",
                    serial_number=f"SN{i:06d}",
                    firmware_version="1.0.0"
                )
                task = handler.on_boot_notification(
                    charging_station, "1.6", "2023-01-01T00:00:00Z"
                )
            elif i % 4 == 1:
                # Status notification
                from ocpp.v21.enums import ConnectorStatusEnumType
                task = handler.on_status_notification(
                    connector_id=1,
                    error_code="NoError",
                    status=ConnectorStatusEnumType.available,
                    timestamp="2023-01-01T00:00:00Z"
                )
            elif i % 4 == 2:
                # Heartbeat
                task = handler.on_heartbeat()
            else:
                # Get variables
                task = handler.on_get_variables(
                    get_variable_data=[{
                        "component": {"name": "ChargingStation"},
                        "variable": {"name": "VendorName"}
                    }]
                )
            
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = time.time()

        # Verify all messages processed
        assert len(results) == num_stations

        # Performance metrics
        total_time = end_time - start_time
        throughput = num_stations / total_time
        
        print(f"Mixed Message Load:")
        print(f"  Messages: {num_stations}")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Throughput: {throughput:.2f} messages/second")

        # Performance assertions
        assert throughput > 5   # At least 5 messages per second
        assert total_time < 20  # Complete within 20 seconds

    @pytest.mark.asyncio
    async def test_database_connection_pool_performance(self, timescale_client):
        """Test database connection pool performance."""
        num_concurrent_queries = 100
        
        # Execute concurrent database queries
        start_time = time.time()
        
        async def db_query(query_id):
            # Simulate database query
            await timescale_client.pg_pool.fetch("SELECT $1 as query_id", query_id)
            return query_id

        tasks = [db_query(i) for i in range(num_concurrent_queries)]
        results = await asyncio.gather(*tasks)
        end_time = time.time()

        # Verify all queries completed
        assert len(results) == num_concurrent_queries
        assert set(results) == set(range(num_concurrent_queries))

        # Performance metrics
        total_time = end_time - start_time
        throughput = num_concurrent_queries / total_time
        
        print(f"Database Connection Pool:")
        print(f"  Concurrent Queries: {num_concurrent_queries}")
        print(f"  Total Time: {total_time:.2f}s")
        print(f"  Throughput: {throughput:.2f} queries/second")

        # Performance assertions
        assert throughput > 50  # At least 50 queries per second
        assert total_time < 10   # Complete within 10 seconds

    @pytest.mark.asyncio
    async def test_memory_usage_under_load(self, timescale_client):
        """Test memory usage under sustained load."""
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        num_iterations = 10
        num_stations_per_iteration = 10
        
        for iteration in range(num_iterations):
            stations = []
            
            # Create station handlers
            for i in range(num_stations_per_iteration):
                station_id = f"MEMORY_TEST_STATION_{iteration}_{i:03d}"
                handler = EnhancedOCPPChargePoint(station_id, timescale_client)
                stations.append(handler)

            # Execute operations
            tasks = []
            for handler in stations:
                task = handler.on_heartbeat()
                tasks.append(task)

            await asyncio.gather(*tasks)
            
            # Check memory usage
            current_memory = process.memory_info().rss / 1024 / 1024  # MB
            memory_increase = current_memory - initial_memory
            
            print(f"Iteration {iteration + 1}: Memory usage: {current_memory:.2f}MB (+{memory_increase:.2f}MB)")

        final_memory = process.memory_info().rss / 1024 / 1024  # MB
        total_memory_increase = final_memory - initial_memory
        
        print(f"Memory Usage Test:")
        print(f"  Initial Memory: {initial_memory:.2f}MB")
        print(f"  Final Memory: {final_memory:.2f}MB")
        print(f"  Total Increase: {total_memory_increase:.2f}MB")
        print(f"  Average per iteration: {total_memory_increase/num_iterations:.2f}MB")

        # Memory usage assertions
        assert total_memory_increase < 500  # Less than 500MB increase
        assert total_memory_increase / num_iterations < 50  # Less than 50MB per iteration


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
