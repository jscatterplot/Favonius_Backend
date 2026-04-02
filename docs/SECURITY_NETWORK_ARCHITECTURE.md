# Security Network Architecture

**Standard:** IEC 62443-3-2 (Security Zones and Conduits)
**System:** Favonius EV Fleet Depot Optimization Platform

---

## 1. Security Zone Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL (Untrusted)                               │
│                                                                             │
│   Fleet Operators    CAISO/ENTSO-E     OpenMeteo      BMS/ITCS (VDV 463)   │
│   (Browser/App)      Price Feeds       Weather API    Transit Systems       │
└──────┬───────────────────┬───────────────┬──────────────┬───────────────────┘
       │ HTTPS             │ HTTPS          │ HTTPS        │ WSS
       │ JWT Auth          │ API Key        │ Public       │ Schema Validation
       │                   │                │              │
┌──────▼───────────────────▼───────────────▼──────────────▼───────────────────┐
│  Z6: CLOUD / DMZ (SL-2)                                                     │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │  Railway Load Balancer / TLS Termination                            │     │
│  │  - TLS 1.2+ (TLS 1.3 preferred)                                    │     │
│  │  - HSTS (max-age=31536000)                                          │     │
│  │  - Geo-blocking (RU/CN/BY) ← OUTERMOST FILTER                      │     │
│  │  - Rate limiting (100/min API, 10/min optimize)                     │     │
│  │  - Security headers (CSP, X-Frame-Options, etc.)                    │     │
│  └─────────────────────────────────────────────────────────────────────┘     │
└──────┬──────────────────────────────────────────────────┬───────────────────┘
       │ C1: API Ingress                                  │ C8: VDV 463
       │ (JWT validated)                                  │ (Schema validated)
       │                                                  │
