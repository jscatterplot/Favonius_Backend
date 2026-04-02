# Saugumo Deklaracija / Security Declaration for ESO

**Document:** Security Declaration per Lithuanian Electric Energy Law Article 73-3
**Prepared for:** ESO (Energijos skirstymo operatorius AB)
**Prepared by:** Favonius Energy
**Date:** ____________
**Version:** 1.0

---

## 1. Deklaruojanti organizacija / Declaring Organization

| Field | Value |
|---|---|
| Company name | Favonius Energy |
| Registration number | ____________ |
| Registered address | ____________ |
| Contact person | ____________ |
| Contact email | ____________ |
| Contact phone | ____________ |

---

## 2. Sistemos aprašymas / System Description

| Field | Value |
|---|---|
| System name | Favonius EV Fleet Depot Optimization Platform |
| System type | Charging Management System (CMS) for EV fleet depots |
| Connection type | Grid-connected EV charging infrastructure (>100 kW) |
| Max grid power (per depot) | Defined per `depots.max_grid_kw` in system configuration |
| Communication protocols | OCPP 1.6 (WebSocket/TLS), VDV 463 (WebSocket/TLS), REST API (HTTPS) |
| Deployment model | Cloud-hosted (Railway) + on-premises charger network |

### 2.1. Įrangos sąrašas / Equipment Inventory

| Component | Manufacturer | Country of Origin | Compliance |
|---|---|---|---|
| Backend software | Favonius Energy | Lithuania/EU | Compliant |
| MILP Solver (primary) | Gurobi Optimization | USA | Compliant |
| MILP Solver (fallback) | HiGHS | UK/EU | Compliant |
| Database | TimescaleDB (Timescale Inc.) | USA | Compliant |
| Authentication | Supabase | USA (Singapore HQ) | Compliant |
| GeoIP Database | MaxMind | USA | Compliant |
| EV Chargers | ____________ | ____________ | To be verified per installation |
| Network equipment | ____________ | ____________ | To be verified per installation |

> **Note:** Charger hardware and network equipment must be verified per depot installation. No equipment from manufacturers headquartered in or controlled by entities in Russia, China, or Belarus is permitted per Article 73-3.

---

## 3. Kibernetinio saugumo priemonės / Cybersecurity Measures

### 3.1. Geografinis blokavimas / Geo-blocking

**Status:** Implemented and enforced.

| Measure | Implementation |
|---|---|
| Blocked countries | Russia (RU), China (CN), Belarus (BY) |
| Blocking mechanism | MaxMind GeoLite2-Country IP resolution |
| Fail behavior | **Fail-closed** — access denied if GeoIP resolution fails |
| Scope | All API endpoints and WebSocket connections (OCPP, VDV 463) |
| Allowlist | Configurable per-IP and CIDR allowlist for authorized auditors |
| Logging | All blocked requests logged to tamper-evident audit trail |

### 3.2. Autentifikavimas ir prieigos kontrolė / Authentication and Access Control

| Measure | Implementation |
|---|---|
| API authentication | JWT (JSON Web Token) with Supabase, key rotation support |
| Station authentication | Certificate, Bearer token, API key, Basic auth, or OCPP password |
| Role-based access control | 4 roles (admin, operator, viewer, auditor) with 13 granular permissions |
| Rate limiting | API: 100 req/min, Optimization: 10 req/min, Handoff: 50 msg/hr |
| Session management | JWT expiry, OCPP heartbeat timeout (30s), `MAX_CONNECTIONS=100` |

### 3.3. Šifravimas / Encryption

| Measure | Implementation |
|---|---|
| Data in transit | TLS 1.2+ on all external communications; TLS 1.3 for WebSocket |
| Cipher suites | ECDHE + ChaCha20/AES-GCM; aNULL, MD5, DSS excluded |
| HSTS | Enabled (max-age=31536000, includeSubDomains) |
| Key management | Environment-based secrets with rotation (`JWT_SECRET_KEY` + `JWT_SECRET_KEY_PREVIOUS`) |

### 3.4. Audito registravimas / Audit Logging

