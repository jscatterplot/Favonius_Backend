# Cybersecurity Compliance Gap Analysis

**Date:** 2026-04-01
**System:** Favonius Energy EV Fleet Depot Optimization Platform
**Scope:** Lithuanian Article 73-3, NIS2/TIS2 Directive, IEC 62443

---

## Executive Summary

This document maps the Favonius security implementation against three regulatory frameworks applicable to EV charging infrastructure operating in Lithuania. The analysis covers **code-level controls** already implemented and identifies **remaining gaps** (both code and documentation/process).

**Overall status:**
- **Lithuanian Article 73-3:** 5/6 technical requirements implemented. 1 documentation deliverable remaining.
- **NIS2/TIS2 (EU 2022/2555):** 8/10 Article 21 sub-requirements have code-level controls. 2 require organizational/process measures only.
- **IEC 62443-3-3:** Targeting SL-2 overall. 5/7 Foundational Requirements met at SL-2. FR 5 (Restricted Data Flow) and FR 6 (Timely Response) have minor gaps.

---

## 1. Lithuanian Electric Energy Law — Article 73-3

Article 73-3 addresses cybersecurity of grid-connected energy equipment, mandating restrictions on equipment/software from countries posing national security concerns (Russia, China, Belarus).

### Requirement Mapping

| # | Requirement | Implementation | Status | File(s) |
|---|---|---|---|---|
| 73-3.1 | **Geo-blocking of RU/CN/BY** — Prevent remote access/data routing through threat countries | `GeoBlockChecker` with MaxMind GeoLite2, fail-closed, allowlist, private IP bypass. Middleware on API + WebSocket. Default blocked: `RU,CN,BY`. | **PASS** | `src/security/geo_block.py`, `src/api/main.py`, `src/websocket_handler/server.py` |
| 73-3.2 | **Security Declaration (Saugumo Deklaracija)** — Submit declaration to ESO (grid operator) certifying compliance | Template structure defined but **document not yet generated**. Requires: company details, equipment list, geo-blocking attestation, audit reference, wireless prohibition attestation. | **GAP (doc)** | Phase 5 deliverable |
| 73-3.3 | **Independent cybersecurity audit** — Third-party audit per NKSC methodology | Audit logging infrastructure (90-day retention, tamper-evident sequencing) supports audit evidence collection. **External audit must be commissioned.** | **READY** (infrastructure in place, audit not yet performed) | `src/security/audit_log.py`, `migrations/008_security_audit_log.sql` |
| 73-3.4 | **Wireless communication prohibition** — Restrictions on cellular/IoT modules from threat-country manufacturers | Backend enforces WSS (TLS 1.2+) for all OCPP/VDV 463. Hardware procurement policy needed for charger communication modules. | **PARTIAL** (software controls in place; hardware procurement policy is organizational) | `src/websocket_handler/server.py` (TLS config), Dockerfile (cipher suites) |
| 73-3.5 | **Supply chain security** — No critical components from restricted manufacturers | SBOM generation via CycloneDX. `pip-audit` pre-commit hook for vulnerability scanning. **Hardware supply chain assessment is organizational.** | **PASS** (software); **GAP** (hardware policy) | `scripts/generate_sbom.py`, `.pre-commit-config.yaml` |
| 73-3.6 | **Audit logging with NKSC retention** — 90-day minimum log retention | TimescaleDB hypertable with 90-day retention policy, tamper-evident sequential numbering, indexed by event type, IP, station, timestamp. | **PASS** | `migrations/008_security_audit_log.sql`, `src/security/audit_log.py` |

### Article 73-3 Summary

| Category | Passed | Gaps |
|---|---|---|
| Technical controls (code) | 5/6 | Wireless hardware procurement policy (organizational) |
| Documentation | 0/1 | Security Declaration template for ESO |
| External actions | 0/1 | Commission independent cybersecurity audit |

---

## 2. NIS2 Directive (EU 2022/2555) / Lithuanian TIS2

EV charging infrastructure operators in the energy sector meeting size thresholds are classified as **essential entities** under NIS2. Lithuania transposed NIS2 as TIS2, effective 2024-10-18. Competent authority: NCSC-LT. CSIRT: CERT-LT (cert@cert.lt).

### Article 21 — Cybersecurity Risk-Management Measures