┌──────▼──────────────────────────────────────────────────▼───────────────────┐
│  Z1: CLOUD/IT ZONE (SL-2)                                                   │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │  FastAPI REST     │  │  RBAC Engine     │  │  VDV 463 Handler         │   │
│  │  (Port 8000)      │  │  (4 roles,       │  │  (Schema validation,     │   │
│  │                   │  │   13 permissions) │  │   MessageType 3 errors)  │   │
│  │  Endpoints:       │  │                  │  │                          │   │
│  │  /optimize        │  │  Admin           │  │  /vdv463/{presystem_id}  │   │
│  │  /depots/{id}/*   │  │  Operator        │  │                          │   │
│  │  /health          │  │  Viewer          │  └──────────────────────────┘   │
│  │  /admin/*         │  │  Auditor         │                                │
│  └────────┬─────────┘  └──────────────────┘                                │
│           │ C2: Control Path (internal, authorized)                         │
└───────────┼─────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────────────┐
│  Z2: CONTROL ZONE (SL-2/3)                                                  │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │  ControllerMgr   │  │  MILP Optimizer  │  │  StateAssembler          │   │
│  │                   │  │  (Pyomo +        │  │  (Aggregates depot       │   │
│  │  Per-depot        │  │   Gurobi/HiGHS)  │  │   state from DB)        │   │
│  │  DepotController  │  │                  │  │                          │   │
│  │  loops            │  │  60s timeout     │  │  Data freshness checks:  │   │
│  │                   │  │  1% MIP gap      │  │  - Telemetry: 15 min     │   │
│  │  Trigger Monitor: │  │                  │  │  - Prices: 24 hr         │   │
│  │  - Price spike    │  │  Hard constraints│  │  - Building load: 1 hr   │   │
│  │  - SoC deviation  │  │  - SoC >= 99%    │  │  - Schedule: 1 hr        │   │
│  │  - Return delay   │  │  - P_grid <= max │  │                          │   │
│  │  - Scheduled      │  │  - Building load │  │                          │   │
│  └────────┬─────────┘  └──────────────────┘  └──────────────────────────┘   │
│           │ C3: OCPP Command (SetChargingProfile)                           │
└───────────┼─────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────────────┐
│  Z3: OT/OCPP ZONE (SL-3)                                                   │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │  OCPP Server     │  │  FleetChargePoint│  │  Dispatch                 │   │
│  │  (Port 9000)     │  │  (Per-charger    │  │  (SetChargingProfile     │   │
│  │                   │  │   OCPP handler)  │  │   to each charger)       │   │
│  │  Station auth:   │  │                  │  │                          │   │
│  │  - Certificate   │  │  OCPP 1.6 ops:   │  │  Records in              │   │
│  │  - Bearer token  │  │  - BootNotify    │  │  charging_commands       │   │
│  │  - API key       │  │  - MeterValues   │  │  table                   │   │
│  │  - Basic auth    │  │  - StatusNotify  │  │                          │   │
│  │  - OCPP password │  │  - Authorize     │  │                          │   │
│  │                   │  │  - Start/Stop Tx │  │                          │   │
│  │  Zone validation: │  │                  │  │                          │   │
│  │  OCPP_EXPECTED_  │  │                  │  │                          │   │
│  │  IP_RANGES       │  │                  │  │                          │   │
│  └────────┬─────────┘  └──────────────────┘  └──────────────────────────┘   │
│           │ C9: OCPP WebSocket (WSS, charger identity verified)             │
└───────────┼─────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────────────┐
│  Z6: FIELD ZONE (SL-3) — Per Depot                                          │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │  EV Chargers     │  │  Stationary      │  │  Network Switch          │   │
│  │  (EVSE)          │  │  Battery (BESS)  │  │  (Depot LAN)             │   │
│  │                   │  │                  │  │                          │   │
│  │  CCS connectors  │  │  Charge/discharge│  │  Expected IP range:      │   │
│  │  ocpp_id mapped  │  │  via optimizer   │  │  e.g. 10.x.y.0/24       │   │
│  │  to charger_id   │  │                  │  │                          │   │
│  └──────────────────┘  └──────────────────┘  └──────────────────────────┘   │
│                                                                             │
│  Physical security: Depot-level access control, tamper detection            │
└─────────────────────────────────────────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────────────┐
│  Z4: DATA ZONE (SL-2)                                                       │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  TimescaleDB (PostgreSQL 16)                                         │   │
│  │                                                                      │   │
│  │  Hypertables:                    Operational tables:                  │   │
│  │  - telemetry (SoC, kW)          - optimization_runs                  │   │
│  │  - prices ($/kWh)               - charging_commands                  │   │
│  │  - weather_forecasts            - interdepot_messages                │   │
│  │  - building_load                - trigger_log                        │   │
│  │  - security_audit_log (90d)     - connector_status                   │   │
│  │                                                                      │   │
│  │  Reference tables:              Security tables:                      │   │
│  │  - depots, vehicles, chargers   - rate_limit_state                   │   │
│  │  - charger_vehicle_access       - security_audit_log                 │   │
│  │  - battery_storage              - (90-day retention, tamper-evident)  │   │
│  │  - schedules                                                         │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐                                │
│  │  Prometheus      │  │  Grafana         │                                │
│  │  (Metrics store) │  │  (Dashboards)    │                                │
│  └──────────────────┘  └──────────────────┘                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Conduit Specifications

| ID | From | To | Protocol | Auth | Encryption | Rate Limit | Validation |
|---|---|---|---|---|---|---|---|
| C1 | External | Z1 | HTTPS | JWT Bearer | TLS 1.2+ | 100/min (API), 10/min (optimize) | Input validators, UUID format |
| C2 | Z1 | Z2 | Internal call | Authorized via RBAC | N/A (same process) | Trigger cooldown (5 min/depot) | Permission check |
| C3 | Z2 | Z3 | Internal dispatch | System-level | N/A (same process) | Per-optimization | Physical constraint validation |
| C4 | Z3 | Z4 | asyncpg | DB credentials | SSL (`sslmode=require`) | Connection pool (max=10) | Parameterized queries |
| C5 | Z1/Z2 | Z4 | asyncpg | DB credentials | SSL | Connection pool (max=10) | Parameterized queries |
| C6 | Z5 | Z2 | HTTPS (outbound) | API keys | TLS 1.2+ | Fetch interval (900s) | Response validation, staleness check |
| C7 | Z1 | Z1 (inter-depot) | HTTPS | JWT Bearer | TLS 1.2+ | 50 msg/hr per depot pair | Depot-pair validation |
| C8 | Z5 | Z1 | WSS | Pre-system ID | TLS 1.2+ | Per-connection | JSON schema validation |
| C9 | Z6 | Z3 | WSS (OCPP 1.6) | Station auth (multi-method) | TLS 1.2+ | MAX_CONNECTIONS=100 | OCPP message validation |

---

## 3. Data Flow Summary

### Inbound (External → System)

```
Fleet Operator → [TLS] → Geo-block → Rate limit → JWT auth → RBAC → Endpoint
EVSE Charger   → [WSS] → Geo-block → Station auth → Zone IP check → OCPP handler
BMS/ITCS       → [WSS] → Geo-block → Pre-system auth → Schema validation → VDV handler
CAISO/ENTSO-E  → [TLS] → (outbound fetch) → Response validation → Price DB
```

### Outbound (System → External)

```
Optimizer → SetChargingProfile → OCPP dispatch → [WSS] → Charger
Incident  → CSIRT notification → [HTTPS] → CERT-LT (cert@cert.lt)
Metrics   → Prometheus scrape  → [HTTP]  → Prometheus → Grafana
```

### Internal (Zone → Zone)

```
API (Z1) → ControllerManager (Z2) → StateAssembler (Z2) → DB (Z4)
                                   → MILP Solver (Z2)
                                   → Allocator (Z2) → Dispatch (Z3) → DB (Z4)
```

---

## 4. Security Controls by Zone Boundary

### Z6 → Z1 Boundary (Internet → Application)

| Control | Implementation | File |
|---|---|---|
| Geo-blocking | GeoBlockMiddleware (outermost) | `src/security/geo_block.py` |
| Rate limiting | RateLimitMiddleware | `src/security/rate_limiter.py` |
| Security headers | SecurityHeadersMiddleware | `src/security/headers.py` |
| CORS | FastAPI CORSMiddleware, fail on `*` in production | `src/api/main.py` |
| Authentication | JWT verification (Supabase) | `src/security/auth.py` |
| Authorization | RBAC (require_permission / require_role) | `src/security/rbac.py` |

### Z3 → Z6 Boundary (OCPP Server → Field)

| Control | Implementation | File |
|---|---|---|
| Geo-blocking | check_ip_blocked() at connection start | `src/websocket_handler/server.py` |
| Station auth | SecurityManager.authenticate_station() | `src/websocket_handler/server.py` |
| Zone IP validation | OCPP_EXPECTED_IP_RANGES check | `src/websocket_handler/server.py` |
| Connection limits | MAX_CONNECTIONS enforcement | `src/websocket_handler/server.py` |
| Protocol validation | OCPP message schema validation | `src/adapters/ocpp/charge_point.py` |

### Z1/Z2 → Z4 Boundary (Application → Database)

| Control | Implementation | File |
|---|---|---|
| Connection security | asyncpg with SSL | `src/api/main.py` (lifespan) |
| Connection pooling | min=2, max=10 | `src/api/main.py` (lifespan) |
| Query safety | Parameterized queries only | `src/db/queries.py` |
| Audit integrity | Append-only security_audit_log, sequence numbering | `migrations/008_security_audit_log.sql` |

---

## 5. Production Configuration Requirements

### CRITICAL: Zone Separation

- **`OCPP_USE_SAME_PORT` MUST be `false` in production.** Running OCPP and REST API on the same port violates IEC 62443 zone separation between Z1 (IT) and Z3 (OT). Same-port mode is only for development/testing.

### Required Environment Variables for Security

| Variable | Zone | Purpose |
|---|---|---|
| `GEO_BLOCK_ENABLED=true` | Z6 | Article 73-3 compliance |
| `GEO_BLOCK_COUNTRIES=RU,CN,BY` | Z6 | Blocked country list |
| `GEO_BLOCK_FAIL_CLOSED=true` | Z6 | Deny on GeoIP failure |
| `OCPP_REQUIRE_AUTH=true` | Z3 | Station authentication |
| `OCPP_EXPECTED_IP_RANGES=<depot CIDRs>` | Z3 | IEC 62443 zone validation |
| `OCPP_USE_SAME_PORT=false` | Z1/Z3 | Zone separation |
| `ENVIRONMENT=production` | All | Enforces CORS strictness |
| `CORS_ORIGINS=<specific origins>` | Z1 | No wildcards in production |
