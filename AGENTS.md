# AGENTS.md

## Cursor Cloud specific instructions

### Services overview

| Service | How to start | Port |
|---|---|---|
| TimescaleDB | `sudo docker compose up -d timescaledb` | 5432 |
| FastAPI API | `uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload` | 8000 |

### Running the API locally

TimescaleDB must be running before the API server. Start it with Docker Compose (see `docker-compose.yml`). Migrations auto-apply on first container start via `/docker-entrypoint-initdb.d`.

Required env vars for local dev:

```bash
DATABASE_URL="postgresql://favonius:favonius_dev@localhost:5432/favonius"
ENVIRONMENT=development
OCPP_SERVER_ENABLED=false
JWT_SECRET_KEY="dev-secret-key-for-testing-only"
```

The API starts a `ControllerManager` at startup that loads depot configs from DB. If a depot has no vehicles (e.g., Depot B in seed data), it logs an error but keeps running.

Gurobi has no license in this environment; the solver falls back to HiGHS automatically. This is expected and does not block development.

### Gotchas

- `~/.local/bin` must be on `PATH` for `pytest`, `uvicorn`, `black`, `ruff`, `mypy` — they install there with `pip install --user`.
- The `version` key in `docker-compose.yml` triggers a warning from Docker Compose v2 — safe to ignore.
- `mypy src` fails with a module-name collision (`core.models` vs `src.core.models`) due to the package layout. This is a pre-existing issue.
- Unit tests (`pytest tests/unit`) run without any external services. Integration/E2E tests require the database.
- Pre-existing lint issues: ~823 ruff warnings and ~239 black formatting issues exist in the codebase.

### Standard commands

See `CLAUDE.md` and `Makefile` for full command reference. Key shortcuts:

- **Lint**: `make lint` (ruff + black + isort + mypy)
- **Format**: `make format`
- **Unit tests**: `pytest tests/unit -v`
- **All tests**: `pytest`
- **Coverage**: `pytest --cov=src --cov-report=html --cov-report=term`

### Docker in this environment

Docker runs as docker-in-docker with `fuse-overlayfs` storage driver and `iptables-legacy`. The daemon must be started manually: `sudo dockerd &>/tmp/dockerd.log &`.
