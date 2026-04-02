# Wireless Communication Module Prohibition Policy

**Regulation:** Lithuanian Electric Energy Law Article 73-3
**Scope:** All EV charging infrastructure connected to the Lithuanian electricity grid
**Effective date:** Upon adoption
**Owner:** Favonius Energy — Security Team

---

## 1. Purpose

This policy prohibits the use of wireless communication modules from manufacturers headquartered in, or controlled by entities in, countries identified as posing national security risks to Lithuania. This is required by Article 73-3 of the Lithuanian Electric Energy Law for all grid-connected energy equipment exceeding 100 kW.

---

## 2. Restricted Countries

The following countries are restricted per Lithuanian national security policy:

| Country | ISO Code | Rationale |
|---|---|---|
| Russian Federation | RU | National security threat designation |
| People's Republic of China | CN | National security threat designation |
| Republic of Belarus | BY | National security threat designation |

---

## 3. Restricted Manufacturers

The following manufacturers (and their subsidiaries, affiliates, and OEM partners) are **prohibited** from supplying communication modules for use in Favonius-managed charging infrastructure:

| Manufacturer | Country | Affected Products |
|---|---|---|
| Huawei Technologies | CN | Cellular modules, IoT modules, network equipment |
| ZTE Corporation | CN | Cellular modules, IoT modules, network equipment |
| Hikvision | CN | Network cameras, IoT devices |
| Dahua Technology | CN | Network cameras, IoT devices |
| Kaspersky Lab | RU | Software, network security appliances |
| Any subsidiary or OEM of the above | Various | All communication-capable components |

> **Note:** This list is not exhaustive. Any communication module manufacturer headquartered in or majority-owned by entities in RU, CN, or BY is prohibited regardless of whether explicitly listed.

---

## 4. Scope of Prohibition

### 4.1. Hardware Components

The following hardware components in depot charging installations must NOT contain restricted wireless modules:

| Component | Communication Function | Verification Required |
|---|---|---|
| EV Chargers (EVSE) | Cellular/WiFi backhaul, OCPP communication | Manufacturer attestation |
| Network switches/routers | WAN connectivity, depot LAN | Equipment datasheet review |
| IoT gateways | Sensor aggregation, edge computing | Component BOM review |
| Stationary battery BMS | Remote monitoring, grid communication | Manufacturer attestation |
| Power meters / smart meters | Grid metering, demand response | Utility equipment verification |
| Security cameras (if installed) | Video surveillance backhaul | Equipment datasheet review |

### 4.2. Software Components

Software-level restrictions are enforced separately via:
- **Geo-blocking** (`src/security/geo_block.py`): Blocks all API/WebSocket access from RU, CN, BY IP ranges
- **Dependency scanning** (`pip-audit`): Monitors Python packages for supply chain risks
- **SBOM generation** (`scripts/generate_sbom.py`): Full software bill of materials for audit

---

## 5. Procurement Requirements

### 5.1. New Equipment

Before procuring any communication-capable equipment for depot installations:

1. **Verify manufacturer country of origin** — Check corporate registration, parent company, and majority ownership
2. **Obtain vendor attestation** — Written declaration that no restricted communication modules are included
3. **Review component BOM** — For chargers and gateways, request bill of materials identifying cellular/WiFi module manufacturer
4. **Document verification** — File procurement verification records for audit trail

### 5.2. Vendor Attestation Template

Vendors must provide a signed statement including:

> *"We, [Vendor Name], hereby attest that the equipment model [Model Number] supplied for use in EV charging infrastructure does not contain wireless communication modules (including cellular, WiFi, Bluetooth, LoRa, Zigbee, or other radio-frequency modules) manufactured by companies headquartered in, or majority-owned by entities in, Russia, China, or Belarus. This includes modules from Huawei, ZTE, and their subsidiaries or OEM partners."*

### 5.3. Existing Equipment

For equipment already installed:

1. **Inventory audit** — Identify all communication modules in existing depot equipment
2. **Compliance check** — Verify each module against the restricted manufacturer list
3. **Remediation plan** — If non-compliant modules are found, create a replacement timeline
4. **Transition period** — Existing installations have until [DATE per Article 73-3 transition timeline] to achieve compliance

---

## 6. Preferred Communication Architecture

| Communication Path | Preferred Method | Alternative |
|---|---|---|
| Charger → Backend (OCPP) | Wired Ethernet + WSS | WiFi (non-restricted AP) + WSS |
| Depot LAN | Wired Ethernet | Managed WiFi (non-restricted AP) |
| Depot → Cloud (WAN) | Fiber/DSL (wired ISP) | 4G/5G (non-restricted carrier module) |
| Monitoring | Wired Prometheus scrape | N/A |

**Wired connections are strongly preferred** for all OCPP communication to eliminate wireless module supply chain risk entirely.

---

## 7. Compliance Verification

### 7.1. Periodic Review

| Activity | Frequency | Responsible |
|---|---|---|
| Equipment inventory audit | Annually | Depot Operations |
| Vendor attestation renewal | At each procurement | Procurement Team |
| Restricted manufacturer list update | Quarterly | Security Team |
| Policy review | Annually | Security Team + Legal |

### 7.2. Non-Compliance Handling

| Severity | Scenario | Action |
|---|---|---|
| Critical | Restricted module found in active equipment | Immediate isolation from network; replacement within 30 days |
| High | Restricted module found in procurement pipeline | Reject shipment; source alternative supplier |
| Medium | Vendor unable to provide attestation | Escalate to Security Team for manual component verification |
| Low | Updated restricted list adds new manufacturer | Audit existing inventory within 90 days |

---

## 8. Record Keeping

All procurement verifications, vendor attestations, and compliance audit results must be retained for a minimum of **5 years** and made available for:

- ESO grid connection inspections
- NKSC cybersecurity audits
- NIS2/TIS2 supervisory authority reviews

Records are stored in the company document management system with references logged in the security audit trail (`security_audit_log` table, event_type: `CONFIG_CHANGE`).

---

*This policy is maintained by Favonius Energy and reviewed annually or upon changes to Lithuanian national security designations.*
