# EV Charging Platform - WebSocket Handler

A streamlined OCPP 2.1 WebSocket handler for Vehicle-to-Grid (V2G) pilots. The service maintains bidirectional communication with EV chargers, persists telemetry to TimescaleDB, synchronises user-facing data via Supabase, ingests market prices, and generates charging/discharging schedules.

## Features

### Core Functionality
- **OCPP 2.1 Protocol Support**: Handles the primary message set required for V2X-capable chargers.
- **Pilot-Scale Performance**: Tuned for up to 100 concurrent charger connections using `uvloop`.
- **V2X Scheduling**: Generates and pushes charging profiles back to stations.
- **Direct Persistence**: Telemetry and schedules are written straight to TimescaleDB.
- **Supabase Integration**: Provides REST endpoints, authentication, and analytics for operators.
- **CAISO Price Feeder**: Periodically fetches market prices and stores a 24-hour outlook.
- **Optimization Loop**: Rolling-horizon scheduler that reacts to price or charger changes.
- **Observability**: Prometheus metrics and structured JSON logging (via `monitoring.py`).

### Current V2X Operation Modes
- **CentralSetpoint**: Cloud-originated power profiles dispatched via `SetChargingProfile`.
- **LocalFrequency / LocalLoadBalancing / ExternalSetpoint**: Hooks are present in the V2X controller for future expansion.

## Runtime Architecture

```
┌──────────────────┐       ┌────────────────────┐       ┌────────────────-────┐
│   EV Chargers    │◄──-──►│  WebSocket Handler │──────►│ Deployment Targets  │
│  (OCPP 2.1)      │       │  (server.py)       │       │ (SetChargingProfile)│
└────────▲─────────┘       └─────────▲──────────┘       └─────────▲───────────┘
         │                            │                           │
         │ telemetry & events         │ schedules & control       │
         ▼                            │                           │
┌──────────────────┐      ┌───────────┴──────────┐       ┌────────────────────┐
│ TimescaleDB      │◄──-──│  Optimization Engine │◄──────│  Price Feeder      │
│ (telemetry,      │      │                      │       │  (CAISO OASIS)     │
│prices, schedules)│      └───────────▲──────────┘       └────────────────────┘
└────────▲─────────┘                  │
         │ analytics & sync           │ Supabase REST/API
         ▼                            ▼
┌──────────────────┐       ┌────────────────────┐
│ Analytics Service│◄──-──►│ Supabase Client    │
│ REST endpoints   │       │ User/org data      │
└──────────────────┘       └────────────────────┘
```

## Quick Start

### Docker Compose (development)
1. **Clone & configure**
```bash
git clone <repository>
   cd Favonius_Backend
   cp .env.example .env  # fill in Supabase/Timescale credentials
```
2. **Launch supporting services** (docker-compose provides TimescaleDB, Prometheus, Grafana, optional tools)
```bash
docker-compose up -d
```
3. **Run the WebSocket handler**
```bash
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   python -m src.websocket_handler.main
   ```
4. **Smoke check**
```bash
   curl http://localhost:8081/health   # health endpoints
   curl http://localhost:8080/metrics  # Prometheus scrape
```

## Configuration

### Environment Variables

