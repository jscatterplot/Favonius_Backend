# EVerest Docker Smoke Test (Backend ↔ Charger OCPP)

This repository now includes a reproducible EVerest-based smoke test for backend charger
communication.

## Why this exists

Our existing Python OCPP simulator is lightweight and fast, but EVerest gives us a
standards-grade SIL charge point implementation with realistic OCPP behavior.

## Upstream references used for this setup

- EVerest demo repo: <https://github.com/EVerest/everest-demo>
- Images and runtime flow (manager + mqtt + nodered) from:
  - `docker-compose.yml`
  - `.env` (`TAG=2025.10.0-single-phase`)
  - `manager/config-docker.json`
- This setup rewrites EVerest `CentralSystemURI` from SteVe to this backend.

## Files added for this harness

- `docker-compose.everest.yml`:
  - `timescaledb`
  - `api` (backend, OCPP enabled on port 9000)
  - `everest-mqtt`
  - `everest-manager`
  - optional `everest-nodered` (`--profile everest-ui`)
- `scripts/everest/run_backend_everest_smoke.sh`:
  - Starts stack
  - Waits until backend health reports DB healthy and OCPP server healthy (charger connected)
  - Verifies BootNotification reached backend logs

## Run the smoke test

```bash
./scripts/everest/run_backend_everest_smoke.sh
```

Expected success signal:

- Script prints:
  - `Health check confirms DB + OCPP charger connection.`
  - `EVerest smoke test passed: backend communicated with charge point charger_01.`

By default the script tears down the stack after success/failure.

Keep stack running for manual debugging:

```bash
KEEP_STACK=1 ./scripts/everest/run_backend_everest_smoke.sh
```

Override charge point ID / CSMS URI if needed:

```bash
EVEREST_CHARGE_POINT_ID=charger_02 EVEREST_CSMS_URI=api:9000/charger_02 \
  ./scripts/everest/run_backend_everest_smoke.sh
```

Then inspect logs:

```bash
docker compose -p favonius-everest-smoke -f docker-compose.everest.yml logs -f api everest-manager
```

Stop stack manually:

```bash
docker compose -p favonius-everest-smoke -f docker-compose.everest.yml down -v --remove-orphans
```

## Optional Node-RED UI

To run with EVerest Node-RED UI:

```bash
docker compose -f docker-compose.everest.yml --profile everest-ui up -d
```

UI endpoint:

- <http://localhost:1880/ui>

## Notes

- This smoke test targets charge point ID `charger_01` so it matches seeded charger data from
  `migrations/001_initial_schema.sql`.
- EVerest demo images are x86-oriented; on Apple Silicon (M1/M2) support may be limited.
- Upstream `everest-demo` compose files enable IPv6 networks by default. This smoke harness uses
  Docker's default bridge network for simpler local compatibility; for strict parity with upstream
  networking, use an IPv6-enabled compose network.

## Official references used for this harness

- EVerest manual (nightly): <https://everest.github.io/nightly/>
- OCPP 1.6 tutorial (`CentralSystemURI` + OCPP config model):
  <https://everest.github.io/nightly/tutorials/how_to_ocpp/index.html>
- EVerest in Software / SIL tutorials:
  <https://everest.github.io/nightly/tutorials/run_sil/index.html>
- LF Energy project page (project context and scope):
  <https://lfenergy.org/projects/everest/>
- EVerest GitHub org:
  <https://github.com/EVerest>
- EVerest demo repository (images, compose templates, scripts):
  <https://github.com/EVerest/everest-demo>

