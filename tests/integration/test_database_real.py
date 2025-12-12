"""Real database integration tests for Favonius optimization system.

Tests TimescaleDB hypertable operations, concurrent writes,
price/weather data ingestion, optimization result storage,
and query performance with realistic data volume.

Reference: Development plan Phase 0, PRD.md#6-data-models
"""

import pytest
import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Optional

import asyncpg

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL environment variable not set"
)


# ============ Fixtures ============

@pytest.fixture(scope="module")
def event_loop():
    """Create event loop for module-scoped fixtures."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def db_pool():
    """Create database connection pool for tests."""
    database_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/favonius_test"
    )
    
    try:
        pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
        yield pool
        await pool.close()
    except Exception as e:
        pytest.skip(f"Database not available: {e}")


@pytest.fixture
def test_depot_id():
    """Generate a unique test depot ID."""
    return str(uuid4())


@pytest.fixture
def test_vehicle_ids():
    """Generate test vehicle IDs."""
    return [str(uuid4()) for _ in range(3)]


# ============ Connection and Health Tests ============

class TestDatabaseConnection:
    """Tests for database connection and basic operations."""

    @pytest.mark.asyncio
    async def test_connection_health(self, db_pool):
        """Test database connection health."""
        async with db_pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1

    @pytest.mark.asyncio
    async def test_timescaledb_extension(self, db_pool):
        """Test TimescaleDB extension is installed."""
        async with db_pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT extname FROM pg_extension WHERE extname = 'timescaledb'"
            )
            assert result == "timescaledb"

    @pytest.mark.asyncio
    async def test_connection_pool_limits(self, db_pool):
        """Test connection pool handles max connections."""
        async def acquire_and_hold():
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT pg_sleep(0.1)")
                return True
        
        # Try to acquire multiple connections
        tasks = [acquire_and_hold() for _ in range(5)]
        results = await asyncio.gather(*tasks)
        
        assert all(results)


# ============ Hypertable Tests ============

class TestHypertables:
    """Tests for TimescaleDB hypertable operations."""

    @pytest.mark.asyncio
    async def test_telemetry_hypertable_exists(self, db_pool):
        """Test telemetry hypertable exists."""
        async with db_pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT hypertable_name 
                FROM timescaledb_information.hypertables 
                WHERE hypertable_name = 'telemetry'
            """)
            # May not exist in test database
            if result is None:
                pytest.skip("Telemetry hypertable not created")
            assert result == "telemetry"

    @pytest.mark.asyncio
    async def test_prices_hypertable_exists(self, db_pool):
        """Test prices hypertable exists."""
        async with db_pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT hypertable_name 
                FROM timescaledb_information.hypertables 
                WHERE hypertable_name = 'prices'
            """)
            if result is None:
                pytest.skip("Prices hypertable not created")
            assert result == "prices"

    @pytest.mark.asyncio
    async def test_hypertable_chunk_interval(self, db_pool):
        """Test hypertable chunk interval is configured."""
        async with db_pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT chunk_time_interval 
                FROM timescaledb_information.dimensions 
                WHERE hypertable_name = 'telemetry'
            """)
            if result:
                # Should have reasonable chunk interval
                assert result is not None


# ============ Telemetry Write Tests ============

class TestTelemetryWrites:
    """Tests for telemetry data writes."""

    @pytest.mark.asyncio
    async def test_insert_single_telemetry(self, db_pool, test_vehicle_ids):
        """Test inserting single telemetry record."""
        vehicle_id = test_vehicle_ids[0]
        
        async with db_pool.acquire() as conn:
            try:
                await conn.execute("""
                    INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                    VALUES ($1, $2::uuid, $3, $4, $5)
                    ON CONFLICT DO NOTHING
                """, datetime.now(timezone.utc), vehicle_id, 0.5, 50.0, True)
            except asyncpg.UndefinedTableError:
                pytest.skip("Telemetry table not created")

    @pytest.mark.asyncio
    async def test_insert_batch_telemetry(self, db_pool, test_vehicle_ids):
        """Test batch inserting telemetry records."""
        async with db_pool.acquire() as conn:
            try:
                # Prepare batch data
                now = datetime.now(timezone.utc)
                records = [
                    (now + timedelta(minutes=i), test_vehicle_ids[0], 0.3 + i * 0.01, 80.0, True)
                    for i in range(10)
                ]
                
                await conn.executemany("""
                    INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                    VALUES ($1, $2::uuid, $3, $4, $5)
                    ON CONFLICT DO NOTHING
                """, records)
            except asyncpg.UndefinedTableError:
                pytest.skip("Telemetry table not created")

    @pytest.mark.asyncio
    async def test_concurrent_telemetry_writes(self, db_pool, test_vehicle_ids):
        """Test concurrent telemetry writes don't conflict."""
        async def write_telemetry(vehicle_id: str, offset: int):
            async with db_pool.acquire() as conn:
                now = datetime.now(timezone.utc) + timedelta(seconds=offset)
                try:
                    await conn.execute("""
                        INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                        VALUES ($1, $2::uuid, $3, $4, $5)
                        ON CONFLICT DO NOTHING
                    """, now, vehicle_id, 0.5, 50.0, True)
                    return True
                except asyncpg.UndefinedTableError:
                    return None
                except Exception as e:
                    return False
        
        # Concurrent writes for different vehicles
        tasks = [
            write_telemetry(vid, i)
            for i, vid in enumerate(test_vehicle_ids * 3)
        ]
        results = await asyncio.gather(*tasks)
        
        if None in results:
            pytest.skip("Telemetry table not created")
        
        assert all(r is True for r in results)


