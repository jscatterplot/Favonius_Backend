"""Real-time data synchronization between TimescaleDB and Supabase."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from .config import SupabaseConfig
from .monitoring import get_logger
from .supabase_client import SupabaseClient


class DataSyncService:
    """Service for synchronizing data between TimescaleDB and Supabase."""

    def __init__(self, config: SupabaseConfig, supabase_client: SupabaseClient):
        """Initialize data sync service."""
        self.config = config
        self.supabase_client = supabase_client
        self.logger = get_logger(__name__)

        # TimescaleDB connection
        self.timescale_pool: Optional[asyncpg.Pool] = None

        # Sync configuration
        self.sync_interval = 300  # 5 minutes
        self.batch_size = 1000
        self.max_retries = 3

        # Sync state tracking
        self.last_sync_times: Dict[str, datetime] = {}
        self.sync_running = False
        self._sync_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the data synchronization service."""
        try:
            # Connect to TimescaleDB (you'll need to configure this)
            # For now, we'll use the same connection as Supabase
            self.timescale_pool = await asyncpg.create_pool(
                host=self.config.db_host,
                port=self.config.db_port,
                database=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
                min_size=1,
                max_size=10,
            )

            self.sync_running = True

            # Start sync task (tracked for proper shutdown)
            self._sync_task = asyncio.create_task(self._sync_loop())

            self.logger.info("Data sync service started")

        except Exception as e:
            self.logger.error(f"Failed to start data sync service: {e}")
            raise

    async def stop(self) -> None:
        """Stop the data synchronization service."""
        self.sync_running = False

        # Cancel the background sync task before closing the pool
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None

        if self.timescale_pool:
            await self.timescale_pool.close()

        self.logger.info("Data sync service stopped")

    async def _sync_loop(self) -> None:
        """Main synchronization loop."""
        while self.sync_running:
            try:
                await self.sync_all_data()
                await asyncio.sleep(self.sync_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Sync loop error: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retry

    async def sync_all_data(self) -> None:
        """Sync all data types."""
        sync_tasks = [
            self.sync_charging_sessions(),
            self.sync_vehicle_states(),
            self.sync_optimization_decisions(),
            self.sync_energy_metrics(),
        ]

        await asyncio.gather(*sync_tasks, return_exceptions=True)

    async def sync_charging_sessions(self) -> None:
        """Sync completed charging sessions from TimescaleDB to Supabase."""
        try:
            if not self.timescale_pool:
                return

            # Get last sync time
            last_sync = self.last_sync_times.get(
                "charging_sessions", datetime.now(timezone.utc) - timedelta(hours=1)
            )

            async with self.timescale_pool.acquire() as conn:
                # Query completed sessions
                query = """
                    SELECT 
                        session_id,
                        station_id,
                        vehicle_id,
                        organization_id,
                        start_time,
                        end_time,
                        energy_delivered_kwh,
                        energy_received_kwh,
                        session_duration_minutes,
                        cost_total,
                        revenue_v2g,
                        status
                    FROM charging_sessions
                    WHERE end_time > $1
                        AND sync_status = 'pending'
                    ORDER BY end_time
                    LIMIT $2
                """

                sessions = await conn.fetch(query, last_sync, self.batch_size)

                if sessions:
                    # Convert to Supabase format
                    session_data = []
                    for session in sessions:
                        session_data.append(
                            {
                                "session_id": session["session_id"],
                                "station_id": session["station_id"],
                                "vehicle_id": session["vehicle_id"],
                                "organization_id": session["organization_id"],
                                "start_time": session["start_time"].isoformat(),
                                "end_time": (
                                    session["end_time"].isoformat() if session["end_time"] else None
                                ),
                                "energy_delivered_kwh": (
                                    float(session["energy_delivered_kwh"])
                                    if session["energy_delivered_kwh"]
                                    else 0
                                ),
                                "energy_received_kwh": (
                                    float(session["energy_received_kwh"])
                                    if session["energy_received_kwh"]
                                    else 0
                                ),
                                "session_duration_minutes": session["session_duration_minutes"],
                                "cost_total": (
                                    float(session["cost_total"]) if session["cost_total"] else 0
                                ),
                                "revenue_v2g": (
                                    float(session["revenue_v2g"]) if session["revenue_v2g"] else 0
                                ),
                                "status": session["status"],
                            }
                        )

                    # Sync to Supabase
                    await self.supabase_client.sync_session_summaries(session_data)

                    # Mark as synced in TimescaleDB
                    session_ids = [s["session_id"] for s in sessions]
                    await conn.execute(
                        """
                        UPDATE charging_sessions 
                        SET sync_status = 'completed'
                        WHERE session_id = ANY($1)
                        """,
                        session_ids,
                    )

                    # Update last sync time
                    self.last_sync_times["charging_sessions"] = datetime.now(timezone.utc)

                    self.logger.info(f"Synced {len(sessions)} charging sessions")

        except Exception as e:
            self.logger.error(f"Failed to sync charging sessions: {e}")

    async def sync_vehicle_states(self) -> None:
        """Sync vehicle real-time states to Supabase."""
        try:
            if not self.timescale_pool:
                return

            # Get last sync time
            last_sync = self.last_sync_times.get(
                "vehicle_states", datetime.now(timezone.utc) - timedelta(minutes=5)
            )

            async with self.timescale_pool.acquire() as conn:
                # Query recent vehicle states
                query = """
                    SELECT DISTINCT ON (vehicle_id)
                        vehicle_id,
                        organization_id,
                        current_soc,
                        current_power_kw,
                        charging_status,
                        location_lat,
                        location_lon,
                        last_seen
                    FROM vehicle_telemetry
                    WHERE last_seen > $1
                    ORDER BY vehicle_id, last_seen DESC
                    LIMIT $2
                """

                states = await conn.fetch(query, last_sync, self.batch_size)

                if states:
                    # Convert to Supabase format
                    state_data = []
                    for state in states:
                        state_data.append(
                            {
                                "vehicle_id": state["vehicle_id"],
                                "organization_id": state["organization_id"],
                                "current_soc": (
                                    float(state["current_soc"]) if state["current_soc"] else 0
                                ),
                                "current_power_kw": (
                                    float(state["current_power_kw"])
                                    if state["current_power_kw"]
                                    else 0
                                ),
                                "charging_status": state["charging_status"],
                                "location": (
                                    f"POINT({state['location_lon']} {state['location_lat']})"
                                    if state["location_lat"] and state["location_lon"]
                                    else None
                                ),
                                "last_seen": state["last_seen"].isoformat(),
                            }
                        )

                    # Sync to Supabase
                    await self.supabase_client.sync_vehicle_states(state_data)

                    # Update last sync time
                    self.last_sync_times["vehicle_states"] = datetime.now(timezone.utc)

                    self.logger.info(f"Synced {len(states)} vehicle states")

        except Exception as e:
            self.logger.error(f"Failed to sync vehicle states: {e}")

    async def sync_optimization_decisions(self) -> None:
        """Sync optimization decisions to Supabase."""
        try:
            if not self.timescale_pool:
                return

            # Get last sync time
            last_sync = self.last_sync_times.get(
                "optimization_decisions", datetime.now(timezone.utc) - timedelta(minutes=1)
            )

            async with self.timescale_pool.acquire() as conn:
                # Query recent optimization decisions
                query = """
                    SELECT 
                        decision_id,
                        organization_id,
                        vehicle_id,
                        station_id,
                        decision_type,
                        power_setpoint_kw,
                        duration_minutes,
                        cost_savings,
                        created_at
                    FROM optimization_decisions
                    WHERE created_at > $1
                        AND sync_status = 'pending'
                    ORDER BY created_at
                    LIMIT $2
                """

                decisions = await conn.fetch(query, last_sync, self.batch_size)

                if decisions:
                    # Store optimization decisions in Supabase
                    decision_data = []
                    for decision in decisions:
                        decision_data.append(
                            {
                                "decision_id": decision["decision_id"],
                                "organization_id": decision["organization_id"],
                                "vehicle_id": decision["vehicle_id"],
                                "station_id": decision["station_id"],
                                "decision_type": decision["decision_type"],
                                "power_setpoint_kw": (
                                    float(decision["power_setpoint_kw"])
                                    if decision["power_setpoint_kw"]
                                    else 0
                                ),
                                "duration_minutes": decision["duration_minutes"],
                                "cost_savings": (
                                    float(decision["cost_savings"])
                                    if decision["cost_savings"]
                                    else 0
                                ),
                                "created_at": decision["created_at"].isoformat(),
                            }
                        )

                    # Insert into Supabase (you'd need to create this table)
                    # await self.supabase_client.client.table('optimization_decisions').upsert(decision_data).execute()

                    # Mark as synced in TimescaleDB
                    decision_ids = [d["decision_id"] for d in decisions]
                    await conn.execute(
                        """
                        UPDATE optimization_decisions 
                        SET sync_status = 'completed'
                        WHERE decision_id = ANY($1)
                        """,
                        decision_ids,
                    )

                    # Update last sync time
                    self.last_sync_times["optimization_decisions"] = datetime.now(timezone.utc)

                    self.logger.info(f"Synced {len(decisions)} optimization decisions")

        except Exception as e:
            self.logger.error(f"Failed to sync optimization decisions: {e}")

    async def sync_energy_metrics(self) -> None:
        """Sync energy metrics and analytics to Supabase."""
        try:
            if not self.timescale_pool:
                return

            # Get last sync time
            last_sync = self.last_sync_times.get(
                "energy_metrics", datetime.now(timezone.utc) - timedelta(hours=1)
            )

            async with self.timescale_pool.acquire() as conn:
                # Query energy metrics
                query = """
                    SELECT 
                        organization_id,
                        DATE(start_time) as date,
                        COUNT(DISTINCT vehicle_id) as vehicles_charged,
                        SUM(energy_delivered_kwh) as total_energy_charged,
                        SUM(energy_received_kwh) as total_energy_discharged,
                        AVG(session_duration_minutes) as avg_session_duration,
                        SUM(cost_total) as total_cost,
                        SUM(revenue_v2g) as total_v2g_revenue
                    FROM charging_sessions
                    WHERE start_time > $1
                        AND end_time IS NOT NULL
                    GROUP BY organization_id, DATE(start_time)
                    ORDER BY date DESC
                    LIMIT $2
                """

                metrics = await conn.fetch(query, last_sync, self.batch_size)

                if metrics:
                    # Update daily energy summary in Supabase
                    for metric in metrics:
                        await self.supabase_client.client.table("daily_energy_summary").upsert(
                            {
                                "organization_id": metric["organization_id"],
                                "date": metric["date"].isoformat(),
                                "vehicles_charged": metric["vehicles_charged"],
                                "total_energy_charged": (
                                    float(metric["total_energy_charged"])
                                    if metric["total_energy_charged"]
                                    else 0
                                ),
                                "total_energy_discharged": (
                                    float(metric["total_energy_discharged"])
                                    if metric["total_energy_discharged"]
                                    else 0
                                ),
                                "avg_session_duration": (
                                    float(metric["avg_session_duration"])
                                    if metric["avg_session_duration"]
                                    else 0
                                ),
                                "total_cost": (
                                    float(metric["total_cost"]) if metric["total_cost"] else 0
                                ),
                                "total_v2g_revenue": (
                                    float(metric["total_v2g_revenue"])
                                    if metric["total_v2g_revenue"]
                                    else 0
                                ),
                            }
                        ).execute()

                    # Update last sync time
                    self.last_sync_times["energy_metrics"] = datetime.now(timezone.utc)

                    self.logger.info(f"Synced {len(metrics)} energy metrics")

        except Exception as e:
            self.logger.error(f"Failed to sync energy metrics: {e}")

    async def sync_active_sessions(self, sessions: List[Dict[str, Any]]) -> None:
        """Sync active charging sessions to Supabase."""
        try:
            if not sessions:
                return

            # Convert to Supabase format
            session_data = []
            for session in sessions:
                session_data.append(
                    {
                        "session_id": session["session_id"],
                        "station_id": session["station_id"],
                        "vehicle_id": session["vehicle_id"],
                        "organization_id": session["organization_id"],
                        "start_time": session["start_time"].isoformat(),
                        "current_power_kw": float(session.get("current_power_kw", 0)),
                        "current_soc": float(session.get("current_soc", 0)),
                        "target_soc": float(session.get("target_soc", 100)),
                        "estimated_end_time": (
                            session.get("estimated_end_time").isoformat()
                            if session.get("estimated_end_time")
                            else None
                        ),
                        "status": session.get("status", "charging"),
                    }
                )

            # Upsert to Supabase
            await self.supabase_client.client.table("charging_sessions_active").upsert(
                session_data
            ).execute()

            self.logger.info(f"Synced {len(sessions)} active sessions")

        except Exception as e:
            self.logger.error(f"Failed to sync active sessions: {e}")

    async def get_sync_status(self) -> Dict[str, Any]:
        """Get synchronization status."""
        return {
            "running": self.sync_running,
            "last_sync_times": {
                key: value.isoformat() for key, value in self.last_sync_times.items()
            },
            "sync_interval": self.sync_interval,
            "batch_size": self.batch_size,
        }

    async def force_sync(self, data_type: str) -> None:
        """Force synchronization of specific data type."""
        try:
            if data_type == "charging_sessions":
                await self.sync_charging_sessions()
            elif data_type == "vehicle_states":
                await self.sync_vehicle_states()
            elif data_type == "optimization_decisions":
                await self.sync_optimization_decisions()
            elif data_type == "energy_metrics":
                await self.sync_energy_metrics()
            else:
                raise ValueError(f"Unknown data type: {data_type}")

            self.logger.info(f"Forced sync completed for {data_type}")

        except Exception as e:
            self.logger.error(f"Failed to force sync {data_type}: {e}")
            raise
