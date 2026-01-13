# Integration Test Execution Guide

**Last Updated**: 2025-01-XX  
**Test Suite**: Main API ↔ WebSocket Handler Integration

## Prerequisites

### Required Packages

```bash
# Core dependencies (from requirements.txt)
pip install pytest pytest-asyncio pytest-mock
pip install asyncpg  # Database mocking
pip install fastapi  # API testing
```

### Optional Packages

```bash
# For TestClient and HTTP testing
pip install httpx

# For coverage reporting
pip install pytest-cov
```

**Note**: Tests that require `httpx` will be automatically skipped if not installed.

## Running Tests

### Run All Integration Tests

```bash
# Run all tests in integration directory
pytest tests/integration/ -v

# Run with coverage
pytest tests/integration/ --cov=src --cov-report=term-missing

# Run without warnings
pytest tests/integration/ -v -W ignore::RuntimeWarning
```

### Run Specific Test Files

```bash
# Main API ↔ WebSocket Handler integration
pytest tests/integration/test_main_api_websocket_integration.py -v

# Internal API tests (requires httpx)
pytest tests/integration/test_websocket_handler_internal_api.py -v

# OCPP client tests (requires httpx)
pytest tests/integration/test_ocpp_client_integration.py -v

# API integration tests (requires httpx)
pytest tests/integration/test_api_integration.py -v
```

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

# Test resilience patterns
pytest tests/integration/test_main_api_websocket_integration.py::TestResilienceAndRecovery -v
```

### Run Specific Tests

```bash
# Single test
pytest tests/integration/test_main_api_websocket_integration.py::TestMainAPIToWebSocketHandler::test_query_connected_charge_points -v

# Multiple tests with pattern
pytest tests/integration/test_main_api_websocket_integration.py -k "telemetry" -v
```

## Test Output

### Successful Test Run

```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.4.2
collected 13 items

tests/integration/test_main_api_websocket_integration.py::TestMainAPIToWebSocketHandler::test_query_connected_charge_points PASSED [  7%]
...
============================== 13 passed in 0.15s ==============================
```

### Skipped Tests (Missing Dependencies)

```
tests/integration/test_websocket_handler_internal_api.py::TestInternalAPIEndpoints SKIPPED [1] (Requires httpx package)
```

## Common Issues and Fixes

### Issue: `ModuleNotFoundError: No module named 'httpx'`

**Symptom**: Test collection fails with import error

**Fix**:
```bash
# Install httpx
pip install httpx

# Or skip tests that require httpx (they will auto-skip)
pytest tests/integration/ -v
```

**Note**: Tests that require `httpx` will automatically skip if not installed.

### Issue: `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited`

**Symptom**: Warnings appear in test output

**Status**: ✅ **FIXED** - All RuntimeWarnings have been resolved

**If you see this warning**:
- Check that `StateAssembler.load_depot_config` is properly mocked
- Ensure database mocks return proper values, not coroutines
- See `tests/integration/test_main_api_websocket_integration.py` for examples

### Issue: `PytestUnknownMarkWarning: Unknown pytest.mark.acceptance`

**Symptom**: Warnings about unknown markers

**Status**: ✅ **FIXED** - Markers registered in `tests/conftest.py`

**If you see this warning**:
- Verify `pytest.ini` has marker definitions
- Check `tests/conftest.py` has `pytest_configure` function
- Run `pytest --markers` to see registered markers

### Issue: Tests fail with `TypeError: object Mock can't be used in 'await' expression`

**Symptom**: AsyncMock not properly configured

**Fix**: Ensure async methods are mocked with `AsyncMock`:
```python
mock_client.get_state = AsyncMock(return_value={"connected": True})
# Not: mock_client.get_state = Mock(return_value={"connected": True})
```

### Issue: `OptimizationResult` initialization errors

**Symptom**: `TypeError: __init__() got an unexpected keyword argument`

**Fix**: Use correct field names:
```python
OptimizationResult(
    peak_demand_kw=250.0,  # Not: peak_demand
    solve_time_s=5.0,      # Not: solve_time
    ...
)
```

## Test Architecture

### Current State (Temporary Workarounds)

1. **OCPP Client vs Server**
   - **Current**: Tests use `ocpp_server` parameter (temporary)
   - **Future**: Will use `ocpp_client` parameter after Phase 4 implementation
   - **Location**: All `DepotController` instantiations in tests

2. **OptimizationEngine Mocking**
   - **Current**: Tests mock `OptimizationEngine` to avoid websocket_handler config imports
   - **Future**: Will use real `OptimizationEngine` after architecture refactoring

3. **Internal API**
   - **Current**: Tests use mocked FastAPI app
   - **Future**: Will test against real internal API server

## Debugging Tips

### Enable Verbose Output

```bash
# Show all test output
pytest tests/integration/ -v -s

# Show full traceback
pytest tests/integration/ -v --tb=long

# Show only failures
pytest tests/integration/ -v --tb=short
```

### Run Tests in Parallel

```bash
# Install pytest-xdist
pip install pytest-xdist

# Run with 4 workers
pytest tests/integration/ -n 4
```

### Debug Specific Test

```bash
# Run with Python debugger
pytest tests/integration/test_main_api_websocket_integration.py::TestMainAPIToWebSocketHandler::test_query_connected_charge_points --pdb
```

### Check Test Collection

```bash
# List all collected tests
pytest tests/integration/ --collect-only

# Show test markers
pytest --markers
```

## Performance

### Expected Test Times

- **test_main_api_websocket_integration.py**: ~0.15s (13 tests)
- **Full integration suite**: ~2-5s (depending on system)

### Slow Tests

If tests are slow:
- Check for real database connections (should be mocked)
- Verify async operations are properly mocked
- Look for network calls (should be mocked)

## Continuous Integration

### GitHub Actions / CI Configuration

```yaml
# Example CI configuration
- name: Run integration tests
  run: |
    pip install -r requirements.txt
    pip install httpx  # Optional dependency
    pytest tests/integration/ -v --cov=src --cov-report=xml
```

## Next Steps

1. ✅ All immediate test issues fixed
2. ⏳ Implement OCPP client (Phase 4)
3. ⏳ Implement internal API (Phase 3)
4. ⏳ Update tests to use real implementations
5. ⏳ Add performance and chaos engineering tests

## References

- [TEST_STATUS.md](TEST_STATUS.md) - Current test status and known issues
- [INTEGRATION_TESTS_README.md](INTEGRATION_TESTS_README.md) - Test documentation
- [PRD_v2.md](../../docs/PRD_v2.md) - Product requirements