| Variable                       | Description                                     | Default |
|--------------------------------|-------------------------------------------------|---------|
| `WEBSOCKET_PORT`               | OCPP server port                                | 9000    |
| `MAX_CONNECTIONS`              | Max concurrent charger sessions                 | 100     |
| `HEARTBEAT_INTERVAL`           | Heartbeat interval (seconds)                    | 30      |
| `ENVIRONMENT`                  | `development`, `staging`, or `production`       | development |
| `LOG_LEVEL`                    | Logging level (`INFO`, `DEBUG`, …)              | INFO    |
| `SUPABASE_URL` / keys          | Supabase project credentials                    | –       |
| `SUPABASE_DB_HOST` / creds     | Supabase Postgres details for direct access     | –       |
| `TIMESCALE_SERVICE_URL`        | TimescaleDB connection URI                      | –       |
| `PRICE_FEEDER_ENABLED`         | Enable CAISO price ingestion                    | true    |
| `PRICE_FEEDER_NODES`           | CSV of CAISO nodes (e.g. `TH_SP15_GEN-APND`)    | TH_SP15_GEN-APND,TH_NP15_GEN-APND |
| `PRICE_FEEDER_FETCH_INTERVAL`  | Price refresh interval (seconds)                | 900     |
| `PRICE_FEEDER_LOOKAHEAD_HOURS` | Hours of price horizon                          | 24      |
| `OPTIMIZATION_ENABLED`         | Enable schedule computation                     | true    |
| `OPTIMIZATION_HORIZON_HOURS`   | Rolling-horizon length                          | 4       |
| `OPTIMIZATION_TIMESTEP_MINUTES`| Decision interval length                        | 60      |
| `OPTIMIZATION_SOC_MIN`         | Minimum allowed state-of-charge (fraction)      | 0.2     |
| `OPTIMIZATION_SOC_TARGET`      | Target state-of-charge before departure         | 0.8     |

TLS-specific variables (`TLS_CERT_PATH`, `TLS_KEY_PATH`, `TLS_VERIFY_CLIENT`) remain optional for local debugging.

## Services & Responsibilities

| Module                               | Role                                                                        |
|--------------------------------------|-----------------------------------------------------------------------------|
| `main.py`                            | Dependency injection & lifecycle management                                 |
| `server.py`                          | WebSocket server, connection lifecycle                                      |
| `connection_manager.py`              | Tracks station sessions and pushes messages                                 |
| `message_handler.py`                 | OCPP message parsing & telemetry persistence                                |
| `timescale_client.py`                | Async access to TimescaleDB                                                 |
| `price_feeder.py`                    | CAISO price ingestion & optimizer trigger                                   |
| `optimization_engine.py`             | Heuristic scheduler that creates `SetChargingProfile` directives            |
| `analytics_service.py`               | Aggregated metrics for the REST API                                         |
| `supabase_client.py`                 | User/fleet queries for Supabase                                             |
| `data_sync.py`                       | Periodic Timescale→Supabase summarisation                                   |
| `monitoring.py`                      | Prometheus metrics, structured logging, health checks                       |

## Development Notes

- **TimescaleDB** is the primary state store. Schema creation in `timescale_schema.py` will run automatically in development mode.
- **Supabase** provides user/org metadata. Populate it with demo data or connect to your project.
- **Price feeder** requires outbound access to CAISO OASIS. In offline environments you may disable it via `PRICE_FEEDER_ENABLED=false`.
- **Optimization engine** ships with a heuristic implementation. Integrate a Julia solver by adapting `optimization_engine.py` to call out to your own service and feed the results back through `ConnectionManager`.

## Observability

- **Health**: `GET /health`, `/readiness`, `/liveness` from `health.py`.
- **Metrics**: Prometheus counters/gauges at `GET /metrics`.
- **Logs**: JSON structured logs; configure sinks via standard logging handlers.

## Troubleshooting

| Issue                        | Checks                                                                    |
|------------------------------|---------------------------------------------------------------------------|
| Chargers fail to connect     | Verify OCPP subprotocol (`ocpp2.1`), TLS configuration, and heartbeat     |
| Telemetry missing in DB      | Inspect `message_handler` logs, confirm Timescale credentials             |
| Price feeder errors          | Confirm CAISO API reachability and node list formatting                   |
| Schedules not applied        | Ensure optimization engine is enabled and `send_charging_profile` succeeds|
| Supabase sync gaps           | Look at `data_sync.py` logs for batched upserts                           |

## Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/my-change`).
3. Make changes and add tests where feasible.
4. Run lint/type checks (`black`, `isort`, `mypy`) and relevant pytest suites.
5. Open a PR describing the change and its impact.

## License

This project is licensed under the MIT License – see [LICENSE](LICENSE).

---

**Built for the future of electric mobility and grid integration.**
