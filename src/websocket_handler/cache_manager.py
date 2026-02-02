"""Advanced caching system for performance optimization."""

import asyncio
import json
import pickle
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from enum import Enum
import hashlib

from .monitoring import get_logger


class CacheStrategy(Enum):
    """Cache strategy types."""
    LRU = "lru"  # Least Recently Used
    TTL = "ttl"  # Time To Live
    WRITE_THROUGH = "write_through"
    WRITE_BACK = "write_back"


@dataclass
class CacheEntry:
    """Cache entry with metadata."""
    key: str
    value: Any
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    ttl: Optional[timedelta] = None
    strategy: CacheStrategy = CacheStrategy.TTL


class CacheManager:
    """Advanced caching system with multiple strategies."""

    def __init__(self, max_size: int = 10000, default_ttl: timedelta = timedelta(minutes=5)):
        """Initialize cache manager."""
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.logger = get_logger(__name__)

        # Cache storage
        self._cache: Dict[str, CacheEntry] = {}
        self._access_order: List[str] = []  # For LRU

        # Lock for thread-safe cache operations
        self._lock = asyncio.Lock()

        # Statistics
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "size": 0
        }

        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self) -> None:
        """Start the cache manager."""
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self.logger.info("Cache manager started")
    
    async def stop(self) -> None:
        """Stop the cache manager."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Cache manager stopped")
    
    async def get(self, key: str, default: Any = None) -> Any:
        """Get value from cache (thread-safe)."""
        async with self._lock:
            if key not in self._cache:
                self.stats["misses"] += 1
                return default

            entry = self._cache[key]

            # Check TTL
            if entry.ttl and datetime.now(timezone.utc) - entry.created_at > entry.ttl:
                await self._evict_unlocked(key)
                self.stats["misses"] += 1
                return default

            # Update access metadata
            entry.last_accessed = datetime.now(timezone.utc)
            entry.access_count += 1

            # Update LRU order
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)

            self.stats["hits"] += 1
            return entry.value
    
    async def set(self, key: str, value: Any, ttl: Optional[timedelta] = None,
                  strategy: CacheStrategy = CacheStrategy.TTL) -> None:
        """Set value in cache (thread-safe)."""
        async with self._lock:
            # Check if we need to evict
            if len(self._cache) >= self.max_size and key not in self._cache:
                await self._evict_lru_unlocked()

            # Create cache entry
            entry = CacheEntry(
                key=key,
                value=value,
                created_at=datetime.now(timezone.utc),
                last_accessed=datetime.now(timezone.utc),
                ttl=ttl or self.default_ttl,
                strategy=strategy
            )

            self._cache[key] = entry

            # Update LRU order
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)

            self.stats["size"] = len(self._cache)
    
    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if key in self._cache:
            await self._evict(key)
            return True
        return False
    
    async def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        self._access_order.clear()
        self.stats["size"] = 0
        self.logger.info("Cache cleared")
    
    async def get_or_set(self, key: str, factory_func, ttl: Optional[timedelta] = None) -> Any:
        """Get value or set it using factory function."""
        value = await self.get(key)
        if value is None:
            value = await factory_func()
            await self.set(key, value, ttl)
        return value
    
    async def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching pattern."""
        import fnmatch
        keys_to_delete = [key for key in self._cache.keys() if fnmatch.fnmatch(key, pattern)]
        
        for key in keys_to_delete:
            await self._evict(key)
        
        return len(keys_to_delete)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        hit_rate = self.stats["hits"] / (self.stats["hits"] + self.stats["misses"]) if (self.stats["hits"] + self.stats["misses"]) > 0 else 0
        
        return {
            **self.stats,
            "hit_rate": hit_rate,
            "max_size": self.max_size,
            "utilization": len(self._cache) / self.max_size
        }
    
    async def _evict(self, key: str) -> None:
        """Evict key from cache (acquires lock)."""
        async with self._lock:
            await self._evict_unlocked(key)

    async def _evict_unlocked(self, key: str) -> None:
        """Evict key from cache (must hold lock)."""
        if key in self._cache:
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            self.stats["evictions"] += 1
            self.stats["size"] = len(self._cache)

    async def _evict_lru(self) -> None:
        """Evict least recently used entry (acquires lock)."""
        async with self._lock:
            await self._evict_lru_unlocked()

    async def _evict_lru_unlocked(self) -> None:
        """Evict least recently used entry (must hold lock)."""
        if self._access_order:
            lru_key = self._access_order[0]
            await self._evict_unlocked(lru_key)
    
    async def _cleanup_loop(self) -> None:
        """Background cleanup loop."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Run every minute
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in cache cleanup: {e}")
    
    async def _cleanup_expired(self) -> None:
        """Clean up expired entries (thread-safe)."""
        now = datetime.now(timezone.utc)

        async with self._lock:
            expired_keys = []

            for key, entry in self._cache.items():
                if entry.ttl and now - entry.created_at > entry.ttl:
                    expired_keys.append(key)

            for key in expired_keys:
                await self._evict_unlocked(key)

        if expired_keys:
            self.logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")


class QueryCache:
    """Specialized cache for database queries."""
    
    def __init__(self, cache_manager: CacheManager):
        """Initialize query cache."""
        self.cache_manager = cache_manager
        self.logger = get_logger(__name__)
    
    def _hash_query(self, query: str, params: tuple) -> str:
        """Generate cache key for query."""
        query_hash = hashlib.md5(f"{query}:{params}".encode()).hexdigest()
        return f"query:{query_hash}"
    
    async def get_query_result(self, query: str, params: tuple, ttl: timedelta = timedelta(minutes=5)) -> Optional[List[Dict[str, Any]]]:
        """Get cached query result."""
        cache_key = self._hash_query(query, params)
        return await self.cache_manager.get(cache_key)
    
    async def set_query_result(self, query: str, params: tuple, result: List[Dict[str, Any]], 
                              ttl: timedelta = timedelta(minutes=5)) -> None:
        """Cache query result."""
        cache_key = self._hash_query(query, params)
        await self.cache_manager.set(cache_key, result, ttl)
    
    async def invalidate_table(self, table_name: str) -> None:
        """Invalidate all cached queries for a table."""
        pattern = f"query:*{table_name}*"
        count = await self.cache_manager.invalidate_pattern(pattern)
        self.logger.info(f"Invalidated {count} cached queries for table {table_name}")


class SessionCache:
    """Specialized cache for WebSocket sessions."""
    
    def __init__(self, cache_manager: CacheManager):
        """Initialize session cache."""
        self.cache_manager = cache_manager
        self.logger = get_logger(__name__)
    
    async def get_session_data(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get cached session data."""
        cache_key = f"session:{station_id}"
        return await self.cache_manager.get(cache_key)
    
    async def set_session_data(self, station_id: str, data: Dict[str, Any], 
                              ttl: timedelta = timedelta(hours=1)) -> None:
        """Cache session data."""
        cache_key = f"session:{station_id}"
        await self.cache_manager.set(cache_key, data, ttl)
    
    async def update_session_data(self, station_id: str, updates: Dict[str, Any]) -> None:
        """Update cached session data."""
        existing_data = await self.get_session_data(station_id) or {}
        existing_data.update(updates)
        await self.set_session_data(station_id, existing_data)
    
    async def invalidate_session(self, station_id: str) -> None:
        """Invalidate session cache."""
        cache_key = f"session:{station_id}"
        await self.cache_manager.delete(cache_key)


