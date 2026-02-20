# Testing Documentation

## Overview

This document describes the testing suite for the Favonius Energy EV Fleet Depot Optimization Platform, including test coverage, setup requirements, and known gaps.

## Test Structure

### Test Categories

- **Unit Tests** (`tests/unit/`): Test individual components in isolation
- **Integration Tests** (`tests/integration/`): Test component interactions
- **Acceptance Tests** (`tests/integration/test_acceptance_*.py`): PRD acceptance criteria validation
- **Performance Tests** (`tests/integration/test_performance_*.py`): Performance benchmarks
- **E2E Tests** (`tests/e2e/`): End-to-end system tests
- **Load Tests** (`tests/load/`): Load and stress testing
- **Security Tests** (`tests/security/`): Security validation
- **Optimization Tests** (`tests/optimization/`): Optimization-specific tests

## Current Test Status

### Passing Tests (337+)

- Core unit tests: optimizer, controller, state assembler, triggers, surrogate model
- Core integration tests: trigger monitor, state-to-optimizer, control loop, full pipeline
- Acceptance tests: AT-01 through AT-07 (after fixes)

### Test Collection Issues

**65 test files cannot be collected** due to missing Python dependencies in the current environment:

- `httpx` - Required for HTTP client tests
- `structlog` - Required for structured logging tests
- `cryptography` - Required for security/encryption tests
- `sqlalchemy` - Required for ORM-based database tests

These tests will run once dependencies are installed:

```bash
pip install httpx structlog cryptography sqlalchemy
```

## Test Setup Requirements

### Basic Setup

1. **Python Environment**: Python 3.12+
2. **Dependencies**: Install from `requirements.txt`
3. **Test Database**: Optional - most tests use mocks

```bash
# Install test dependencies
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-benchmark

# Run all tests
pytest

# Run specific test categories
pytest -m unit
pytest -m integration
pytest -m acceptance
pytest -m performance
```

### Full Test Suite Setup

For complete test coverage including database and OCPP tests:

1. **TimescaleDB Instance**:
   ```bash
   # Using Docker
   docker run -d --name timescaledb \
     -e POSTGRES_PASSWORD=postgres \
     -p 5432:5432 \
     timescale/timescaledb:latest-pg15
   
   # Set environment variable
   export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test
   ```

2. **OCPP Charger Simulators**:
   - Required for `test_ocpp_*.py` tests
   - See `scripts/simulation/` for simulator setup

3. **Additional Dependencies**:
   ```bash
   pip install httpx structlog cryptography sqlalchemy
   ```

### EVerest Docker Smoke Test (closed-loop OCPP)

Use EVerest SIL to validate backend communication with a standards-grade simulated charger:

```bash
./scripts/everest/run_backend_everest_smoke.sh
```

Optional pytest wrapper (explicit opt-in only):

```bash
RUN_EVEREST_DOCKER_TEST=1 pytest tests/integration/test_everest_docker_smoke.py -v
```

Reference: `docs/EVEREST_TESTING.md`

## Test Markers

