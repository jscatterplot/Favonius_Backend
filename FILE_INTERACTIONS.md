# File Interaction Map

## Core Orchestration Layer

### `main.py` → Orchestrates everything
**Imports from:**
- `config.py` → Gets all configuration
- `server.py` → Starts WebSocket server
- `health.py` → Starts health check server
- `supabase_client.py` → User data management
- `auth_manager.py` → Authentication/authorization
- `data_sync.py` → Data synchronization
- `api_server.py` → REST API server
- `timescale_client.py` → Time-series database
- `telemetry_ingestion.py` → Kafka to TimescaleDB
- `analytics_service.py` → Analytics generation
- `monitoring.py` → Metrics setup

**Passes to:**
- Config objects to all services
- Supabase client to auth_manager and api_server
- Redis client to server.py
- Kafka producer to server.py

## WebSocket Communication Layer

### `server.py` → Core WebSocket handler
**Imports from:**
- `config.py` → WebSocket configuration
- `connection_manager.py` → Connection tracking
- `message_handler.py` → Message processing
- `redis_enhanced.py` → State management
- `kafka_producer.py` → Event streaming
- `monitoring.py` → Metrics

**Creates instances of:**
- ConnectionManager (with Redis client)
- MessageHandler (with Redis integration)
- KafkaProducer

**Passes to:**
- Redis client to connection_manager and message_handler
- Kafka producer to message_handler

### `connection_manager.py` → Connection tracking
**Imports from:**
- `redis_enhanced.py` → Distributed connection state
- `monitoring.py` → Connection metrics

**Receives from `server.py`:**
- Redis client instance
- WebSocket configuration

### `message_handler.py` → Message processing
**Imports from:**
- `redis_integration.py` → Redis operations
- `kafka_producer.py` → Event publishing
- `v2x_controller.py` → V2X operations
- `monitoring.py` → Message metrics

**Receives from `server.py`:**
- Redis integration service
- Kafka producer
- V2X controller

## Data Layer

### `redis_enhanced.py` → Enhanced Redis client
**Imports from:**
- `monitoring.py` → Redis operation metrics

**Used by:**
- `server.py` → Creates instance
- `connection_manager.py` → Connection state
- `redis_integration.py` → Data operations
- `v2x_controller.py` → V2X state

### `redis_integration.py` → Redis integration bridge
**Imports from:**
- `redis_enhanced.py` → Data classes and client
- `monitoring.py` → Performance timing

**Receives from `message_handler.py`:**
- Redis client instance

### `kafka_producer.py` → Event streaming
**Imports from:**
- `monitoring.py` → Kafka metrics

**Used by:**
- `server.py` → Creates instance
- `message_handler.py` → Publishes events
- `v2x_controller.py` → V2X events

### `timescale_client.py` → Time-series database
**Imports from:**
- `monitoring.py` → Database metrics

**Used by:**
- `main.py` → Creates instance
- `telemetry_ingestion.py` → Data insertion
- `analytics_service.py` → Data queries

### `telemetry_ingestion.py` → Kafka to TimescaleDB
**Imports from:**
- `timescale_client.py` → Database operations
- `monitoring.py` → Ingestion metrics

**Receives from `main.py`:**
- TimescaleDB client instance

## API Layer

### `api_server.py` → REST API endpoints
**Imports from:**
- `config.py` → Supabase configuration
- `supabase_client.py` → User data operations
- `auth_manager.py` → Auth decorators
- `monitoring.py` → API logging

**Receives from `main.py`:**
- Supabase configuration
- Supabase client instance
- Auth manager instance

### `auth_manager.py` → Authentication/authorization
**Imports from:**
- `config.py` → Supabase configuration
- `supabase_client.py` → User data
- `monitoring.py` → Auth logging

**Receives from `main.py`:**
- Supabase configuration
- Supabase client instance

### `supabase_client.py` → User data management
**Imports from:**
- `config.py` → Database configuration
- `monitoring.py` → Database logging

**Receives from `main.py`:**
- Supabase configuration

**Used by:**
- `auth_manager.py` → User authentication
- `api_server.py` → Data operations
- `data_sync.py` → Data synchronization

## Data Synchronization

### `data_sync.py` → TimescaleDB ↔ Supabase sync
**Imports from:**
- `config.py` → Database configuration
- `supabase_client.py` → Supabase operations
- `monitoring.py` → Sync logging

**Receives from `main.py`:**
- Supabase configuration
- Supabase client instance

### `database_schema.py` → Database schema management
**Imports from:**
- `config.py` → Database configuration
- `monitoring.py` → Schema logging

**Used by:**
- Database initialization scripts
- Migration processes

## Business Logic

### `v2x_controller.py` → V2X operations
**Imports from:**
- `redis_enhanced.py` → State management
- `kafka_producer.py` → Event publishing
- `monitoring.py` → V2X metrics

**Receives from `message_handler.py`:**
- Redis client instance
- Kafka producer instance

### `analytics_service.py` → Analytics generation
**Imports from:**
- `timescale_client.py` → Data queries
- `monitoring.py` → Analytics logging

**Receives from `main.py`:**
- TimescaleDB client instance

## Monitoring & Health

### `monitoring.py` → Metrics and logging
**Imports from:**
- None (standalone utility)

**Used by:**
- ALL other files for metrics and logging

### `health.py` → Health check endpoints
**Imports from:**
- `monitoring.py` → Health checker

**Receives from `main.py`:**
- Health check configuration

## Configuration

### `config.py` → Configuration definitions
**Imports from:**
- None (Pydantic models only)

**Used by:**
- `main.py` → Loads all configurations
- All service files → Receive specific configs

## Key Interaction Patterns

### 1. Dependency Injection Pattern
- `main.py` creates all instances
- Passes dependencies to services
- Services receive what they need

### 2. Service Layer Pattern
- Business logic in dedicated services
- Data access through clients
- Clear separation of concerns

### 3. Event-Driven Pattern
- Kafka for async communication
- Redis for real-time state
- WebSocket for live updates

### 4. Monitoring Integration
- Every file imports `monitoring.py`
- Consistent metrics collection
- Centralized logging

## Review Order for Understanding

### Start Here (Core):
1. `config.py` - Understand configuration structure
2. `main.py` - See how everything connects
3. `monitoring.py` - Understand metrics/logging

### Then WebSocket Layer:
4. `server.py` - Core WebSocket handling
5. `connection_manager.py` - Connection management
6. `message_handler.py` - Message processing
7. `redis_enhanced.py` - Data structures
8. `redis_integration.py` - Redis operations

### Then Data Flow:
9. `kafka_producer.py` - Event streaming
10. `timescale_client.py` - Time-series storage
11. `telemetry_ingestion.py` - Data ingestion

### Then API Layer:
12. `supabase_client.py` - User data
13. `auth_manager.py` - Authentication
14. `api_server.py` - REST endpoints

### Finally Business Logic:
15. `v2x_controller.py` - V2X operations
16. `analytics_service.py` - Analytics
17. `data_sync.py` - Data synchronization
18. `health.py` - Health checks
19. `database_schema.py` - Database schema
