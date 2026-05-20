"""TimescaleDB client for time-series data operations."""

import asyncio
import time
import base64
import hashlib
import hmac
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg
import pandas as pd
from cryptography import x509
from sqlalchemy import text

from .config import TimescaleConfig
from .meter_value_utils import (
    DEFAULT_SYNTHESIZED_DELTA_CAP_WH,
    compute_energy_kwh,
    normalize_energy_to_wh,
    synthesize_energy_kwh_from_meter_stop,
)
from .monitoring import get_logger


def _synthesized_delta_cap_wh() -> int:
    """Resolve the synthesized-delta cap (Wh) from env, with a safe default.

    Env var ``OCPP_SYNTHESIZED_DELTA_CAP_KWH`` is read in kWh (operator-
    friendly) and converted to Wh. Non-numeric / non-positive values fall
    back to ``DEFAULT_SYNTHESIZED_DELTA_CAP_WH`` (50 kWh) so a typo in the
    deployment env never widens the cap silently.
    """
    raw = os.getenv("OCPP_SYNTHESIZED_DELTA_CAP_KWH")
    if not raw:
        return DEFAULT_SYNTHESIZED_DELTA_CAP_WH
    try:
        cap_kwh = float(raw)
    except ValueError:
        return DEFAULT_SYNTHESIZED_DELTA_CAP_WH
    if cap_kwh <= 0:
        return DEFAULT_SYNTHESIZED_DELTA_CAP_WH
    return int(cap_kwh * 1000)


