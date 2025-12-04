# Favonius Energy - EV Fleet Depot Optimization Platform

An integrated depot energy management platform that coordinates EV charging schedules, stationary batteries, and building loads to reduce electricity costs by 30-50% for commercial fleet operators. The system supports OCPP 1.6/2.0.1 communication with chargers, V2G capabilities, and MILP-based optimization.

## Features

### Core Functionality
- **OCPP 1.6/2.0.1 Protocol Support**: Primary support for OCPP 1.6 with 2.0.1 ready for smart charging profiles.
- **Fleet Depot Optimization**: MILP-based optimization engine (Pyomo + HiGHS) for demand charge reduction.
- **V2G Support**: Vehicle-to-Grid capabilities for bidirectional charging and grid services.
- **Energy Consumption Surrogate Model**: Gaussian Process model for predicting vehicle energy consumption.
- **Demand Charge Minimization**: Optimizes charging schedules to reduce peak demand charges (30-50% reduction target).
- **Stationary Battery Dispatch**: Coordinates battery storage for peak shaving.
- **CAISO Price Integration**: Real-time and day-ahead market price feeds for TOU arbitrage.
- **Inter-Depot Vehicle Handoff**: Messaging system for multi-depot fleet coordination.
- **Observability**: Prometheus metrics and structured JSON logging.

### Optimization Capabilities
- **24-hour Rolling Horizon**: Optimizes charging schedules with 15-minute timesteps.
- **Hard Constraints**: Ensures vehicles reach ≥99% SoC by departure time.
- **Demand Charge Tracking**: Tracks and minimizes monthly peak demand.
- **Re-optimization Triggers**: Automatic re-optimization on price spikes, SoC deviations, and schedule changes.

## Runtime Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL INPUTS                             │
├─────────┬─────────┬─────────┬─────────┬─────────┬─────────────────┤
│ Weather │ Market/ │  Fleet  │ Vehicle │ Inter-  │ Building Load   │
│   API   │ Utility │  Mgmt   │Telemetry│  Depot  │  (optional)     │
└────┬────┴────┬────┴────┬────┴────┬────┴────┬────┴────────┬────────┘
     │         │         │         │         │             │
     ▼         ▼         ▼         ▼         ▼             ▼
┌─────────────────────────────────────────────────────────────────────┐
│              ENERGY CONSUMPTION SURROGATE MODEL                     │
│              (Gaussian Process / MLP)                               │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        STATE ASSEMBLER                              │
│  • Current SoC (vehicles + battery)                                 │
│  • Price schedule (TOU / CAISO DAM)                                 │
│  • Vehicle availability windows                                     │
│  • Energy consumption forecasts                                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       MILP OPTIMIZER                                │
│                       (Pyomo + HiGHS)                               │
│  Objective: min(Energy Cost + Demand Charges)                       │
│  Solve time target: < 30 seconds                                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CONTROL OUTPUT LAYER                             │
├─────────────────┬─────────────────┬─────────────────────────────────┤
│ OCPP Commands   │ Battery Modbus  │ Data Logging (TimescaleDB)      │
│ SetChargingProf │ Charge/Discharge│ Telemetry, Results, Triggers    │
└─────────────────┴─────────────────┴─────────────────────────────────┘
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
- **Optimization engine** uses Pyomo + HiGHS for MILP optimization. Julia solver available as alternative via `julia_bridge.py`.

## Observability

- **Health**: `GET /health`, `/readiness`, `/liveness` from `health.py`.
- **Metrics**: Prometheus counters/gauges at `GET /metrics`.
- **Logs**: JSON structured logs; configure sinks via standard logging handlers.

## Troubleshooting

| Issue                        | Checks                                                                    |
|------------------------------|---------------------------------------------------------------------------|
| Chargers fail to connect     | Verify OCPP subprotocol (`ocpp1.6` or `ocpp2.1`), TLS configuration, and heartbeat     |
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
