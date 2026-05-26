# Testing Guide

This is the deep companion to the **Testing** section of `CLAUDE.md`. It covers how
to install the test toolchain, the markers actually registered in `pytest.ini`, the
suites that gate a deploy, and day-to-day pytest invocation tips. Where this guide
and `CLAUDE.md` disagree, `pytest.ini` and the `Makefile` are the source of truth.

## Setup

### Install the toolchain

The dev extras pull in pytest, the async/mock plugins, coverage, and every library
the suite imports (httpx, structlog, cryptography, sqlalchemy, etc.). Install them
with the package, not piecemeal:

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Or via the Makefile (also runs `pre-commit install`):

```bash
make install-dev
```

Python 3.12+ is required.

### Test database (integration / e2e / golden gates)

Most unit tests mock the database. Integration tests, the workflow/agent-SQL golden
gates, and the e2e acceptance tests need a real TimescaleDB instance (and, for the
agent-SQL path, a paired static/Supabase database):

```bash
docker-compose up -d timescaledb
```

Migrations apply automatically when the container boots (they are mounted to
`/docker-entrypoint-initdb.d`); to apply manually run
`python scripts/run_migrations.py`. Point the suite at the DB via the standard
connection env vars (`DATABASE_URL` / `STATIC_DATABASE_URL`).

## Running tests

### Common commands

```bash
pytest                       # everything pytest can discover under tests/
pytest tests/unit -v         # unit only (no DB)
pytest -m unit               # by marker
pytest -m "not slow"         # skip slow suites
pytest --cov=src --cov-report=html --cov-report=term   # with coverage
```

Makefile shortcuts: `make test`, `make test-unit`, `make test-integration`,
`make test-e2e`, `make test-coverage`.

### Selecting and narrowing

```bash
pytest tests/unit/test_optimizer.py                    # one file
pytest tests/unit/test_optimizer.py::test_solve        # one test
pytest -k "telemetry and not slow"                     # substring / boolean match
pytest tests/integration/ --collect-only               # list what would run
pytest --markers                                       # show registered markers
```

### Running in parallel

`pytest-xdist` is included in the dev extras:

```bash
pytest -n auto               # one worker per CPU
pytest tests/unit -n 4       # fixed worker count
```

Skip xdist for DB-backed suites that share fixtures unless those fixtures are
worker-isolated.

### Debugging a failure

```bash
pytest path::test --pdb          # drop into the debugger at the failure
pytest path::test -s             # don't capture stdout (see prints/logs)
pytest path::test --tb=long      # full traceback (pytest.ini defaults to short)
pytest --lf                      # rerun only last-failed
pytest -x                        # stop at first failure
```

## Markers

The markers registered in `pytest.ini` (`--strict-markers` is on, so an
unregistered marker is an error):

| Marker | Meaning |
|---|---|
| `unit` | Unit tests |
| `integration` | Integration tests |
| `load` | Load tests |
| `e2e` | End-to-end tests |
| `slow` | Slow-running tests |
| `citrineos` | Tests requiring CitrineOS |
| `docker` | Tests requiring Docker |
| `compliance` | OCPP compliance tests |
| `critical` | Critical-path tests |
| `edge_case` | Edge-case tests |
| `security` | Security tests |
| `acceptance` | PRD acceptance-criteria tests |
| `performance` | Performance benchmark tests |
| `database` | Tests requiring a real database connection |
| `snapshot` | Snapshot tests against the live LLM (gated; nightly only) |
| `workflow_golden` | Parametrised workflow eval scenarios (real DB required) |
| `agent_sql_golden` | Parametrised agent-SQL eval scenarios (real TS + static DB pair required) |
| `agent_sql_real` | Agent-SQL real-DB + real-Anthropic integration tests (TS + static pair AND `ANTHROPIC_API_KEY`) |
| `agent_sql_live` | Nightly agent-SQL live shadow suite (TS + static pair AND `ANTHROPIC_API_KEY`; non-gating) |

`asyncio_mode = auto`, so `async def test_*` functions run without an explicit
`@pytest.mark.asyncio` decorator.

## Golden-test gates

These deterministic suites gate every deploy for the agent surfaces — they replay a
fixed LLM trace through a fake Anthropic client, so they need no live model:

- **Chat agent, fast path:** `tests/golden/agent_consumption.yaml` (50 Q&A pairs),
  driven by `tests/golden/test_agent_golden.py`.