class DeviceModelCache:
    """Specialized cache for device model variables."""
    
    def __init__(self, cache_manager: CacheManager):
        """Initialize device model cache."""
        self.cache_manager = cache_manager
        self.logger = get_logger(__name__)
    
    def _get_variable_key(self, station_id: str, component_name: str, component_instance: str,
                         variable_name: str, variable_instance: str) -> str:
        """Generate cache key for device variable."""
        return f"device_var:{station_id}:{component_name}:{component_instance}:{variable_name}:{variable_instance}"
    
    async def get_variable(self, station_id: str, component_name: str, component_instance: str,
                          variable_name: str, variable_instance: str) -> Optional[Dict[str, Any]]:
        """Get cached device variable."""
        cache_key = self._get_variable_key(station_id, component_name, component_instance, variable_name, variable_instance)
        return await self.cache_manager.get(cache_key)
    
    async def set_variable(self, station_id: str, component_name: str, component_instance: str,
                          variable_name: str, variable_instance: str, value: Dict[str, Any],
                          ttl: timedelta = timedelta(minutes=10)) -> None:
        """Cache device variable."""
        cache_key = self._get_variable_key(station_id, component_name, component_instance, variable_name, variable_instance)
        await self.cache_manager.set(cache_key, value, ttl)
    
    async def invalidate_station(self, station_id: str) -> None:
        """Invalidate all cached variables for a station."""
        pattern = f"device_var:{station_id}:*"
        count = await self.cache_manager.invalidate_pattern(pattern)
        self.logger.info(f"Invalidated {count} cached device variables for station {station_id}")


