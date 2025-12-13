# API Specifications

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD_v2.md#7-api-specifications](PRD_v2.md#7-api-specifications).

## REST API Endpoints

### POST /optimize
Trigger optimization for a depot.

**Request:**
```json
{
    "depot_id": "uuid",
    "horizon_hours": 24,
    "force": false
}
```

**Response:**
```json
{
    "run_id": "uuid",
    "depot_id": "uuid",
    "status": "optimal",
    "objective_value": 1234.56,
    "solve_time_s": 12.3,
    "peak_demand_kw": 450.0,
    "solver_used": "gurobi",
    "schedule": {
        "bus_1": {
            "charging_power": [0, 0, 80, 80, ...],
            "soc": [0.3, 0.3, 0.35, 0.40, ...]
        }
    }
}
```

**Response Fields:**
- `solver_used`: Which solver was used ('gurobi' or 'highs'). Tracks fallback events for monitoring.

**Status Values:**
- `optimal`: Optimal solution found
- `feasible`: Feasible solution found (may have hit time limit)
- `degraded`: Solution found with relaxed constraints (some vehicles may not reach target SoC)
- `infeasible`: No feasible solution found
- `timeout`: Optimization exceeded time limit

**Solver Reliability:**
- Primary solver: Gurobi (commercial, high performance)
- Fallback solver: HiGHS (open-source, automatic fallback if Gurobi fails)
- The `solver_used` field indicates which solver was used for this optimization
- Fallback occurs automatically on Gurobi license failure or connection errors
- See PRD Section 8.2 for detailed solver configuration

**Error Codes:**
- 400: Invalid request (missing depot_id, etc.)
- 404: Depot not found
- 500: Optimization failed (infeasible, timeout)

---

### GET /depots/{depot_id}/state
Get current depot state.

**Response:**
```json
{
    "depot_id": "uuid",
    "timestamp": "2025-12-04T10:00:00Z",
    "vehicle_socs": {
        "bus_1": 0.45,
        "bus_2": 0.82
    },
    "battery_soc": 0.55,
    "current_month_peak_kw": 380.0,
    "current_price_kwh": 0.15,
    "building_load_kw": 45.0,
    "incoming_vehicles": [
        {
            "vehicle_id": "uuid",
            "external_id": "bus_201",
            "expected_soc": 0.35,
            "arrival_time": "2025-12-04T14:30:00Z"
        }
    ]
}
```

---

### GET /depots/{depot_id}/schedule
Get current charging schedule.

**Response:**
```json
{
    "depot_id": "uuid",
    "run_id": "uuid",
    "generated_at": "2025-12-04T09:00:00Z",
    "horizon_start": "2025-12-04T09:00:00Z",
    "horizon_end": "2025-12-05T09:00:00Z",
    "schedule": { ... }
}
```

---

### POST /depots/{depot_id}/vehicles/{vehicle_id}/handoff
Send inter-depot handoff message.

**Request:**
```json
{
    "dest_depot_id": "uuid",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
}
```

**Response:**
```json
{
    "message_id": "uuid",
    "status": "sent"
}
```

---

### POST /depots/{depot_id}/handoff/receive
Receive inter-depot handoff message (called by origin depot).

**Request:**
```json
{
    "message_id": "uuid",
    "origin_depot_id": "uuid",
    "vehicle_id": "uuid",
    "external_id": "bus_201",
    "expected_soc": 0.35,
    "arrival_time": "2025-12-04T14:30:00Z",
    "battery_kwh": 324.0,
    "max_charge_kw": 150.0
}
```

**Response:**
```json
{
    "status": "acknowledged",
    "acknowledged_at": "2025-12-04T10:00:05Z"
}
```

---

### GET /health
Health check endpoint.

**Response:**
```json
{
    "status": "healthy",
    "timestamp": "2025-12-04T10:00:00Z",
    "components": {
        "database": "healthy",
        "ocpp_server": "healthy",
        "gurobi_license": "valid",
        "highs_available": "true"
    }
}
```

---

## WebSocket API (OCPP)

The platform implements an OCPP 1.6 Central System at `ws://<host>:9000/{ocpp_id}`.

**Supported Messages:**

| Direction | Message | Purpose |
|-----------|---------|---------|
| CP → CS | BootNotification | Charger registration |
| CP → CS | StatusNotification | Charger status updates |
| CP → CS | MeterValues | Energy, SoC, and max_charge_kw readings |
| CP → CS | StartTransaction | Charging session start |
| CP → CS | StopTransaction | Charging session end |
| CS → CP | SetChargingProfile | Dispatch charging schedule |
| CS → CP | RemoteStartTransaction | Initiate charging |
| CS → CP | RemoteStopTransaction | Stop charging |

For detailed OCPP integration specifications, see [PRD_v2.md#9-1-ocpp-integration](PRD_v2.md#9-1-ocpp-integration).

---

## Implementation Notes

- All endpoints use FastAPI framework
- Authentication: JWT tokens (1 hour access, 24 hour refresh) - per PRD Section 10.3
- TLS: Required for all exposed ports in production (PRD Section 10.3)
- Error responses follow consistent format
- Request/response logging included
- OpenAPI schema auto-generated from FastAPI

For complete specifications, see [PRD_v2.md](PRD_v2.md).

