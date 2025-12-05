# TODO Comments Review

**Date:** 2025-01-27  
**Reference:** Phases 0-3 Review - Gap Analysis

## Summary

This document categorizes all TODO comments found in the codebase as either:
- **Acceptable MVP Placeholder**: Documented for future implementation, acceptable for MVP
- **Needs Attention**: Should be addressed or tracked for near-term implementation

## TODO Comments by File

### src/core/state/assembler.py

#### Line 143: Battery SoC Query
```python
# TODO: Query from battery_storage table when implemented
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** Returns fixed value (0.5) for MVP. Battery storage is a future feature per PRD.  
**Action:** Document for Phase 4+ implementation.

#### Line 321: Demand Charge Rate Query
```python
# TODO: Query from depots table when demand_charge_rate_kw column exists
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** Returns hardcoded PG&E E-19 rate ($20/kW) for MVP. Depot-specific rates are future enhancement.  
**Action:** Document for Phase 4+ implementation.

#### Line 340: Building Power Query
```python
# TODO: Query from building_loads table when implemented
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** Returns zero for MVP. Building load integration is a future feature.  
**Action:** Document for Phase 4+ implementation.

---

### src/api/main.py

#### Line 137: Depot Config Query
```python
# TODO: Query from depots, vehicles, chargers, battery_storage tables
```
**Status:** ⚠️ **Needs Attention (Low Priority)**  
**Rationale:** Currently returns hardcoded default config. For MVP, this is acceptable but should be tracked for Phase 4.  
**Action:** Create ticket for Phase 4 implementation. For MVP, document that default config is used.

---

### src/adapters/ocpp/server.py

#### Line 205: Vehicle ID Mapping
```python
vehicle_id = charge_point_id  # TODO: Map to actual vehicle_id
```
**Status:** ⚠️ **Needs Attention**  
**Rationale:** The `src/adapters/ocpp/mapping.py` helper exists but is not used here. Should use `get_vehicle_id_from_charger()` from mapping helper.  
**Action:** Update to use mapping helper:
```python
from src.adapters.ocpp.mapping import get_vehicle_id_from_charger
vehicle_id = await get_vehicle_id_from_charger(self.pool, charge_point_id)
```

---

### src/db/models.py

#### Line 6: SQLAlchemy Models
```python
# TODO: Implement SQLAlchemy models based on PRD Section 6.1
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** Project uses `asyncpg` directly for database operations, not SQLAlchemy. Models are defined as Python dataclasses in `src/core/models.py`. SQLAlchemy models would be redundant for current architecture.  
**Action:** Remove TODO or update to note that asyncpg + dataclasses is the chosen approach.

---

### src/websocket_handler/timescale_client.py

#### Line 3146: Running Status
```python
"is_running": False,  # TODO: Implement actual running status
"next_run_time": None,  # TODO: Implement next run time calculation
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** WebSocket handler features are outside MVP scope (Phases 0-3).  
**Action:** Document for future phase.

---

### src/websocket_handler/fleet_api.py

#### Line 99: Station ID
```python
"station_id": "default_station",  # TODO: Get from vehicle mapping
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** WebSocket handler features are outside MVP scope (Phases 0-3).  
**Action:** Document for future phase.

---

### src/websocket_handler/demand_forecaster.py

#### Line 589: Station IDs
```python
station_ids = ["default_station"]  # TODO: Get from configuration
```
**Status:** ✅ **Acceptable MVP Placeholder**  
**Rationale:** WebSocket handler features are outside MVP scope (Phases 0-3).  
**Action:** Document for future phase.

---

## Recommendations

### Immediate Actions

1. **Fix OCPP Server Vehicle ID Mapping** (Priority: Medium)
   - File: `src/adapters/ocpp/server.py:205`
   - Use existing mapping helper from `src/adapters/ocpp/mapping.py`
   - This ensures proper vehicle-to-charger association

### Short-term (Phase 4)

2. **Implement Depot Config Query** (Priority: Low)
   - File: `src/api/main.py:137`
   - Query actual depot configuration from database
   - Replace hardcoded defaults

### Long-term (Post-MVP)

3. **Battery Storage Integration**
   - File: `src/core/state/assembler.py:143`
   - Query from `battery_storage` table

4. **Depot-Specific Demand Charge Rates**
   - File: `src/core/state/assembler.py:321`
   - Query from `depots` table

5. **Building Load Integration**
   - File: `src/core/state/assembler.py:340`
   - Query from `building_loads` table

6. **WebSocket Handler Enhancements**
   - Files: `src/websocket_handler/*.py`
   - Implement actual running status, station mappings, etc.

---

## Conclusion

**Total TODOs Found:** 10  
**Acceptable MVP Placeholders:** 8  
**Needs Attention:** 2

Most TODO comments are acceptable placeholders for MVP. The main item requiring attention is the OCPP server vehicle ID mapping, which should use the existing mapping helper.

