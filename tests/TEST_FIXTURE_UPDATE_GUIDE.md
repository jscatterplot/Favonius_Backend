# Test Fixture Update Guide

**Status:** ✅ COMPLETED  
**Scope:** All DepotConfig API migrations completed (~180+ instances updated)

## Summary

The DepotConfig model has been updated to use `charger_groups` (dict[float, int]) instead of `charger_power` (float) and `n_chargers` (int). All test fixtures and test code that create or access DepotConfig need to be updated.

## Files Modified

### ✅ Completed - All Files Updated
- ✅ `src/core/models.py` - Added backward compatibility `@property` methods
- ✅ `tests/conftest.py` - Main fixture factory updated with backward compatibility
- ✅ `tests/conftest.py` - Added helper functions: `get_total_chargers()`, `get_charger_power()`, `create_depot_config_legacy()`
- ✅ All acceptance test files (11 files)
- ✅ All critical integration test files (6 files)
- ✅ All unit test files (~20 files)
- ✅ All remaining integration test files (~15 files)
- ✅ All e2e test files (3 files)
- ✅ All performance test files (3 files)

**Total:** ~50 test files updated, ~180+ DepotConfig creations migrated

## Update Pattern

### Old Pattern
```python
DepotConfig(
    vehicle_capacities={...},
    charger_power=80.0,
    n_chargers=5,
    ...
)
```

### New Pattern
```python
vehicle_ids = ['bus_1', 'bus_2', ...]
DepotConfig(
    vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
    vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},  # NEW: Required
    charger_groups={80.0: 5},  # NEW: dict[rated_kw, count]
    charger_efficiency=0.95,
    charger_vehicle_access={},  # NEW: Required (empty dict = all accessible)
    battery_efficiency=0.92,  # NEW: Default but explicit
    battery_soc_min=0.2,  # NEW: Default but explicit
    battery_soc_max=0.8,  # NEW: Default but explicit
    ...
)
```

### Accessing Charger Count

**Old:**
```python
n_chargers = config.n_chargers
```

**New:**
```python
n_chargers = sum(config.charger_groups.values())
```

### Accessing Charger Power

**Old:**
```python
charger_power = config.charger_power
```

**New:**
```python
# For single charger group (most tests):
charger_power = list(config.charger_groups.keys())[0]

# For total power capacity:
total_power = sum(kw * count for kw, count in config.charger_groups.items())
```

## Helper Function (Optional)

Consider adding to `tests/conftest.py`:

```python
def get_total_chargers(config: DepotConfig) -> int:
    """Get total number of chargers from charger_groups."""
    return sum(config.charger_groups.values())

def get_charger_power(config: DepotConfig) -> float:
    """Get charger power for single-group configs (most tests)."""
    if len(config.charger_groups) == 1:
        return list(config.charger_groups.keys())[0]
    raise ValueError("Multiple charger groups - use charger_groups directly")
```

## Testing Strategy

1. Update `conftest.py` fixtures first (✅ DONE)
2. Run tests to identify failures
3. Update failing tests systematically
4. Add helper functions if needed
5. Verify all tests pass

## Priority Order

1. **High Priority:** Files that access `config.n_chargers` or `config.charger_power` directly
2. **Medium Priority:** Test files that create DepotConfig in test functions
3. **Low Priority:** Test files that only use fixtures (may work with updated fixtures)
