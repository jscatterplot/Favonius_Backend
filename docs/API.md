# API Specifications

## Reference
This document is extracted from the Product Requirements Document. For the authoritative specification, see [PRD.md#7-api-specifications](PRD.md#7-api-specifications).

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
    "status": "completed",
    "objective_value": 1234.56,
    "solve_time_seconds": 12.3,
    "peak_demand_kw": 450.0,
    "schedule": {
        "bus_1": {
            "charging_power": [0, 0, 80, 80, ...],
            "soc": [0.3, 0.3, 0.35, 0.40, ...]
        }
    }
}
```

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
    "current_price_kwh": 0.15
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
    "arrival_time": "2025-12-04T14:30:00Z"
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

### GET /health
Health check endpoint.

**Response:**
```json
{
    "status": "healthy",
    "timestamp": "2025-12-04T10:00:00Z",
    "components": {
        "database": "healthy",
        "ocpp_server": "healthy"
    }
}
```

---

## WebSocket API (OCPP)

The platform implements an OCPP 1.6 Central System at `ws://<host>:9000/{charger_id}`.

**Supported Messages:**

| Direction | Message | Purpose |
|-----------|---------|---------|
| CP → CS | BootNotification | Charger registration |
| CP → CS | StatusNotification | Charger status updates |
| CP → CS | MeterValues | Energy and SoC readings |
| CP → CS | StartTransaction | Charging session start |
| CP → CS | StopTransaction | Charging session end |
| CS → CP | SetChargingProfile | Dispatch charging schedule |
| CS → CP | RemoteStartTransaction | Initiate charging |
| CS → CP | RemoteStopTransaction | Stop charging |

For detailed OCPP integration specifications, see [PRD.md#9-1-ocpp-integration](PRD.md#9-1-ocpp-integration).

---

## Implementation Notes

- All endpoints use FastAPI framework
- Authentication: JWT tokens (future)
- Error responses follow consistent format
- Request/response logging included
- OpenAPI schema auto-generated from FastAPI

For complete specifications, see [PRD.md](PRD.md).

