"""Enhanced connection pooling and performance optimizations."""

import asyncio
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import asyncpg
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from .config import SupabaseConfig, TimescaleConfig
from .monitoring import get_logger


class PoolStrategy(Enum):
    """Connection pool strategies."""

    STATIC = "static"  # Fixed size pool
    DYNAMIC = "dynamic"  # Variable size pool
    HYBRID = "hybrid"  # Combination of both


@dataclass
class PoolMetrics:
    """Connection pool metrics."""

    total_connections: int
    active_connections: int
    idle_connections: int
    waiting_requests: int
    connection_time_ms: float
    query_time_ms: float
    pool_hit_rate: float


class EnhancedConnectionPool:
    """Enhanced connection pool with monitoring and optimization."""

    def __init__(
        self,
        config: Union[TimescaleConfig, SupabaseConfig],
        pool_strategy: PoolStrategy = PoolStrategy.HYBRID,
    ):
        """Initialize enhanced connection pool."""
        self.config = config
        self.pool_strategy = pool_strategy
        self.logger = get_logger(__name__)

        # Connection pools
        self.asyncpg_pool: Optional[asyncpg.Pool] = None
        self.sqlalchemy_engine: Optional[Engine] = None

        # Pool configuration
        self.min_connections = getattr(config, "pool_size", 5)
        self.max_connections = getattr(config, "max_connections", 100)
        self.connection_timeout = getattr(config, "connection_timeout", 30)
        self.statement_timeout = getattr(config, "statement_timeout", 30)

        # Performance tracking
        self.metrics = {
            "total_queries": 0,
            "successful_queries": 0,
            "failed_queries": 0,
            "total_query_time": 0.0,
            "total_connection_time": 0.0,
            "connection_attempts": 0,
            "successful_connections": 0,
            "pool_hits": 0,
            "pool_misses": 0,
        }

        # Connection health tracking
        self.connection_health: Dict[str, Dict[str, Any]] = {}

        # Background monitoring task
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False

    async def initialize(self) -> None:
        """Initialize connection pools."""
        try:
            await self._create_asyncpg_pool()
            await self._create_sqlalchemy_engine()

            self._running = True
            self._monitoring_task = asyncio.create_task(self._monitor_pools())

            self.logger.info(
                f"Enhanced connection pool initialized with {self.min_connections}-{self.max_connections} connections"
            )

        except Exception as e:
            self.logger.error(f"Failed to initialize connection pool: {e}")
            raise

    async def _create_asyncpg_pool(self) -> None:
        """Create asyncpg connection pool with optimizations."""
        start_time = time.time()

        try:
            # Determine optimal pool size based on strategy
            if self.pool_strategy == PoolStrategy.STATIC:
                pass
            elif self.pool_strategy == PoolStrategy.DYNAMIC:
                pass
            else:  # HYBRID
                min(self.min_connections * 2, self.max_connections)

            sslmode = getattr(self.config, "sslmode", "require")
            ssl_context: Optional[ssl.SSLContext | bool] = None
            if sslmode in ("disable", "false", "0"):
                ssl_context = False
            elif sslmode in ("verify-ca", "verify-full"):
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = sslmode == "verify-full"
                ssl_context.verify_mode = ssl.CERT_REQUIRED
            elif sslmode in ("require", "prefer"):
                # PostgreSQL "require" = encrypt but do NOT verify server certificate.
                # create_default_context() loads system CAs; we disable verification
                # so self-signed certs (e.g. Timescale Cloud) are accepted.
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            else:
                ssl_context = True

            self.asyncpg_pool = await asyncpg.create_pool(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=ssl_context,
                min_size=self.min_connections,
                max_size=self.max_connections,
                command_timeout=self.statement_timeout,
                server_settings={
                    "statement_timeout": f"{self.statement_timeout}s",
                    "idle_in_transaction_session_timeout": f"{self.connection_timeout}s",
                    "tcp_keepalives_idle": "600",
                    "tcp_keepalives_interval": "30",
                    "tcp_keepalives_count": "3",
                    # Note: shared_preload_libraries, max_connections, shared_buffers
                    # are server-level (postmaster) settings and cannot be set per-session.
                    # They are omitted here to avoid errors on managed services like
                    # Timescale Cloud.
                    "work_mem": "4MB",
                    "maintenance_work_mem": "64MB",
                    "random_page_cost": "1.1",
                    "effective_cache_size": "1GB",
                    "effective_io_concurrency": "200",
                },
            )

            connection_time = (time.time() - start_time) * 1000
            self.metrics["total_connection_time"] += connection_time
            self.metrics["successful_connections"] += 1

            self.logger.info(f"AsyncPG pool created in {connection_time:.2f}ms")

        except Exception as e:
            self.metrics["connection_attempts"] += 1
            self.logger.error(f"Failed to create AsyncPG pool: {e}")
            raise

    async def _create_sqlalchemy_engine(self) -> None:
        """Create SQLAlchemy engine with optimizations."""
        try:
            service_url = getattr(self.config, "service_url", None)
            if not service_url:
                # Construct service URL
                service_url = f"postgresql://{self.config.user}:{self.config.password}@{self.config.host}:{self.config.port}/{self.config.database}"
            elif service_url.startswith("postgres://"):
                # SQLAlchemy expects the canonical dialect name "postgresql".
                service_url = service_url.replace("postgres://", "postgresql://", 1)

            self.sqlalchemy_engine = create_engine(
                service_url,
                poolclass=QueuePool,
                pool_size=self.min_connections,
                max_overflow=self.max_connections - self.min_connections,
                pool_timeout=self.connection_timeout,
                pool_recycle=600,  # 10 minutes - faster recovery after DB restarts
                pool_pre_ping=True,
                echo=False,
                connect_args={"options": "-c timezone=utc -c statement_timeout=30000"},
            )

            self.logger.info("SQLAlchemy engine created with optimizations")

        except Exception as e:
            self.logger.warning(
                f"Failed to create SQLAlchemy engine: {e}. Continuing with asyncpg only."
            )
            self.sqlalchemy_engine = None

    async def execute_query(self, query: str, *args, **kwargs) -> List[Dict[str, Any]]:
        """Execute query with performance monitoring."""
        start_time = time.time()
        self.metrics["total_queries"] += 1

        try:
            # Add timeout on connection acquire to prevent indefinite hangs
            async with asyncio.timeout(self.connection_timeout):
                async with self.asyncpg_pool.acquire() as conn:
                    # Track connection health
                    conn_id = id(conn)
                    self.connection_health[conn_id] = {
                        "acquired_at": datetime.now(timezone.utc),
                        "query_count": self.connection_health.get(conn_id, {}).get("query_count", 0)
                        + 1,
                    }

                    # Execute query
                    if args:
                        rows = await conn.fetch(query, *args)
                    else:
                        rows = await conn.fetch(query)

                    query_time = (time.time() - start_time) * 1000
                    self.metrics["total_query_time"] += query_time
                    self.metrics["successful_queries"] += 1

                    # Log slow queries
                    if query_time > 1000:  # > 1 second
                        self.logger.warning(
                            f"Slow query detected: {query_time:.2f}ms - {query[:100]}..."
                        )

                    return [dict(row) for row in rows]

        except asyncio.TimeoutError:
            query_time = (time.time() - start_time) * 1000
            self.metrics["failed_queries"] += 1
            self.logger.error(
                f"Connection acquire timeout after {query_time:.2f}ms for query: {query[:100]}..."
            )
            raise
        except Exception as e:
            query_time = (time.time() - start_time) * 1000
            self.metrics["failed_queries"] += 1
            self.logger.error(f"Query failed after {query_time:.2f}ms: {e}")
            raise

    async def execute_transaction(self, queries: List[tuple]) -> List[Any]:
        """Execute multiple queries in a transaction."""
        start_time = time.time()

        try:
            # Add timeout on connection acquire
            async with asyncio.timeout(self.connection_timeout):
                async with self.asyncpg_pool.acquire() as conn:
                    async with conn.transaction():
                        results = []
                        for query, args in queries:
                            if args:
                                result = await conn.fetch(query, *args)
                            else:
                                result = await conn.fetch(query)
                            results.append([dict(row) for row in result])

                        transaction_time = (time.time() - start_time) * 1000
                        self.logger.info(f"Transaction completed in {transaction_time:.2f}ms")

                        return results

        except asyncio.TimeoutError:
            transaction_time = (time.time() - start_time) * 1000
            self.logger.error(
                f"Connection acquire timeout for transaction after {transaction_time:.2f}ms"
            )
            raise
        except Exception as e:
            transaction_time = (time.time() - start_time) * 1000
            self.logger.error(f"Transaction failed after {transaction_time:.2f}ms: {e}")
            raise

    async def batch_insert(
        self, table: str, data: List[Dict[str, Any]], batch_size: int = 1000
    ) -> None:
        """Perform batch insert with optimal performance."""
        if not data:
            return

        start_time = time.time()
        total_rows = len(data)

        try:
            # Add timeout on connection acquire
            async with asyncio.timeout(self.connection_timeout):
                async with self.asyncpg_pool.acquire() as conn:
                    # Get column names from first row
                    columns = list(data[0].keys())
                    columns_str = ", ".join(columns)
                    placeholders = ", ".join([f"${i+1}" for i in range(len(columns))])

                    query = f"INSERT INTO {table} ({columns_str}) VALUES ({placeholders})"

                    # Process in batches
                    for i in range(0, total_rows, batch_size):
                        batch = data[i : i + batch_size]

                        # Prepare batch data
                        batch_values = []
                        for row in batch:
                            batch_values.extend([row[col] for col in columns])

                        # Execute batch insert
                        await conn.executemany(
                            query, [tuple(row[col] for col in columns) for row in batch]
                        )

                        self.logger.debug(
                            f"Inserted batch {i//batch_size + 1}/{(total_rows + batch_size - 1)//batch_size}"
                        )

                    batch_time = (time.time() - start_time) * 1000
                    self.logger.info(
                        f"Batch insert completed: {total_rows} rows in {batch_time:.2f}ms"
                    )

        except asyncio.TimeoutError:
            batch_time = (time.time() - start_time) * 1000
            self.logger.error(
                f"Connection acquire timeout for batch insert after {batch_time:.2f}ms"
            )
            raise
        except Exception as e:
            batch_time = (time.time() - start_time) * 1000
            self.logger.error(f"Batch insert failed after {batch_time:.2f}ms: {e}")
            raise

    def get_pool_metrics(self) -> PoolMetrics:
        """Get current pool metrics."""
        if not self.asyncpg_pool:
            return PoolMetrics(0, 0, 0, 0, 0.0, 0.0, 0.0)

        pool_size = self.asyncpg_pool.get_size()
        pool_idle = self.asyncpg_pool.get_idle_size()

        avg_query_time = (
            self.metrics["total_query_time"] / self.metrics["total_queries"]
            if self.metrics["total_queries"] > 0
            else 0.0
        )

        avg_connection_time = (
            self.metrics["total_connection_time"] / self.metrics["successful_connections"]
            if self.metrics["successful_connections"] > 0
            else 0.0
        )

        hit_rate = (
            self.metrics["pool_hits"] / (self.metrics["pool_hits"] + self.metrics["pool_misses"])
            if (self.metrics["pool_hits"] + self.metrics["pool_misses"]) > 0
            else 0.0
        )

        return PoolMetrics(
            total_connections=pool_size,
            active_connections=pool_size - pool_idle,
            idle_connections=pool_idle,
            waiting_requests=0,  # asyncpg doesn't expose this
            connection_time_ms=avg_connection_time,
            query_time_ms=avg_query_time,
            pool_hit_rate=hit_rate,
        )

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check on connection pool."""
        try:
            start_time = time.time()

            # Add timeout on connection acquire for health checks
            async with asyncio.timeout(10):  # 10s timeout for health checks
                async with self.asyncpg_pool.acquire() as conn:
                    # Simple query to test connection
                    await conn.fetchval("SELECT 1")

                    response_time = (time.time() - start_time) * 1000

                    return {
                        "status": "healthy",
                        "response_time_ms": response_time,
                        "pool_size": self.asyncpg_pool.get_size(),
                        "idle_connections": self.asyncpg_pool.get_idle_size(),
                        "timestamp": datetime.now(timezone.utc),
                    }

        except asyncio.TimeoutError:
            return {
                "status": "unhealthy",
                "error": "Connection acquire timeout",
                "timestamp": datetime.now(timezone.utc),
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e), "timestamp": datetime.now(timezone.utc)}

    async def optimize_pool(self) -> None:
        """Optimize pool configuration based on usage patterns."""
        metrics = self.get_pool_metrics()

        # Adjust pool size based on utilization
        if metrics.pool_hit_rate < 0.8 and metrics.idle_connections > self.min_connections:
            # Too many idle connections, reduce pool size
            new_size = max(self.min_connections, int(metrics.total_connections * 0.9))
            self.logger.info(f"Optimizing pool: reducing size to {new_size}")

        elif (
            metrics.pool_hit_rate > 0.95
            and metrics.active_connections > metrics.total_connections * 0.8
        ):
            # High utilization, consider increasing pool size
            new_size = min(self.max_connections, int(metrics.total_connections * 1.1))
            self.logger.info(f"Optimizing pool: increasing size to {new_size}")

    async def _monitor_pools(self) -> None:
        """Background monitoring task."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes

                # Clean up old connection health data
                now = datetime.now(timezone.utc)
                expired_connections = [
                    conn_id
                    for conn_id, health in self.connection_health.items()
                    if now - health["acquired_at"] > timedelta(hours=1)
                ]

                for conn_id in expired_connections:
                    del self.connection_health[conn_id]

                # Log pool metrics
                metrics = self.get_pool_metrics()
                self.logger.info(
                    f"Pool metrics: {metrics.total_connections} total, "
                    f"{metrics.active_connections} active, "
                    f"{metrics.idle_connections} idle, "
                    f"{metrics.query_time_ms:.2f}ms avg query time"
                )

                # Optimize pool if needed
                await self.optimize_pool()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in pool monitoring: {e}")

    async def close(self) -> None:
        """Close connection pools."""
        self._running = False

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        if self.asyncpg_pool:
            await self.asyncpg_pool.close()

        if self.sqlalchemy_engine:
            self.sqlalchemy_engine.dispose()

        self.logger.info("Enhanced connection pool closed")