| # | Article 21 Sub-requirement | Implementation | Status | File(s) |
|---|---|---|---|---|
| 21(2)(a) | **Risk analysis and information security policies** | Asset inventory via DB schema (depots, chargers, vehicles tables). Risk assessment framework is organizational — not code. | **PARTIAL** — Code supports asset tracking; formal risk assessment document needed |  |
| 21(2)(b) | **Incident handling** | `IncidentDetector` with severity classification (CRITICAL/HIGH/MEDIUM/LOW), automated detection (brute force, geo-block storm, unauthorized access), incident lifecycle (detected→investigating→contained→notified→resolved). | **PASS** | `src/security/incident_response.py` |
| 21(2)(c) | **Business continuity and crisis management** | HiGHS solver fallback, data freshness detection (`DataFreshnessMonitor`), warm-start from prior solutions. **BCP/DR documents are organizational.** | **PARTIAL** — Technical resilience implemented; formal BCP document needed | `src/core/optimizer/solver.py`, `src/security/data_freshness.py`, `src/core/optimizer/warm_start.py` |
| 21(2)(d) | **Supply chain security** | CycloneDX SBOM generation, `pip-audit` pre-commit hook, dependency pinning in `pyproject.toml`. | **PASS** (software supply chain) | `scripts/generate_sbom.py`, `.pre-commit-config.yaml`, `pyproject.toml` |
| 21(2)(e) | **Vulnerability handling and disclosure** | `pip-audit` for dependency CVEs, `bandit` for static analysis, `ruff` for code quality. **Coordinated vulnerability disclosure policy is organizational.** | **PARTIAL** — Automated scanning in place; disclosure policy document needed | `.pre-commit-config.yaml` |
| 21(2)(f) | **Assessing effectiveness of cybersecurity measures** | Security test suite (159 tests across 8 test files), `/health` endpoint for component verification. **Penetration testing program is organizational.** | **PARTIAL** — Automated testing in place; formal assessment program needed | `tests/security/` |
| 21(2)(g) | **Cyber hygiene and training** | RBAC with least-privilege roles. **Training program is organizational.** | **PARTIAL** — Technical controls support hygiene; training program needed | `src/security/rbac.py` |
| 21(2)(h) | **Cryptography and encryption** | TLS 1.2+ enforced (TLSv1.3 in WebSocket SSL context), modern cipher suites (ECDHE, ChaCha20), JWT key rotation via `JWT_SECRET_KEY` + `JWT_SECRET_KEY_PREVIOUS`, HSTS headers. | **PASS** | `src/security/auth.py`, `src/security/secrets.py`, `src/security/headers.py`, Dockerfile |
| 21(2)(i) | **Access control, HR security, asset management** | 4-role RBAC (admin/operator/viewer/auditor), 13 granular permissions, `require_permission()` and `require_role()` FastAPI dependencies. **HR processes are organizational.** | **PASS** (technical); **GAP** (HR policy) | `src/security/rbac.py` |
| 21(2)(j) | **MFA, continuous authentication, secured communications** | Rate limiting as continuous session monitoring. JWT with expiry. OCPP heartbeat timeout. **MFA for admin access not yet implemented in backend** (delegated to Supabase). | **PARTIAL** — MFA depends on Supabase configuration | `src/security/rate_limiter.py`, `src/security/auth.py` |

### Article 23 — Incident Reporting

| # | Requirement | Implementation | Status | File(s) |
|---|---|---|---|---|
| 23.1 | **24-hour early warning to CSIRT** | `IncidentDetector` tracks `csirt_notified_at`. `generate_csirt_notification()` produces structured payload for CERT-LT (cert@cert.lt). | **PASS** (capability in place) | `src/security/incident_response.py` |
| 23.2 | **72-hour incident notification** | Incident lifecycle supports status transitions. Structured incident records with full metadata. | **PASS** (capability in place) | `src/security/incident_response.py` |
| 23.3 | **1-month final report** | Incident records preserved with full audit trail for report generation. | **PASS** (data available) | `src/security/incident_response.py`, `src/security/audit_log.py` |
| 23.4 | **Reporting channel to CERT-LT** | CSIRT contact hardcoded: `CERT-LT (cert@cert.lt)`. **Actual channel registration is organizational.** | **PARTIAL** — Contact configured; registration with NCSC-LT needed | `src/security/incident_response.py` |

### Article 32/34 — Supervision and Fines

| # | Requirement | Status |
|---|---|---|
| 32.1 | Accommodate on-site/remote audits | **READY** — Audit log infrastructure supports evidence extraction |
| 32.2 | Provide evidence of cybersecurity policies | **GAP** — Formal policy documents needed |
| 34.1 | Awareness of EUR 10M / 2% turnover fine exposure | **Acknowledged** |

### NIS2/TIS2 Summary

| Category | Passed | Partial | Gaps |
|---|---|---|---|
| Article 21 technical controls | 5/10 | 5/10 | 0/10 |
| Article 23 incident reporting | 3/4 | 1/4 | 0/4 |
| Organizational/process measures | — | — | Risk assessment, BCP, training, disclosure policy, MFA enforcement |

---

## 3. IEC 62443 — Industrial Automation and Control Systems Security

Target: **SL-2** overall, **SL-3** for OCPP command path (Z2→Z3 conduit).

