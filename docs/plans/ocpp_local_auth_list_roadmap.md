# OCPP Local Authorization List Roadmap

This roadmap stages `SendLocalList` / `GetLocalListVersion` support so chargers can
authorize RFID tags while offline, without blocking immediate tap-to-start
reliability. The send path is implemented in `src/adapters/ocpp/local_auth_sync.py`
and driven from the OCPP 1.6 charge-point handler (`FleetChargePoint`), which the
legacy `src/websocket_handler` orchestrates per session
(`OCPP16Session._delayed_local_auth_sync`).

## Status at a glance (as of 2026-05-26)

| Phase | Scope | Status |
|---|---|---|
| 1 | Contract + storage baseline | **Shipped** (variances below) |
| 2 | CSMS → charger local-list sync (`SendLocalList`) | **Shipped** |
| 3 | Charger → CSMS introspection + drift metrics | **Partial** — primitive only |
| 4 | Operational hardening (Differential, conflict policy, E2E, support runbook) | **Not started** |

## Phase 1 — Contract and storage baseline — SHIPPED

- Central authorization remains the fail-closed source of truth: the pushed list is
  the strict subset of idTags the online `Authorize` path would accept at that
  charger today (`src/db/queries.py::list_authorized_id_tags`) — no tag works
  offline that would not work online.
- Persistent state landed on `charging_stations` via Supabase migrations:
  - `supabase/011_charger_local_auth_state.sql` — `local_list_version`,
    `local_list_synced_at`, `local_list_last_status`.
  - `supabase/012_local_list_support_probe.sql` — firmware-scoped capability
    probe cache (`local_list_supported`, `local_list_probed_firmware`,
    `local_list_probed_at`).
- **Variance — feature flag is inverted.** The planned `OCPP_LOCAL_LIST_ENABLED`
  (default `false`) was *not* implemented. Instead the feature is **on by default**
  and gated by a kill switch `OCPP_DISABLE_LOCAL_AUTH_LIST` (default `false`), so an
  operator falls back to pure central Authorize without a code change. Update any
  ops docs that still reference `OCPP_LOCAL_LIST_ENABLED`.
- **Variance — no separate status taxonomy table.** Per-attempt outcomes are stored
  inline as OCPP wire statuses on `charging_stations.local_list_last_status`
  (`Accepted` | `Failed` | `NotSupported` | `VersionMismatch`, plus internal markers
  such as `UnsupportedFeatureProfile`, `UnsupportedFromBootstrap`); there is no
  separate per-token status table. Expiry / `parentIdTag` grouping are not modelled.

## Phase 2 — CSMS → charger local-list sync — SHIPPED

- `sync_charger()` pushes the full approved idTag list via `SendLocalList`
  (`update_type="Full"`), bumping `local_list_version` only on `Accepted`.
- Trigger is **reconnect/boot**, not a mid-session DB trigger: on every
  BootNotification, `_delayed_local_auth_sync` waits for queued-command replay to
  finish (so `SendLocalList` does not interleave with `SetChargingProfile`) and then
  pushes. Version compare provides the auto-heal — replaying the same list under a
  new version is idempotent per OCPP 1.6 §5.16.
- First sync per charger also flips the OCPP config keys that make the charger
  consult the list (`LocalAuthListEnabled`, `LocalPreAuthorize`,
  `AuthorizationCacheEnabled`) and disables the ABB vendor key `FreevendEnabled`.
  A fail-fast bootstrap short-circuits when the critical key is rejected.
- Capability handling beyond the original plan, hardened against the pilot depot ABB
  Terra AC V1.8.x firmware: a `GetConfiguration` probe
  (`SupportedFeatureProfiles`, `LocalAuthListMaxLength`) plus a firmware-scoped
  negative cache short-circuit every subsequent reconnect for chargers that don't
  implement `LocalAuthListManagement`. The ABB 16-entry cap is enforced in
  `FleetChargePoint.send_local_list`.
- Outgoing payloads are not persisted verbatim for audit — only the version and last
  status are retained. Full per-payload audit was deferred as unnecessary for v1
  (the list is always derivable from `list_authorized_id_tags`).

## Phase 3 — Charger → CSMS introspection — PARTIAL

- **Shipped:** the `GetLocalListVersion` primitive exists
  (`FleetChargePoint.get_local_list_version`, `src/adapters/ocpp/charge_point.py`).
- **Remaining:** no periodic polling loop, no drift alert (charger version ≠ CSMS
  target), and no convergence/push-success/mismatch metrics. Today reconciliation is
  implicit — the next reconnect re-pushes a higher version regardless of the
  charger's reported version.

## Phase 4 — Operational hardening — NOT STARTED

- Partial (`Differential`) updates — deliberately out of scope for v1; Full-only
  keeps reconciliation simple.
- Conflict policy for duplicate / rotated RFID tags.
- E2E tests covering offline windows and reconnect replay.
- Support runbook: force-resync by station, inspect last applied list.

## Remaining work on the legacy `src/websocket_handler` path

The send path is exercised through `OCPP16Session` (legacy handler) today. The
new-architecture `src/adapters/ocpp/server.py` path does not yet wire
`sync_charger` into its own BootNotification handling — when OCPP traffic migrates
fully off the legacy handler, the boot-time sync trigger must be re-attached there.

## Non-goals

- Dynamic prepaid/billing enforcement in local lists.
- Cross-org shared token policies.
- Replacing central online authorization as the default path.