| Measure | Implementation |
|---|---|
| Storage | TimescaleDB hypertable (`security_audit_log`) |
| Retention | 90 days (automatic policy) |
| Tamper evidence | PostgreSQL sequence-based sequential numbering |
| Event types | AUTH_SUCCESS, AUTH_FAILURE, GEO_BLOCK, RATE_LIMIT, ACCESS_DENIED, CONFIG_CHANGE, CONNECTION_REJECT, STATION_LOCKOUT, INCIDENT_DETECT |
| Indices | By event_type, source_ip, station_id, seq_number |

### 3.5. Incidentų valdymas / Incident Management

| Measure | Implementation |
|---|---|
| Detection | Automated detection of brute force, geo-block storms, unauthorized access |
| Classification | NIS2-aligned severity levels (CRITICAL, HIGH, MEDIUM, LOW) |
| CSIRT notification | Structured payload generation for CERT-LT (cert@cert.lt) |
| Reporting timeline | 24-hour early warning, 72-hour notification, 1-month final report |

### 3.6. Tiekimo grandinės saugumas / Supply Chain Security

| Measure | Implementation |
|---|---|
| SBOM | CycloneDX 1.5 JSON generation (`scripts/generate_sbom.py`) |
| Dependency scanning | `pip-audit` pre-commit hook for CVE detection |
| Static analysis | `bandit` for Python security scanning |
| Code quality | `ruff`, `black`, `mypy` for code quality enforcement |

---

## 4. Belaidžio ryšio draudimas / Wireless Communication Prohibition

Favonius Energy attests that:

1. **No wireless communication modules from manufacturers headquartered in or controlled by entities in Russia, China, or Belarus** are used in the charging management system.
2. All OCPP charger-to-backend communication uses **wired network connections with TLS encryption**.
3. The wireless prohibition policy (see `docs/WIRELESS_PROHIBITION_POLICY.md`) governs hardware procurement for all depot installations.
4. Charger hardware procurement requires vendor attestation that cellular/IoT communication modules do not originate from restricted manufacturers (including but not limited to: Huawei, ZTE, and their subsidiaries).

---

## 5. IEC 62443 saugumo zonos / IEC 62443 Security Zones

The system implements security zone segmentation per IEC 62443-3-2:

| Zone | Components | Security Level |
|---|---|---|
| Z1: Cloud/IT | REST API, authentication, monitoring | SL-2 |
| Z2: Control | Optimization engine, depot controller | SL-2/3 |
| Z3: OT/OCPP | OCPP WebSocket server, charger dispatch | SL-3 |
| Z4: Data | TimescaleDB, telemetry, audit logs | SL-2 |
| Z5: External | Price feeds, weather, VDV 463 external | SL-2 |

See `docs/SECURITY_NETWORK_ARCHITECTURE.md` for the full zone and conduit diagram.

---

## 6. Nepriklausomas auditas / Independent Audit

| Item | Status |
|---|---|
| Audit provider | To be commissioned |
| Audit methodology | NKSC cybersecurity audit methodology |
| Audit scope | Full system per Article 73-3 requirements |
| Last audit date | ____________ |
| Next audit date | ____________ |
| Audit report reference | ____________ |

---

## 7. Atitikties patvirtinimas / Compliance Attestation

We, Favonius Energy, hereby declare that:

1. Our EV charging management system complies with the cybersecurity requirements of the Lithuanian Electric Energy Law Article 73-3.
2. We have implemented geo-blocking of access from Russia, China, and Belarus with fail-closed behavior.
3. No critical system components originate from manufacturers in restricted countries.
4. Wireless communication modules from restricted manufacturers are prohibited in all depot installations.
5. We maintain tamper-evident audit logs with 90-day retention per NKSC requirements.
6. We have incident response procedures aligned with NIS2/TIS2 Directive requirements.
7. We will commission an independent cybersecurity audit per NKSC methodology.

**Authorized signatory:**

| Field | Value |
|---|---|
| Name | ____________ |
| Title | ____________ |
| Date | ____________ |
| Signature | ____________ |

---

*This declaration is prepared in accordance with Lithuanian Electric Energy Law Article 73-3 and should be submitted to ESO (Energijos skirstymo operatorius AB) as part of the grid connection application for EV charging infrastructure.*