### Security Zones (Implemented)

| Zone | Components | Target SL | Implementation |
|---|---|---|---|
| Z1: Cloud/IT | FastAPI REST, Supabase Auth | SL-2 | JWT auth, rate limiting, CORS, security headers |
| Z2: Control | DepotController, MILP optimizer, StateAssembler | SL-2/3 | Input validation, solver fallback, data freshness checks |
| Z3: OT/OCPP | OCPPServer, FleetChargePoint, dispatch | SL-3 | Geo-blocking, station auth, zone IP validation |
| Z4: Data | TimescaleDB, telemetry, prices | SL-2 | Connection pooling, parameterized queries, audit logging |
| Z5: External | CAISO/ENTSO-E, VDV 463, weather | SL-2 | Schema validation, rate limiting, staleness detection |

### Foundational Requirements Assessment

| FR | Requirement | Current SL | Target SL | Status | Key Controls |
|---|---|---|---|---|---|
| FR 1 | Identification & Authentication | SL-2 | SL-2 | **PASS** | JWT auth with rotation (`auth.py`), OCPP station auth (`server.py`), rate limiting on failed attempts (`rate_limiter.py`) |
| FR 2 | Use Control / Authorization | SL-2 | SL-2 | **PASS** | 4-role RBAC (`rbac.py`), 13 permissions, `require_permission()`/`require_role()` dependencies, heartbeat timeout, `MAX_CONNECTIONS` |
| FR 3 | System Integrity | SL-2 | SL-2 | **PASS** | Input validation (`validators.py`), VDV 463 schema validation, OCPP message validation, solver output bounded by hard constraints (99% SoC, max_grid_kw), `bandit` static analysis |
| FR 4 | Data Confidentiality | SL-2 | SL-2 | **PASS** | TLS 1.2+ on all external comms, WSS for OCPP, HSTS headers, JWT secret rotation, secrets not in code. **Note:** DB encryption at rest depends on hosting provider. |
| FR 5 | Restricted Data Flow | SL-1/2 | SL-2 | **PARTIAL** | OCPP on port 9000, API on 8000 (separate entry points). **Risk:** `OCPP_USE_SAME_PORT=true` violates zone separation. IEC 62443 zone IP validation (`OCPP_EXPECTED_IP_RANGES`) implemented as informational logging. |
| FR 6 | Timely Response to Events | SL-2 | SL-2 | **PASS** | Tamper-evident audit log with 90-day retention, Prometheus metrics, `IncidentDetector` with automated detection, CSIRT notification generation. |
| FR 7 | Resource Availability | SL-2 | SL-2 | **PASS** | Rate limiting (API: 100/min, optimize: 10/min, handoff: 50/hr), `MAX_CONNECTIONS=100`, solver timeout (60s), HiGHS fallback, warm-start, data freshness monitoring. |

### IEC 62443 Summary

| Category | Passed | Partial | Gaps |
|---|---|---|---|
| Foundational Requirements (SL-2) | 6/7 | 1/7 | FR 5 partial: `OCPP_USE_SAME_PORT` zone violation |
| Security Zones defined | Yes | — | — |
| Conduit requirements | Implemented | — | — |
| Component-level (62443-4-2) | — | — | OCPP 1.6 lacks mutual TLS (inherent protocol limitation; OCPP 2.0.1 addresses this) |

---

## 4. Cross-Framework Compliance Matrix

| Security Control | Implementation | Art. 73-3 | NIS2 Art. 21 | IEC 62443 FR |
|---|---|---|---|---|
| Geo-blocking (RU/CN/BY) | `geo_block.py` — MaxMind GeoLite2, fail-closed | **73-3.1** | — | FR 5 (zone boundary) |
| JWT auth + key rotation | `auth.py`, `secrets.py` | — | 21(2)(h) | FR 1 (SR 1.1, 1.5) |
| RBAC (4 roles, 13 permissions) | `rbac.py` | — | 21(2)(i) | FR 2 (SR 2.1) |
| Rate limiting (hybrid) | `rate_limiter.py` — in-memory + PostgreSQL | — | 21(2)(j) | FR 7 (SR 7.1) |
| Security headers (HSTS, CSP) | `headers.py` | — | 21(2)(h) | FR 4 (SR 4.1) |
| Tamper-evident audit log | `audit_log.py` + migration 008 | **73-3.6** | 21(2)(b) | FR 6 (SR 6.1) |
| Incident detection + CSIRT | `incident_response.py` | — | **Art. 23** | FR 6 (SR 6.1) |
| Input validation | `validators.py` | — | 21(2)(a) | FR 3 (SR 3.5) |
| SBOM generation | `generate_sbom.py` (CycloneDX) | **73-3.5** | 21(2)(d) | — |
| Dependency scanning | `pip-audit` pre-commit hook | **73-3.5** | 21(2)(d), (e) | FR 3 (SR 3.2) |
| Static analysis | `bandit` pre-commit hook | — | 21(2)(e) | FR 3 (SR 3.2) |
| Station authentication | `server.py` — Bearer/API key/Basic/OCPP password | — | 21(2)(i) | FR 1 (SR 1.2) |
| IEC 62443 zone validation | `server.py` — `OCPP_EXPECTED_IP_RANGES` | — | — | **FR 5 (SR 5.2)** |
| TLS enforcement | Dockerfile cipher suites, WSS, HSTS | **73-3.4** | 21(2)(h) | FR 4 (SR 4.3) |
| Data freshness monitoring | `data_freshness.py` | — | 21(2)(c) | FR 7 (SR 7.6) |
| Solver fallback (HiGHS) | `solver.py` | — | 21(2)(c) | FR 7 (SR 7.6) |
| CORS hardening | `main.py` — fail startup if `*` in production | — | 21(2)(a) | FR 5 (SR 5.2) |

