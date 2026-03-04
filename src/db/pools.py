"""Database connection pool container for dual-database architecture.

Favonius uses two separate PostgreSQL instances:
- static: Supabase — reference/config data (depots, vehicles, chargers, schedules)
- ts:     TimescaleDB — time-series and operational data (telemetry, prices,
          optimization_runs, trigger_log, etc.)

This module provides the DatabasePools dataclass that is created once at
application startup and threaded through every component that needs DB access.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass
class DatabasePools:
    """Holds both database connection pools.

    Attributes:
        static: Pool connected to Supabase (reference/config tables).
        ts: Pool connected to TimescaleDB (time-series and operational tables).
    """

    static: asyncpg.Pool
    ts: asyncpg.Pool
