# Test Fixture Update Progress

**Date:** 2025-12-13  
**Status:** In Progress - Critical fixes completed, bulk updates remaining

## Completed ✅

### Core Fixtures Updated
- ✅ `tests/conftest.py` - `depot_config_factory` updated with backward compatibility
- ✅ `tests/conftest.py` - `sample_depot_config` updated
- ✅ `tests/conftest.py` - `sample_depot_config_small` updated

### Critical Property Access Fixes
- ✅ `tests/integration/test_state_to_optimizer.py` - Fixed `depot_config.n_chargers` access
- ✅ `tests/integration/test_full_pipeline.py` - Fixed `config.n_chargers` access (2 places)
- ✅ `tests/integration/test_performance_benchmarks.py` - Fixed `config.n_chargers` and `config.charger_power` access
- ✅ `tests/unit/test_optimizer.py` - Fixed `simple_depot_config.charger_power` access
- ✅ `tests/unit/test_optimizer.py` - Fixed `simple_depot_config.n_chargers` access (2 places)
- ✅ `tests/unit/test_optimizer.py` - Updated `test_more_vehicles_than_chargers` to use charger_groups

## Remaining Work ⚠️

### High Priority - Direct Property Access
1. **`tests/unit/test_models.py`** - Multiple tests assert on `charger_power` and `n_chargers`:
   - `test_depot_config_basic` (lines 42-44)
   - `test_depot_config_to_dict` (line 96)
   - `test_depot_config_large_fleet` (line 113)
   - Plus tests that create DepotConfig with old pattern (lines 33-39, 51-59, 66-76, 83-91, 102-110)

2. **`tests/integration/test_acceptance_at05_full.py`** - Creates configs and copies properties:
   - `depot_a_config` fixture (lines 38-50) - needs update
   - `depot_b_config` fixture (lines 52-64) - needs update
   - Multiple places copy `depot_b_config.charger_power` and `depot_b_config.n_chargers` (lines 247, 249, 380, 382, 522, 524, 582, 584)

### Medium Priority - DepotConfig Creation
~180 test files create DepotConfig instances using old pattern. These will fail when fixtures are updated. Priority order:

1. Acceptance tests (`test_acceptance_*.py`)
2. Integration tests (`test_*_integration.py`)
3. Unit tests (`test_*.py`)

## Update Pattern Reference

### Creating DepotConfig
```python
# OLD
DepotConfig(
    vehicle_capacities={...},
    charger_power=80.0,
    n_chargers=5,
    ...
)

# NEW
vehicle_ids = ['bus_1', 'bus_2', ...]
DepotConfig(
    vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
    vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},  # Required
    charger_groups={80.0: 5},  # Required: dict[rated_kw, count]
    charger_efficiency=0.95,
    charger_vehicle_access={},  # Required (empty = all accessible)
    battery_efficiency=0.92,  # Default but explicit
    battery_soc_min=0.2,  # Default but explicit
    battery_soc_max=0.8,  # Default but explicit
    ...
)
```

### Accessing Properties
```python
# OLD
n_chargers = config.n_chargers
charger_power = config.charger_power

# NEW
n_chargers = sum(config.charger_groups.values())
charger_power = list(config.charger_groups.keys())[0]  # Single group
# OR
total_power = sum(kw * count for kw, count in config.charger_groups.items())
```

## Next Steps

1. **Fix test_models.py** - Rewrite tests to use charger_groups
2. **Fix test_acceptance_at05_full.py** - Update fixtures and property accesses
3. **Run test suite** - Identify remaining failures
4. **Systematic update** - Fix remaining files in priority order
5. **Add helper functions** - Consider adding `get_total_chargers()` and `get_charger_power()` to conftest.py

## Estimated Remaining Work

- **High Priority:** 2 files (test_models.py, test_acceptance_at05_full.py)
- **Medium Priority:** ~20-30 files (acceptance and critical integration tests)
- **Low Priority:** ~150 files (other unit and integration tests)

**Total:** ~180 files need DepotConfig creation pattern updates
