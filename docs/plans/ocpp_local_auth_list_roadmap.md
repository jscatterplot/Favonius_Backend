# OCPP Local Authorization List Roadmap

This roadmap stages `SendLocalList` / `GetLocalListVersion` support on the legacy
`src/websocket_handler` path without blocking immediate RFID tap-to-start reliability.

## Phase 1 (now): Contract and storage baseline

- Keep central authorization fail-closed as source of truth.
- Add shared status taxonomy (`accepted`, `invalid`, `blocked`, `expired`, `concurrent_tx`).
- Define persistent tables/columns for:
  - station local-list version,
  - last pushed list checksum,
  - per-token local status metadata.
- Add feature flag (`OCPP_LOCAL_LIST_ENABLED`) default `false`.

## Phase 2: CSMS -> charger local-list sync

- Implement `SendLocalList` request builder and send path in websocket handler.
- Persist each outgoing list payload + version for reconciliation/audit.
- Handle charger ACKs and record status (`Accepted`, `VersionMismatch`, `Failed`).
- On reconnect/boot, compare expected vs. actual local-list version and auto-heal.

## Phase 3: Charger -> CSMS introspection

- Implement `GetLocalListVersion` roundtrip and periodic polling.
- Alert on drift (charger version != CSMS target version).
- Add metrics for push success ratio, mismatch count, and convergence latency.

## Phase 4: Operational hardening

- Partial updates (`Differential`) and full rebuild fallback (`Full`).
- Conflict policy for duplicate/rotated RFID tags.
- E2E tests with offline windows and reconnect replay.
- Runbook for support: force-resync by station, inspect last applied list.

## Non-goals for this sprint

- Dynamic prepaid/billing enforcement in local lists.
- Cross-org shared token policies.
- Replacing central online authorization as the default path.
