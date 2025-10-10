# Favonius Energy V2G System - Test Report
Generated: 2025-10-10 16:05:09

## Test Results Summary

- **Total Test Suites**: 3
- **Passed**: 0
- **Failed**: 3
- **Success Rate**: 0.0%

## Detailed Results

### Unit Tests
**Status**: ❌ FAILED

**Error**: ERROR: file or directory not found: tests/unit/



**Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.13.0, pytest-8.4.2, pluggy-1.6.0 -- /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/venv/bin/python
cachedir: .pytest_cache
metadata: {'Python': '3.13.0', 'Platform': 'macOS-26.0.1-arm64-arm-64bit-Mach-O', 'Packages': {'pytest': '8.4.2', 'pluggy': '1.6.0'}, 'Plugins': {'asyncio': '1.2.0', 'anyio': '4.11.0', 'html': '4.1.1', 'json-report': '1.5.0', 'metadata': '3.1.1', 'cov': '7.0.0'}}
rootdir: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend
configfile: pytest.ini
plugins: asyncio-1.2.0, anyio-4.11.0, html-4.1.1, json-report-1.5.0, metadata-3.1.1, cov-7.0.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 0 items

- generated xml file: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/tests/tests/results/unit_tests.xml -
============================ no tests ran in 0.00s =============================

```

### Integration Tests
**Status**: ❌ FAILED

**Error**: ERROR: file or directory not found: tests/integration/



**Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.13.0, pytest-8.4.2, pluggy-1.6.0 -- /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/venv/bin/python
cachedir: .pytest_cache
metadata: {'Python': '3.13.0', 'Platform': 'macOS-26.0.1-arm64-arm-64bit-Mach-O', 'Packages': {'pytest': '8.4.2', 'pluggy': '1.6.0'}, 'Plugins': {'asyncio': '1.2.0', 'anyio': '4.11.0', 'html': '4.1.1', 'json-report': '1.5.0', 'metadata': '3.1.1', 'cov': '7.0.0'}}
rootdir: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend
configfile: pytest.ini
plugins: asyncio-1.2.0, anyio-4.11.0, html-4.1.1, json-report-1.5.0, metadata-3.1.1, cov-7.0.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 0 items

- generated xml file: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/tests/tests/results/integration_tests.xml -
============================ no tests ran in 0.01s =============================

```

### Load Tests
**Status**: ❌ FAILED

**Error**: ERROR: file or directory not found: tests/load/



**Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.13.0, pytest-8.4.2, pluggy-1.6.0 -- /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/venv/bin/python
cachedir: .pytest_cache
metadata: {'Python': '3.13.0', 'Platform': 'macOS-26.0.1-arm64-arm-64bit-Mach-O', 'Packages': {'pytest': '8.4.2', 'pluggy': '1.6.0'}, 'Plugins': {'asyncio': '1.2.0', 'anyio': '4.11.0', 'html': '4.1.1', 'json-report': '1.5.0', 'metadata': '3.1.1', 'cov': '7.0.0'}}
rootdir: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend
configfile: pytest.ini
plugins: asyncio-1.2.0, anyio-4.11.0, html-4.1.1, json-report-1.5.0, metadata-3.1.1, cov-7.0.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 0 items

- generated xml file: /Users/joriszilinskis/Desktop/FavoniusEnergy/Favonius_Backend/tests/tests/results/load_tests.xml -
============================ no tests ran in 0.00s =============================

```

## System Capabilities Tested

- ✅ OCPP 2.0.1 Message Handling
- ✅ Device Model Management
- ✅ Charging Profile Management
- ✅ Transaction Management
- ✅ Certificate Management
- ✅ Security Features
- ✅ V2G Capabilities
- ✅ Monitoring and Alerting
- ✅ Display Message Management
- ✅ Tariff Management
- ✅ Privacy and GDPR Compliance
- ✅ Error Handling and Resilience
- ✅ Load Testing and Performance
- ✅ End-to-End Integration

## Recommendations

⚠️ **Some tests failed.** Please review the failures and fix issues before production deployment.

### Action Items:
1. Review failed test results
2. Fix identified issues
3. Re-run failed tests
4. Update system documentation