class PriceCache:
    """Specialized cache for electricity prices."""
    
    def __init__(self, cache_manager: CacheManager):
        """Initialize price cache."""
        self.cache_manager = cache_manager
        self.logger = get_logger(__name__)
    
    async def get_prices(self, station_id: str, node_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get cached electricity prices."""
        cache_key = f"prices:{station_id}:{node_id or 'default'}"
        return await self.cache_manager.get(cache_key)
    
    async def set_prices(self, station_id: str, prices: Dict[str, Any], 
                        node_id: Optional[str] = None, ttl: timedelta = timedelta(minutes=5)) -> None:
        """Cache electricity prices."""
        cache_key = f"prices:{station_id}:{node_id or 'default'}"
        await self.cache_manager.set(cache_key, prices, ttl)
    
    async def invalidate_prices(self, station_id: Optional[str] = None) -> None:
        """Invalidate price cache."""
        if station_id:
            pattern = f"prices:{station_id}:*"
        else:
            pattern = "prices:*"
        
        count = await self.cache_manager.invalidate_pattern(pattern)
        self.logger.info(f"Invalidated {count} cached price entries")


# Global cache manager instance
_cache_manager: Optional[CacheManager] = None
_query_cache: Optional[QueryCache] = None
_session_cache: Optional[SessionCache] = None
_device_model_cache: Optional[DeviceModelCache] = None
_price_cache: Optional[PriceCache] = None


async def get_cache_manager() -> CacheManager:
    """Get global cache manager instance."""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = CacheManager()
        await _cache_manager.start()
    return _cache_manager


async def get_query_cache() -> QueryCache:
    """Get global query cache instance."""
    global _query_cache
    if _query_cache is None:
        cache_manager = await get_cache_manager()
        _query_cache = QueryCache(cache_manager)
    return _query_cache


async def get_session_cache() -> SessionCache:
    """Get global session cache instance."""
    global _session_cache
    if _session_cache is None:
        cache_manager = await get_cache_manager()
        _session_cache = SessionCache(cache_manager)
    return _session_cache


async def get_device_model_cache() -> DeviceModelCache:
    """Get global device model cache instance."""
    global _device_model_cache
    if _device_model_cache is None:
        cache_manager = await get_cache_manager()
        _device_model_cache = DeviceModelCache(cache_manager)
    return _device_model_cache


async def get_price_cache() -> PriceCache:
    """Get global price cache instance."""
    global _price_cache
    if _price_cache is None:
        cache_manager = await get_cache_manager()
        _price_cache = PriceCache(cache_manager)
    return _price_cache


async def shutdown_caches() -> None:
    """Shutdown all cache instances."""
    global _cache_manager, _query_cache, _session_cache, _device_model_cache, _price_cache
    
    if _cache_manager:
        await _cache_manager.stop()
        _cache_manager = None
    
    _query_cache = None
    _session_cache = None
    _device_model_cache = None
    _price_cache = None
