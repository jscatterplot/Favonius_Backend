"""FastAPI REST API application.

Reference: PRD.md#7-api-specifications
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..core.controller import DepotController
from ..core.models import DepotConfig
from ..core.state.assembler import StateAssembler
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# Database connection pool
db_pool: Optional[asyncpg.Pool] = None

# Depot controllers cache
depot_controllers: dict[str, DepotController] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_pool
    # Initialize database connection pool
    database_url = os.getenv(
        'DATABASE_URL',
        'postgresql://postgres:postgres@localhost:5432/favonius',
    )
    try:
        db_pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
        logger.info("Database connection pool initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}")
        db_pool = None

    yield

    # Close database connection pool
    if db_pool:
        await db_pool.close()
        logger.info("Database connection pool closed")


app = FastAPI(
    title="Favonius Energy API",
    version="0.1.0",
    description="EV Fleet Depot Optimization Platform API",
    lifespan=lifespan,
)


class OptimizationRequest(BaseModel):
    """Request to run optimization."""

    depot_id: str
    horizon_hours: int = 24
    force: bool = False


class OptimizationResponse(BaseModel):
    """Optimization result response."""

    run_id: str
    depot_id: str
    status: str
    objective_value: float
    solve_time_seconds: float
    peak_demand_kw: float
    schedule: dict


class DepotStateResponse(BaseModel):
    """Depot state response."""

    depot_id: str
    timestamp: str
    vehicle_socs: dict[str, float]
    battery_soc: float
    current_month_peak_kw: float
    current_price_kwh: float


class ScheduleResponse(BaseModel):
    """Schedule response."""

    depot_id: str
    run_id: str
    generated_at: str
    horizon_start: str
    horizon_end: str
    schedule: dict


class HandoffRequest(BaseModel):
    """Inter-depot handoff request."""

    dest_depot_id: str
    expected_soc: float
    arrival_time: datetime


class HandoffResponse(BaseModel):
    """Inter-depot handoff response."""

    message_id: str
    status: str


async def _get_depot_config(depot_id: str) -> DepotConfig:
    """Get depot configuration from database.

    Args:
        depot_id: Depot identifier

    Returns:
        DepotConfig object

    Raises:
        HTTPException: If depot not found or config invalid
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    # For MVP: Use default config or fetch from database
    # TODO: Query from depots, vehicles, chargers, battery_storage tables
    # For now, return a default config
    return DepotConfig(
        vehicle_capacities={'bus_1': 324.0, 'bus_2': 324.0},
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@app.post("/optimize", response_model=OptimizationResponse)
async def run_optimization(request: OptimizationRequest):
    """Trigger depot charging optimization.

    Reference: PRD.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Get depot config
        config = await _get_depot_config(request.depot_id)

        # Get or create controller
        if request.depot_id not in depot_controllers:
            depot_controllers[request.depot_id] = DepotController(
                pool=db_pool,
                depot_id=request.depot_id,
                config=config,
            )

        controller = depot_controllers[request.depot_id]

        # Run optimization
        result = await controller.run_optimization("api_request")

        return OptimizationResponse(
            run_id=str(result.run_id),
            depot_id=request.depot_id,
            status=result.status,
            objective_value=result.objective_value,
            solve_time_seconds=result.solve_time,
            peak_demand_kw=result.peak_demand,
            schedule=result.schedule,
        )

    except Exception as e:
        logger.error(f"Optimization failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/depots/{depot_id}/state", response_model=DepotStateResponse)
async def get_depot_state(depot_id: str):
    """Get current depot state.

    Reference: PRD.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        config = await _get_depot_config(depot_id)
        assembler = StateAssembler(db_pool, depot_id, config)
        state = await assembler.get_current_state(24)

        # Get current price (first timestep)
        current_price = state.prices[0] if state.prices else 0.15

        return DepotStateResponse(
            depot_id=depot_id,
            timestamp=datetime.utcnow().isoformat(),
            vehicle_socs=state.vehicle_socs,
            battery_soc=state.battery_soc,
            current_month_peak_kw=state.current_month_peak,
            current_price_kwh=current_price,
        )

    except Exception as e:
        logger.error(f"Failed to get depot state: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/depots/{depot_id}/schedule", response_model=ScheduleResponse)
async def get_depot_schedule(depot_id: str):
    """Get current charging schedule.

    Reference: PRD.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        # Get latest optimization result from database
        query = """
        SELECT run_id, run_time, schedule_json, horizon_start, horizon_end
        FROM optimization_runs
        WHERE depot_id = $1
        ORDER BY run_time DESC
        LIMIT 1
        """
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(query, depot_id)

        if not row:
            raise HTTPException(
                status_code=404, detail="No schedule found for depot"
            )

        return ScheduleResponse(
            depot_id=depot_id,
            run_id=str(row['run_id']),
            generated_at=row['run_time'].isoformat(),
            horizon_start=row['horizon_start'].isoformat(),
            horizon_end=row['horizon_end'].isoformat(),
            schedule=row['schedule_json'],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get schedule: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
    response_model=HandoffResponse,
)
async def send_handoff(
    depot_id: str, vehicle_id: str, request: HandoffRequest
):
    """Send inter-depot handoff message.

    Reference: PRD.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        from uuid import uuid4

        message_id = uuid4()

        query = """
        INSERT INTO interdepot_messages
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        async with db_pool.acquire() as conn:
            await conn.execute(
                query,
                message_id,
                depot_id,
                request.dest_depot_id,
                vehicle_id,
                datetime.utcnow(),
                request.expected_soc,
                request.arrival_time,
            )

        logger.info(
            f"Handoff message sent: {message_id} from {depot_id} "
            f"to {request.dest_depot_id} for vehicle {vehicle_id}"
        )

        return HandoffResponse(message_id=str(message_id), status="sent")

    except Exception as e:
        logger.error(f"Failed to send handoff: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint.

    Reference: PRD.md#7-1-rest-api-endpoints
    """
    db_status = "healthy" if db_pool else "unavailable"

    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "database": db_status,
            "ocpp_server": "unknown",
        },
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint.

    Reference: Development plan Step 7.2
    """
    return Response(
        content=generate_latest(), media_type=CONTENT_TYPE_LATEST
    )

