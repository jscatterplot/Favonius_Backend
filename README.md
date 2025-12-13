# Favonius Energy - EV Fleet Depot Optimization Platform

An integrated depot energy management platform that coordinates EV charging schedules, stationary batteries, and building loads to reduce electricity costs by 30-50% for commercial fleet operators. The system supports OCPP 1.6/2.0.1 communication with chargers and MILP-based optimization.

**Reference:** `docs/PRD_v2.md` (Product Requirements Document - single source of truth)

## Features

### Core Functionality
- **OCPP 1.6/2.0.1 Protocol Support**: Primary support for OCPP 1.6 with 2.0.1 ready for smart charging profiles (per PRD Section 9.1).
- **Fleet Depot Optimization**: MILP-based optimization engine (Pyomo + Gurobi primary, HiGHS fallback) for demand charge reduction (per PRD Section 8.2).
- **Energy Consumption Surrogate Model**: Gaussian Process model for predicting vehicle energy consumption (per PRD Section 8.6).
- **Demand Charge Minimization**: Optimizes charging schedules to reduce peak demand charges (30-50% reduction target per PRD Section 1.3).
- **Stationary Battery Dispatch**: Coordinates battery storage for peak shaving.
- **Building Load Integration**: **REQUIRED** for accurate grid power calculation (per PRD Section 9.4).
- **CAISO Price Integration**: Real-time and day-ahead market price feeds for TOU arbitrage.
- **Inter-Depot Vehicle Handoff**: Messaging system for multi-depot fleet coordination (per PRD Section 5.4).
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
│   API   │ Utility │  Mgmt   │Telemetry│  Depot  │    Meter        │
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
│                       (Pyomo + Gurobi / HiGHS fallback)            │
│  Objective: min(Energy Cost + Demand Charges)                       │
│  Solve time target: < 60 seconds (per PRD Section 8.3)             │
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
- **Optimization engine** uses Pyomo + Gurobi (primary) with HiGHS fallback for MILP optimization (per PRD Section 8.2). Gurobi license required for production. Julia solver available as reference implementation in `optimization/mip_solver.jl`.

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

## Development Environment

### AI-Assisted Development (Cursor IDE)

This project uses Cursor IDE with custom rules for AI-assisted development:

- **Root configuration:** `.cursorrules` - Main AI assistant persona and PRD references
- **Domain-specific rules:** `.cursor/rules/` - Specialized patterns for optimization, OCPP, and TimescaleDB
  - `optimization.mdc` - MILP formulation patterns (PRD Section 8)
  - `ocpp.mdc` - OCPP protocol patterns (PRD Section 9.1)
  - `timescale.mdc` - TimescaleDB best practices (PRD Section 6)

**Using Cursor:**
- Reference PRD sections using `@PRD_v2.md#section-name`
- AI suggestions automatically follow PRD constraints and patterns
- Domain-specific rules provide detailed implementation guidance

### Git Hooks

**Pre-commit hooks** (via pre-commit framework):
- Code formatting (black, isort)
- Linting (ruff)
- Type checking (mypy)
- Security checks (bandit)

**Commit message validation:**
- Enforces conventional commit format: `type(scope): description`
- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
- Scope examples: `optimizer`, `ocpp`, `api`, `db`

**Setup:**
```bash
# Install pre-commit hooks
pre-commit install

# Run hooks manually
pre-commit run --all-files
```

### Code Quality

- **Type hints:** Required on all functions
- **Docstrings:** Google format
- **Line length:** 100 characters max
- **Testing:** pytest with ≥80% coverage target

## Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/my-change`).
3. Make changes and add tests where feasible.
4. Run lint/type checks (`black`, `isort`, `mypy`) and relevant pytest suites.
5. Use conventional commit format for commit messages (enforced by git hooks).
6. Open a PR describing the change and its impact.

## License

This project is licensed under the MIT License – see [LICENSE](LICENSE).

---

**Built for the future of electric mobility and grid integration.**