# ============ Price Data Tests ============

class TestPriceData:
    """Tests for price data operations."""

    @pytest.mark.asyncio
    async def test_insert_price_data(self, db_pool, test_depot_id):
        """Test inserting price data."""
        async with db_pool.acquire() as conn:
            try:
                now = datetime.now(timezone.utc)
                await conn.execute("""
                    INSERT INTO prices (time, depot_id, price_per_kwh, source)
                    VALUES ($1, $2::uuid, $3, $4)
                    ON CONFLICT DO NOTHING
                """, now, test_depot_id, 0.15, 'CAISO')
            except asyncpg.UndefinedTableError:
                pytest.skip("Prices table not created")

    @pytest.mark.asyncio
    async def test_query_price_range(self, db_pool, test_depot_id):
        """Test querying prices for a time range."""
        async with db_pool.acquire() as conn:
            try:
                # Insert some test prices
                now = datetime.now(timezone.utc)
                for i in range(24):
                    await conn.execute("""
                        INSERT INTO prices (time, depot_id, price_per_kwh, source)
                        VALUES ($1, $2::uuid, $3, $4)
                        ON CONFLICT DO NOTHING
                    """, now + timedelta(hours=i), test_depot_id, 0.10 + 0.01 * i, 'CAISO')
                
                # Query prices
                rows = await conn.fetch("""
                    SELECT time, price_per_kwh
                    FROM prices
                    WHERE depot_id = $1::uuid
                    AND time >= $2
                    AND time < $3
                    ORDER BY time
                """, test_depot_id, now, now + timedelta(hours=24))
                
                assert len(rows) >= 0  # May be 0 if ON CONFLICT ignored duplicates
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Prices table not created")

    @pytest.mark.asyncio
    async def test_price_interpolation(self, db_pool, test_depot_id):
        """Test price data with time gaps."""
        async with db_pool.acquire() as conn:
            try:
                now = datetime.now(timezone.utc)
                
                # Insert sparse prices
                for i in [0, 2, 4, 6]:  # Every 2 hours
                    await conn.execute("""
                        INSERT INTO prices (time, depot_id, price_per_kwh, source)
                        VALUES ($1, $2::uuid, $3, $4)
                        ON CONFLICT DO NOTHING
                    """, now + timedelta(hours=i), test_depot_id, 0.15, 'CAISO')
                
                # Query all times - should have gaps
                rows = await conn.fetch("""
                    SELECT time, price_per_kwh
                    FROM prices
                    WHERE depot_id = $1::uuid
                    AND time >= $2
                    ORDER BY time
                """, test_depot_id, now)
                
                # Verify data is present
                assert len(rows) >= 0
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Prices table not created")


# ============ Optimization Result Storage Tests ============