- **Chat agent, SQL mode:** `tests/golden/agent_sql*.yaml` + `tests/golden/agent_sql/`,
  driven by `tests/golden/test_agent_sql_golden.py` (marker `agent_sql_golden`;
  requires a TimescaleDB + static DB pair).
- **Depot Agent workflows:** scenarios under `tests/golden/workflows/` against
  `_schema.yaml`, driven by `tests/golden/workflows/test_workflow_golden.py`
  (marker `workflow_golden`; real DB). CI enforces a 90% runner-coverage floor
  (`.github/workflows/workflow-golden.yml`).

A separate, **non-gating** nightly shadow runs the agent-SQL path against the live
model (`tests/live/test_agent_sql_live.py`, marker `agent_sql_live`).

## Acceptance-test coverage (PRD Section 11)

| ID | Scenario | Where |
|---|---|---|
| AT-01 | End-to-end optimization (≥99% SoC at departure) | `tests/integration/` |
| AT-02 | Demand-charge reduction (30–50%) | `tests/integration/` |
| AT-03 | Price-spike re-optimization (within 60s of >25% jump) | `tests/integration/` |
| AT-04 | SoC-deviation handling (>5%) | `tests/integration/` |
| AT-05 | Return-time deviation handling (>15 min late) | `tests/integration/` |
| AT-06 | Inter-depot handoff | `tests/integration/` |
| AT-07 | Building-load integration (grid calc includes building load) | `tests/integration/` |
| AT-17 | Alerts pipeline end-to-end (Faulted → trigger → email → Resend webhook → ack → resolve) | `tests/e2e/test_alerts_pipeline_e2e.py` |
| AT-18 | Agent Search end-to-end ("How much did John charge last month?" → resolve → aggregate → reply; cross-org → `not_found`) | `tests/e2e/test_agent_search.py` |

## Coverage requirements

| Scope | Floor | Source |
|---|---|---|
| Overall | ≥ 80% | `pytest.ini` (`fail_under = 80`) |
| Optimizer | ≥ 90% | PRD Section 11.2 |
| Surrogate model | ≥ 90% | PRD Section 11.2 |

Coverage source/omit and the threshold live in `pytest.ini` under
`[coverage:run]` / `[coverage:report]`. Generate an HTML report with
`pytest --cov=src --cov-report=html` (output in `htmlcov/`).

## Performance expectations

Solve-time targets apply to **both** Gurobi (primary) and HiGHS (fallback); HiGHS may
be slower but must still meet the bound (PRD Section 8.2/8.3):

- 10 vehicles: < 60 s
- 20 vehicles: < 60 s
- 50 vehicles: < 60 s (may require tuning)

Warm-start speedup target: > 3× (PRD Section 8.5).

## Solver-reliability checks (PRD Section 8.2)

- Gurobi license failure → automatic fallback to HiGHS.
- `solver_used` field in the result records which solver ran (`'gurobi'` | `'highs'`).
- Fallback events are logged with a reason.
- `GET /health` reports solver availability.

## EVerest closed-loop OCPP smoke test

Validate backend ↔ a standards-grade simulated charger with EVerest SIL:

```bash
make everest-up        # docker compose -f docker-compose.everest.yml up -d --build
make test-everest      # ./scripts/everest/run_backend_everest_smoke.sh
make everest-down
```

Optional pytest wrapper (explicit opt-in):

```bash
RUN_EVEREST_DOCKER_TEST=1 pytest tests/integration/test_everest_docker_smoke.py -v
```

Reference: `docs/EVEREST_TESTING.md`.

## Troubleshooting

- **`PytestUnknownMarkWarning` / "unknown marker" error** — `--strict-markers` is on;
  register the marker in `pytest.ini` before using it.
- **`object Mock can't be used in 'await' expression`** — mock async methods with
  `AsyncMock`, not `Mock`, so the awaited call returns a value rather than a coroutine.
- **DB connection errors in integration/e2e/golden suites** — confirm TimescaleDB (and
  the static DB for agent-SQL) is up and `DATABASE_URL` / `STATIC_DATABASE_URL` point
  at it; migrations apply on container boot or via `scripts/run_migrations.py`.
- **`InfeasibleModelError` in simulation tests** — some randomized scenarios are
  genuinely infeasible; this is expected, not a regression.
- **Slow "unit" tests** — usually an un-mocked DB connection or network call; verify
  the fixture mocks the I/O boundary.