Configured in `pytest.ini`:

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.acceptance` - PRD acceptance criteria tests
- `@pytest.mark.performance` - Performance benchmarks
- `@pytest.mark.e2e` - End-to-end tests
- `@pytest.mark.slow` - Slow-running tests
- `@pytest.mark.database` - Tests requiring real database
- `@pytest.mark.security` - Security tests

## Known Test Gaps

### 1. Database Integration Tests

**Files**: `tests/integration/test_database_real.py`, `tests/unit/test_database_*.py`

**Status**: Cannot run without TimescaleDB instance

**Requirements**:
- Running TimescaleDB instance
- Test database schema initialized
- Connection string configured

### 2. OCPP Protocol Tests

**Files**: `tests/integration/test_ocpp_*.py`, `tests/unit/test_ocpp_*.py`

**Status**: Cannot run without OCPP charger simulators

**Requirements**:
- OCPP 1.6/2.0.1 charger simulators
- Network connectivity
- OCPP server running

### 3. Full E2E Tests

**Files**: `tests/e2e/test_*.py`

**Status**: Some tests require full system setup

**Requirements**:
- All services running (API, optimizer, database, OCPP)
- Test environment configured
- External dependencies available

### 4. Load and Stress Tests

**Files**: `tests/load/test_*.py`

**Status**: Requires dedicated test environment

**Requirements**:
- Isolated test environment
- Performance monitoring tools
- Sufficient resources for load generation

### 5. Security Tests

**Files**: `tests/security/test_*.py`

**Status**: Some tests require `cryptography` library

**Requirements**:
- `cryptography` package installed
- Security testing tools (optional)

## Performance Test Expectations

### Warm-Start Speedup

**Current**: Varies by solver
**Target**: > 3x speedup (PRD Section 8.5)

**Note**: Warm-start speedup with Gurobi:
- Gurobi supports effective warm-starting via WarmStart option
- Expected > 3x speedup per PRD Section 8.5
- Model rebuilding overhead is minimal with proper implementation

### Solve Time Targets

- **10 vehicles**: < 60 seconds
- **20 vehicles**: < 60 seconds (PRD Section 8.3 requirement)
- **50 vehicles**: < 60 seconds (may require optimization tuning)

**Note:** Solve time targets apply to both Gurobi (primary) and HiGHS (fallback) solvers. HiGHS may be slower but must still meet the < 60 second requirement per PRD Section 8.2.

## Acceptance Test Coverage

All PRD acceptance criteria (AT-01 through AT-07) are validated:

- **AT-01**: End-to-end optimization (99% SoC at departure)
- **AT-02**: Demand charge reduction
- **AT-03**: Price spike re-optimization
- **AT-04**: SoC deviation handling
- **AT-05**: Return time deviation handling
- **AT-06**: Inter-depot handoff
- **AT-07**: Building load integration

## Running Tests

### Quick Test Run

```bash
# Run all unit and integration tests (no external dependencies)
pytest -m "unit or integration" --ignore=tests/e2e --ignore=tests/load

# Run acceptance tests only
pytest -m acceptance

# Run with coverage
pytest --cov=src --cov-report=html
```

### Full Test Suite

```bash
# Install all dependencies first
pip install -r requirements.txt httpx structlog cryptography sqlalchemy

# Set up test database
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/favonius_test

# Run all tests
pytest
```

### Specific Test Categories

```bash
# Unit tests only
pytest -m unit

# Integration tests
pytest -m integration

# Performance benchmarks
pytest -m performance

# Slow tests (may take longer)
pytest -m slow
```

## Test Coverage Goals

- **Optimizer**: ≥ 90% coverage (PRD Section 11.2)
- **Surrogate Model**: ≥ 90% coverage (PRD Section 11.2)
- **Overall**: ≥ 80% coverage

## Solver Reliability Tests

Per PRD Section 8.2, the system must support automatic fallback to HiGHS when Gurobi fails:

- **Gurobi license failure test**: Verify automatic fallback to HiGHS
- **Solver tracking**: Verify `solver_used` field in OptimizationResult
- **Fallback logging**: Verify fallback events are logged with reason
- **Health endpoint**: Verify solver availability reporting

Reference: PRD Section 8.2, Integration Test Requirements (PRD Section 11.3)

## Continuous Integration

Tests should run in CI/CD pipeline with:

1. Core unit and integration tests (no external dependencies)
2. Acceptance tests
3. Performance benchmarks (may be run separately)
4. Coverage reporting

## Troubleshooting

### Common Issues

1. **Missing dependencies**: Install from `requirements.txt`
2. **Database connection errors**: Check `TEST_DATABASE_URL` environment variable
3. **OCPP connection failures**: Ensure charger simulators are running
4. **Infeasible optimization errors**: Some test scenarios may be infeasible - this is expected in simulation tests

### Test Failures

If tests fail:

1. Check test output for specific error messages
2. Verify all dependencies are installed
3. Ensure test environment is configured correctly
4. Review test logs for detailed error information

## Future Improvements

1. **Mock OCPP Servers**: Create mock OCPP servers for testing without real chargers
2. **Test Fixtures**: Improve test fixtures for database and OCPP scenarios
3. **Test Data**: Create comprehensive test data sets for various scenarios
4. **Performance Baselines**: Establish performance baselines for regression testing
5. **Coverage Gaps**: Increase coverage for edge cases and error handling