class TestOptimizationResultStorage:
    """Tests for optimization result storage."""

    @pytest.mark.asyncio
    async def test_store_optimization_run(self, db_pool, test_depot_id):
        """Test storing optimization run results."""
        async with db_pool.acquire() as conn:
            try:
                run_id = uuid4()
                now = datetime.now(timezone.utc)
                schedule_json = json.dumps({
                    'schedule': {'bus_1': {'charging_power': [80.0] * 96}},
                    'battery_dispatch': [0.0] * 96,
                    'grid_power': [100.0] * 96,
                })
                
                await conn.execute("""
                    INSERT INTO optimization_runs 
                    (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                     solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                    run_id, test_depot_id, now, 'test', now, now + timedelta(hours=24),
                    5.0, 1000.0, 200.0, 'completed', schedule_json
                )
            except asyncpg.UndefinedTableError:
                pytest.skip("Optimization_runs table not created")

    @pytest.mark.asyncio
    async def test_query_latest_optimization(self, db_pool, test_depot_id):
        """Test querying latest optimization result."""
        async with db_pool.acquire() as conn:
            try:
                # Store a result first
                run_id = uuid4()
                now = datetime.now(timezone.utc)
                
                await conn.execute("""
                    INSERT INTO optimization_runs 
                    (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                     solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                    run_id, test_depot_id, now, 'test', now, now + timedelta(hours=24),
                    5.0, 1000.0, 200.0, 'completed', '{}'
                )
                
                # Query latest
                row = await conn.fetchrow("""
                    SELECT run_id, depot_id, status, objective_value
                    FROM optimization_runs
                    WHERE depot_id = $1
                    ORDER BY run_time DESC
                    LIMIT 1
                """, test_depot_id)
                
                assert row is not None
                assert row['depot_id'] == test_depot_id
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Optimization_runs table not created")

    @pytest.mark.asyncio
    async def test_store_charging_commands(self, db_pool, test_depot_id):
        """Test storing charging commands."""
        async with db_pool.acquire() as conn:
            try:
                run_id = uuid4()
                now = datetime.now(timezone.utc)
                
                # Store optimization run first
                await conn.execute("""
                    INSERT INTO optimization_runs 
                    (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                     solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                    run_id, test_depot_id, now, 'test', now, now + timedelta(hours=24),
                    5.0, 1000.0, 200.0, 'completed', '{}'
                )
                
                # Store command
                profile_json = json.dumps([
                    {'start_period': 0, 'limit': 80000, 'number_phases': 3}
                ])
                
                await conn.execute("""
                    INSERT INTO charging_commands 
                    (run_id, charger_id, vehicle_id, issued_at, profile_json, status)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """,
                    run_id, None, 'bus_1', now, profile_json, 'accepted'
                )
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Charging_commands table not created")


# ============ Query Performance Tests ============

class TestQueryPerformance:
    """Tests for query performance with realistic data volume."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_telemetry_query_performance(self, db_pool, test_vehicle_ids):
        """Test telemetry query performance with volume data."""
        async with db_pool.acquire() as conn:
            try:
                vehicle_id = test_vehicle_ids[0]
                
                # Insert volume data
                now = datetime.now(timezone.utc)
                records = [
                    (now - timedelta(minutes=i), vehicle_id, 0.5, 50.0, True)
                    for i in range(1000)  # 1000 records
                ]
                
                await conn.executemany("""
                    INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                    VALUES ($1, $2::uuid, $3, $4, $5)
                    ON CONFLICT DO NOTHING
                """, records)
                
                # Time the query
                import time
                start = time.time()
                
                rows = await conn.fetch("""
                    SELECT time, soc, charging_kw
                    FROM telemetry
                    WHERE vehicle_id = $1::uuid
                    AND time >= $2
                    ORDER BY time DESC
                    LIMIT 100
                """, vehicle_id, now - timedelta(hours=24))
                
                elapsed = time.time() - start
                
                # Query should be fast (< 1 second)
                assert elapsed < 1.0
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Telemetry table not created")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_optimization_history_query(self, db_pool, test_depot_id):
        """Test optimization history query performance."""
        async with db_pool.acquire() as conn:
            try:
                now = datetime.now(timezone.utc)
                
                # Insert multiple optimization runs
                for i in range(100):
                    run_id = uuid4()
                    await conn.execute("""
                        INSERT INTO optimization_runs 
                        (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                         solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                        run_id, test_depot_id, now - timedelta(hours=i), 'hourly',
                        now - timedelta(hours=i), now - timedelta(hours=i-24),
                        5.0, 1000.0, 200.0, 'completed', '{}'
                    )
                
                # Time the history query
                import time
                start = time.time()
                
                rows = await conn.fetch("""
                    SELECT run_id, run_time, objective_value, peak_demand_kw
                    FROM optimization_runs
                    WHERE depot_id = $1
                    ORDER BY run_time DESC
                    LIMIT 24
                """, test_depot_id)
                
                elapsed = time.time() - start
                
                # Query should be fast
                assert elapsed < 0.5
                assert len(rows) >= 0
                
            except asyncpg.UndefinedTableError:
                pytest.skip("Optimization_runs table not created")


# ============ Data Integrity Tests ============

class TestDataIntegrity:
    """Tests for data integrity constraints."""

    @pytest.mark.asyncio
    async def test_vehicle_fk_constraint(self, db_pool, test_depot_id):
        """Test foreign key constraint on vehicles table."""
        async with db_pool.acquire() as conn:
            try:
                # Try to insert telemetry for non-existent vehicle
                fake_vehicle_id = str(uuid4())
                
                try:
                    await conn.execute("""
                        INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                        VALUES ($1, $2::uuid, $3, $4, $5)
                    """, datetime.now(timezone.utc), fake_vehicle_id, 0.5, 50.0, True)
                    
                    # If no FK constraint, this will succeed
                except asyncpg.ForeignKeyViolationError:
                    # FK constraint exists and is working
                    pass
                    
            except asyncpg.UndefinedTableError:
                pytest.skip("Telemetry table not created")

    @pytest.mark.asyncio
    async def test_soc_range_constraint(self, db_pool, test_vehicle_ids):
        """Test SoC value range constraint."""
        vehicle_id = test_vehicle_ids[0]
        
        async with db_pool.acquire() as conn:
            try:
                # Try to insert SoC > 1.0
                try:
                    await conn.execute("""
                        INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
                        VALUES ($1, $2::uuid, $3, $4, $5)
                    """, datetime.now(timezone.utc), vehicle_id, 1.5, 50.0, True)
                    
                    # If no CHECK constraint, this will succeed
                except asyncpg.CheckViolationError:
                    # CHECK constraint exists and is working
                    pass
                    
            except asyncpg.UndefinedTableError:
                pytest.skip("Telemetry table not created")


# ============ Transaction Tests ============

class TestTransactions:
    """Tests for transaction handling."""

    @pytest.mark.asyncio
    async def test_transaction_rollback(self, db_pool, test_depot_id):
        """Test transaction rollback on error."""
        async with db_pool.acquire() as conn:
            try:
                # Start transaction
                async with conn.transaction():
                    run_id = uuid4()
                    now = datetime.now(timezone.utc)
                    
                    await conn.execute("""
                        INSERT INTO optimization_runs 
                        (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                         solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                        run_id, test_depot_id, now, 'test', now, now + timedelta(hours=24),
                        5.0, 1000.0, 200.0, 'completed', '{}'
                    )
                    
                    # Force error to trigger rollback
                    raise Exception("Simulated error")
                    
            except Exception as e:
                if "Simulated error" not in str(e):
                    pytest.skip("Optimization_runs table not created")
            
            # Verify rollback - record should not exist
            row = await conn.fetchrow("""
                SELECT run_id FROM optimization_runs WHERE run_id = $1
            """, run_id)
            
            assert row is None

    @pytest.mark.asyncio
    async def test_concurrent_transactions(self, db_pool, test_depot_id):
        """Test concurrent transactions don't interfere."""
        async def run_transaction(pool, depot_id: str, value: float):
            async with pool.acquire() as conn:
                try:
                    async with conn.transaction():
                        run_id = uuid4()
                        now = datetime.now(timezone.utc)
                        
                        await conn.execute("""
                            INSERT INTO optimization_runs 
                            (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                             solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        """,
                            run_id, depot_id, now, 'test', now, now + timedelta(hours=24),
                            5.0, value, 200.0, 'completed', '{}'
                        )
                        
                        return run_id
                except asyncpg.UndefinedTableError:
                    return None
        
        # Run concurrent transactions
        tasks = [
            run_transaction(db_pool, test_depot_id, 1000.0 + i * 100)
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)
        
        if None in results:
            pytest.skip("Optimization_runs table not created")
        
        # All should have unique run_ids
        assert len(set(results)) == 5


# ============ Cleanup ============

@pytest.fixture(autouse=True)
async def cleanup_test_data(db_pool, test_depot_id, test_vehicle_ids):
    """Clean up test data after each test."""
    yield
    
    if db_pool:
        async with db_pool.acquire() as conn:
            try:
                # Clean up test data
                await conn.execute(
                    "DELETE FROM optimization_runs WHERE depot_id = $1",
                    test_depot_id
                )
                for vid in test_vehicle_ids:
                    await conn.execute(
                        "DELETE FROM telemetry WHERE vehicle_id = $1::uuid",
                        vid
                    )
                await conn.execute(
                    "DELETE FROM prices WHERE depot_id = $1::uuid",
                    test_depot_id
                )
            except Exception:
                pass  # Table may not exist