---

## 5. Identified Gaps and Remediation Plan

### Code/Technical Gaps

| # | Gap | Framework | Severity | Remediation |
|---|---|---|---|---|
| G1 | `OCPP_USE_SAME_PORT=true` violates IEC 62443 zone separation | IEC 62443 FR 5 | Medium | Document as production configuration requirement: always use separate ports. Add startup warning when enabled in production. |
| G2 | MFA for admin endpoints not enforced at backend level | NIS2 21(2)(j) | Low | Delegated to Supabase. Document Supabase MFA configuration requirement for admin users. |
| G3 | OCPP 1.6 lacks mutual TLS | IEC 62443-4-2 CR 1.2 | Medium | Inherent protocol limitation. Mitigated by station authentication + IP range validation. Full fix requires OCPP 2.0.1 Security Profile 2/3 (PRD marks this as "future-ready"). |

### Documentation/Process Gaps (Phase 5 Deliverables)

| # | Gap | Framework | Deliverable |
|---|---|---|---|
| D1 | Security Declaration for ESO | Art. 73-3.2 | Template document with company details, equipment inventory, compliance attestation |
| D2 | Network architecture diagram | Art. 73-3 / IEC 62443 | Diagram showing security zones, conduits, and data flows per Section 3 above |
| D3 | Wireless prohibition policy | Art. 73-3.4 | Policy document for hardware procurement restricting communication modules from threat countries |
| D4 | Formal risk assessment | NIS2 21(2)(a) | Risk assessment document covering all IACS assets and communication channels |
| D5 | Business continuity plan | NIS2 21(2)(c) | BCP/DR document with RTO/RPO targets |
| D6 | Vulnerability disclosure policy | NIS2 21(2)(e) | Public disclosure process (security.txt, responsible disclosure) |
| D7 | Security training program | NIS2 21(2)(g) | Training curriculum for staff and management body |
| D8 | Entity registration with NCSC-LT | NIS2/TIS2 | Registration as essential entity in energy sector |
| D9 | Commission independent audit | Art. 73-3.3 | Engage third-party auditor per NKSC methodology |

---

## 6. Test Coverage Summary

| Test File | Tests | Coverage Area |
|---|---|---|
| `tests/security/test_geo_blocking.py` | 38 | Geo-blocking logic, fail-closed, allowlist, CIDR, private IP bypass |
| `tests/security/test_rate_limiter.py` | 39 | Sliding window, DB sync, cross-instance coordination, handoff limits |
| `tests/security/test_audit_logging.py` | 19 | Batch writing, DB persistence, event types, flush behavior |
| `tests/security/test_rbac.py` | 15 | Role-permission matrix, require_permission, require_role |
| `tests/security/test_incident_response.py` | 14 | Detection thresholds, CSIRT payload, incident lifecycle |
| `tests/security/test_auth_rotation.py` | 12 | JWT key rotation, multi-key verification, expiry |
| `tests/security/test_secrets_rotation.py` | 12 | Secrets manager, rotation detection, access log |
| `tests/security/test_websocket_security_integration.py` | 10 | WebSocket geo-blocking, station auth, zone validation |
| **Total** | **159** | |

---

## 7. Conclusion

The Favonius backend has **comprehensive code-level security controls** covering the technical requirements of all three frameworks. The primary remaining work is:

1. **Documentation deliverables** (Phase 5): Security Declaration, network diagram, policies
2. **Organizational measures**: Risk assessment, BCP, training, NCSC-LT registration, independent audit
3. **Minor technical items**: Production configuration documentation for zone separation, Supabase MFA enforcement

No critical code gaps exist. The system is **audit-ready** from a technical evidence standpoint, with tamper-evident logging, structured incident response, and comprehensive test coverage.
