# Integration Tests for Main API ↔ WebSocket Handler

## Overview

This directory contains comprehensive integration tests for the integrated architecture where:
- **WebSocket Handler** is telemetry-only (OCPP → Main API + TimescaleDB)
- **Main API** handles all optimization and communicates with WebSocket Handler via internal API
- **Backup heuristic** activates when Main API is down > 1 hour

## Test Files

### 1. `test_main_api_websocket_integration.py`

**Purpose**: Tests the integration between Main API and WebSocket Handler.

**Test Classes**:
- `TestMainAPIToWebSocketHandler` - Main API querying WebSocket Handler
- `TestTelemetryFlow` - Telemetry flow from charger to TimescaleDB and Main API
- `TestCommandDispatchFlow` - Command dispatch from Main API via WebSocket Handler
- `TestBackupHeuristicActivation` - Backup heuristic activation/deactivation
- `TestEndToEndIntegration` - Complete end-to-end flows
- `TestResilienceAndRecovery` - Error handling and resilience patterns

**Key Test Scenarios**:
- ✅ Main API can query WebSocket Handler for connected charge points
- ✅ Main API can query charge point state
- ✅ Main API can send SetChargingProfile via WebSocket Handler
- ✅ Telemetry flows from charger → WebSocket Handler → TimescaleDB
- ✅ Telemetry is forwarded to Main API for state assembly
- ✅ Optimization results are dispatched via WebSocket Handler
- ✅ Backup heuristic activates after Main API down > 1 hour
- ✅ Backup heuristic deactivates when Main API recovers
- ✅ End-to-end flow: charger → telemetry → optimization → command
- ✅ Error handling when WebSocket Handler is unavailable
- ✅ Circuit breaker pattern for repeated failures
- ✅ Retry logic with exponential backoff

### 2. `test_websocket_handler_internal_api.py`

**Purpose**: Tests the internal API endpoints exposed by WebSocket Handler.

**Test Classes**:
- `TestInternalAPIEndpoints` - All internal API endpoints
- `TestInternalAPIAuthentication` - Authentication and authorization
- `TestInternalAPIErrorHandling` - Error handling

**Key Test Scenarios**:
- ✅ `GET /internal/charge-points` - List all connected charge points
- ✅ `GET /internal/charge-points/{station_id}` - Get charge point state
- ✅ `POST /internal/charge-points/{station_id}/set-charging-profile` - Send command
- ✅ `GET /internal/health` - Health check
- ✅ 404 handling for non-existent charge points
- ✅ Error handling for connection manager failures
- ✅ Error handling for charge point send failures

### 3. `test_ocpp_client_integration.py`

**Purpose**: Tests the OCPP client that Main API uses to communicate with WebSocket Handler.

**Test Classes**:
- `TestOCPPClientImplementation` - Client implementation
- `TestOCPPClientErrorHandling` - Error handling
- `TestOCPPClientCircuitBreaker` - Circuit breaker pattern

**Key Test Scenarios**:
- ✅ Client initialization with base URL and API key
- ✅ Getting connected charge points via HTTP
- ✅ Getting charge point state via HTTP
- ✅ Sending charging profile via HTTP
- ✅ Connection error handling
- ✅ Retry logic for transient failures
- ✅ Timeout handling
- ✅ Circuit breaker opens after max failures
- ✅ Circuit breaker closes after recovery

## Current Test Status

**✅ 13/13 tests passing** in `test_main_api_websocket_integration.py`

See [TEST_STATUS.md](TEST_STATUS.md) for detailed status and known issues.

## Running the Tests

### Run All Integration Tests

```bash
# Run all integration tests
pytest tests/integration/test_main_api_websocket_integration.py -v

# Run with coverage
pytest tests/integration/test_main_api_websocket_integration.py \
  --cov=src \
  --cov-report=html:tests/results/coverage_html \
  --cov-report=term-missing
```

### Known Issues

- ⚠️ Tests currently use `ocpp_server` instead of `ocpp_client` (temporary until Phase 4)
- ⚠️ `OptimizationEngine` is mocked to avoid websocket_handler config dependencies
- ⚠️ 3 non-blocking warnings about async coroutines (tests still pass)
- ⚠️ `test_websocket_handler_internal_api.py` and `test_ocpp_client_integration.py` require `httpx` package (not yet installed)

### Run Specific Test Classes

```bash
# Test Main API to WebSocket Handler communication
pytest tests/integration/test_main_api_websocket_integration.py::TestMainAPIToWebSocketHandler -v

# Test telemetry flow
pytest tests/integration/test_main_api_websocket_integration.py::TestTelemetryFlow -v

# Test command dispatch
pytest tests/integration/test_main_api_websocket_integration.py::TestCommandDispatchFlow -v

# Test backup heuristic
pytest tests/integration/test_main_api_websocket_integration.py::TestBackupHeuristicActivation -v

# Test end-to-end integration
pytest tests/integration/test_main_api_websocket_integration.py::TestEndToEndIntegration -v
```

### Run Internal API Tests

```bash
# Test internal API endpoints
pytest tests/integration/test_websocket_handler_internal_api.py -v
```

### Run OCPP Client Tests

```bash
# Test OCPP client
pytest tests/integration/test_ocpp_client_integration.py -v
```

## Test Dependencies

These tests use mocks for:
- Database connections (`mock_db_pool`)
- TimescaleDB client (`mock_timescale_client`)
- Connection manager (`mock_connection_manager`)
- OCPP client (`mock_ocpp_client`)
- WebSocket Handler internal API (`mock_websocket_handler_internal_api`)

For tests that require real components, use:
- `real_db_pool` fixture (requires `TEST_DATABASE_URL` environment variable)
- Real TimescaleDB connection (requires test database setup)

## Test Coverage

### Integration Points Covered

1. **Main API → WebSocket Handler**
   - Querying charge points
   - Getting charge point state
   - Sending commands

2. **WebSocket Handler → Main API**
   - Internal API endpoints
   - Health checks
   - Error responses

3. **Telemetry Flow**
   - Charger → WebSocket Handler → TimescaleDB
   - Charger → WebSocket Handler → Main API

4. **Command Flow**
   - Main API → WebSocket Handler → Charger

5. **Backup Heuristic**
   - Activation conditions
   - Deactivation on recovery
   - Health check monitoring

6. **Error Handling**
   - Connection failures
   - Timeout handling
   - Circuit breaker pattern
   - Retry logic

## Future Enhancements

1. **Real Component Tests**
   - Tests with actual WebSocket Handler running
   - Tests with actual Main API running
   - End-to-end tests with real chargers (OCPP simulator)

2. **Performance Tests**
   - Latency measurements for command dispatch
   - Throughput tests for telemetry ingestion
   - Load tests for internal API

3. **Chaos Engineering**
   - Network partition scenarios
   - Service degradation tests
   - Recovery time measurements

## References

- Architecture Integration Plan: `.cursor/plans/architecture_integration_review_&_refactoring_*.plan.md`
- PRD Section 5.2: Component Responsibilities
- PRD Section 9.1: OCPP Integration