class TimescaleClient:
    """TimescaleDB client for time-series operations."""

    def __init__(self, config: TimescaleConfig):
        """Initialize TimescaleDB client."""
        self.config = config
        self.logger = get_logger(__name__)

        # Enhanced connection pool
        from .connection_pool import EnhancedConnectionPool

        self.connection_pool: Optional[EnhancedConnectionPool] = None

        # Legacy connection pools (for backward compatibility)
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.sqlalchemy_engine = None

        # Connection state
        self.connected = False

        # Static-table source. RFID/vehicle/charging_stations rows live in
        # Supabase; ``lookup_id_tag`` routes there when wired by main.py.
        # Left None during construction so unit tests that build the client
        # in isolation continue to use ``pg_pool``.
        self._supabase_client: Any = None

        # Strong references to in-flight fire-and-forget cost tasks.
        # ``asyncio.create_task`` only registers a weak reference with
        # the event loop, so a task whose only reference is the local
        # variable in the scheduler is eligible for garbage collection
        # before it completes (officially documented in asyncio).
        # Holding it in a set + ``add_done_callback`` to discard on
        # completion is the canonical pattern.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def set_supabase_client(self, supabase_client: Any) -> None:
        """Attach a SupabaseClient for static-identity lookups.

        ``lookup_id_tag`` queries ``vehicles`` / ``rfid_cards`` /
        ``charging_stations`` — those tables live in Supabase. Without
        this wiring the lookup hits TimescaleDB, where migration 029
        dropped the static shadows, producing schema errors that the
        OCPP 1.6 charger interprets as a hard authorization rejection.
        """
        self._supabase_client = supabase_client

    def _static_pool(self) -> Any:
        """Return the connection pool that owns the static identity tables.

        Prefer the wired Supabase pool; fall back to ``pg_pool`` so legacy
        deployments and existing unit tests (which mock ``pg_pool`` only)
        continue to work without modification.
        """
        if self._supabase_client is not None:
            pool = getattr(self._supabase_client, "db_pool", None)
            if pool is not None:
                return pool
        return self.pg_pool

    async def connect(self) -> None:
        """Establish connections to TimescaleDB with retry logic."""
        await self._connect_with_retry()

    async def _connect_with_retry(self, max_retries: int = 3, base_delay: float = 1.0) -> None:
        """Connect with exponential backoff retry and jitter."""
        for attempt in range(max_retries):
            try:
                await self._establish_connections()
                await self._verify_timescale_extension()
                await self._test_connections()

                self.connected = True
                self.logger.info("TimescaleDB client connected successfully")
                return

            except Exception as e:
                if attempt == max_retries - 1:
                    self.logger.error(
                        f"Failed to connect to TimescaleDB after {max_retries} attempts: {e}"
                    )
                    raise

                # Calculate delay with exponential backoff
                delay = base_delay * (2**attempt)
                # Add jitter: 50-100% of base delay to prevent connection storms
                delay *= 0.5 + random.random() * 0.5

                self.logger.warning(
                    f"TimescaleDB connection attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

    async def _establish_connections(self) -> None:
        """Establish the actual database connections."""
        # Create enhanced connection pool
        from .connection_pool import EnhancedConnectionPool, PoolStrategy

        self.connection_pool = EnhancedConnectionPool(self.config, PoolStrategy.HYBRID)
        await self.connection_pool.initialize()

        # Set legacy pools for backward compatibility
        self.pg_pool = self.connection_pool.asyncpg_pool
        self.sqlalchemy_engine = self.connection_pool.sqlalchemy_engine

    async def disconnect(self) -> None:
        """Close all connections."""
        if self.connection_pool:
            await self.connection_pool.close()

        # Legacy cleanup
        if self.pg_pool:
            await self.pg_pool.close()

        if self.sqlalchemy_engine:
            self.sqlalchemy_engine.dispose()

        self.connected = False
        self.logger.info("TimescaleDB client disconnected")

    async def _verify_timescale_extension(self) -> None:
        """Verify TimescaleDB extension is installed."""
        try:
            async with self.pg_pool.acquire() as conn:
                # Check if TimescaleDB extension exists
                result = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
                )
                if not result:
                    self.logger.warning("TimescaleDB extension not found, attempting to create it")
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                    self.logger.info("TimescaleDB extension created successfully")
                else:
                    self.logger.info("TimescaleDB extension verified")
        except Exception as e:
            self.logger.error(f"Failed to verify TimescaleDB extension: {e}")
            raise

    async def health_check(self) -> bool:
        """Check connection health."""
        try:
            if not self.connected or not self.pg_pool:
                return False

            async with self.pg_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as e:
            self.logger.warning(f"TimescaleDB health check failed: {e}")
            return False

    async def reconnect(self) -> None:
        """Reconnect to TimescaleDB."""
        self.logger.info("Attempting to reconnect to TimescaleDB")
        await self.disconnect()
        await self.connect()

    async def _test_connections(self) -> None:
        """Test all connections."""
        # Test asyncpg connection
        async with self.pg_pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            if result != 1:
                raise Exception("AsyncPG connection test failed")

        # Test SQLAlchemy connection (if available)
        if self.sqlalchemy_engine:
            with self.sqlalchemy_engine.connect() as conn:
                result = conn.execute(text("SELECT 1")).scalar()
                if result != 1:
                    raise Exception("SQLAlchemy connection test failed")

    # Telemetry Data Operations
    async def insert_telemetry_batch(self, telemetry_data: List[Dict[str, Any]]) -> None:
        """Insert batch of telemetry data into the unified telemetry table.

        The schema for telemetry is defined by the SQL migrations under
        ``migrations/`` and mirrored in ``src/db/models.py`` /
        ``src/db/queries.py``. This method must remain aligned with those
        definitions (columns, types, and conflict keys) and should not
        introduce additional Timescale-specific columns that are unknown to
        the main API layer.
        """
        if not telemetry_data:
            return

        try:
            async with self.pg_pool.acquire() as conn:
                static_pool = self._static_pool()
                static_conn = conn if static_pool is self.pg_pool else await static_pool.acquire()
                inserted = 0
                try:
                    for data in telemetry_data:
                        station_id = data.get("station_id")
                        connector_id = data.get("connector_id", 1)
                        session_id = data.get("session_id")
                        power_kw = data.get("power_kw")
                        soc_percent = data.get("soc_percent")
                        max_charge_kw = data.get("max_charge_power_kw")
                        transaction_id = self._coerce_transaction_id(session_id)

                        raw_sample = data.get("raw_sample")
                        if raw_sample:
                            await conn.execute(
                                """
                                INSERT INTO telemetry_samples (
                                    time,
                                    station_id,
                                    connector_id,
                                    transaction_id,
                                    measurand,
                                    phase,
                                    location,
                                    unit,
                                    context,
                                    format,
                                    value
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                                """,
                                raw_sample.get("timestamp", data["time"]),
                                station_id,
                                connector_id,
                                transaction_id,
                                raw_sample.get("measurand"),
                                raw_sample.get("phase"),
                                raw_sample.get("location"),
                                raw_sample.get("unit"),
                                raw_sample.get("context"),
                                raw_sample.get("format"),
                                raw_sample.get("value"),
                            )

                        # Live session metrics: keep the open charging_sessions row
                        # fresh so per-charger power/SoC is observable in real time
                        # without requiring vehicle attribution. When this row
                        # carries an Energy.Active.Import.Register sample we also
                        # advance last_meter_wh / energy_delivered_kwh on the
                        # session — that is the running-meter source of truth the
                        # orphan-recovery job falls back to when StopTransaction
                        # is missing.
                        meter_wh = self._extract_register_wh(raw_sample) if raw_sample else None
                        if station_id and transaction_id is not None:
                            await self._update_session_live_metrics(
                                conn,
                                station_id,
                                transaction_id,
                                power_kw,
                                soc_percent,
                                max_charge_kw,
                                meter_wh,
                            )

                        # Charger-keyed telemetry view (telemetry table). vehicle_id
                        # is optional enrichment; writes must continue even when
                        # idTag/session mapping is unavailable.
                        vehicle_id = data.get("vehicle_id")
                        if not vehicle_id:
                            vehicle_id = await self._resolve_vehicle_id_from_session(
                                conn, session_id
                            )

                        charger_id = await self._resolve_charger_id(station_id, conn=static_conn)
                        soc = (soc_percent / 100.0) if soc_percent is not None else None
                        is_plugged = (power_kw > 0.1) if power_kw is not None else None

                        await conn.execute(
                            """
                            INSERT INTO telemetry (
                                time, station_id, connector_id, transaction_id,
                                vehicle_id, charger_id, soc, charging_kw, is_plugged, max_charge_kw
                            )
                            VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7, $8, $9, $10)
                            ON CONFLICT (time, station_id, connector_id) DO UPDATE
                            SET transaction_id = COALESCE(
                                    EXCLUDED.transaction_id,
                                    telemetry.transaction_id
                                ),
                                vehicle_id = COALESCE(EXCLUDED.vehicle_id, telemetry.vehicle_id),
                                charger_id = COALESCE(EXCLUDED.charger_id, telemetry.charger_id),
                                soc = COALESCE(EXCLUDED.soc, telemetry.soc),
                                charging_kw = COALESCE(EXCLUDED.charging_kw, telemetry.charging_kw),
                                is_plugged = COALESCE(EXCLUDED.is_plugged, telemetry.is_plugged),
                                max_charge_kw = COALESCE(EXCLUDED.max_charge_kw, telemetry.max_charge_kw)
                            """,
                            data["time"],
                            station_id or "unknown",
                            int(connector_id) if connector_id is not None else 1,
                            transaction_id,
                            str(vehicle_id) if vehicle_id else None,
                            str(charger_id) if charger_id else None,
                            soc,
                            power_kw,
                            is_plugged,
                            max_charge_kw,
                        )
                        inserted += 1
                finally:
                    if static_conn is not conn:
                        await static_pool.release(static_conn)

                if inserted:
                    self.logger.debug(f"Inserted {inserted} telemetry records into telemetry")

        except Exception as e:
            self.logger.error(f"Failed to insert telemetry batch: {e}")
            raise

    async def lookup_session_connector(self, station_id: str, transaction_id: int) -> Optional[int]:
        """Resolve connector_id for an open/closed charging session transaction."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT connector_id
                  FROM charging_sessions
                 WHERE station_id = $1
                   AND transaction_id = $2
                 ORDER BY start_time DESC
                 LIMIT 1
                """,
                station_id,
                transaction_id,
            )
            if row is None:
                return None
            value = row.get("connector_id")
            return int(value) if value is not None else None

    async def is_transaction_open(self, station_id: str, transaction_id: int) -> bool:
        """Return True iff a live charging_sessions row exists with end_time IS NULL.

        Used by ``_on_transaction_start`` in the OCPP 1.6 adapter to
        self-heal stale in-memory state after orphan recovery closes a DB
        row. The DB is the source of truth; the in-memory transactions
        dict is a cache that can drift when the recovery loop closes a
        row without notifying the WS layer.
        """
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM charging_sessions
                 WHERE station_id = $1
                   AND transaction_id = $2
                   AND end_time IS NULL
                   AND source = 'live'
                 LIMIT 1
                """,
                station_id,
                transaction_id,
            )
            return row is not None

    async def _resolve_vehicle_id_from_session(
        self, conn: asyncpg.Connection, session_id: Optional[str]
    ) -> Optional[str]:
        """Resolve an attached vehicle for a charging session.

        Upstream callers pass either the ``charging_sessions.session_id``
        UUID PK or the OCPP 1.6 integer ``transaction_id`` (legacy adapter,
        ``ocpp16_adapter.py``). Detect numeric vs UUID and dispatch to the
        right column rather than letting an int hit the UUID-typed
        ``session_id`` and fail with "invalid UUID '1'".
        """
        if not session_id:
            return None
        key = str(session_id)
        try:
            uuid.UUID(key)
        except (ValueError, TypeError):
            try:
                tx_id = int(key)
            except (TypeError, ValueError):
                return None
            row = await conn.fetchrow(
                """
                SELECT vehicle_id
                  FROM charging_sessions
                 WHERE transaction_id = $1
                 ORDER BY start_time DESC
                 LIMIT 1
                """,
                tx_id,
            )
        else:
            row = await conn.fetchrow(
                "SELECT vehicle_id FROM charging_sessions WHERE session_id = $1::uuid LIMIT 1",
                key,
            )
        return row["vehicle_id"] if row and row["vehicle_id"] else None

    @staticmethod
    def _coerce_transaction_id(value: Any) -> Optional[int]:
        """Coerce a session_id-like value into an OCPP 1.6 integer transaction id.

        Returns None for UUID session ids and other non-integer inputs so the
        caller can safely pass it to columns typed BIGINT.
        """
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_register_wh(raw_sample: Dict[str, Any]) -> Optional[int]:
        """Return Wh value when the raw OCPP sample is Energy.Active.Import.Register.

        Charger-reported energy registers come through with vendor-dependent
        units (``Wh`` / ``kWh`` / ``kVAh``) and an optional ``multiplier``
        power-of-ten offset. ``normalize_energy_to_wh`` centralises the
        conversion; this method just selects which samples to forward to it.

        Returns ``None`` for non-register measurands, malformed values, or
        when the normaliser rejects the unit (defence in depth — the caller
        treats ``None`` as "no register reading in this MeterValues row").
        """
        if not raw_sample:
            return None
        if raw_sample.get("measurand") != "Energy.Active.Import.Register":
            return None
        value = raw_sample.get("value")
        if value is None:
            return None
        try:
            wh = normalize_energy_to_wh(
                float(value),
                raw_sample.get("unit"),
                raw_sample.get("multiplier"),
            )
        except (TypeError, ValueError):
            return None
        if wh < 0:
            return None
        return int(wh)

    async def _update_session_live_metrics(
        self,
        conn: asyncpg.Connection,
        station_id: str,
        transaction_id: int,
        power_kw: Optional[float],
        soc_percent: Optional[float],
        max_charge_kw: Optional[float],
        meter_wh: Optional[int] = None,
    ) -> None:
        """Refresh charging_sessions live fields for the open session.

        Writes per-MeterValues snapshots of charging power and SoC onto the
        open charging_sessions row so per-charger charging rate is observable
        without joining telemetry. ``max_charge_power_kw`` is bumped only when
        the new value is higher so the column captures the session peak.

        When ``meter_wh`` is supplied (Energy.Active.Import.Register sample)
        the row's ``last_meter_wh`` advances monotonically via ``GREATEST`` —
        guards against late/duplicate samples regressing the register.

        If ``meter_start_wh`` is NULL on the row (the "deferred" sentinel
        set when StartTransaction carried meterStart=0 — see
        ocpp16_adapter._on_transaction_start) the same write backfills it
        with the first positive sample so the rest of the energy pipeline
        (live UPDATE, close, orphan recovery) can compute a delta.
        Trade-off: we lose the small slice of energy delivered between
        StartTransaction and the first MeterValues frame (typically
        ≤ 50 Wh at AC speeds), but we recover the whole-session kWh
        attribution that would otherwise be NULL forever.

        Energy is recomputed against the *effective* start register —
        ``COALESCE(meter_start_wh, sample_when_positive)`` — so on the
        very first backfilled sample the result is 0.0 (correct: no energy
        accumulated on our deferred baseline yet); subsequent samples
        produce real deltas. The same guards as ``compute_energy_kwh``
        apply (start > 0, current >= start). NULL is preserved on any
        bracket the helper would have refused.
        """
        soc = (soc_percent / 100.0) if soc_percent is not None else None
        try:
            await conn.execute(
                """
                UPDATE charging_sessions
                   SET current_power_kw     = COALESCE($3, current_power_kw),
                       current_soc          = COALESCE($4, current_soc),
                       max_charge_power_kw  = GREATEST(max_charge_power_kw, $5),
                       meter_start_wh       = CASE
                           WHEN meter_start_wh IS NULL
                            AND $6::bigint IS NOT NULL
                            AND $6::bigint > 0
                           THEN $6::bigint
                           ELSE meter_start_wh
                       END,
                       last_meter_wh        = CASE
                           WHEN $6::bigint IS NOT NULL
                           THEN GREATEST(COALESCE(last_meter_wh, $6::bigint), $6::bigint)
                           ELSE last_meter_wh
                       END,
                       last_meter_seen_at   = CASE
                           WHEN $6::bigint IS NOT NULL THEN NOW()
                           ELSE last_meter_seen_at
                       END,
                       energy_delivered_kwh = CASE
                           WHEN $6::bigint IS NOT NULL
                            AND COALESCE(
                                meter_start_wh,
                                CASE WHEN $6::bigint > 0 THEN $6::bigint END
                            ) IS NOT NULL
                            AND COALESCE(
                                meter_start_wh,
                                CASE WHEN $6::bigint > 0 THEN $6::bigint END
                            ) > 0
                            AND GREATEST(
                                COALESCE(last_meter_wh, $6::bigint),
                                $6::bigint
                            ) >= COALESCE(
                                meter_start_wh,
                                CASE WHEN $6::bigint > 0 THEN $6::bigint END
                            )
                           THEN (
                               GREATEST(
                                   COALESCE(last_meter_wh, $6::bigint),
                                   $6::bigint
                               )
                               - COALESCE(
                                   meter_start_wh,
                                   CASE WHEN $6::bigint > 0 THEN $6::bigint END
                               )
                           ) / 1000.0
                           ELSE energy_delivered_kwh
                       END,
                       updated_at           = NOW()
                 WHERE station_id     = $1
                   AND transaction_id = $2
                   AND end_time IS NULL
                   AND source = 'live'
                """,
                station_id,
                transaction_id,
                power_kw,
                soc,
                max_charge_kw,
                meter_wh,
            )
        except Exception as exc:
            self.logger.debug(
                "Live session metric update skipped: station=%s tx=%s err=%s",
                station_id,
                transaction_id,
                exc,
            )

    async def _resolve_charger_id(
        self, station_id: Optional[str], conn: Optional[asyncpg.Connection] = None
    ) -> Optional[str]:
        """Resolve ``charging_stations.id`` for a given OCPP station_id.

        ``charging_stations`` lives in Supabase (migration 029 dropped the
        TimescaleDB shadow), so the lookup must go through ``_static_pool``;
        a direct ``pg_pool`` query trips ``UndefinedTableError`` and would
        bubble up through ``insert_telemetry_batch``, taking the whole
        telemetry batch with it. ``_static_pool`` falls back to ``pg_pool``
        for tests and legacy deployments without a Supabase wiring.

        Returns ``None`` when the station is unknown — telemetry rows are
        still inserted with a NULL ``charger_id``.
        """
        if not station_id:
            return None
        if conn is None:
            async with self._static_pool().acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id AS charger_id FROM charging_stations WHERE station_id = $1 LIMIT 1",
                    station_id,
                )
        else:
            row = await conn.fetchrow(
                "SELECT id AS charger_id FROM charging_stations WHERE station_id = $1 LIMIT 1",
                station_id,
            )
        return row["charger_id"] if row else None

    # Electricity Prices — ``electricity_prices`` hypertable (migration 034)

    async def store_electricity_prices(self, price_points: List[Dict[str, Any]]) -> None:
        """Store electricity price points into the ``electricity_prices`` hypertable.

        Migration 041 added a uniqueness constraint on
        ``(time, node_id, market_type)``. The feeder runs on a ~15-minute
        cadence and pulls the same day-ahead window every interval, so
        most rows in any given call already exist. Earlier draft used
        ``copy_records_to_table`` which has no conflict-resolution
        semantics — the next periodic run would have hit unique-key
        violations and silently skipped refreshing the table. Switched
        to ``INSERT … ON CONFLICT DO NOTHING`` so re-ingesting the
        same hour is a no-op. Volume is modest (≤ 24 hours × handful
        of zones per tick); the COPY-speed advantage was overkill for
        this rate.
        """
        if not price_points:
            return

        rows = [
            (
                point["time"],
                point["node_id"],
                point["market_type"],
                point.get("lmp_price_mwh"),
                point.get("energy_component_mwh"),
                point.get("congestion_component_mwh"),
                point.get("loss_component_mwh"),
                point.get("ghg_adder_mwh"),
                point.get("price_confidence"),
                point.get("forecast_horizon_minutes"),
            )
            for point in price_points
        ]

        try:
            async with self.pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(
                        """
                        INSERT INTO electricity_prices (
                            time,
                            node_id,
                            market_type,
                            lmp_price_mwh,
                            energy_component_mwh,
                            congestion_component_mwh,
                            loss_component_mwh,
                            ghg_adder_mwh,
                            price_confidence,
                            forecast_horizon_minutes
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (time, node_id, market_type) DO NOTHING
                        """,
                        rows,
                    )
                self.logger.debug(
                    "Stored %s electricity price records (electricity_prices)",
                    len(price_points),
                )

        except Exception as e:
            self.logger.error(f"Failed to store electricity prices: {e}")
            raise

    async def get_latest_prices(self, nodes: List[str], start: datetime) -> List[Dict[str, Any]]:
        """Fetch price data for nodes since a given time."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT time, node_id, market_type, lmp_price_mwh,
                           energy_component_mwh, congestion_component_mwh,
                           loss_component_mwh, ghg_adder_mwh, price_confidence,
                           forecast_horizon_minutes
                    FROM electricity_prices
                    WHERE node_id = ANY($1)
                      AND time >= $2
                    ORDER BY time ASC
                """
                rows = await conn.fetch(query, nodes, start)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to fetch electricity prices: {e}")
            raise

    async def get_telemetry_data(
        self, station_id: str, start_time: datetime, end_time: datetime, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Get telemetry data for a station."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT time, station_id, evse_id, connector_id, session_id,
                           power_kw, energy_kwh, voltage_v, current_a, frequency_hz,
                           soc_percent, temperature_c, grid_frequency_mhz,
                           reactive_power_kvar, power_factor
                    FROM telemetry_data
                    WHERE station_id = $1 
                        AND time >= $2 
                        AND time <= $3
                    ORDER BY time DESC
                    LIMIT $4
                """

                rows = await conn.fetch(query, station_id, start_time, end_time, limit)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to get telemetry data: {e}")
            raise

    # Charging Sessions Operations
    async def create_charging_session(self, session_data: Dict[str, Any]) -> str:
        """Create a new charging session."""
        try:
            async with self.pg_pool.acquire() as conn:
                session_id = await conn.fetchval(
                    """
                    INSERT INTO charging_sessions (
                        station_id, evse_id, connector_id, vehicle_id, id_token,
                        start_time, start_soc_percent, operation_mode,
                        fleet_operator_id, site_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING session_id
                """,
                    session_data["station_id"],
                    session_data["evse_id"],
                    session_data["connector_id"],
                    session_data.get("vehicle_id"),
                    session_data.get("id_token"),
                    session_data["start_time"],
                    session_data.get("start_soc_percent"),
                    session_data.get("operation_mode"),
                    session_data.get("fleet_operator_id"),
                    session_data.get("site_id"),
                )

                return str(session_id)

        except Exception as e:
            self.logger.error(f"Failed to create charging session: {e}")
            raise

    async def update_charging_session(self, session_id: str, updates: Dict[str, Any]) -> None:
        """Update charging session."""
        try:
            async with self.pg_pool.acquire() as conn:
                # Build dynamic update query
                set_clauses = []
                values = []
                param_count = 1

                for key, value in updates.items():
                    if key in [
                        "end_time",
                        "end_soc_percent",
                        "energy_delivered_kwh",
                        "energy_received_kwh",
                        "max_charge_power_kw",
                        "max_discharge_power_kw",
                    ]:
                        set_clauses.append(f"{key} = ${param_count}")
                        values.append(value)
                        param_count += 1

                if set_clauses:
                    set_clauses.append("updated_at = NOW()")
                    values.append(session_id)

                    query = f"""
                        UPDATE charging_sessions 
                        SET {', '.join(set_clauses)}
                        WHERE session_id = ${param_count}
                    """

                    await conn.execute(query, *values)

        except Exception as e:
            self.logger.error(f"Failed to update charging session: {e}")
            raise

    async def get_charging_sessions(
        self, fleet_operator_id: str, start_time: datetime, end_time: datetime, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get charging sessions for a fleet operator."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT session_id, station_id, evse_id, connector_id, vehicle_id,
                           start_time, end_time, start_soc_percent, end_soc_percent,
                           energy_delivered_kwh, energy_received_kwh, operation_mode,
                           fleet_operator_id, site_id, created_at, updated_at
                    FROM charging_sessions
                    WHERE fleet_operator_id = $1 
                        AND start_time >= $2 
                        AND start_time <= $3
                    ORDER BY start_time DESC
                    LIMIT $4
                """

                rows = await conn.fetch(query, fleet_operator_id, start_time, end_time, limit)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to get charging sessions: {e}")
            raise

    # Optimization Decisions Operations
    async def store_optimization_decision(self, decision_data: Dict[str, Any]) -> str:
        """Store optimization decision."""
        try:
            async with self.pg_pool.acquire() as conn:
                decision_id = await conn.fetchval(
                    """
                    INSERT INTO optimization_decisions (
                        time, optimization_window_start, optimization_window_end,
                        fleet_operator_id, site_id, algorithm_version,
                        objective_function, objective_value, computation_time_ms,
                        constraints_satisfied, decision_payload
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING decision_id
                """,
                    decision_data["time"],
                    decision_data["optimization_window_start"],
                    decision_data["optimization_window_end"],
                    decision_data.get("fleet_operator_id"),
                    decision_data.get("site_id"),
                    decision_data.get("algorithm_version"),
                    decision_data.get("objective_function"),
                    decision_data.get("objective_value"),
                    decision_data.get("computation_time_ms"),
                    decision_data.get("constraints_satisfied"),
                    json.dumps(decision_data.get("decision_payload", {})),
                )

                return str(decision_id)

        except Exception as e:
            self.logger.error(f"Failed to store optimization decision: {e}")
            raise

    async def insert_station_info(self, station_info: Dict[str, Any]) -> None:
        """Insert station information."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO station_info (
                        station_id, serial_number, model, vendor_name, 
                        firmware_version, modem, boot_reason, timestamp
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (station_id) DO UPDATE SET
                        serial_number = EXCLUDED.serial_number,
                        model = EXCLUDED.model,
                        vendor_name = EXCLUDED.vendor_name,
                        firmware_version = EXCLUDED.firmware_version,
                        modem = EXCLUDED.modem,
                        boot_reason = EXCLUDED.boot_reason,
                        timestamp = EXCLUDED.timestamp
                """,
                    station_info["station_id"],
                    station_info["serial_number"],
                    station_info["model"],
                    station_info["vendor_name"],
                    station_info.get("firmware_version"),
                    station_info.get("modem"),
                    station_info["boot_reason"],
                    station_info["timestamp"],
                )
        except Exception as e:
            self.logger.error(f"Failed to insert station info: {e}")
            raise

    async def insert_connector_status(self, status_data: Dict[str, Any]) -> None:
        """Insert connector status.

        Populates the optional ``organization_id`` and ``depot_id`` columns
        (migration 029) when present in ``status_data``; the trigger uses these
        to create ``notification_alerts`` rows without relying on shadow tables.
        """
        # Local import keeps this module importable in environments without
        # prometheus_client installed (e.g. some CI shards).
        from .monitoring import DB_WRITE_LATENCY

        start = time.perf_counter()
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO connector_status (
                        station_id, connector_id, status, error_code,
                        timestamp, organization_id, depot_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                    status_data["station_id"],
                    status_data["connector_id"],
                    status_data["status"],
                    status_data["error_code"],
                    status_data["timestamp"],
                    status_data.get("organization_id"),
                    status_data.get("depot_id"),
                )
        except Exception as e:
            self.logger.error(f"Failed to insert connector status: {e}")
            raise
        finally:
            DB_WRITE_LATENCY.labels(table="connector_status").observe(
                max(time.perf_counter() - start, 1e-6)
            )

    async def insert_transaction_event(self, transaction_data: Dict[str, Any]) -> None:
        """Insert transaction event."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO transaction_events (
                        transaction_id, event_type, timestamp, station_id,
                        evse_id, connector_id, charging_state, stopped_reason, remote_start_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                    transaction_data["transaction_id"],
                    transaction_data["event_type"],
                    transaction_data["timestamp"],
                    transaction_data["station_id"],
                    transaction_data["evse_id"],
                    transaction_data["connector_id"],
                    transaction_data.get("charging_state"),
                    transaction_data.get("stopped_reason"),
                    transaction_data.get("remote_start_id"),
                )
        except Exception as e:
            self.logger.error(f"Failed to insert transaction event: {e}")
            raise

    async def insert_data_transfer(self, transfer_data: Dict[str, Any]) -> None:
        """Insert data transfer information."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO data_transfers (
                        station_id, vendor_id, message_id, data, timestamp
                    ) VALUES ($1, $2, $3, $4, $5)
                """,
                    transfer_data["station_id"],
                    transfer_data["vendor_id"],
                    transfer_data["message_id"],
                    transfer_data.get("data"),
                    transfer_data["timestamp"],
                )
        except Exception as e:
            self.logger.error(f"Failed to insert data transfer: {e}")
            raise

    async def insert_ev_charging_needs(self, needs_data: Dict[str, Any]) -> None:
        """Insert EV charging needs."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ev_charging_needs (
                        station_id, evse_id, requested_energy_transfer, departure_time,
                        ac_charging_parameters, dc_charging_parameters, timestamp
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                    needs_data["station_id"],
                    needs_data["evse_id"],
                    needs_data.get("requested_energy_transfer"),
                    needs_data.get("departure_time"),
                    (
                        json.dumps(needs_data.get("ac_charging_parameters"))
                        if needs_data.get("ac_charging_parameters")
                        else None
                    ),
                    (
                        json.dumps(needs_data.get("dc_charging_parameters"))
                        if needs_data.get("dc_charging_parameters")
                        else None
                    ),
                    needs_data["timestamp"],
                )
        except Exception as e:
            self.logger.error(f"Failed to insert EV charging needs: {e}")
            raise

    async def insert_ev_charging_schedule(self, schedule_data: Dict[str, Any]) -> None:
        """Insert EV charging schedule."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ev_charging_schedules (
                        station_id, evse_id, time_base, charging_schedule_period,
                        duration, start_schedule, charging_rate_unit, timestamp
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                    schedule_data["station_id"],
                    schedule_data["evse_id"],
                    schedule_data["time_base"],
                    json.dumps(schedule_data.get("charging_schedule_period", [])),
                    schedule_data.get("duration"),
                    schedule_data.get("start_schedule"),
                    schedule_data.get("charging_rate_unit"),
                    schedule_data["timestamp"],
                )
        except Exception as e:
            self.logger.error(f"Failed to insert EV charging schedule: {e}")
            raise

    async def get_optimization_decisions(
        self, fleet_operator_id: str, start_time: datetime, end_time: datetime
    ) -> List[Dict[str, Any]]:
        """Get optimization decisions."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT decision_id, time, optimization_window_start, optimization_window_end,
                           algorithm_version, objective_function, objective_value,
                           computation_time_ms, constraints_satisfied, decision_payload
                    FROM optimization_decisions
                    WHERE fleet_operator_id = $1 
                        AND time >= $2 
                        AND time <= $3
                    ORDER BY time DESC
                """

                rows = await conn.fetch(query, fleet_operator_id, start_time, end_time)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to get optimization decisions: {e}")
            raise

    # Charging Schedules Operations
    async def store_charging_schedule(self, schedule_data: Dict[str, Any]) -> str:
        """Store charging schedule."""
        try:
            async with self.pg_pool.acquire() as conn:
                schedule_id = await conn.fetchval(
                    """
                    INSERT INTO charging_schedules (
                        station_id, evse_id, decision_id, profile_id,
                        start_time, end_time, schedule_periods,
                        priority, stacking_level, purpose
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING schedule_id
                """,
                    schedule_data["station_id"],
                    schedule_data["evse_id"],
                    schedule_data.get("decision_id"),
                    schedule_data["profile_id"],
                    schedule_data["start_time"],
                    schedule_data["end_time"],
                    json.dumps(schedule_data["schedule_periods"]),
                    schedule_data.get("priority", 0),
                    schedule_data.get("stacking_level", 0),
                    schedule_data.get("purpose"),
                )

                return str(schedule_id)

        except Exception as e:
            self.logger.error(f"Failed to store charging schedule: {e}")
            raise

    async def get_active_charging_sessions(
        self, start: datetime, end: datetime
    ) -> List[Dict[str, Any]]:
        """Retrieve charging sessions within a time window."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT session_id, station_id, evse_id, connector_id,
                           start_time, end_time, energy_delivered_kwh,
                           energy_received_kwh, start_soc_percent, end_soc_percent
                    FROM charging_sessions
                    WHERE (start_time <= $2 AND (end_time IS NULL OR end_time >= $1))
                """
                rows = await conn.fetch(query, start, end)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to get active charging sessions: {e}")
            raise

    async def update_schedule_execution(
        self, schedule_id: str, executed_at: datetime, execution_status: str
    ) -> None:
        """Update schedule execution status."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE charging_schedules 
                    SET executed_at = $1, execution_status = $2
                    WHERE schedule_id = $3
                """,
                    executed_at,
                    execution_status,
                    schedule_id,
                )

        except Exception as e:
            self.logger.error(f"Failed to update schedule execution: {e}")
            raise

    async def get_electricity_prices(
        self, node_id: str, start_time: datetime, end_time: datetime, market_type: str = None
    ) -> List[Dict[str, Any]]:
        """Get electricity prices."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT time, node_id, market_type, lmp_price_mwh,
                           energy_component_mwh, congestion_component_mwh,
                           loss_component_mwh, ghg_adder_mwh, price_confidence,
                           forecast_horizon_minutes
                    FROM electricity_prices
                    WHERE node_id = $1 
                        AND time >= $2 
                        AND time <= $3
                """
                params = [node_id, start_time, end_time]

                if market_type:
                    query += " AND market_type = $4"
                    params.append(market_type)

                query += " ORDER BY time DESC"

                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]

        except Exception as e:
            self.logger.error(f"Failed to get electricity prices: {e}")
            raise

    # Analytics Operations
    async def get_hourly_energy_aggregates(
        self, station_id: str, start_time: datetime, end_time: datetime
    ) -> pd.DataFrame:
        """Get hourly energy aggregates using pandas."""
        try:
            query = """
                SELECT hour, station_id, avg_power_kw, max_power_kw, min_power_kw,
                       energy_charged_kwh, energy_discharged_kwh, avg_soc, sample_count
                FROM hourly_energy_aggregates
                WHERE station_id = %s 
                    AND hour >= %s 
                    AND hour <= %s
                ORDER BY hour DESC
            """

            df = pd.read_sql(
                query, self.sqlalchemy_engine, params=[station_id, start_time, end_time]
            )

            return df

        except Exception as e:
            self.logger.error(f"Failed to get hourly energy aggregates: {e}")
            raise

    async def get_daily_fleet_metrics(
        self, fleet_operator_id: str, start_time: datetime, end_time: datetime
    ) -> pd.DataFrame:
        """Get daily fleet metrics."""
        try:
            query = """
                SELECT day, fleet_operator_id, active_stations, total_sessions,
                       total_energy_charged_kwh, total_energy_discharged_kwh,
                       avg_session_duration_hours
                FROM daily_fleet_metrics
                WHERE fleet_operator_id = %s 
                    AND day >= %s 
                    AND day <= %s
                ORDER BY day DESC
            """

            df = pd.read_sql(
                query, self.sqlalchemy_engine, params=[fleet_operator_id, start_time, end_time]
            )

            return df

        except Exception as e:
            self.logger.error(f"Failed to get daily fleet metrics: {e}")
            raise

    async def get_energy_usage_summary(
        self, fleet_operator_id: str, start_time: datetime, end_time: datetime
    ) -> Dict[str, Any]:
        """Get energy usage summary."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT 
                        COUNT(DISTINCT session_id) as total_sessions,
                        SUM(energy_delivered_kwh) as total_energy_charged,
                        SUM(energy_received_kwh) as total_energy_discharged,
                        AVG(EXTRACT(EPOCH FROM (end_time - start_time))/3600) as avg_session_duration_hours,
                        COUNT(DISTINCT station_id) as active_stations
                    FROM charging_sessions
                    WHERE fleet_operator_id = $1 
                        AND start_time >= $2 
                        AND start_time <= $3
                        AND end_time IS NOT NULL
                """

                result = await conn.fetchrow(query, fleet_operator_id, start_time, end_time)
                return dict(result) if result else {}

        except Exception as e:
            self.logger.error(f"Failed to get energy usage summary: {e}")
            raise

    # Health Check
    async def health_check(self) -> Dict[str, Any]:  # noqa: F811
        """Check TimescaleDB connection health."""
        try:
            if not self.connected:
                return {"status": "disconnected", "error": "Not connected"}

            # Test asyncpg connection
            pg_status = "healthy"
            try:
                async with self.pg_pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            except Exception as e:
                pg_status = f"unhealthy: {str(e)}"

            # Test SQLAlchemy connection
            sqlalchemy_status = "healthy"
            try:
                with self.sqlalchemy_engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            except Exception as e:
                sqlalchemy_status = f"unhealthy: {str(e)}"

            # Check TimescaleDB extension
            timescale_status = "healthy"
            try:
                async with self.pg_pool.acquire() as conn:
                    result = await conn.fetchval(
                        "SELECT extname FROM pg_extension WHERE extname = 'timescaledb'"
                    )
                    if not result:
                        timescale_status = "timescaledb extension not found"
            except Exception as e:
                timescale_status = f"unhealthy: {str(e)}"

            return {
                "status": (
                    "healthy"
                    if all(s == "healthy" for s in [pg_status, sqlalchemy_status, timescale_status])
                    else "degraded"
                ),
                "asyncpg": pg_status,
                "sqlalchemy": sqlalchemy_status,
                "timescaledb": timescale_status,
                "pool_size": self.pg_pool.get_size() if self.pg_pool else 0,
                "max_connections": self.config.max_connections,
            }

        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    # Utility Methods
    async def execute_query(self, query: str, *args) -> List[Dict[str, Any]]:
        """Execute a custom query with enhanced performance monitoring."""
        if self.connection_pool:
            return await self.connection_pool.execute_query(query, *args)
        else:
            # Fallback to legacy method
            return await self._legacy_execute_query(query, *args)

    async def _legacy_execute_query(self, query: str, *args) -> List[Dict[str, Any]]:
        """Legacy query execution method."""
        try:
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Failed to execute query: {e}")
            raise

    async def execute_command(self, command: str, *args) -> str:
        """Execute a command (INSERT, UPDATE, DELETE)."""
        try:
            async with self.pg_pool.acquire() as conn:
                result = await conn.execute(command, *args)
                return result
        except Exception as e:
            self.logger.error(f"Failed to execute command: {e}")
            raise

    # ===== NEW OCPP 2.0.1 DATABASE METHODS =====

    async def create_device_component(
        self, station_id: str, component_name: str, instance: str
    ) -> None:
        """Create device component."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_components (station_id, component_name, instance, created_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (station_id, component_name, instance) DO NOTHING
            """,
                station_id,
                component_name,
                instance,
                datetime.now(timezone.utc),
            )

    async def get_device_variable(
        self,
        station_id: str,
        component_name: str,
        component_instance: str,
        variable_name: str,
        variable_instance: str,
        attribute_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Get device variable value."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value FROM device_variables
                WHERE station_id = $1 AND component_name = $2 AND component_instance = $3
                AND variable_name = $4 AND variable_instance = $5 AND attribute_type = $6
            """,
                station_id,
                component_name,
                component_instance,
                variable_name,
                variable_instance,
                attribute_type,
            )

            return dict(row) if row else None

    async def set_device_variable(
        self,
        station_id: str,
        component_name: str,
        component_instance: str,
        variable_name: str,
        variable_instance: str,
        attribute_type: str,
        value: Any,
    ) -> None:
        """Set device variable value."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_variables (
                    station_id, component_name, component_instance, variable_name, 
                    variable_instance, attribute_type, value, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (station_id, component_name, component_instance, variable_name, variable_instance, attribute_type)
                DO UPDATE SET value = $7, updated_at = $8
            """,
                station_id,
                component_name,
                component_instance,
                variable_name,
                variable_instance,
                attribute_type,
                str(value),
                datetime.now(timezone.utc),
            )

    async def get_device_components(self, station_id: str) -> List[Dict[str, Any]]:
        """Get device components."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT component_name, instance FROM device_components
                WHERE station_id = $1 ORDER BY component_name, instance
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_device_variables(self, station_id: str) -> List[Dict[str, Any]]:
        """Get device variables."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT component_name, component_instance, variable_name, variable_instance,
                       actual_value, target_value, default_value
                FROM device_variables WHERE station_id = $1
                ORDER BY component_name, variable_name
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def store_device_report(
        self,
        station_id: str,
        request_id: int,
        generated_at: str,
        tbc: bool,
        seq_no: int,
        report_data: List[Dict[str, Any]],
    ) -> None:
        """Store device report."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_reports (
                    station_id, request_id, generated_at, tbc, seq_no, report_data, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                station_id,
                request_id,
                generated_at,
                tbc,
                seq_no,
                json.dumps(report_data),
                datetime.now(timezone.utc),
            )

    async def store_charging_profile(self, profile_data: Dict[str, Any]) -> None:
        """Store charging profile."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO charging_profiles (
                    station_id, evse_id, profile_id, stack_level, purpose, kind,
                    schedule, valid_from, valid_to, transaction_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (station_id, evse_id, profile_id)
                DO UPDATE SET stack_level = $4, purpose = $5, kind = $6,
                             schedule = $7, valid_from = $8, valid_to = $9,
                             transaction_id = $10, updated_at = $11
            """,
                profile_data["station_id"],
                profile_data["evse_id"],
                profile_data["profile_id"],
                profile_data["stack_level"],
                profile_data["purpose"],
                profile_data["kind"],
                json.dumps(profile_data["schedule"]),
                profile_data.get("valid_from"),
                profile_data.get("valid_to"),
                profile_data.get("transaction_id"),
                datetime.now(timezone.utc),
            )

    async def remove_charging_profile(self, station_id: str, evse_id: int, profile_id: int) -> None:
        """Remove charging profile."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM charging_profiles
                WHERE station_id = $1 AND evse_id = $2 AND profile_id = $3
            """,
                station_id,
                evse_id,
                profile_id,
            )

    async def get_charging_profiles(
        self,
        station_id: str,
        evse_id: int,
        profile_id: Optional[int] = None,
        purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get charging profiles."""
        async with self.pg_pool.acquire() as conn:
            query = """
                SELECT schedule FROM charging_profiles
                WHERE station_id = $1 AND evse_id = $2
            """
            params = [station_id, evse_id]

            if profile_id is not None:
                query += " AND profile_id = $3"
                params.append(profile_id)
            elif purpose is not None:
                query += " AND purpose = $3"
                params.append(purpose)
            elif stack_level is not None:
                query += " AND stack_level = $3"
                params.append(stack_level)

            query += " ORDER BY stack_level DESC, created_at DESC"

            rows = await conn.fetch(query, *params)
            return [{"schedule": json.loads(row["schedule"])} for row in rows]

    async def get_active_charging_profiles(
        self, station_id: str, evse_id: int, current_time: datetime
    ) -> List[Dict[str, Any]]:
        """Get active charging profiles."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT schedule FROM charging_profiles
                WHERE station_id = $1 AND evse_id = $2
                AND (valid_from IS NULL OR valid_from <= $3)
                AND (valid_to IS NULL OR valid_to >= $3)
                ORDER BY stack_level DESC, created_at DESC
            """,
                station_id,
                evse_id,
                current_time,
            )

            return [{"schedule": json.loads(row["schedule"])} for row in rows]

    async def store_reported_charging_profile(self, profile_data: Dict[str, Any]) -> None:
        """Store reported charging profile."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reported_charging_profiles (
                    station_id, evse_id, request_id, profile, reported_at
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                profile_data["station_id"],
                profile_data["evse_id"],
                profile_data["request_id"],
                json.dumps(profile_data["profile"]),
                profile_data["reported_at"],
            )

    async def store_transaction(self, transaction_data: Dict[str, Any]) -> None:
        """Store transaction."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO transactions (
                    station_id, transaction_id, evse_id, connector_id,
                    id_token, id_token_type, charging_state, remote_start_id, started_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (station_id, transaction_id)
                DO UPDATE SET charging_state = $7, updated_at = $9
            """,
                transaction_data["station_id"],
                transaction_data["transaction_id"],
                transaction_data["evse_id"],
                transaction_data["connector_id"],
                transaction_data["id_token"],
                transaction_data["id_token_type"],
                transaction_data["charging_state"],
                transaction_data.get("remote_start_id"),
                transaction_data["started_at"],
            )

    async def update_transaction(self, transaction_data: Dict[str, Any]) -> None:
        """Update transaction."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE transactions SET
                    charging_state = $3, stopped_reason = $4, time_spent_charging = $5,
                    ended_at = $6, updated_at = $6
                WHERE station_id = $1 AND transaction_id = $2
            """,
                transaction_data["station_id"],
                transaction_data["transaction_id"],
                transaction_data["charging_state"],
                transaction_data.get("stopped_reason"),
                transaction_data.get("time_spent_charging"),
                transaction_data["ended_at"],
            )

    async def get_transaction(
        self, station_id: str, transaction_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get transaction."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT transaction_id, charging_state, time_spent_charging, stopped_reason,
                       remote_start_id, evse_id, connector_id
                FROM transactions WHERE station_id = $1 AND transaction_id = $2
            """,
                station_id,
                transaction_id,
            )

            return dict(row) if row else None

    async def store_transaction_event(self, event_data: Dict[str, Any]) -> None:
        """Store transaction event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO transaction_events (
                    station_id, transaction_id, event_type, timestamp, charging_state,
                    time_spent_charging, stopped_reason, remote_start_id, evse_id,
                    connector_id, meter_value, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
                event_data["station_id"],
                event_data["transaction_id"],
                event_data["event_type"],
                event_data["timestamp"],
                event_data.get("charging_state"),
                event_data.get("time_spent_charging"),
                event_data.get("stopped_reason"),
                event_data.get("remote_start_id"),
                event_data.get("evse_id"),
                event_data.get("connector_id"),
                json.dumps(event_data.get("meter_value", [])),
                datetime.now(timezone.utc),
            )

    async def get_id_token_info(self, id_token: str, token_type: str) -> Optional[Dict[str, Any]]:
        """Get ID token information."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cache_timeout, charging_priority, language1, language2,
                       group_id_token, personal_message
                FROM authorization_cache
                WHERE id_token = $1 AND token_type = $2
                AND expires_at > $3
            """,
                id_token,
                token_type,
                datetime.now(timezone.utc),
            )

            return dict(row) if row else None

    async def store_tariff(self, station_id: str, tariff_data: Dict[str, Any]) -> None:
        """Store tariff."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tariffs (
                    station_id, tariff_id, currency, tariff_element,
                    start_date_time, end_date_time, min_price, max_price, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (station_id, tariff_id)
                DO UPDATE SET currency = $3, tariff_element = $4,
                             start_date_time = $5, end_date_time = $6,
                             min_price = $7, max_price = $8, updated_at = $9
            """,
                station_id,
                tariff_data["tariff_id"],
                tariff_data["currency"],
                json.dumps(tariff_data["tariff_element"]),
                tariff_data.get("start_date_time"),
                tariff_data.get("end_date_time"),
                tariff_data.get("min_price"),
                tariff_data.get("max_price"),
                datetime.now(timezone.utc),
            )

    async def get_tariff(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get tariff."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT tariff_id, currency, tariff_element, start_date_time,
                       end_date_time, min_price, max_price
                FROM tariffs WHERE station_id = $1
                ORDER BY created_at DESC LIMIT 1
            """,
                station_id,
            )

            if row:
                data = dict(row)
                data["tariff_element"] = json.loads(data["tariff_element"])
                return data

            return None

    async def get_transaction_energy(self, station_id: str, transaction_id: str) -> Dict[str, Any]:
        """Get transaction energy consumption."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT SUM(energy_kwh) as energy_kwh, SUM(power_kw) as power_kw
                FROM telemetry
                WHERE station_id = $1 AND session_id = $2
                AND time >= (
                    SELECT started_at FROM transactions 
                    WHERE station_id = $1 AND transaction_id = $2
                )
            """,
                station_id,
                transaction_id,
            )

            return dict(row) if row else {"energy_kwh": 0.0, "power_kw": 0.0}

    async def store_transaction_cost(self, cost_data: Dict[str, Any]) -> None:
        """Store transaction cost."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO transaction_costs (
                    station_id, transaction_id, total_cost, currency,
                    cost_breakdown, calculated_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (station_id, transaction_id)
                DO UPDATE SET total_cost = $3, currency = $4,
                             cost_breakdown = $5, calculated_at = $6
            """,
                cost_data["station_id"],
                cost_data["transaction_id"],
                cost_data["total_cost"],
                cost_data["currency"],
                json.dumps(cost_data["cost_breakdown"]),
                cost_data["calculated_at"],
            )

    async def get_evse_status(self, station_id: str, evse_id: int) -> Optional[Dict[str, Any]]:
        """Get EVSE status."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status FROM connector_status
                WHERE station_id = $1 AND evse_id = $2
                ORDER BY timestamp DESC LIMIT 1
            """,
                station_id,
                evse_id,
            )

            return dict(row) if row else None

    async def store_reset_request(self, reset_data: Dict[str, Any]) -> None:
        """Store reset request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reset_requests (
                    station_id, reset_type, evse_id, requested_at
                ) VALUES ($1, $2, $3, $4)
            """,
                reset_data["station_id"],
                reset_data["reset_type"],
                reset_data.get("evse_id"),
                reset_data["requested_at"],
            )

    async def store_availability_change(self, availability_data: Dict[str, Any]) -> None:
        """Store availability change."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO availability_changes (
                    station_id, evse_id, operational_status, changed_at
                ) VALUES ($1, $2, $3, $4)
            """,
                availability_data["station_id"],
                availability_data.get("evse_id"),
                availability_data["operational_status"],
                availability_data["changed_at"],
            )

    async def store_trigger_message(self, trigger_data: Dict[str, Any]) -> None:
        """Store trigger message."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trigger_messages (
                    station_id, evse_id, requested_message, triggered_at
                ) VALUES ($1, $2, $3, $4)
            """,
                trigger_data["station_id"],
                trigger_data.get("evse_id"),
                trigger_data["requested_message"],
                trigger_data["triggered_at"],
            )

    async def store_unlock_connector(self, unlock_data: Dict[str, Any]) -> None:
        """Store unlock connector request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO unlock_connector_requests (
                    station_id, evse_id, connector_id, unlocked_at
                ) VALUES ($1, $2, $3, $4)
            """,
                unlock_data["station_id"],
                unlock_data["evse_id"],
                unlock_data["connector_id"],
                unlock_data["unlocked_at"],
            )

    # ===== CERTIFICATE MANAGEMENT METHODS =====

    async def store_certificate(self, certificate_data: Dict[str, Any]) -> None:
        """Store certificate."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO certificates (
                    station_id, certificate_type, certificate_data, certificate_chain,
                    issuer_name, subject_name, serial_number, valid_from, valid_to,
                    status, installation_date, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (station_id, certificate_type, serial_number)
                DO UPDATE SET certificate_data = $3, certificate_chain = $4,
                             issuer_name = $5, subject_name = $6,
                             valid_from = $8, valid_to = $9, status = $10,
                             installation_date = $11, updated_at = $12
            """,
                certificate_data["station_id"],
                certificate_data["certificate_type"],
                certificate_data["certificate_data"],
                certificate_data["certificate_chain"],
                certificate_data["issuer_name"],
                certificate_data["subject_name"],
                certificate_data["serial_number"],
                certificate_data["valid_from"],
                certificate_data["valid_to"],
                certificate_data["status"],
                certificate_data["installation_date"],
                datetime.now(timezone.utc),
            )

    async def get_certificate(
        self, station_id: str, certificate_type: str
    ) -> Optional[Dict[str, Any]]:
        """Get certificate."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT certificate_data, certificate_chain, issuer_name, subject_name,
                       serial_number, valid_from, valid_to, status, installation_date
                FROM certificates WHERE station_id = $1 AND certificate_type = $2
                ORDER BY installation_date DESC LIMIT 1
            """,
                station_id,
                certificate_type,
            )

            return dict(row) if row else None

    async def count_certificates(self, station_id: str, certificate_type: str) -> int:
        """Count certificates of a type."""
        async with self.pg_pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM certificates
                WHERE station_id = $1 AND certificate_type = $2
            """,
                station_id,
                certificate_type,
            )

            return count or 0

    async def get_installed_certificates(
        self, station_id: str, certificate_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Get installed certificates."""
        async with self.pg_pool.acquire() as conn:
            if certificate_type:
                rows = await conn.fetch(
                    """
                    SELECT certificate_type, certificate_data, certificate_chain,
                           issuer_name, subject_name, serial_number, valid_from,
                           valid_to, status, installation_date
                    FROM certificates WHERE station_id = $1 AND certificate_type = $2
                    ORDER BY installation_date DESC
                """,
                    station_id,
                    certificate_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT certificate_type, certificate_data, certificate_chain,
                           issuer_name, subject_name, serial_number, valid_from,
                           valid_to, status, installation_date
                    FROM certificates WHERE station_id = $1
                    ORDER BY installation_date DESC
                """,
                    station_id,
                )

            return [dict(row) for row in rows]

    async def find_certificate_by_hash(
        self,
        station_id: str,
        certificate_type: str,
        hash_algorithm: str,
        issuer_name_hash: str,
        issuer_key_hash: str,
        serial_number: str,
    ) -> Optional[str]:
        """Find certificate by hash data."""
        async with self.pg_pool.acquire() as conn:
            certificate_id = await conn.fetchval(
                """
                SELECT id FROM certificates
                WHERE station_id = $1 AND certificate_type = $2
                AND serial_number = $3
                ORDER BY installation_date DESC LIMIT 1
            """,
                station_id,
                certificate_type,
                serial_number,
            )

            return str(certificate_id) if certificate_id else None

    async def delete_certificate(self, station_id: str, certificate_id: str) -> None:
        """Delete certificate."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM certificates
                WHERE station_id = $1 AND id = $2
            """,
                station_id,
                certificate_id,
            )

    # ===== SECURITY MANAGEMENT METHODS =====

    async def store_auth_token(self, token_data: Dict[str, Any]) -> None:
        """Store authentication token."""
        token_hash = self._hash_secret(token_data["token"])
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_tokens (
                    station_id, token, token_hash, token_type, expires_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (station_id, token_type)
                DO UPDATE SET token = $2, token_hash = $3, expires_at = $5, created_at = $6
            """,
                token_data["station_id"],
                token_data["token"],
                token_hash,
                token_data["token_type"],
                token_data["expires_at"],
                token_data["created_at"],
            )

    async def update_token_usage(self, station_id: str, token: str, last_used: datetime) -> None:
        """Update token usage statistics."""
        token_hash = self._hash_secret(token)
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_tokens SET
                    last_used = $3, usage_count = usage_count + 1
                WHERE station_id = $1 AND (token_hash = $2 OR token = $4)
            """,
                station_id,
                token_hash,
                last_used,
                token,
            )

    async def revoke_auth_token(self, station_id: str, revoked_at: datetime) -> None:
        """Revoke authentication token."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_tokens SET
                    revoked_at = $2, expires_at = $2
                WHERE station_id = $1 AND revoked_at IS NULL
            """,
                station_id,
                revoked_at,
            )

    async def store_security_event(self, event_data: Dict[str, Any]) -> None:
        """Store security event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO security_events (
                    station_id, event_type, timestamp, tech_info, additional_info, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                event_data["station_id"],
                event_data["event_type"],
                event_data["timestamp"],
                event_data.get("tech_info"),
                json.dumps(event_data.get("additional_info", {})),
                datetime.now(timezone.utc),
            )

    async def get_security_events(
        self,
        station_id: Optional[str] = None,
        event_type: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get security events with filters."""
        async with self.pg_pool.acquire() as conn:
            query = "SELECT event_type, timestamp, tech_info, additional_info FROM security_events WHERE 1=1"
            params = []

            if station_id:
                query += " AND station_id = $" + str(len(params) + 1)
                params.append(station_id)

            if event_type:
                query += " AND event_type = $" + str(len(params) + 1)
                params.append(event_type)

            if start_time:
                query += " AND timestamp >= $" + str(len(params) + 1)
                params.append(start_time)

            if end_time:
                query += " AND timestamp <= $" + str(len(params) + 1)
                params.append(end_time)

            query += " ORDER BY timestamp DESC LIMIT 1000"

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def record_invalid_rfid_attempt(self, station_id: str, id_tag: str) -> None:
        """Persist an invalid RFID authorization attempt for abuse controls.

        Note: ``id_tag`` is a fleet card identifier and may be linkable to a
        driver. Treat ``security_events`` rows of this type as PII for the
        purposes of retention and access control — consider scoping any
        retention policy to keep this event_type for the minimum window the
        abuse-controls hot path actually needs (currently 60 s) plus whatever
        compliance window applies (typically 30–90 days).
        """
        await self.store_security_event(
            {
                "station_id": station_id,
                "event_type": "rfid_authorization_invalid",
                "timestamp": datetime.now(timezone.utc),
                "tech_info": "RFID/idTag authorization denied",
                "additional_info": {"id_tag": id_tag},
            }
        )

    async def count_recent_invalid_rfid_attempts(
        self,
        station_id: str,
        id_tag: str,
        window_seconds: int = 60,
    ) -> int:
        """Count invalid RFID attempts for a station/tag in a recent window.

        Backed by ``idx_security_events_rfid_invalid`` (migration 028) — a
        partial functional index on ``(station_id, additional_info ->> 'id_tag',
        timestamp)`` scoped to ``event_type = 'rfid_authorization_invalid'``.
        Update or drop both together if the query shape changes.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        async with self.pg_pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(*)
                  FROM security_events
                 WHERE station_id = $1
                   AND event_type = 'rfid_authorization_invalid'
                   AND timestamp >= $2
                   AND (additional_info ->> 'id_tag') = $3
                """,
                station_id,
                cutoff,
                id_tag,
            )
            return int(value or 0)

    async def clear_invalid_rfid_attempts(self, station_id: str, id_tag: str) -> None:
        """Record successful recovery after previous invalid attempts.

        The invalid-attempt rows are append-only audit data. This marker keeps
        the trail intact while giving operators a visible recovery signal.
        """
        if await self.count_recent_invalid_rfid_attempts(station_id, id_tag, 300) == 0:
            return
        await self.store_security_event(
            {
                "station_id": station_id,
                "event_type": "rfid_authorization_recovered",
                "timestamp": datetime.now(timezone.utc),
                "tech_info": "RFID/idTag authorized after previous invalid attempts",
                "additional_info": {"id_tag": id_tag},
            }
        )

    async def consume_operator_override(
        self, station_id: str, id_tag: str
    ) -> Optional[Dict[str, Any]]:
        """Claim or reuse a recent unexpired override for StartTransaction flow.

        Used by ``RFIDAuthorizationService.authorize`` after ``lookup_id_tag``
        misses but before the invalid-attempt audit row is written.

        Why this is not strictly single-use:
        many OCPP 1.6 chargers send both ``Authorize`` and ``StartTransaction``
        for the same RemoteStart idTag. The first call should consume the row,
        and the immediately following second call should still pass. We allow
        reuse only for already-consumed rows from the last 120 seconds.
        """
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH claimed AS (
                    UPDATE operator_authorization_overrides
                       SET consumed_at = NOW()
                     WHERE station_id = $1
                       AND id_tag = $2
                       AND consumed_at IS NULL
                       AND expires_at > NOW()
                    RETURNING id, station_id, connector_id, organization_id, depot_id,
                              created_by, reason, expires_at, consumed_at
                )
                SELECT * FROM claimed
                UNION ALL
                SELECT id, station_id, connector_id, organization_id, depot_id,
                       created_by, reason, expires_at, consumed_at
                  FROM operator_authorization_overrides
                 WHERE station_id = $1
                   AND id_tag = $2
                   AND consumed_at IS NOT NULL
                   AND consumed_at > NOW() - INTERVAL '120 seconds'
                   AND NOT EXISTS (SELECT 1 FROM claimed)
                 LIMIT 1
                """,
                station_id,
                id_tag,
            )
            return dict(row) if row else None

    async def validate_api_key(self, station_id: str, api_key: str) -> bool:
        """Validate API key."""
        api_key_hash = self._hash_secret(api_key)
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM api_keys
                WHERE station_id = $1
                AND (
                    api_key_hash = $2
                    OR api_key = $4
                )
                AND active = true
                AND (expires_at IS NULL OR expires_at > $3)
            """,
                station_id,
                api_key_hash,
                datetime.now(timezone.utc),
                api_key,
            )

            return row is not None

    @staticmethod
    def _hash_secret(secret: str) -> str:
        """Hash sensitive secrets with HMAC-SHA256 and a server-side pepper."""
        pepper = os.getenv("AUTH_SECRET_PEPPER", "")
        if pepper:
            return hmac.new(
                pepper.encode("utf-8"),
                secret.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    async def resolve_station_id(self, station_id: str) -> str:
        """Return the canonical station id for a path-supplied station or alias."""
        async with self.pg_pool.acquire() as conn:
            canonical = await conn.fetchval(
                """
                SELECT canonical_station_id
                FROM ocpp_station_aliases
                WHERE alias_station_id = $1
                  AND active = TRUE
                LIMIT 1
                """,
                station_id,
            )
        return str(canonical) if canonical else station_id

    async def ensure_station_alias(
        self,
        alias_station_id: str,
        canonical_station_id: str,
        *,
        source: str = "auto-multipath",
        notes: str = "Auto-registered from OCPP multi-segment path",
    ) -> bool:
        """Best-effort upsert of an OCPP station alias.

        Inserts ``(alias_station_id → canonical_station_id)`` only when the
        canonical id matches an existing ``charging_stations.station_id`` row
        and no row already exists for the alias. Idempotent — existing aliases
        are never overwritten, even if they point at a different canonical.
        Returns ``True`` iff a new row was actually created.

        The alias only enables resolution; Basic Auth still gates every
        connection, so an attacker who supplies a real canonical in the path
        but an unknown serial gets a wasted alias row and a 1008 close — no
        authorization boundary is crossed.
        """
        if alias_station_id == canonical_station_id:
            return False
        # The EXISTS subquery references ``charging_stations`` which lives
        # in Supabase; using ``pg_pool`` directly raises ``UndefinedTableError``
        # against the real Timescale DB. ``_static_pool`` routes to Supabase
        # when wired and falls back to ``pg_pool`` for tests. ``ocpp_station_aliases``
        # exists in both DBs (migration 024 + supabase/008), so the INSERT
        # half of the statement is correct against either pool.
        async with self._static_pool().acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO ocpp_station_aliases
                    (alias_station_id, canonical_station_id, source, notes)
                SELECT $1::VARCHAR, $2::VARCHAR, $3::VARCHAR, $4::TEXT
                WHERE EXISTS (
                    SELECT 1 FROM charging_stations WHERE station_id = $2::VARCHAR
                )
                ON CONFLICT (alias_station_id) DO NOTHING
                """,
                alias_station_id,
                canonical_station_id,
                source,
                notes,
            )
        return isinstance(result, str) and result.endswith(" 1")

    async def is_basic_auth_username_allowed(self, station_id: str, username: str) -> bool:
        """Return True when username is the canonical station id or an active alias."""
        if hmac.compare_digest(username, station_id):
            return True
        async with self.pg_pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT 1
                    FROM ocpp_station_aliases
                    WHERE alias_station_id = $1
                      AND canonical_station_id = $2
                      AND active = TRUE
                    LIMIT 1
                    """,
                    username,
                    station_id,
                )
            )

    # ===== OCPP 1.6 PILOT HELPERS (migration 012) =====

    async def next_transaction_id(self) -> int:
        """Return the next OCPP 1.6 transactionId from the DB sequence.

        Uses the ``ocpp_transaction_id`` sequence created in
        migrations/012_ocpp_pilot_hardening.sql so IDs survive restarts.
        """
        async with self.pg_pool.acquire() as conn:
            return int(await conn.fetchval("SELECT nextval('ocpp_transaction_id')"))

    async def next_charging_profile_id(self) -> int:
        """Return the next OCPP 1.6 chargingProfileId from the DB sequence."""
        async with self.pg_pool.acquire() as conn:
            return int(await conn.fetchval("SELECT nextval('ocpp_charging_profile_id')"))

    async def lookup_id_tag(
        self,
        id_tag: str,
        station_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve an OCPP idTag to known vehicle/card/driver identity.

        Vehicle primary idTags are authoritative when ``vehicles.id_tag``
        matches. Otherwise an active row in ``rfid_cards`` is sufficient on
        its own — vehicle and driver assignments are best-effort enrichment
        and a card without either is still a valid authorization.

        Resilient to missing reference tables: deployments that have not
        provisioned ``vehicles``, ``drivers`` or the assignment join tables
        (e.g. cards-only fleets) still authorize active cards. Each
        ``UndefinedTableError`` is logged once per process so schema drift
        is visible without spamming the log on every Authorize.

        Static identity tables live in Supabase; ``_static_pool`` returns
        the Supabase pool when wired in, falling back to ``pg_pool``
        for legacy deployments and unit tests.
        """
        async with self._static_pool().acquire() as conn:
            # Vehicle-primary tag (e.g. printed on the vehicle itself).
            # Optional — if the deployment does not provision ``vehicles``,
            # fall through to the cards lookup rather than failing closed
            # on the whole authorize flow.
            try:
                station_filter = ""
                params: list[Any] = [id_tag]
                if station_id is not None:
                    station_filter = (
                        "AND EXISTS (SELECT 1 FROM charging_stations c "
                        "WHERE c.station_id = $2 AND c.site_id = v.site_id)"
                    )
                    params.append(station_id)
                rows = await conn.fetch(
                    f"""
                    SELECT v.id::text AS vehicle_id,
                           v.site_id::text AS depot_id,
                           NULL::text AS driver_id,
                           NULL::text AS card_id,
                           'vehicle'::text AS source
                    FROM vehicles v
                    WHERE v.id_tag = $1
                      AND COALESCE(v.status, 'active') = 'active'
                      {station_filter}
                    LIMIT 2
                    """,
                    *params,
                )
                if len(rows) > 1:
                    self.logger.error(
                        "Rejecting id_tag lookup for %r: multiple vehicles share the same id_tag.",
                        id_tag,
                    )
                    return None
                if rows:
                    return dict(rows[0])
            except asyncpg.exceptions.UndefinedTableError as exc:
                self._log_missing_reference_table_once("vehicles", exc)

            # RFID card lookup. The card row alone is sufficient to
            # authorize. Vehicle/driver attribution is enriched separately
            # so a deployment without those reference tables still works.
            card_filter = ""
            card_params: list[Any] = [id_tag]
            if station_id is not None:
                card_filter = (
                    "AND EXISTS (SELECT 1 FROM charging_stations ch "
                    "WHERE ch.station_id = $2 AND ch.site_id = c.site_id)"
                )
                card_params.append(station_id)
            try:
                card_rows = await conn.fetch(
                    f"""
                    SELECT c.id::text AS card_id,
                           c.site_id::text AS depot_id
                    FROM rfid_cards c
                    WHERE c.id_tag = $1
                      AND c.status = 'active'
                      {card_filter}
                    LIMIT 2
                    """,
                    *card_params,
                )
            except asyncpg.exceptions.UndefinedTableError as exc:
                self._log_missing_reference_table_once("rfid_cards", exc)
                return None

            if len(card_rows) > 1:
                self.logger.error(
                    "Rejecting id_tag lookup for %r: multiple active RFID cards share it.",
                    id_tag,
                )
                return None
            if not card_rows:
                return None

            card_row = dict(card_rows[0])
            card_id = card_row["card_id"]
            depot_id = card_row["depot_id"]

            return {
                "card_id": card_id,
                "depot_id": depot_id,
                "vehicle_id": await self._lookup_card_vehicle_assignment(conn, card_id, depot_id),
                "driver_id": await self._lookup_card_driver_assignment(conn, card_id, depot_id),
                "source": "rfid_card",
            }

    async def _lookup_card_vehicle_assignment(
        self,
        conn: "asyncpg.Connection",
        card_id: str,
        depot_id: str,
    ) -> Optional[str]:
        """Return the active vehicle attached to ``card_id``, or None.

        Tolerates missing ``rfid_card_vehicle_assignments`` / ``vehicles``
        so cards without an attached vehicle still authorize.
        """
        try:
            return await conn.fetchval(
                """
                SELECT cva.vehicle_id::text
                FROM rfid_card_vehicle_assignments cva
                JOIN vehicles v ON v.id = cva.vehicle_id
                WHERE cva.card_id = $1::uuid
                  AND v.site_id = $2::uuid
                  AND COALESCE(v.status, 'active') = 'active'
                ORDER BY v.external_id
                LIMIT 1
                """,
                card_id,
                depot_id,
            )
        except asyncpg.exceptions.UndefinedTableError as exc:
            self._log_missing_reference_table_once("rfid_card_vehicle_assignments", exc)
            return None

    async def _lookup_card_driver_assignment(
        self,
        conn: "asyncpg.Connection",
        card_id: str,
        depot_id: str,
    ) -> Optional[str]:
        """Return the active driver attached to ``card_id``, or None.

        Tolerates missing ``rfid_card_driver_assignments`` / ``drivers`` so
        cards without an attached driver still authorize.
        """
        try:
            return await conn.fetchval(
                """
                SELECT cda.driver_id::text
                FROM rfid_card_driver_assignments cda
                JOIN drivers dr ON dr.id = cda.driver_id
                WHERE cda.card_id = $1::uuid
                  AND dr.site_id = $2::uuid
                  AND dr.status = 'active'
                ORDER BY dr.display_name
                LIMIT 1
                """,
                card_id,
                depot_id,
            )
        except asyncpg.exceptions.UndefinedTableError as exc:
            self._log_missing_reference_table_once("rfid_card_driver_assignments", exc)
            return None

    def _log_missing_reference_table_once(self, table_label: str, exc: BaseException) -> None:
        """Log a missing-relation error at WARNING, once per process.

        Schema drift (e.g. a deployment running without ``vehicles``)
        should be visible to operators but not flood the log on every
        Authorize attempt.
        """
        cache = TimescaleClient._missing_reference_table_logs
        if table_label in cache:
            return
        cache.add(table_label)
        self.logger.warning(
            "rfid_lookup_missing_reference_table table=%s error=%s "
            "(continuing without enrichment from this table)",
            table_label,
            exc,
        )

    # Process-wide cache so each missing reference table is logged once,
    # not once per ``TimescaleClient`` instance and not once per Authorize.
    _missing_reference_table_logs: set[str] = set()

    # ===== OCPP 1.6 RECOVERY HELPERS (migration 013) =====

    async def fetch_open_sessions(self, station_id: str) -> List[Dict[str, Any]]:
        """Return rows for sessions that are still open at ``station_id``.

        Used by ``OCPP16Session._on_boot`` to repopulate
        ``FleetChargePoint.transactions`` after a handler restart so
        StopTransaction does not orphan the row. ``meter_start_wh`` is
        included so the close path can still compute a billing-grade
        energy delta across a handler restart (migration 036).
        """
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT transaction_id, connector_id, evse_id, id_token,
                       start_time, meter_start_wh
                  FROM charging_sessions
                 WHERE station_id = $1
                   AND end_time IS NULL
                   AND source = 'live'
                   AND transaction_id IS NOT NULL
                 ORDER BY start_time ASC
                """,
                station_id,
            )
            return [dict(r) for r in rows]

    async def insert_open_session(
        self,
        *,
        station_id: str,
        transaction_id: int,
        evse_id: int,
        connector_id: int,
        id_token: Optional[str],
        start_time: datetime,
        vehicle_id: Optional[str] = None,
        driver_id: Optional[str] = None,
        card_id: Optional[str] = None,
        meter_start_wh: Optional[int] = None,
    ) -> None:
        """Insert an open ``charging_sessions`` row at StartTransaction.

        ``end_time`` is left NULL so ``fetch_open_sessions`` can find it on
        boot. ``transaction_id`` is the OCPP 1.6 integer id from the
        ``ocpp_transaction_id`` sequence. ``meter_start_wh`` is the raw
        Wh reading from OCPP StartTransaction.meterStart; the close path
        subtracts it from meter_stop_wh to compute energy_delivered_kwh
        (migration 036). Optional only to keep older simulator paths
        working — production OCPP traffic always carries it.
        """
        async with self.pg_pool.acquire() as conn:
            async with conn.transaction():
                # Lock key built in Python to avoid asyncpg's prepared-statement
                # type inference treating $2 as text (via the `||` chain) and
                # rejecting the integer transaction_id with "expected str, got
                # int". Single text bind is unambiguous and equivalent in
                # lock semantics.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{station_id}:{transaction_id}",
                )
                existing_open = await conn.fetchval(
                    """
                    SELECT session_id::text
                    FROM charging_sessions
                    WHERE station_id = $1
                      AND transaction_id = $2
                      AND end_time IS NULL
                      AND source = 'live'
                    LIMIT 1
                    """,
                    station_id,
                    transaction_id,
                )
                if existing_open:
                    self.logger.info(
                        "Skipping duplicate open session insert for station=%s tx_id=%s session_id=%s",
                        station_id,
                        transaction_id,
                        existing_open,
                    )
                    return
                await conn.execute(
                    """
                    INSERT INTO charging_sessions (
                        station_id, transaction_id, evse_id, connector_id,
                        id_token, start_time, vehicle_id, driver_id, card_id,
                        meter_start_wh
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid, $10
                    )
                    """,
                    station_id,
                    transaction_id,
                    evse_id,
                    connector_id,
                    id_token,
                    start_time,
                    vehicle_id,
                    driver_id,
                    card_id,
                    meter_start_wh,
                )

    async def close_open_session(
        self,
        station_id: str,
        transaction_id: int,
        end_time: datetime,
        meter_stop_wh: Optional[int] = None,
        stop_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Mark a ``charging_sessions`` row closed at StopTransaction.

        Writes ``end_time`` and ``meter_stop_wh`` and stores the kWh delta
        computed by :func:`compute_energy_kwh` against the row's stored
        ``meter_start_wh``. The helper rejects every untrustworthy bracket
        (missing start/stop, ``meter_start_wh = 0``, ``meter_stop < meter_start``)
        and the caller writes ``NULL`` in those cases — NULL beats a
        confidently-wrong billing number for accounting.

        The helper is also called from the orphan-recovery job
        (:meth:`recover_orphaned_sessions`) so the formula lives in exactly
        one place (Issue 7A).

        Args:
            stop_reason: Optional value to stamp into ``stop_reason``.
                ``None`` leaves the column untouched. The orphan recovery
                job passes ``'orphaned_recovered'``; the OCPP handler passes
                the charger-supplied ``reason`` field.

        Returns:
            ``None`` if no live, still-open row matched (idempotent retry,
            already-closed session, or imported source). Otherwise a dict
            of the RETURNING values: ``meter_start_wh``, ``last_meter_wh``
            (running register at close time — distinguishes "deferred start
            never backfilled" from "legacy row" in the anomaly classifier),
            and the freshly written ``energy_delivered_kwh`` (which may be
            NULL on anomaly). Callers use the values to decide whether to
            emit a WARN.
        """
        async with self.pg_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT session_id, meter_start_wh, last_meter_wh
                      FROM charging_sessions
                     WHERE station_id = $1
                       AND transaction_id = $2
                       AND end_time IS NULL
                       AND source = 'live'
                     FOR UPDATE
                    """,
                    station_id,
                    transaction_id,
                )
                if row is None:
                    return None
                session_id = row["session_id"]
                meter_start_wh = row["meter_start_wh"]
                last_meter_wh = row["last_meter_wh"]
                energy_kwh = compute_energy_kwh(meter_stop_wh, meter_start_wh)
                effective_stop_reason = stop_reason

                # Phase 2 synthesis fallback. Only fires when:
                #   * the bracket-based delta refused (compute returned None)
                #   * no meter_start_wh was ever recorded (deferred-and-not-
                #     backfilled — first-MeterValues backfill never happened)
                #   * no last_meter_wh either (charger never sent a register
                #     sample)
                # In that triple-NULL state ``meter_stop_wh`` is the only
                # energy signal we have. Some ABB Terra AC firmwares emit
                # it as a per-session delta rather than an absolute register
                # so we trust it under a 50 kWh cap (operator-tunable via
                # ``OCPP_SYNTHESIZED_DELTA_CAP_KWH``). The stop_reason is
                # suffixed so the row is queryable for audit; operators can
                # filter ``LIKE '%synthesized_delta%'`` to find them.
                synthesized = False
                if (
                    energy_kwh is None
                    and meter_start_wh is None
                    and last_meter_wh is None
                    and meter_stop_wh is not None
                ):
                    energy_kwh = synthesize_energy_kwh_from_meter_stop(
                        meter_stop_wh,
                        cap_wh=_synthesized_delta_cap_wh(),
                    )
                    if energy_kwh is not None:
                        synthesized = True
                        suffix = "synthesized_delta"
                        base = stop_reason or "unknown"
                        # Defensive: keep within stop_reason VARCHAR(64).
                        effective_stop_reason = f"{base}|{suffix}"[:64]
                        self.logger.info(
                            "Synthesized energy_delivered_kwh=%.3f from meter_stop_wh=%s "
                            "for station=%s tx_id=%s (no meter_start_wh, no register "
                            "samples). Capped at %s kWh. stop_reason suffixed.",
                            energy_kwh,
                            meter_stop_wh,
                            station_id,
                            transaction_id,
                            _synthesized_delta_cap_wh() / 1000.0,
                        )

                updated = await conn.fetchrow(
                    """
                    UPDATE charging_sessions
                       SET end_time             = $3,
                           meter_stop_wh        = $4::bigint,
                           energy_delivered_kwh = $5,
                           stop_reason          = COALESCE($6, stop_reason),
                           updated_at           = NOW()
                     WHERE station_id     = $1
                       AND transaction_id = $2
                       AND session_id     = $7
                       AND end_time IS NULL
                       AND source = 'live'
                 RETURNING meter_start_wh, last_meter_wh, energy_delivered_kwh
                    """,
                    station_id,
                    transaction_id,
                    end_time,
                    meter_stop_wh,
                    energy_kwh,
                    effective_stop_reason,
                    session_id,
                )
                if updated is None:
                    # Lost the race to another writer between SELECT and UPDATE.
                    return None
                result = dict(updated)
                result["session_id"] = session_id
                # Surface the synthesis flag so the OCPP handler can pick a
                # distinct log line / metric without re-deriving the
                # condition. Not a column on charging_sessions — the
                # ``synthesized_delta`` suffix on stop_reason is the durable
                # audit trail.
                result["synthesized"] = synthesized
        # Transaction committed + connection released. Schedule the cost
        # calculation as a fire-and-forget task — if it crashes, the row
        # stays cost_total=NULL and the backfill script catches it on the
        # next run (its predicate covers exactly this case).
        self._schedule_session_cost(session_id)
        return result

    async def _resolve_bidding_zone(self, site_id: Optional[uuid.UUID]) -> Optional[str]:
        """Cross-pool helper: site_id (Supabase) → ENTSO-E EIC code.

        Returns ``None`` when no static pool is configured (legacy tests
        with no SupabaseClient) or when the depot has no
        ``tariff_config.entsoe_zone`` and no recognized
        ``sites.timezone``. The cost task treats ``None`` as
        ``unpriceable``.
        """
        if site_id is None:
            return None
        static_pool = self._static_pool()
        if static_pool is None:
            return None
        # Lazy import — keeps the WS handler's import path lean.
        from ..db.queries import resolve_bidding_zone

        try:
            async with static_pool.acquire() as conn:
                return await resolve_bidding_zone(conn, site_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("resolve_bidding_zone failed for site %s: %s", site_id, exc)
            return None

    async def _resolve_site_id_for_station(self, station_id: Optional[str]) -> Optional[uuid.UUID]:
        """Cross-pool helper: OCPP ``station_id`` → ``sites.id`` (Supabase).

        ``insert_open_session`` writes the OCPP station_id but not
        ``site_id`` (it has no Supabase round-trip on the StartTransaction
        hot path), so live ``charging_sessions`` rows close with
        ``site_id IS NULL``. The cost calculator short-circuits to
        ``'no_depot'`` for those rows — meaning every live session would
        skip pricing entirely. This helper backfills the join from
        Supabase's ``charging_stations`` so the cost task gets a real
        depot identifier without bloating the close-path commit.

        Returns ``None`` when the static pool isn't configured (legacy
        single-pool tests) or when the station_id doesn't match any row.
        """
        if not station_id:
            return None
        static_pool = self._static_pool()
        if static_pool is None:
            return None
        try:
            async with static_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT site_id FROM charging_stations WHERE station_id = $1 LIMIT 1",
                    station_id,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "site_id lookup failed for station %s: %s", station_id, exc
            )
            return None
        return row["site_id"] if row else None

    def _schedule_session_cost(self, session_id: uuid.UUID) -> None:
        """Schedule the post-commit cost calculation for one session.

        Fire-and-forget. ``_compute_and_write_cost`` swallows every
        exception so close-path callers are never affected. Errors are
        logged and counted via ``SESSION_COST_COMPUTE_FAILURES``.

        The task is held in ``self._background_tasks`` so the event
        loop's weak-reference table doesn't lose track of it mid-flight
        — a stray garbage collection between the StopTransaction commit
        and this task's first ``await`` would otherwise silently drop
        the cost calc (the row sits at ``cost_total=NULL`` until the
        next backfill run, which is the documented safety net but
        defeats the whole point of the live close path).
        """
        task = asyncio.create_task(
            self._compute_and_write_cost(session_id),
            name=f"session-cost-{session_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _compute_and_write_cost(self, session_id: uuid.UUID) -> None:
        """Fetch the just-closed session, run the cost calculator, update the row.

        Lazy-imports ``src.core.billing`` and ``src.monitoring.metrics``
        to keep the legacy WS handler's hot import path lean.
        """
        from ..core.billing import compute_session_cost, write_session_cost
        from ..monitoring.metrics import (
            SESSION_COST_COMPUTE_FAILURES,
            SESSION_COST_COMPUTED,
            SESSION_COST_DURATION,
        )

        started = time.monotonic()
        try:
            async with self.pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT session_id, site_id, vehicle_id, station_id, connector_id,
                           transaction_id, start_time, end_time,
                           energy_delivered_kwh, cost_total, cost_total_source
                      FROM charging_sessions
                     WHERE session_id = $1
                    """,
                    session_id,
                )
            if row is None:
                self.logger.warning(
                    "Session %s vanished before cost calc could read it",
                    session_id,
                )
                return

            # Resolve the bidding zone for this session's depot via the
            # static (Supabase) pool. The calculator never crosses pools
            # itself; the WS handler owns the join.
            row_dict = dict(row)
            # Live sessions land with site_id=NULL because insert_open_session
            # doesn't write it (no Supabase round-trip on the StartTransaction
            # hot path). Backfill from station_id via Supabase so the cost
            # calculator can resolve a bidding zone and avoid the
            # ``'no_depot'`` short-circuit for every live row.
            if row_dict.get("site_id") is None:
                row_dict["site_id"] = await self._resolve_site_id_for_station(
                    row_dict.get("station_id")
                )
            row_dict["bidding_zone"] = await self._resolve_bidding_zone(row_dict.get("site_id"))

            result = await compute_session_cost(self.pg_pool, row_dict)
            # Only count the metric when the UPDATE actually landed.
            # write_session_cost returns False on lost-race (another
            # writer beat us to it) or when the row stopped being
            # eligible — neither is a "computed total" event. Counting
            # those would inflate the dashboard's success rate.
            wrote = await write_session_cost(self.pg_pool, session_id, result)
            if wrote:
                SESSION_COST_COMPUTED.labels(source=result.source).inc()
        except asyncpg.PostgresError as exc:
            self.logger.exception("DB error computing cost for session %s: %s", session_id, exc)
            SESSION_COST_COMPUTE_FAILURES.labels(reason="db_error").inc()
        except Exception as exc:  # noqa: BLE001 — fire-and-forget must not raise
            self.logger.exception(
                "Unexpected error computing cost for session %s: %s",
                session_id,
                exc,
            )
            SESSION_COST_COMPUTE_FAILURES.labels(reason="unexpected").inc()
        finally:
            SESSION_COST_DURATION.observe(time.monotonic() - started)

    async def recover_orphaned_sessions(
        self,
        stale_after_seconds: int = 1800,
        batch_limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Close charging_sessions rows whose StopTransaction never arrived.

        A row is treated as orphaned when:

          * ``end_time IS NULL`` and ``source = 'live'`` (still considered
            open from the WebSocket handler's perspective), and
          * ``last_seen_at`` is older than ``stale_after_seconds`` (the
            WebSocket close hook stamps this column, so 'old' means the
            charger socket has been closed for at least that long), or
          * ``last_seen_at IS NULL`` AND ``start_time`` is older than
            ``stale_after_seconds`` AND the row has no recent handler
            activity (``COALESCE(last_meter_seen_at, updated_at,
            start_time)`` older than the same threshold). Covers handler
            restarts that never stamped ``last_seen_at``, and excludes
            live sessions (MeterValues refresh ``updated_at`` /
            ``last_meter_seen_at``) including after ``clear_sessions_seen``
            nulls ``last_seen_at`` on reconnect.

        For each orphaned row the synthesized ``meter_stop_wh`` is the
        running ``last_meter_wh`` from migration 038 — populated by the
        MeterValues handler — falling back to ``NULL`` (which leaves
        ``energy_delivered_kwh = NULL`` per :func:`compute_energy_kwh`)
        when no MeterValues with a register reading ever arrived.

        Args:
            stale_after_seconds: Threshold in seconds. 30 minutes (1800s)
                is conservative — well past any realistic legit session
                pause. The recovery loop in ``main.py`` reads this from
                ``ORPHAN_RECOVERY_THRESHOLD_S``.
            batch_limit: Maximum rows closed per call. Defends against a
                pathological recovery sweep blocking the connection for
                too long; the next loop iteration picks up the rest.

        Returns:
            List of dicts describing each closed row (session_id,
            station_id, transaction_id, meter_start_wh, meter_stop_wh,
            energy_delivered_kwh). Used by the caller to emit one
            ``audit_log`` entry per row.
        """
        closed: List[Dict[str, Any]] = []
        async with self.pg_pool.acquire() as conn:
            stale_rows = await conn.fetch(
                """
                SELECT session_id,
                       station_id,
                       transaction_id,
                       meter_start_wh,
                       last_meter_wh,
                       last_seen_at,
                       start_time
                  FROM charging_sessions
                 WHERE end_time IS NULL
                   AND source = 'live'
                   AND transaction_id IS NOT NULL
                   AND (
                       (last_seen_at IS NOT NULL
                        AND last_seen_at < NOW() - make_interval(secs => $1))
                       OR
                       (last_seen_at IS NULL
                        AND start_time < NOW() - make_interval(secs => $1)
                        AND COALESCE(last_meter_seen_at, updated_at, start_time)
                            < NOW() - make_interval(secs => $1))
                   )
                 ORDER BY COALESCE(last_seen_at, start_time) ASC
                 LIMIT $2
                """,
                stale_after_seconds,
                batch_limit,
            )

            now = datetime.now(timezone.utc)
            for row in stale_rows:
                station_id = row["station_id"]
                transaction_id = int(row["transaction_id"])
                last_meter_wh = row["last_meter_wh"]
                meter_start_wh = row["meter_start_wh"]
                energy_kwh = compute_energy_kwh(last_meter_wh, meter_start_wh)
                close_time = row["last_seen_at"] or now

                updated = await conn.fetchrow(
                    """
                    UPDATE charging_sessions
                       SET end_time             = $3,
                           meter_stop_wh        = $4::bigint,
                           energy_delivered_kwh = $5,
                           stop_reason          = 'orphaned_recovered',
                           updated_at           = NOW()
                     WHERE station_id     = $1
                       AND transaction_id = $2
                       AND session_id     = $7
                       AND end_time IS NULL
                       AND source = 'live'
                       AND (
                           (last_seen_at IS NOT NULL
                            AND last_seen_at < NOW() - make_interval(secs => $6))
                           OR
                           (last_seen_at IS NULL
                            AND start_time < NOW() - make_interval(secs => $6)
                            AND COALESCE(last_meter_seen_at, updated_at, start_time)
                                < NOW() - make_interval(secs => $6))
                       )
                 RETURNING session_id, station_id, transaction_id,
                           meter_start_wh, meter_stop_wh, energy_delivered_kwh
                    """,
                    station_id,
                    transaction_id,
                    close_time,
                    last_meter_wh,
                    energy_kwh,
                    stale_after_seconds,
                    row["session_id"],
                )
                if updated is not None:
                    closed.append(dict(updated))

        # Schedule cost calcs for every orphan-recovered row. Done after
        # the connection has been released so the post-commit tasks run
        # against the canonical row state.
        for row in closed:
            self._schedule_session_cost(row["session_id"])
        return closed

    async def mark_sessions_seen(self, station_id: str) -> None:
        """Stamp ``last_seen_at = NOW()`` on every open session at the station.

        Called from the WebSocket close hook so we can tell a stale-but-open
        session apart from a live one after a handler crash.
        """
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charging_sessions
                   SET last_seen_at = NOW()
                 WHERE station_id = $1
                   AND end_time IS NULL
                   AND source = 'live'
                """,
                station_id,
            )

    async def clear_sessions_seen(self, station_id: str) -> None:
        """Clear stale ``last_seen_at`` stamps for open live sessions.

        Called from the boot/reconnect path before open sessions are
        reloaded into memory. A disconnect can legitimately be followed by
        a reconnect while charging continues; leaving the old disconnect
        timestamp in place would let orphan recovery close an active
        session after ``stale_after_seconds`` elapses.

        ``last_seen_at`` is nulled so the row is not treated as "just
        disconnected" by case-1 of orphan recovery.
        """
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charging_sessions
                   SET last_seen_at = NULL
                 WHERE station_id = $1
                   AND end_time IS NULL
                   AND source = 'live'
                """,
                station_id,
            )

    async def mark_connectors_unavailable(self, station_id: str) -> None:
        """Append an ``Unavailable`` row for every known connector at the station.

        ``connector_status`` is append-only (the read query returns the latest
        per (station_id, connector_id)), so we INSERT one row per connector
        we have seen recently rather than UPDATE-in-place. If we have no prior
        rows for the station (a station that never sent StatusNotification),
        this is a no-op — there is nothing meaningful to mark Unavailable.
        """
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_status (
                    station_id, connector_id, status, error_code, timestamp
                )
                SELECT DISTINCT ON (connector_id)
                       station_id, connector_id, 'Unavailable', 'ConnectionLost', NOW()
                  FROM connector_status
                 WHERE station_id = $1
                 ORDER BY connector_id, timestamp DESC
                """,
                station_id,
            )

    async def mark_connectors_available_after_reconnect(self, station_id: str) -> int:
        """Undo a stale ``mark_connectors_unavailable`` row when the WS reconnects.

        ``mark_connectors_unavailable`` writes ``(Unavailable, 'ConnectionLost')``
        on every WS drop. ``derive_charger_status`` then forces ``offline``
        regardless of how recent the heartbeat is (see
        ``src/api/fleet_list.py``). When the charger reconnects but does NOT
        send a fresh StatusNotification — common for many ABB Terra firmwares
        on quick reconnects — that stale row outlives the actual offline
        window and the GET /depots/{id}/chargers pill stays stuck on
        ``offline`` even while OCPP frames flow.

        We append an ``(Available, NULL)`` row ONLY for connectors whose
        latest row is exactly the close-hook's marker, identified by
        ``error_code = 'ConnectionLost'``. That precisely undoes our own
        marker without clobbering:
          * a real ``Faulted`` from the charger,
          * a charger-issued ``Unavailable`` carrying a different error code
            (or NULL — handled by the equality on 'ConnectionLost'),
          * any state newer than the close-hook row (the latest-row check
            naturally excludes those).

        Returns the number of connectors whose status was rewritten — useful
        for observability so we can spot misbehaving firmwares whose stuck
        states are routinely corrected on reconnect.
        """
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (connector_id)
                           station_id, connector_id, status, error_code
                      FROM connector_status
                     WHERE station_id = $1
                     ORDER BY connector_id, timestamp DESC
                )
                INSERT INTO connector_status (
                    station_id, connector_id, status, error_code, timestamp
                )
                SELECT station_id, connector_id, 'Available', NULL, NOW()
                  FROM latest
                 WHERE status = 'Unavailable'
                   AND error_code = 'ConnectionLost'
                RETURNING connector_id
                """,
                station_id,
            )
            return len(rows)

    async def enqueue_charging_command(
        self,
        *,
        charge_point_id: str,
        connector_id: int,
        payload: Dict[str, Any],
        expires_in_min: int = 60,
        command_type: str = "set_charging_profile",
    ) -> int:
        """Persist a SetChargingProfile that could not be delivered immediately.

        Returns the new ``queue_id``. The replay path
        (``replay_queued_commands_for``) re-reads any rows that are still
        pending and not expired the next time the charger boots.
        """
        async with self.pg_pool.acquire() as conn:
            queue_id = await conn.fetchval(
                """
                INSERT INTO charging_command_queue (
                    charge_point_id, connector_id, command_type,
                    payload, expires_at
                ) VALUES (
                    $1, $2, $3, $4::jsonb,
                    NOW() + ($5 || ' minutes')::interval
                )
                RETURNING queue_id
                """,
                charge_point_id,
                connector_id,
                command_type,
                json.dumps(payload),
                str(expires_in_min),
            )
            return int(queue_id)

    async def fetch_pending_commands(self, charge_point_id: str) -> List[Dict[str, Any]]:
        """Return non-expired pending commands for a cp_id, oldest first."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT queue_id, connector_id, command_type, payload,
                       attempt_count
                  FROM charging_command_queue
                 WHERE charge_point_id = $1
                   AND status = 'pending'
                   AND expires_at > NOW()
                 ORDER BY enqueued_at ASC
                """,
                charge_point_id,
            )
            return [dict(r) for r in rows]

    async def mark_command_acked(self, queue_id: int) -> None:
        """Mark queued command as acknowledged by the charger."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charging_command_queue
                   SET status = 'acked',
                       sent_at = NOW(),
                       acked_at = NOW(),
                       attempt_count = attempt_count + 1
                 WHERE queue_id = $1
                   AND status = 'pending'
                """,
                queue_id,
            )

    async def mark_command_failed(self, queue_id: int, error: str) -> None:
        """Charger Rejected, or the WS push raised. Increment attempts."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charging_command_queue
                   SET status = 'failed',
                       last_error = $2,
                       attempt_count = attempt_count + 1
                 WHERE queue_id = $1
                   AND status = 'pending'
                """,
                queue_id,
                error[:500],
            )

    async def mark_command_sent(self, queue_id: int) -> None:
        """Mark a queued command as successfully pushed to the charger.

        Distinct from ``mark_command_acked``: 'sent' is what the new
        queue-mediated dispatch path (session 3) uses when the WebSocket
        push returns Accepted. 'acked' is reserved for an OCPP-conf level
        ack we may surface later. Both are terminal success states.
        """
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charging_command_queue
                   SET status = 'sent',
                       sent_at = NOW(),
                       attempt_count = attempt_count + 1
                 WHERE queue_id = $1
                   AND status = 'pending'
                """,
                queue_id,
            )

    async def fetch_pending_commands_all(
        self, limit: int = 200, exclude_charge_point_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Return non-expired pending commands across every charger, oldest first.

        Used by the dispatch queue consumer to drain newly enqueued
        SetChargingProfile rows for *any* connected station (the per-station
        ``fetch_pending_commands`` is for the boot-time replay path).
        """
        async with self.pg_pool.acquire() as conn:
            if exclude_charge_point_ids:
                rows = await conn.fetch(
                    """
                    SELECT queue_id, charge_point_id, connector_id,
                           command_type, payload, attempt_count
                      FROM charging_command_queue
                     WHERE status = 'pending'
                       AND expires_at > NOW()
                       AND NOT (charge_point_id = ANY($2::text[]))
                     ORDER BY enqueued_at ASC
                     LIMIT $1
                    """,
                    limit,
                    exclude_charge_point_ids,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT queue_id, charge_point_id, connector_id,
                           command_type, payload, attempt_count
                      FROM charging_command_queue
                     WHERE status = 'pending'
                       AND expires_at > NOW()
                     ORDER BY enqueued_at ASC
                     LIMIT $1
                    """,
                    limit,
                )
            return [dict(r) for r in rows]

    async def expire_overdue_commands(self) -> int:
        """Move expired ``pending`` rows to ``expired``. Returns rowcount."""
        async with self.pg_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE charging_command_queue
                   SET status = 'expired'
                 WHERE status = 'pending'
                   AND expires_at <= NOW()
                """)
            # asyncpg returns "UPDATE n"
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

    async def queue_depth_by_status(self) -> Dict[str, int]:
        """Sample ``charging_command_queue`` depth grouped by status.

        Used by the metrics sampler to publish ``charging_command_queue_depth``.
        Returns a dict like ``{'pending': 3, 'sent': 17, 'failed': 0, ...}``.
        """
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT status, COUNT(*)::bigint AS n
                  FROM charging_command_queue
                 GROUP BY status
                """)
        return {r["status"]: int(r["n"]) for r in rows}

    async def fetch_admin_state(self, charge_point_id: str) -> Dict[str, Any]:
        """Aggregate state for ``/admin/ocpp/{cp_id}/state``.

        Reads:
          - latest connector_status row per connector
          - open ``charging_sessions`` rows (transaction_id, connector, id_token)
          - charging_command_queue rollup + most-recent row

        Connection / boot metadata (vendor, model, last_boot_at, last_heartbeat_at)
        is layered on by the API handler from the in-memory FleetChargePoint.
        """
        async with self.pg_pool.acquire() as conn:
            connectors = await conn.fetch(
                """
                SELECT DISTINCT ON (connector_id)
                       connector_id, status, error_code, timestamp AS updated_at
                  FROM connector_status
                 WHERE station_id = $1
                 ORDER BY connector_id, timestamp DESC
                """,
                charge_point_id,
            )
            txns = await conn.fetch(
                """
                SELECT transaction_id, connector_id, id_token, start_time
                  FROM charging_sessions
                 WHERE station_id = $1
                   AND end_time IS NULL
                   AND source = 'live'
                   AND transaction_id IS NOT NULL
                 ORDER BY start_time ASC
                """,
                charge_point_id,
            )
            queue_counts = await conn.fetch(
                """
                SELECT status, COUNT(*)::bigint AS n
                  FROM charging_command_queue
                 WHERE charge_point_id = $1
                 GROUP BY status
                """,
                charge_point_id,
            )
            last_command = await conn.fetchrow(
                """
                SELECT queue_id, status, sent_at, acked_at,
                       enqueued_at, last_error
                  FROM charging_command_queue
                 WHERE charge_point_id = $1
                 ORDER BY enqueued_at DESC
                 LIMIT 1
                """,
                charge_point_id,
            )

        return {
            "connectors": [dict(r) for r in connectors],
            "active_transactions": [dict(r) for r in txns],
            "queue_counts": {r["status"]: int(r["n"]) for r in queue_counts},
            "last_command": dict(last_command) if last_command else None,
        }

    async def count_active_transactions_by_station(self) -> Dict[str, int]:
        """Return open-transaction counts grouped by station (for metrics)."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT station_id, COUNT(*)::bigint AS n
                  FROM charging_sessions
                 WHERE end_time IS NULL
                   AND source = 'live'
                   AND transaction_id IS NOT NULL
                 GROUP BY station_id
                """)
        return {r["station_id"]: int(r["n"]) for r in rows}

    # ===== PLUG & CHARGE METHODS =====

    async def store_contract_info(self, contract_data: Dict[str, Any]) -> None:
        """Store contract information."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO contracts (
                    contract_id, ev_contract_id, certificate_chain, contract_certificate,
                    valid_from, valid_to, status, energy_contract_id, tariff_id,
                    max_power, max_energy, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (contract_id)
                DO UPDATE SET ev_contract_id = $2, certificate_chain = $3,
                             contract_certificate = $4, valid_from = $5, valid_to = $6,
                             status = $7, energy_contract_id = $8, tariff_id = $9,
                             max_power = $10, max_energy = $11, updated_at = $12
            """,
                contract_data["contract_id"],
                contract_data["ev_contract_id"],
                contract_data["certificate_chain"],
                contract_data["contract_certificate"],
                contract_data["valid_from"],
                contract_data["valid_to"],
                contract_data["status"],
                contract_data.get("energy_contract_id"),
                contract_data.get("tariff_id"),
                contract_data.get("max_power"),
                contract_data.get("max_energy"),
                datetime.now(timezone.utc),
            )

    async def get_contract_info(self, contract_id: str) -> Optional[Dict[str, Any]]:
        """Get contract information."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT contract_id, ev_contract_id, certificate_chain, contract_certificate,
                       valid_from, valid_to, status, energy_contract_id, tariff_id,
                       max_power, max_energy
                FROM contracts WHERE contract_id = $1
            """,
                contract_id,
            )

            return dict(row) if row else None

    async def revoke_contract(self, contract_id: str, reason: str, revoked_at: datetime) -> None:
        """Revoke contract."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE contracts SET
                    status = 'Revoked', revoked_at = $2, revoked_reason = $3
                WHERE contract_id = $1
            """,
                contract_id,
                revoked_at,
                reason,
            )

    async def get_trusted_root_certificates(self) -> List[x509.Certificate]:
        """Get trusted root certificates."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT certificate_data FROM certificates
                WHERE certificate_type = 'V2GRootCertificate' AND status = 'Valid'
            """)

            certificates = []
            for row in rows:
                try:
                    cert_bytes = base64.b64decode(row["certificate_data"])
                    cert = x509.load_pem_x509_certificate(cert_bytes)
                    certificates.append(cert)
                except Exception as e:
                    self.logger.error(f"Error loading trusted root certificate: {e}")

            return certificates

    async def get_energy_contract(self, energy_contract_id: str) -> Optional[Dict[str, Any]]:
        """Get energy contract."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT energy_contract_id, active, valid_from, valid_to, max_power, max_energy
                FROM energy_contracts WHERE energy_contract_id = $1
            """,
                energy_contract_id,
            )

            return dict(row) if row else None

    async def get_tariff_by_id(self, tariff_id: str) -> Optional[Dict[str, Any]]:
        """Get tariff by ID."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT tariff_id, active, valid_from, valid_to, currency, tariff_element
                FROM tariffs WHERE tariff_id = $1
            """,
                tariff_id,
            )

            return dict(row) if row else None

    async def get_evse_capabilities(
        self, station_id: str, evse_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get EVSE capabilities."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT max_power, connector_types, supported_protocols, v2g_capable
                FROM evse_capabilities WHERE station_id = $1 AND evse_id = $2
            """,
                station_id,
                evse_id,
            )

            return dict(row) if row else None

    async def get_token_balance(self, id_token: str) -> Optional[Dict[str, Any]]:
        """Get token balance."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT balance, currency, last_updated
                FROM token_balances WHERE id_token = $1
            """,
                id_token,
            )

            return dict(row) if row else None

    # ===== SMART CHARGING METHODS =====

    async def store_grid_constraint(self, constraint_data: Dict[str, Any]) -> None:
        """Store grid constraint."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO grid_constraints (
                    constraint_id, constraint_type, location, max_power, min_power,
                    valid_from, valid_to, priority, description, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (constraint_id)
                DO UPDATE SET constraint_type = $2, location = $3, max_power = $4,
                             min_power = $5, valid_from = $6, valid_to = $7,
                             priority = $8, description = $9, updated_at = $10
            """,
                constraint_data["constraint_id"],
                constraint_data["constraint_type"],
                constraint_data["location"],
                constraint_data["max_power"],
                constraint_data["min_power"],
                constraint_data.get("valid_from"),
                constraint_data.get("valid_to"),
                constraint_data["priority"],
                constraint_data.get("description"),
                datetime.now(timezone.utc),
            )

    async def remove_grid_constraint(self, constraint_id: str) -> None:
        """Remove grid constraint."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM grid_constraints WHERE constraint_id = $1
            """,
                constraint_id,
            )

    async def get_active_grid_constraints(self, location: str) -> List[Dict[str, Any]]:
        """Get active grid constraints for location."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT constraint_id, constraint_type, location, max_power, min_power,
                       valid_from, valid_to, priority, description
                FROM grid_constraints
                WHERE location = $1 AND active = true
                AND (valid_from IS NULL OR valid_from <= $2)
                AND (valid_to IS NULL OR valid_to >= $2)
                ORDER BY priority DESC
            """,
                location,
                datetime.now(timezone.utc),
            )

            return [dict(row) for row in rows]

    async def store_demand_response_event(self, event_data: Dict[str, Any]) -> None:
        """Store demand response event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO demand_response_events (
                    event_id, signal, start_time, end_time, target_reduction,
                    target_increase, affected_stations, priority, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
                event_data["event_id"],
                event_data["signal"],
                event_data["start_time"],
                event_data["end_time"],
                event_data.get("target_reduction"),
                event_data.get("target_increase"),
                event_data["affected_stations"],
                event_data["priority"],
                datetime.now(timezone.utc),
            )

    async def get_station_evse_ids(self, station_id: str) -> List[int]:
        """Get all EVSE IDs for station."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT evse_id FROM evse_capabilities
                WHERE station_id = $1 ORDER BY evse_id
            """,
                station_id,
            )

            return [row["evse_id"] for row in rows]

    async def get_evse_priority(self, station_id: str, evse_id: int) -> Optional[str]:
        """Get EVSE priority."""
        async with self.pg_pool.acquire() as conn:
            priority = await conn.fetchval(
                """
                SELECT priority FROM evse_priorities
                WHERE station_id = $1 AND evse_id = $2
            """,
                station_id,
                evse_id,
            )

            return priority

    async def get_evse_price_sensitivity(self, station_id: str, evse_id: int) -> Optional[float]:
        """Get EVSE price sensitivity."""
        async with self.pg_pool.acquire() as conn:
            sensitivity = await conn.fetchval(
                """
                SELECT price_sensitivity FROM evse_configurations
                WHERE station_id = $1 AND evse_id = $2
            """,
                station_id,
                evse_id,
            )

            return sensitivity

    async def get_current_evse_power(self, station_id: str, evse_id: int) -> Optional[float]:
        """Get current EVSE power consumption."""
        async with self.pg_pool.acquire() as conn:
            power = await conn.fetchval(
                """
                SELECT power_kw FROM telemetry
                WHERE station_id = $1 AND evse_id = $2
                ORDER BY time DESC LIMIT 1
            """,
                station_id,
                evse_id,
            )

            return power

    async def get_current_electricity_prices(self, station_id: str) -> Dict[str, float]:
        """Get current electricity prices."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT current_price, forecast_prices FROM electricity_prices
                WHERE station_id = $1
                ORDER BY timestamp DESC LIMIT 1
            """,
                station_id,
            )

            if row:
                return {
                    "current": row["current_price"],
                    "forecast": json.loads(row.get("forecast_prices", "{}")),
                }

            return {}

    async def get_evse_v2g_capability(self, station_id: str, evse_id: int) -> Optional[bool]:
        """Get EVSE V2G capability."""
        async with self.pg_pool.acquire() as conn:
            v2g_capable = await conn.fetchval(
                """
                SELECT v2g_capable FROM evse_capabilities
                WHERE station_id = $1 AND evse_id = $2
            """,
                station_id,
                evse_id,
            )

            return v2g_capable

    async def update_evse_power_limit(
        self, station_id: str, evse_id: int, multiplier: float
    ) -> None:
        """Update EVSE power limit."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE evse_capabilities SET
                    current_power_limit = max_power * $3,
                    updated_at = $4
                WHERE station_id = $1 AND evse_id = $2
            """,
                station_id,
                evse_id,
                multiplier,
                datetime.now(timezone.utc),
            )

    # ===== DIAGNOSTICS & FIRMWARE METHODS =====

    async def store_log_request(self, log_data: Dict[str, Any]) -> None:
        """Store log request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO log_requests (
                    station_id, log_type, request_id, retry_count, retry_interval,
                    status, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                log_data["station_id"],
                log_data["log_type"],
                log_data["request_id"],
                log_data["retry_count"],
                log_data["retry_interval"],
                log_data["status"],
                log_data["created_at"],
            )

    async def update_log_request_status(
        self, station_id: str, request_id: int, status: str, additional_info: Optional[str] = None
    ) -> None:
        """Update log request status."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE log_requests SET
                    status = $3, additional_info = $4, updated_at = $5
                WHERE station_id = $1 AND request_id = $2
            """,
                station_id,
                request_id,
                status,
                additional_info,
                datetime.now(timezone.utc),
            )

    async def store_log_file(self, log_file_data: Dict[str, Any]) -> None:
        """Store log file information."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO log_files (
                    station_id, request_id, log_type, file_path, file_size, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                log_file_data["station_id"],
                log_file_data["request_id"],
                log_file_data["log_type"],
                log_file_data["file_path"],
                log_file_data["file_size"],
                log_file_data["created_at"],
            )

    async def store_notify_event(self, event_data: Dict[str, Any]) -> None:
        """Store notify event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notify_events (
                    station_id, event_type, timestamp, tech_info, additional_info, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                event_data["station_id"],
                event_data["event_type"],
                event_data["timestamp"],
                event_data.get("tech_info"),
                event_data.get("additional_info"),
                datetime.now(timezone.utc),
            )

    async def get_diagnostic_logs(self, station_id: str) -> List[Dict[str, Any]]:
        """Get diagnostic logs."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, level, message, component, event_type, additional_info
                FROM diagnostic_logs
                WHERE station_id = $1
                ORDER BY timestamp DESC
                LIMIT 10000
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_security_logs(self, station_id: str) -> List[Dict[str, Any]]:
        """Get security logs."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, event_type, tech_info, additional_info
                FROM security_events
                WHERE station_id = $1
                ORDER BY timestamp DESC
                LIMIT 10000
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_firmware_status_logs(self, station_id: str) -> List[Dict[str, Any]]:
        """Get firmware status logs."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, status, additional_info
                FROM firmware_status_logs
                WHERE station_id = $1
                ORDER BY timestamp DESC
                LIMIT 10000
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_local_list_logs(self, station_id: str) -> List[Dict[str, Any]]:
        """Get local list logs."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, action, id_token, additional_info
                FROM local_list_logs
                WHERE station_id = $1
                ORDER BY timestamp DESC
                LIMIT 10000
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_supported_log_types(self, station_id: str) -> List[str]:
        """Get supported log types for station."""
        async with self.pg_pool.acquire() as conn:
            supported_types = await conn.fetchval(
                """
                SELECT supported_log_types FROM station_capabilities
                WHERE station_id = $1
            """,
                station_id,
            )

            return supported_types.split(",") if supported_types else []

    async def store_firmware_request(self, firmware_data: Dict[str, Any]) -> None:
        """Store firmware request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO firmware_requests (
                    station_id, request_id, location, retrieve_date_time, retry_interval,
                    retries, retry_back_off_random_range, checksum, checksum_algorithm,
                    signing_certificate, signature, signing_certificate_chain,
                    request_start_time, request_stop_time, status, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
                firmware_data["station_id"],
                firmware_data["request_id"],
                firmware_data["location"],
                firmware_data["retrieve_date_time"],
                firmware_data.get("retry_interval"),
                firmware_data.get("retries"),
                firmware_data.get("retry_back_off_random_range"),
                firmware_data.get("checksum"),
                firmware_data.get("checksum_algorithm"),
                firmware_data.get("signing_certificate"),
                firmware_data.get("signature"),
                firmware_data.get("signing_certificate_chain"),
                firmware_data.get("request_start_time"),
                firmware_data.get("request_stop_time"),
                firmware_data["status"],
                firmware_data["created_at"],
            )

    async def update_firmware_request_status(
        self, station_id: str, request_id: int, status: str, additional_info: Optional[str] = None
    ) -> None:
        """Update firmware request status."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE firmware_requests SET
                    status = $3, additional_info = $4, updated_at = $5
                WHERE station_id = $1 AND request_id = $2
            """,
                station_id,
                request_id,
                status,
                additional_info,
                datetime.now(timezone.utc),
            )

    async def get_firmware_request_status(self, station_id: str, request_id: int) -> Optional[str]:
        """Get firmware request status."""
        async with self.pg_pool.acquire() as conn:
            status = await conn.fetchval(
                """
                SELECT status FROM firmware_requests
                WHERE station_id = $1 AND request_id = $2
            """,
                station_id,
                request_id,
            )

            return status

    async def get_firmware_request_info(
        self, station_id: str, request_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        """Get firmware request information."""
        async with self.pg_pool.acquire() as conn:
            if request_id:
                row = await conn.fetchrow(
                    """
                    SELECT location, retrieve_date_time, checksum, checksum_algorithm,
                           signing_certificate, signature, status
                    FROM firmware_requests
                    WHERE station_id = $1 AND request_id = $2
                """,
                    station_id,
                    request_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT location, retrieve_date_time, checksum, checksum_algorithm,
                           signing_certificate, signature, status
                    FROM firmware_requests
                    WHERE station_id = $1
                    ORDER BY created_at DESC LIMIT 1
                """,
                    station_id,
                )

            return dict(row) if row else None

    async def cancel_firmware_request(self, station_id: str, checksum: str) -> None:
        """Cancel firmware request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE firmware_requests SET
                    status = 'Cancelled', updated_at = $3
                WHERE station_id = $1 AND checksum = $2
            """,
                station_id,
                checksum,
                datetime.now(timezone.utc),
            )

    async def store_firmware_update_event(self, event_data: Dict[str, Any]) -> None:
        """Store firmware update event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO firmware_update_events (
                    station_id, tech_info, additional_info, timestamp, created_at
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                event_data["station_id"],
                event_data.get("tech_info"),
                event_data.get("additional_info"),
                event_data["timestamp"],
                datetime.now(timezone.utc),
            )

    async def store_firmware_failure_event(self, event_data: Dict[str, Any]) -> None:
        """Store firmware failure event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO firmware_failure_events (
                    station_id, request_id, failure_type, timestamp, created_at
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                event_data["station_id"],
                event_data.get("request_id"),
                event_data["failure_type"],
                event_data["timestamp"],
                datetime.now(timezone.utc),
            )

    async def update_station_firmware_version(self, station_id: str, version: str) -> None:
        """Update station firmware version."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE device_variables SET
                    value = $3, updated_at = $4
                WHERE station_id = $1 AND component_name = 'ChargingStation' 
                AND variable_name = 'FirmwareVersion' AND attribute_type = 'Actual'
            """,
                station_id,
                version,
                datetime.now(timezone.utc),
            )

    async def store_reset_event(self, event_data: Dict[str, Any]) -> None:
        """Store reset event."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reset_events (
                    station_id, reset_type, tech_info, timestamp, created_at
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                event_data["station_id"],
                event_data["reset_type"],
                event_data.get("tech_info"),
                event_data["timestamp"],
                datetime.now(timezone.utc),
            )

    # ===== MONITORING & ALERTING METHODS =====

    async def store_monitoring_report(self, report_data: Dict[str, Any]) -> None:
        """Store monitoring report."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO monitoring_reports (
                    station_id, request_id, monitoring_base, monitoring_criteria,
                    component_variable, generated_at, tbc, seq_no, report_data, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                report_data["station_id"],
                report_data["request_id"],
                report_data["monitoring_base"],
                report_data.get("monitoring_criteria"),
                report_data.get("component_variable"),
                report_data["generated_at"],
                report_data["tbc"],
                report_data["seq_no"],
                report_data["report_data"],
                report_data["created_at"],
            )

    async def store_notified_monitoring_report(self, report_data: Dict[str, Any]) -> None:
        """Store notified monitoring report."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notified_monitoring_reports (
                    station_id, request_id, generated_at, tbc, seq_no, report_data, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                report_data["station_id"],
                report_data["request_id"],
                report_data["generated_at"],
                report_data["tbc"],
                report_data["seq_no"],
                report_data["report_data"],
                report_data["created_at"],
            )

    async def store_variable_monitoring(self, monitoring_data: Dict[str, Any]) -> None:
        """Store variable monitoring configuration."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO variable_monitoring (
                    station_id, component_name, component_instance, variable_name,
                    variable_instance, monitoring_criterion, severity, threshold,
                    delta, period, enabled, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (station_id, component_name, component_instance, variable_name, variable_instance)
                DO UPDATE SET monitoring_criterion = $6, severity = $7, threshold = $8,
                             delta = $9, period = $10, enabled = $11, updated_at = $12
            """,
                monitoring_data["station_id"],
                monitoring_data["component_name"],
                monitoring_data["component_instance"],
                monitoring_data["variable_name"],
                monitoring_data["variable_instance"],
                monitoring_data["monitoring_criterion"],
                monitoring_data["severity"],
                monitoring_data.get("threshold"),
                monitoring_data.get("delta"),
                monitoring_data.get("period"),
                monitoring_data["enabled"],
                monitoring_data["created_at"],
            )

    async def remove_variable_monitoring(
        self,
        station_id: str,
        component_name: str,
        component_instance: str,
        variable_name: str,
        variable_instance: str,
    ) -> None:
        """Remove variable monitoring."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM variable_monitoring
                WHERE station_id = $1 AND component_name = $2 AND component_instance = $3
                AND variable_name = $4 AND variable_instance = $5
            """,
                station_id,
                component_name,
                component_instance,
                variable_name,
                variable_instance,
            )

    async def remove_all_variable_monitoring(self, station_id: str) -> None:
        """Remove all variable monitoring for station."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM variable_monitoring WHERE station_id = $1
            """,
                station_id,
            )

    async def get_configuration_variables(self, station_id: str) -> List[Dict[str, Any]]:
        """Get configuration variables."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT component_name, component_instance, variable_name, variable_instance,
                       actual_value, target_value, default_value
                FROM device_variables
                WHERE station_id = $1 AND component_name IN ('ChargingStation', 'EVSE', 'Connector')
                ORDER BY component_name, variable_name
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_all_variables(self, station_id: str) -> List[Dict[str, Any]]:
        """Get all variables."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT component_name, component_instance, variable_name, variable_instance,
                       actual_value, target_value, default_value, min_set_value, max_set_value
                FROM device_variables WHERE station_id = $1
                ORDER BY component_name, variable_name
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_inventory_summary(self, station_id: str) -> List[Dict[str, Any]]:
        """Get inventory summary."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT component_name, instance, COUNT(*) as variable_count,
                       MAX(updated_at) as last_updated
                FROM device_variables WHERE station_id = $1
                GROUP BY component_name, instance
                ORDER BY component_name, instance
            """,
                station_id,
            )

            return [dict(row) for row in rows]

    async def get_variable_monitoring_config(
        self, component_name: str, variable_name: str, monitoring_criterion: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Get variable monitoring configuration."""
        async with self.pg_pool.acquire() as conn:
            if monitoring_criterion:
                row = await conn.fetchrow(
                    """
                    SELECT station_id, component_name, component_instance, variable_name,
                           variable_instance, monitoring_criterion, severity, threshold,
                           delta, period, enabled
                    FROM variable_monitoring
                    WHERE component_name = $1 AND variable_name = $2 
                    AND monitoring_criterion = $3 AND enabled = true
                    LIMIT 1
                """,
                    component_name,
                    variable_name,
                    monitoring_criterion,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT station_id, component_name, component_instance, variable_name,
                           variable_instance, monitoring_criterion, severity, threshold,
                           delta, period, enabled
                    FROM variable_monitoring
                    WHERE component_name = $1 AND variable_name = $2 AND enabled = true
                    LIMIT 1
                """,
                    component_name,
                    variable_name,
                )

            return dict(row) if row else None

    async def get_all_active_variable_monitoring(self) -> List[Dict[str, Any]]:
        """Get all active variable monitoring."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT station_id, component_name, component_instance, variable_name,
                       variable_instance, monitoring_criterion, severity, threshold,
                       delta, period
                FROM variable_monitoring WHERE enabled = true
            """)

            return [dict(row) for row in rows]

    async def get_current_variable_value(
        self, station_id: str, component_name: str, variable_name: str
    ) -> Optional[Any]:
        """Get current variable value."""
        async with self.pg_pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT actual_value FROM device_variables
                WHERE station_id = $1 AND component_name = $2 AND variable_name = $3
                ORDER BY updated_at DESC LIMIT 1
            """,
                station_id,
                component_name,
                variable_name,
            )

            return value

    async def get_previous_variable_value(
        self, station_id: str, component_name: str, variable_name: str
    ) -> Optional[Any]:
        """Get previous variable value."""
        async with self.pg_pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT actual_value FROM device_variables
                WHERE station_id = $1 AND component_name = $2 AND variable_name = $3
                ORDER BY updated_at DESC LIMIT 1 OFFSET 1
            """,
                station_id,
                component_name,
                variable_name,
            )

            return value

    async def store_periodic_monitoring_data(self, data: Dict[str, Any]) -> None:
        """Store periodic monitoring data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO periodic_monitoring_data (
                    station_id, component_name, variable_name, value, timestamp, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
                data["station_id"],
                data["component_name"],
                data["variable_name"],
                data["value"],
                data["timestamp"],
                datetime.now(timezone.utc),
            )

    async def store_alert_rule(self, rule_data: Dict[str, Any]) -> None:
        """Store alert rule."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert_rules (
                    rule_id, name, description, component_name, variable_name,
                    condition, threshold, severity, enabled, cooldown_minutes,
                    notification_channels, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (rule_id)
                DO UPDATE SET name = $2, description = $3, component_name = $4,
                             variable_name = $5, condition = $6, threshold = $7,
                             severity = $8, enabled = $9, cooldown_minutes = $10,
                             notification_channels = $11, updated_at = $12
            """,
                rule_data["rule_id"],
                rule_data["name"],
                rule_data["description"],
                rule_data["component_name"],
                rule_data["variable_name"],
                rule_data["condition"],
                rule_data["threshold"],
                rule_data["severity"],
                rule_data["enabled"],
                rule_data["cooldown_minutes"],
                rule_data["notification_channels"],
                rule_data["created_at"],
            )

    async def remove_alert_rule(self, rule_id: str) -> None:
        """Remove alert rule."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM alert_rules WHERE rule_id = $1
            """,
                rule_id,
            )

    async def get_enabled_alert_rules(self) -> List[Dict[str, Any]]:
        """Get enabled alert rules."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT rule_id, name, description, component_name, variable_name,
                       condition, threshold, severity, cooldown_minutes
                FROM alert_rules WHERE enabled = true
            """)

            return [dict(row) for row in rows]

    async def get_alert_rules_for_variable(
        self, component_name: str, variable_name: str
    ) -> List[Dict[str, Any]]:
        """Get alert rules for variable."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT rule_id, name, condition, threshold, severity, cooldown_minutes
                FROM alert_rules
                WHERE component_name = $1 AND variable_name = $2 AND enabled = true
            """,
                component_name,
                variable_name,
            )

            return [dict(row) for row in rows]

    async def get_stations_with_component_variable(
        self, component_name: str, variable_name: str
    ) -> List[Dict[str, Any]]:
        """Get stations with component variable."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT station_id, actual_value as value
                FROM device_variables
                WHERE component_name = $1 AND variable_name = $2
                ORDER BY station_id
            """,
                component_name,
                variable_name,
            )

            return [dict(row) for row in rows]

    async def store_alert(self, alert_data: Dict[str, Any]) -> None:
        """Store alert."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alerts (
                    alert_id, rule_id, station_id, component_name, variable_name,
                    current_value, threshold_value, severity, status, message,
                    triggered_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
                alert_data["alert_id"],
                alert_data["rule_id"],
                alert_data["station_id"],
                alert_data["component_name"],
                alert_data["variable_name"],
                alert_data["current_value"],
                alert_data["threshold_value"],
                alert_data["severity"],
                alert_data["status"],
                alert_data["message"],
                alert_data["triggered_at"],
                alert_data["created_at"],
            )

    async def get_active_alerts(
        self, station_id: Optional[str] = None, severity: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get active alerts."""
        async with self.pg_pool.acquire() as conn:
            query = """
                SELECT alert_id, rule_id, station_id, component_name, variable_name,
                       current_value, threshold_value, severity, status, message,
                       triggered_at, acknowledged_at, resolved_at, additional_info
                FROM alerts WHERE status = 'Active'
            """
            params = []

            if station_id:
                query += " AND station_id = $" + str(len(params) + 1)
                params.append(station_id)

            if severity:
                query += " AND severity = $" + str(len(params) + 1)
                params.append(severity)

            query += " ORDER BY triggered_at DESC"

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def acknowledge_alert(
        self, alert_id: str, acknowledged_by: str, acknowledged_at: datetime
    ) -> None:
        """Acknowledge alert."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE alerts SET
                    status = 'Acknowledged', acknowledged_at = $2, acknowledged_by = $3
                WHERE alert_id = $1
            """,
                alert_id,
                acknowledged_at,
                acknowledged_by,
            )

    async def resolve_alert(self, alert_id: str, resolved_by: str, resolved_at: datetime) -> None:
        """Resolve alert."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE alerts SET
                    status = 'Resolved', resolved_at = $2, resolved_by = $3
                WHERE alert_id = $1
            """,
                alert_id,
                resolved_at,
                resolved_by,
            )

    async def cleanup_old_alerts(self, cutoff_date: datetime) -> None:
        """Clean up old alerts."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM alerts WHERE triggered_at < $1
            """,
                cutoff_date,
            )

    # ===== DISPLAY MESSAGE MANAGEMENT =====

    async def store_display_message(self, message_data: Dict[str, Any]) -> None:
        """Store display message."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO display_messages (
                    message_id, station_id, evse_id, connector_id, message_type,
                    message_content, language, priority, state, valid_from, valid_to,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (message_id)
                DO UPDATE SET
                    message_type = EXCLUDED.message_type,
                    message_content = EXCLUDED.message_content,
                    language = EXCLUDED.language,
                    priority = EXCLUDED.priority,
                    state = EXCLUDED.state,
                    valid_from = EXCLUDED.valid_from,
                    valid_to = EXCLUDED.valid_to,
                    updated_at = EXCLUDED.updated_at
            """,
                message_data["message_id"],
                message_data["station_id"],
                message_data.get("evse_id"),
                message_data.get("connector_id"),
                message_data["message_type"],
                message_data["message_content"],
                message_data.get("language", "en"),
                message_data.get("priority", 0),
                message_data.get("state", "active"),
                message_data.get("valid_from"),
                message_data.get("valid_to"),
                message_data["created_at"],
                message_data["updated_at"],
            )

    async def get_display_messages(
        self,
        station_id: str,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
        state: str = "active",
    ) -> List[Dict[str, Any]]:
        """Get display messages for station."""
        async with self.pg_pool.acquire() as conn:
            query = """
                SELECT * FROM display_messages
                WHERE station_id = $1 AND state = $2
            """
            params = [station_id, state]

            if evse_id is not None:
                query += " AND (evse_id = $" + str(len(params) + 1) + " OR evse_id IS NULL)"
                params.append(evse_id)

            if connector_id is not None:
                query += (
                    " AND (connector_id = $" + str(len(params) + 1) + " OR connector_id IS NULL)"
                )
                params.append(connector_id)

            query += " ORDER BY priority DESC, created_at DESC"

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def clear_display_message(self, message_id: str) -> None:
        """Clear display message."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE display_messages
                SET state = 'inactive', updated_at = NOW()
                WHERE message_id = $1
            """,
                message_id,
            )

    async def clear_all_display_messages(
        self, station_id: str, evse_id: Optional[int] = None, connector_id: Optional[int] = None
    ) -> None:
        """Clear all display messages for station/EVSE/connector."""
        async with self.pg_pool.acquire() as conn:
            query = """
                UPDATE display_messages
                SET state = 'inactive', updated_at = NOW()
                WHERE station_id = $1 AND state = 'active'
            """
            params = [station_id]

            if evse_id is not None:
                query += " AND (evse_id = $" + str(len(params) + 1) + " OR evse_id IS NULL)"
                params.append(evse_id)

            if connector_id is not None:
                query += (
                    " AND (connector_id = $" + str(len(params) + 1) + " OR connector_id IS NULL)"
                )
                params.append(connector_id)

            await conn.execute(query, *params)

    async def store_display_message_history(self, history_data: Dict[str, Any]) -> None:
        """Store display message history."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO display_message_history (
                    message_id, station_id, action, timestamp, details
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                history_data["message_id"],
                history_data["station_id"],
                history_data["action"],
                history_data["timestamp"],
                json.dumps(history_data.get("details", {})),
            )

    async def get_display_message_history(
        self,
        station_id: str,
        message_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get display message history."""
        async with self.pg_pool.acquire() as conn:
            query = """
                SELECT * FROM display_message_history
                WHERE station_id = $1
            """
            params = [station_id]

            if message_id:
                query += " AND message_id = $" + str(len(params) + 1)
                params.append(message_id)

            if start_time:
                query += " AND timestamp >= $" + str(len(params) + 1)
                params.append(start_time)

            if end_time:
                query += " AND timestamp <= $" + str(len(params) + 1)
                params.append(end_time)

            query += " ORDER BY timestamp DESC"

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def cleanup_expired_display_messages(self) -> None:
        """Clean up expired display messages."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute("""
                UPDATE display_messages
                SET state = 'expired', updated_at = NOW()
                WHERE state = 'active' AND valid_to IS NOT NULL AND valid_to < NOW()
            """)

    # ===== TARIFF AND COST MANAGEMENT =====

    async def store_tariff(self, tariff_data: Dict[str, Any]) -> None:  # noqa: F811
        """Store tariff."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tariffs (
                    tariff_id, station_id, tariff_description, tariff_currency,
                    tariff_priority, valid_from, valid_to, tariff_data,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (tariff_id)
                DO UPDATE SET
                    tariff_description = EXCLUDED.tariff_description,
                    tariff_currency = EXCLUDED.tariff_currency,
                    tariff_priority = EXCLUDED.tariff_priority,
                    valid_from = EXCLUDED.valid_from,
                    valid_to = EXCLUDED.valid_to,
                    tariff_data = EXCLUDED.tariff_data,
                    updated_at = EXCLUDED.updated_at
            """,
                tariff_data["tariff_id"],
                tariff_data["station_id"],
                tariff_data.get("tariff_description"),
                tariff_data.get("tariff_currency", "USD"),
                tariff_data.get("tariff_priority", 0),
                tariff_data.get("valid_from"),
                tariff_data.get("valid_to"),
                json.dumps(tariff_data["tariff_data"]),
                tariff_data["created_at"],
                tariff_data["updated_at"],
            )

    async def get_active_tariffs(
        self, station_id: str, timestamp: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get active tariffs for station."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM tariffs
                WHERE station_id = $1
                AND (valid_from IS NULL OR valid_from <= $2)
                AND (valid_to IS NULL OR valid_to > $2)
                ORDER BY tariff_priority DESC, created_at DESC
            """,
                station_id,
                timestamp,
            )

            return [dict(row) for row in rows]

    async def store_tariff_element(self, element_data: Dict[str, Any]) -> None:
        """Store tariff element."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tariff_elements (
                    element_id, tariff_id, element_type, price_per_unit,
                    currency, unit, valid_from, valid_to, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (element_id)
                DO UPDATE SET
                    element_type = EXCLUDED.element_type,
                    price_per_unit = EXCLUDED.price_per_unit,
                    currency = EXCLUDED.currency,
                    unit = EXCLUDED.unit,
                    valid_from = EXCLUDED.valid_from,
                    valid_to = EXCLUDED.valid_to
            """,
                element_data["element_id"],
                element_data["tariff_id"],
                element_data["element_type"],
                element_data["price_per_unit"],
                element_data.get("currency", "USD"),
                element_data["unit"],
                element_data.get("valid_from"),
                element_data.get("valid_to"),
                element_data["created_at"],
            )

    async def get_tariff_elements(
        self, tariff_id: str, timestamp: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get tariff elements."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM tariff_elements
                WHERE tariff_id = $1
                AND (valid_from IS NULL OR valid_from <= $2)
                AND (valid_to IS NULL OR valid_to > $2)
                ORDER BY element_type, created_at
            """,
                tariff_id,
                timestamp,
            )

            return [dict(row) for row in rows]

    async def store_tou_period(self, period_data: Dict[str, Any]) -> None:
        """Store time-of-use period."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tou_periods (
                    period_id, tariff_id, period_name, start_time, end_time,
                    day_of_week, month, day_of_month, price_multiplier, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (period_id)
                DO UPDATE SET
                    period_name = EXCLUDED.period_name,
                    start_time = EXCLUDED.start_time,
                    end_time = EXCLUDED.end_time,
                    day_of_week = EXCLUDED.day_of_week,
                    month = EXCLUDED.month,
                    day_of_month = EXCLUDED.day_of_month,
                    price_multiplier = EXCLUDED.price_multiplier
            """,
                period_data["period_id"],
                period_data["tariff_id"],
                period_data["period_name"],
                period_data["start_time"],
                period_data["end_time"],
                period_data.get("day_of_week"),
                period_data.get("month"),
                period_data.get("day_of_month"),
                period_data.get("price_multiplier", 1.0),
                period_data["created_at"],
            )

    async def get_tou_periods(
        self, tariff_id: str, timestamp: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get time-of-use periods for tariff."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM tou_periods
                WHERE tariff_id = $1
                ORDER BY day_of_week, start_time
            """,
                tariff_id,
            )

            return [dict(row) for row in rows]

    async def store_cost_update(self, cost_data: Dict[str, Any]) -> None:
        """Store cost update."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cost_updates (
                    update_id, station_id, transaction_id, evse_id, connector_id,
                    total_cost, currency, cost_breakdown, calculated_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                cost_data["update_id"],
                cost_data["station_id"],
                cost_data.get("transaction_id"),
                cost_data.get("evse_id"),
                cost_data.get("connector_id"),
                cost_data["total_cost"],
                cost_data.get("currency", "USD"),
                json.dumps(cost_data.get("cost_breakdown", {})),
                cost_data["calculated_at"],
                cost_data["created_at"],
            )

    async def get_cost_updates(
        self,
        station_id: str,
        transaction_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get cost updates."""
        async with self.pg_pool.acquire() as conn:
            query = """
                SELECT * FROM cost_updates
                WHERE station_id = $1
            """
            params = [station_id]

            if transaction_id:
                query += " AND transaction_id = $" + str(len(params) + 1)
                params.append(transaction_id)

            if start_time:
                query += " AND calculated_at >= $" + str(len(params) + 1)
                params.append(start_time)

            if end_time:
                query += " AND calculated_at <= $" + str(len(params) + 1)
                params.append(end_time)

            query += " ORDER BY calculated_at DESC"

            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def calculate_tou_multiplier(self, tariff_id: str, timestamp: datetime) -> float:
        """Calculate time-of-use multiplier for given timestamp."""
        periods = await self.get_tou_periods(tariff_id, timestamp)

        current_time = timestamp.time()
        current_weekday = timestamp.weekday()  # 0=Monday, 6=Sunday
        current_month = timestamp.month
        current_day = timestamp.day

        for period in periods:
            # Check if period matches current time
            if (
                period["start_time"] <= current_time <= period["end_time"]
                and (period["day_of_week"] is None or period["day_of_week"] == current_weekday)
                and (period["month"] is None or period["month"] == current_month)
                and (period["day_of_month"] is None or period["day_of_month"] == current_day)
            ):
                return float(period["price_multiplier"])

        return 1.0  # Default multiplier

    # ===== GDPR COMPLIANCE =====

    async def store_customer_information_request(self, request_data: Dict[str, Any]) -> None:
        """Store customer information request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO customer_information (
                    request_id, station_id, customer_certificate_id, id_token,
                    customer_identifier, request_type, status, requested_at,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                request_data["request_id"],
                request_data["station_id"],
                request_data.get("customer_certificate_id"),
                request_data.get("id_token"),
                request_data.get("customer_identifier"),
                request_data["request_type"],
                request_data.get("status", "pending"),
                request_data["requested_at"],
                request_data["created_at"],
                request_data["updated_at"],
            )

    async def get_customer_information_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get customer information request."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM customer_information WHERE request_id = $1
            """,
                request_id,
            )
            return dict(row) if row else None

    async def update_customer_information_status(
        self, request_id: str, status: str, error_message: Optional[str] = None
    ) -> None:
        """Update customer information request status."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE customer_information
                SET status = $1, error_message = $2, updated_at = NOW()
                WHERE request_id = $3
            """,
                status,
                error_message,
                request_id,
            )

    async def store_data_retention_policy(self, policy_data: Dict[str, Any]) -> None:
        """Store data retention policy."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO data_retention_policies (
                    policy_id, data_type, retention_period_days, anonymization_required,
                    deletion_method, policy_description, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (policy_id)
                DO UPDATE SET
                    retention_period_days = EXCLUDED.retention_period_days,
                    anonymization_required = EXCLUDED.anonymization_required,
                    deletion_method = EXCLUDED.deletion_method,
                    policy_description = EXCLUDED.policy_description,
                    updated_at = EXCLUDED.updated_at
            """,
                policy_data["policy_id"],
                policy_data["data_type"],
                policy_data["retention_period_days"],
                policy_data.get("anonymization_required", False),
                policy_data.get("deletion_method", "soft"),
                policy_data.get("policy_description"),
                policy_data["created_at"],
                policy_data["updated_at"],
            )

    async def get_data_retention_policies(
        self, data_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get data retention policies."""
        async with self.pg_pool.acquire() as conn:
            if data_type:
                rows = await conn.fetch(
                    """
                    SELECT * FROM data_retention_policies WHERE data_type = $1
                    ORDER BY created_at DESC
                """,
                    data_type,
                )
            else:
                rows = await conn.fetch("""
                    SELECT * FROM data_retention_policies ORDER BY created_at DESC
                """)
            return [dict(row) for row in rows]

    async def store_consent_record(self, consent_data: Dict[str, Any]) -> None:
        """Store consent record."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO consent_records (
                    consent_id, customer_identifier, consent_type, consent_status,
                    consent_date, revocation_date, expiry_date, consent_method,
                    consent_source, legal_basis, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (consent_id)
                DO UPDATE SET
                    consent_status = EXCLUDED.consent_status,
                    revocation_date = EXCLUDED.revocation_date,
                    expiry_date = EXCLUDED.expiry_date,
                    updated_at = EXCLUDED.updated_at
            """,
                consent_data["consent_id"],
                consent_data["customer_identifier"],
                consent_data["consent_type"],
                consent_data["consent_status"],
                consent_data["consent_date"],
                consent_data.get("revocation_date"),
                consent_data.get("expiry_date"),
                consent_data.get("consent_method"),
                consent_data.get("consent_source"),
                consent_data.get("legal_basis"),
                consent_data["created_at"],
                consent_data["updated_at"],
            )

    async def get_consent_records(
        self, customer_identifier: str, consent_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get consent records for customer."""
        async with self.pg_pool.acquire() as conn:
            if consent_type:
                rows = await conn.fetch(
                    """
                    SELECT * FROM consent_records
                    WHERE customer_identifier = $1 AND consent_type = $2
                    ORDER BY consent_date DESC
                """,
                    customer_identifier,
                    consent_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM consent_records
                    WHERE customer_identifier = $1
                    ORDER BY consent_date DESC
                """,
                    customer_identifier,
                )
            return [dict(row) for row in rows]

    async def store_pii_anonymization_log(self, log_data: Dict[str, Any]) -> None:
        """Store PII anonymization log."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pii_anonymization_log (
                    log_id, customer_identifier, data_type, anonymization_method,
                    original_value_hash, anonymized_value, anonymization_date,
                    retention_policy_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
                log_data["log_id"],
                log_data["customer_identifier"],
                log_data["data_type"],
                log_data["anonymization_method"],
                log_data.get("original_value_hash"),
                log_data.get("anonymized_value"),
                log_data["anonymization_date"],
                log_data.get("retention_policy_id"),
                log_data["created_at"],
            )

    async def get_pii_anonymization_log(
        self, customer_identifier: str, data_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get PII anonymization log."""
        async with self.pg_pool.acquire() as conn:
            if data_type:
                rows = await conn.fetch(
                    """
                    SELECT * FROM pii_anonymization_log
                    WHERE customer_identifier = $1 AND data_type = $2
                    ORDER BY anonymization_date DESC
                """,
                    customer_identifier,
                    data_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM pii_anonymization_log
                    WHERE customer_identifier = $1
                    ORDER BY anonymization_date DESC
                """,
                    customer_identifier,
                )
            return [dict(row) for row in rows]

    async def store_data_subject_request(self, request_data: Dict[str, Any]) -> None:
        """Store data subject rights request."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO data_subject_requests (
                    request_id, customer_identifier, request_type, request_status,
                    request_date, completion_date, verification_method, verification_status,
                    request_details, response_data, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
                request_data["request_id"],
                request_data["customer_identifier"],
                request_data["request_type"],
                request_data.get("request_status", "pending"),
                request_data["request_date"],
                request_data.get("completion_date"),
                request_data.get("verification_method"),
                request_data.get("verification_status", "pending"),
                json.dumps(request_data.get("request_details", {})),
                json.dumps(request_data.get("response_data", {})),
                request_data["created_at"],
                request_data["updated_at"],
            )

    async def get_data_subject_requests(
        self, customer_identifier: str, request_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get data subject rights requests."""
        async with self.pg_pool.acquire() as conn:
            if request_type:
                rows = await conn.fetch(
                    """
                    SELECT * FROM data_subject_requests
                    WHERE customer_identifier = $1 AND request_type = $2
                    ORDER BY request_date DESC
                """,
                    customer_identifier,
                    request_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM data_subject_requests
                    WHERE customer_identifier = $1
                    ORDER BY request_date DESC
                """,
                    customer_identifier,
                )
            return [dict(row) for row in rows]

    async def anonymize_customer_data(self, customer_identifier: str, data_type: str) -> None:
        """Anonymize customer data based on retention policy."""
        async with self.pg_pool.acquire() as conn:
            # Get retention policy for data type
            policy = await conn.fetchrow(
                """
                SELECT * FROM data_retention_policies WHERE data_type = $1
            """,
                data_type,
            )

            if not policy:
                return

            # Anonymize based on policy
            if policy["deletion_method"] == "anonymize":
                # Update records to anonymized values
                await conn.execute(
                    """
                    UPDATE transactions
                    SET id_token = 'ANONYMIZED', customer_id = 'ANONYMIZED'
                    WHERE customer_id = $1
                """,
                    customer_identifier,
                )

                await conn.execute(
                    """
                    UPDATE transaction_events
                    SET id_token = 'ANONYMIZED'
                    WHERE id_token = $1
                """,
                    customer_identifier,
                )

                # Log anonymization
                log_data = {
                    "log_id": str(uuid.uuid4()),
                    "customer_identifier": customer_identifier,
                    "data_type": data_type,
                    "anonymization_method": "anonymize",
                    "anonymization_date": datetime.now(timezone.utc),
                    "retention_policy_id": policy["policy_id"],
                    "created_at": datetime.now(timezone.utc),
                }
                await self.store_pii_anonymization_log(log_data)

            elif policy["deletion_method"] == "hard":
                # Hard delete records
                await conn.execute(
                    """
                    DELETE FROM transactions WHERE customer_id = $1
                """,
                    customer_identifier,
                )

                await conn.execute(
                    """
                    DELETE FROM transaction_events WHERE id_token = $1
                """,
                    customer_identifier,
                )

                # Log deletion
                log_data = {
                    "log_id": str(uuid.uuid4()),
                    "customer_identifier": customer_identifier,
                    "data_type": data_type,
                    "anonymization_method": "delete",
                    "anonymization_date": datetime.now(timezone.utc),
                    "retention_policy_id": policy["policy_id"],
                    "created_at": datetime.now(timezone.utc),
                }
                await self.store_pii_anonymization_log(log_data)

    # ===== ERROR HANDLING AND RESILIENCE =====

    async def store_circuit_breaker_state(self, state_data: Dict[str, Any]) -> None:
        """Store circuit breaker state."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO circuit_breaker_states (
                    service_name, state, failure_count, last_failure_time,
                    last_success_time, failure_threshold, timeout_seconds,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (service_name)
                DO UPDATE SET
                    state = EXCLUDED.state,
                    failure_count = EXCLUDED.failure_count,
                    last_failure_time = EXCLUDED.last_failure_time,
                    last_success_time = EXCLUDED.last_success_time,
                    updated_at = EXCLUDED.updated_at
            """,
                state_data["service_name"],
                state_data["state"],
                state_data["failure_count"],
                state_data["last_failure_time"],
                state_data["last_success_time"],
                state_data["failure_threshold"],
                state_data["timeout_seconds"],
                state_data["created_at"],
                state_data["updated_at"],
            )

    async def get_circuit_breaker_state(self, service_name: str) -> Optional[Dict[str, Any]]:
        """Get circuit breaker state."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM circuit_breaker_states WHERE service_name = $1
            """,
                service_name,
            )
            return dict(row) if row else None

    async def store_dead_letter_message(self, message_data: Dict[str, Any]) -> None:
        """Store dead letter queue message."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    message_id, original_message, error_message, error_type,
                    retry_count, max_retries, next_retry_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
                message_data["message_id"],
                json.dumps(message_data["original_message"]),
                message_data["error_message"],
                message_data["error_type"],
                message_data["retry_count"],
                message_data["max_retries"],
                message_data["next_retry_at"],
                message_data["created_at"],
            )

    async def get_retryable_dlq_messages(self) -> List[Dict[str, Any]]:
        """Get messages ready for retry."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM dead_letter_queue
                WHERE next_retry_at <= NOW() AND processed_at IS NULL
                ORDER BY created_at ASC
            """)
            return [dict(row) for row in rows]

    async def update_dlq_message_retry(
        self, message_id: str, retry_count: int, next_retry_at: datetime
    ) -> None:
        """Update dead letter queue message retry info."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE dead_letter_queue
                SET retry_count = $1, next_retry_at = $2
                WHERE message_id = $3
            """,
                retry_count,
                next_retry_at,
                message_id,
            )

    async def mark_dlq_message_processed(self, message_id: str) -> None:
        """Mark dead letter queue message as processed."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE dead_letter_queue
                SET processed_at = NOW()
                WHERE message_id = $1
            """,
                message_id,
            )

    async def store_retry_attempt(self, attempt_data: Dict[str, Any]) -> None:
        """Store retry attempt."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO retry_attempts (
                    attempt_id, message_id, attempt_number, error_message,
                    attempt_time, success, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                attempt_data["attempt_id"],
                attempt_data["message_id"],
                attempt_data["attempt_number"],
                attempt_data["error_message"],
                attempt_data["attempt_time"],
                attempt_data["success"],
                attempt_data["created_at"],
            )

    async def store_health_check_result(self, result_data: Dict[str, Any]) -> None:
        """Store health check result."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO health_check_results (
                    check_id, service_name, check_type, status,
                    response_time_ms, error_message, check_time, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
                result_data["check_id"],
                result_data["service_name"],
                result_data["check_type"],
                result_data["status"],
                result_data["response_time_ms"],
                result_data["error_message"],
                result_data["check_time"],
                result_data["created_at"],
            )

    async def get_degradation_rules(self) -> List[Dict[str, Any]]:
        """Get degradation rules."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM degradation_rules WHERE enabled = true
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in rows]

    async def cleanup_old_health_check_results(self, cutoff_date: datetime) -> None:
        """Clean up old health check results."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM health_check_results WHERE check_time < $1
            """,
                cutoff_date,
            )

    # Vehicle Routes and Fleet Management Methods

    async def store_vehicle_route(self, route_data: Dict[str, Any]) -> None:
        """Store vehicle route data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicle_routes (
                    route_id, vehicle_id, station_id, departure_time, arrival_time,
                    destination, route_distance_km, required_soc_percent, actual_soc_percent,
                    route_status, route_priority, estimated_duration_hours,
                    override_type, override_reason, operator_id, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            """,
                route_data["route_id"],
                route_data["vehicle_id"],
                route_data["station_id"],
                route_data["departure_time"],
                route_data.get("arrival_time"),
                route_data.get("destination"),
                route_data.get("route_distance_km"),
                route_data["required_soc_percent"],
                route_data.get("actual_soc_percent"),
                route_data.get("route_status", "scheduled"),
                route_data.get("route_priority", 1),
                route_data.get("estimated_duration_hours"),
                route_data.get("override_type"),
                route_data.get("override_reason"),
                route_data.get("operator_id"),
                route_data["created_at"],
                route_data.get("updated_at", route_data["created_at"]),
            )

    async def get_vehicle_routes(self, vehicle_id: str) -> List[Dict[str, Any]]:
        """Get all routes for a vehicle."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM vehicle_routes 
                WHERE vehicle_id = $1 
                ORDER BY departure_time DESC
            """,
                vehicle_id,
            )
            return [dict(row) for row in rows]

    async def update_vehicle_route(self, update_data: Dict[str, Any]) -> None:
        """Update vehicle route data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicle_routes SET
                    departure_time = COALESCE($2, departure_time),
                    arrival_time = COALESCE($3, arrival_time),
                    destination = COALESCE($4, destination),
                    route_distance_km = COALESCE($5, route_distance_km),
                    required_soc_percent = COALESCE($6, required_soc_percent),
                    actual_soc_percent = COALESCE($7, actual_soc_percent),
                    route_status = COALESCE($8, route_status),
                    route_priority = COALESCE($9, route_priority),
                    estimated_duration_hours = COALESCE($10, estimated_duration_hours),
                    override_type = COALESCE($11, override_type),
                    override_reason = COALESCE($12, override_reason),
                    operator_id = COALESCE($13, operator_id),
                    updated_at = $14
                WHERE route_id = $1
            """,
                update_data["route_id"],
                update_data.get("departure_time"),
                update_data.get("arrival_time"),
                update_data.get("destination"),
                update_data.get("route_distance_km"),
                update_data.get("required_soc_percent"),
                update_data.get("actual_soc_percent"),
                update_data.get("route_status"),
                update_data.get("route_priority"),
                update_data.get("estimated_duration_hours"),
                update_data.get("override_type"),
                update_data.get("override_reason"),
                update_data.get("operator_id"),
                update_data["updated_at"],
            )

    async def cancel_vehicle_routes(self, vehicle_id: str) -> None:
        """Cancel all routes for a vehicle."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicle_routes SET
                    route_status = 'cancelled',
                    updated_at = NOW()
                WHERE vehicle_id = $1 AND route_status IN ('scheduled', 'in_progress')
            """,
                vehicle_id,
            )

    async def get_active_routes(self) -> List[Dict[str, Any]]:
        """Get all active routes."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM vehicle_routes 
                WHERE route_status IN ('scheduled', 'in_progress')
                AND departure_time > NOW()
                ORDER BY departure_time ASC
            """)
            return [dict(row) for row in rows]

    async def store_vehicle_fleet(self, vehicle_data: Dict[str, Any]) -> None:
        """Store vehicle fleet data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vehicle_fleet (
                    vehicle_id, station_id, battery_capacity_kwh, max_charge_rate_kw,
                    max_discharge_rate_kw, current_soc_kwh, min_soc_kwh,
                    charge_efficiency, discharge_efficiency, vehicle_type,
                    make, model, year, is_active, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (vehicle_id) DO UPDATE SET
                    station_id = EXCLUDED.station_id,
                    battery_capacity_kwh = EXCLUDED.battery_capacity_kwh,
                    max_charge_rate_kw = EXCLUDED.max_charge_rate_kw,
                    max_discharge_rate_kw = EXCLUDED.max_discharge_rate_kw,
                    current_soc_kwh = EXCLUDED.current_soc_kwh,
                    min_soc_kwh = EXCLUDED.min_soc_kwh,
                    charge_efficiency = EXCLUDED.charge_efficiency,
                    discharge_efficiency = EXCLUDED.discharge_efficiency,
                    vehicle_type = EXCLUDED.vehicle_type,
                    make = EXCLUDED.make,
                    model = EXCLUDED.model,
                    year = EXCLUDED.year,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
            """,
                vehicle_data["vehicle_id"],
                vehicle_data["station_id"],
                vehicle_data["battery_capacity_kwh"],
                vehicle_data["max_charge_rate_kw"],
                vehicle_data["max_discharge_rate_kw"],
                vehicle_data.get("current_soc_kwh"),
                vehicle_data.get("min_soc_kwh", 15.0),
                vehicle_data.get("charge_efficiency", 0.95),
                vehicle_data.get("discharge_efficiency", 0.90),
                vehicle_data.get("vehicle_type"),
                vehicle_data.get("make"),
                vehicle_data.get("model"),
                vehicle_data.get("year"),
                vehicle_data.get("is_active", True),
                vehicle_data["created_at"],
                vehicle_data.get("updated_at", vehicle_data["created_at"]),
            )

    async def get_all_vehicles(self) -> List[Dict[str, Any]]:
        """Get all vehicles."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM vehicle_fleet 
                WHERE is_active = true
                ORDER BY vehicle_id
            """)
            return [dict(row) for row in rows]

    async def get_active_vehicles(self) -> List[Dict[str, Any]]:
        """Get active vehicles."""
        async with self.pg_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT vf.*, vr.departure_time, vr.required_soc_percent
                FROM vehicle_fleet vf
                LEFT JOIN vehicle_routes vr ON vf.vehicle_id = vr.vehicle_id
                WHERE vf.is_active = true
                AND (vr.route_status IS NULL OR vr.route_status IN ('scheduled', 'in_progress'))
                ORDER BY vf.vehicle_id
            """)
            return [dict(row) for row in rows]

    async def store_price_forecast(self, forecast_data: Dict[str, Any]) -> None:
        """Store price forecast data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO price_forecasts (
                    forecast_id, forecast_time, horizon_start, horizon_end,
                    node_id, market_type, forecast_prices, confidence_intervals,
                    model_version, accuracy_score, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
                forecast_data["forecast_id"],
                forecast_data["forecast_time"],
                forecast_data["horizon_start"],
                forecast_data["horizon_end"],
                forecast_data["node_id"],
                forecast_data["market_type"],
                json.dumps(forecast_data["forecast_prices"]),
                (
                    json.dumps(forecast_data.get("confidence_intervals"))
                    if forecast_data.get("confidence_intervals")
                    else None
                ),
                forecast_data.get("model_version"),
                forecast_data.get("accuracy_score"),
                forecast_data["created_at"],
            )

    async def store_demand_forecast(self, forecast_data: Dict[str, Any]) -> None:
        """Store demand forecast data."""
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO demand_forecasts (
                    forecast_id, forecast_time, horizon_start, horizon_end,
                    station_id, forecast_demand, confidence_intervals,
                    model_version, accuracy_score, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
                forecast_data["forecast_id"],
                forecast_data["forecast_time"],
                forecast_data["horizon_start"],
                forecast_data["horizon_end"],
                forecast_data["station_id"],
                json.dumps(forecast_data["forecast_demand"]),
                (
                    json.dumps(forecast_data.get("confidence_intervals"))
                    if forecast_data.get("confidence_intervals")
                    else None
                ),
                forecast_data.get("model_version"),
                forecast_data.get("accuracy_score"),
                forecast_data["created_at"],
            )

    async def get_optimization_status(self) -> Dict[str, Any]:
        """Get current optimization status."""
        async with self.pg_pool.acquire() as conn:
            # Get latest optimization decision
            latest_decision = await conn.fetchrow("""
                SELECT objective_value, computation_time_ms, time, constraints_satisfied
                FROM optimization_decisions
                ORDER BY time DESC
                LIMIT 1
            """)

            # Get active vehicles count
            active_vehicles = await conn.fetchval("""
                SELECT COUNT(*) FROM vehicle_fleet WHERE is_active = true
            """)

            # Get pending routes count
            pending_routes = await conn.fetchval("""
                SELECT COUNT(*) FROM vehicle_routes 
                WHERE route_status = 'scheduled' AND departure_time > NOW()
            """)

            return {
                "is_running": False,  # TODO: Implement actual running status
                "last_run_time": latest_decision["time"] if latest_decision else None,
                "next_run_time": None,  # TODO: Implement next run time calculation
                "active_vehicles": active_vehicles or 0,
                "pending_routes": pending_routes or 0,
                "solver_status": (
                    "optimal"
                    if latest_decision and latest_decision["constraints_satisfied"]
                    else "unknown"
                ),
                "last_objective_value": (
                    float(latest_decision["objective_value"])
                    if latest_decision and latest_decision["objective_value"]
                    else None
                ),
                "last_solve_time_ms": (
                    float(latest_decision["computation_time_ms"])
                    if latest_decision and latest_decision["computation_time_ms"]
                    else None
                ),
            }