class ConnectionPoolManager:
    """Manages multiple connection pools."""

    def __init__(self):
        """Initialize connection pool manager."""
        self.logger = get_logger(__name__)
        self.pools: Dict[str, EnhancedConnectionPool] = {}

    async def create_pool(
        self,
        name: str,
        config: Union[TimescaleConfig, SupabaseConfig],
        strategy: PoolStrategy = PoolStrategy.HYBRID,
    ) -> EnhancedConnectionPool:
        """Create a new connection pool."""
        if name in self.pools:
            raise ValueError(f"Pool '{name}' already exists")

        pool = EnhancedConnectionPool(config, strategy)
        await pool.initialize()

        self.pools[name] = pool
        self.logger.info(f"Created connection pool '{name}'")

        return pool

    async def get_pool(self, name: str) -> Optional[EnhancedConnectionPool]:
        """Get connection pool by name."""
        return self.pools.get(name)

    async def health_check_all(self) -> Dict[str, Dict[str, Any]]:
        """Perform health check on all pools."""
        results = {}

        for name, pool in self.pools.items():
            try:
                results[name] = await pool.health_check()
            except Exception as e:
                results[name] = {
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc),
                }

        return results

    async def close_all(self) -> None:
        """Close all connection pools."""
        for name, pool in self.pools.items():
            try:
                await pool.close()
                self.logger.info(f"Closed connection pool '{name}'")
            except Exception as e:
                self.logger.error(f"Error closing pool '{name}': {e}")

        self.pools.clear()


# Global connection pool manager
_pool_manager: Optional[ConnectionPoolManager] = None


async def get_pool_manager() -> ConnectionPoolManager:
    """Get global connection pool manager."""
    global _pool_manager
    if _pool_manager is None:
        _pool_manager = ConnectionPoolManager()
    return _pool_manager


async def shutdown_pools() -> None:
    """Shutdown all connection pools."""
    global _pool_manager
    if _pool_manager:
        await _pool_manager.close_all()
        _pool_manager = None
