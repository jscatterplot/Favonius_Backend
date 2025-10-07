# File Interaction Map

## Core Orchestration

### `config.py`
- Defines configuration models (`WebSocketConfig`, `TimescaleConfig`, `SupabaseConfig`, `PriceFeederConfig`, `OptimizationServiceConfig`, etc.).
- Used by almost every other module via `Config.from_env()` in `main.py`.

### `main.py`
- Central orchestrator that wires everything together.
- Creates instances of:
  - `TimescaleClient`, `SupabaseClient`, `AnalyticsService`, `PriceFeederService`, `OptimizationEngine`.
  - `OCPPWebSocketServer`, `HealthCheckServer`, `APIServer`, `DataSyncService`.
- Starts/stops service lifecycles and passes dependencies (Timescale client, Supabase client, configs).

## WebSocket Path

### `server.py`
- Owns the WebSocket server.
- Creates `ConnectionManager` and `MessageHandler`.
- Relies on `config.py` for settings and on `TimescaleClient` for data persistence.

### `connection_manager.py`
- Tracks active charger connections, message sending, and statistics.
- No Redis dependency anymore; uses in-memory structures.
- Called by `server.py` and referenced by `OptimizationEngine` for pushing SetChargingProfile commands.

### `message_handler.py`
- Handles OCPP messages.
- Writes telemetry directly to Timescale via `TimescaleClient`.
- Uses `ConnectionManager` to respond to chargers.

## Data Layer

### `timescale_client.py`
- Async TimescaleDB access (telemetry inserts, price storage, charging sessions, schedules, optimization decisions).
- Used by `message_handler`, `analytics_service`, `price_feeder`, `optimization_engine`, `data_sync`.

### `timescale_schema.py`
- Creates Timescale tables/hypertables (telemetry, charging_sessions, electricity_prices, optimization_decisions).
- Invoked from `main.py` during development environment startup.

### `supabase_client.py`
- Handles Supabase REST/Pg connections for user/org data.
- Consumed by `auth_manager`, `api_server`, `data_sync`, and `optimization_engine` for fleet data.

### `data_sync.py`
- Periodically syncs Timescale data (sessions, telemetry summaries) back to Supabase.
- Uses `TimescaleClient` via direct asyncpg pool and `SupabaseClient` for upsert operations.

## Auxiliary Services

### `price_feeder.py`
- Fetches CAISO prices with `aiohttp`.
- Stores results using `TimescaleClient` and notifies `OptimizationEngine` on updates.
- Configured through `PriceFeederConfig`.

### `optimization_engine.py`
- Reads active sessions & prices from `TimescaleClient`.
- Creates heuristic charging schedules and sends SetChargingProfile via `ConnectionManager`.
- Persists schedules/decisions back to Timescale.

### `analytics_service.py`
- Query layer on top of Timescale for aggregated metrics exposed via API.

### `health.py`
- HTTP health endpoints; uses monitoring health checker for `TimescaleClient`, `connection_manager`, etc.

### `monitoring.py`
- Sets up Prometheus metrics & structured logging.
- Shared by nearly all modules.

## API Layer

### `api_server.py`
- REST interface for Supabase-backed entities (organizations, vehicles, stations, analytics).
- Uses `SupabaseClient` and `AuthManager` for auth + data operations.

### `auth_manager.py`
- JWT verification and permission checks using Supabase data.

## Notable Cross-Cutting Interactions
- `main.py` injects shared clients (`TimescaleClient`, `SupabaseClient`) into services.
- `OptimizationEngine` triggers resurfacing of schedules when `PriceFeederService` detects new price curves.
- `ConnectionManager` acts as the delivery point for optimization results back to chargers.
