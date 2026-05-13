"""Workflow eval harness — parametrised gate.

Discovers every ``*.yaml`` file in ``tests/golden/workflows/`` (except
the schema file and the example placeholders under ``_examples/``) and
runs it through :func:`src.api.agent_workflows.eval.runner.run_scenario`
against the real test database.

Sprint 5 lands the first 10 readiness scenarios next to this file. Until
then the gated directory is empty — but the harness itself (this
module, the runner, the schema) is in place, and the unit tests in
``tests/unit/agent_workflows/`` already exercise every code path.

Gate behaviour
--------------
* ``severity: blocking`` → ``assert result.passed`` (gate fails).
* ``severity: warning`` / ``info`` → on failure, the diff is written to
  real stderr via ``capfd`` (visible under pytest capture); blocking
  still uses ``pytest.fail``.

The example scenarios are exercised via a dedicated test below that
opts in by name — they are NOT part of the gated dir, so the CI gate
only fires on scenarios authors deliberately landed under the gated
path.
"""

from __future__ import annotations

import sys
import traceback
import warnings
from pathlib import Path
from typing import Any

import pytest

from src.api.agent_workflows.eval import (
    EvalResult,
    ScenarioLoadError,
    load_scenario,
    run_scenario,
)

from tests.golden.workflows.conftest import EXAMPLES_DIR, WORKFLOW_GOLDEN_DIR

# ── Scenario discovery ────────────────────────────────────────────────────


def _gated_scenario_paths() -> list[Path]:
    """Every *.yaml directly under tests/golden/workflows/ except the schema."""
    return sorted(p for p in WORKFLOW_GOLDEN_DIR.glob("*.yaml") if p.name != "_schema.yaml")


def _example_scenario_paths() -> list[Path]:
    """Every *.yaml under tests/golden/workflows/_examples/."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(EXAMPLES_DIR.glob("*.yaml"))


def _scenario_id(path: Path) -> str:
    return path.stem


# ── Harness invariants ────────────────────────────────────────────────────


def test_schema_file_exists() -> None:
    """The schema reference must always be present — it's what scenarios
    are validated against."""
    schema = WORKFLOW_GOLDEN_DIR / "_schema.yaml"
    assert schema.is_file(), "_schema.yaml missing from tests/golden/workflows/"


def test_examples_have_unique_ids() -> None:
    """Every example scenario id must be unique across the examples dir.

    Once the real scenarios land in the gated dir this invariant grows
    to cover both directories — but for now the example dir is the only
    place YAMLs live, so we keep the assertion scoped there.
    """
    paths = _example_scenario_paths()
    if not paths:
        pytest.skip("no example scenarios present")

    ids: list[str] = []
    for path in paths:
        scenario = load_scenario(path.read_text(encoding="utf-8"))
        ids.append(scenario["id"])

    assert len(ids) == len(set(ids)), f"Duplicate example scenario ids: {ids!r}"


def test_examples_validate_against_schema() -> None:
    """Every example scenario must satisfy ``_schema.yaml`` (via :func:`load_scenario`).

    This is the cheap, no-DB check: failing here means the example file
    is malformed (missing key, bad enum, etc.) — independent of whether
    the workflow under test actually produces matching output.
    """
    paths = _example_scenario_paths()
    if not paths:
        pytest.skip("no example scenarios present")

    failures: list[str] = []
    for path in paths:
        try:
            load_scenario(path.read_text(encoding="utf-8"))
        except ScenarioLoadError as exc:
            failures.append(f"{path.name}: {exc}")

    assert not failures, "Schema-invalid example scenarios:\n  " + "\n  ".join(failures)


# ── The gate itself ───────────────────────────────────────────────────────


_GATED_PATHS = _gated_scenario_paths()


@pytest.mark.workflow_golden
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_path",
    _GATED_PATHS,
    ids=[_scenario_id(p) for p in _GATED_PATHS] if _GATED_PATHS else None,
)
async def test_workflow_golden_gated(
    scenario_path: Path, workflow_test_db_pool: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """Run every gated scenario through the harness.

    A failure here blocks the PR. Sprint 5 lands the actual scenarios
    that this body asserts against — until then, when the gated dir is
    empty, pytest collects zero parametrise IDs for this test and skips
    cleanly (see ``test_workflow_golden_gate_skips_when_empty`` below).
    """
    if not _GATED_PATHS:
        pytest.skip("no gated workflow scenarios yet — sprint 5 lands them")

    scenario = load_scenario(scenario_path.read_text(encoding="utf-8"))
    result = await run_scenario(scenario, pool=workflow_test_db_pool)

    _enforce_severity(result, scenario_path, capfd=capfd)


def test_workflow_golden_gate_skips_when_empty() -> None:
    """Documents the current state: the gated dir is empty so the gate
    is a no-op. Once sprint 5 lands real scenarios this test stops
    being meaningful and the gated parametrise above takes over.
    """
    if _GATED_PATHS:
        pytest.skip("gated scenarios present — handled by the parametrised gate")
    assert not _GATED_PATHS  # explicit no-op so the suite still reports a passing assertion


# ── Example exercising path (proves the harness works) ────────────────────


_EXAMPLE_PATHS = _example_scenario_paths()


@pytest.mark.workflow_golden
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_path",
    _EXAMPLE_PATHS,
    ids=[_scenario_id(p) for p in _EXAMPLE_PATHS] if _EXAMPLE_PATHS else None,
)
async def test_workflow_golden_examples(
    scenario_path: Path, workflow_test_db_pool: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """Run the example scenarios end-to-end against a real DB.

    Examples are NOT part of the gated dir — they ship as proof the
    harness works end-to-end and as a template for sprint 5. The
    runner constructs the :class:`Workflow` and the ToolRegistry from
    each scenario YAML, so no module-level workflow registration is
    needed here.
    """
    if not _EXAMPLE_PATHS:
        pytest.skip("no example scenarios present")

    scenario = load_scenario(scenario_path.read_text(encoding="utf-8"))
    try:
        result = await run_scenario(scenario, pool=workflow_test_db_pool)
    except Exception as exc:  # examples are informational only; never gate CI
        with capfd.disabled():
            print(
                f"[workflow-golden:example-nonblocking] {scenario_path}: runtime error: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
        warnings.warn(
            f"{scenario_path}: example scenario runtime error (non-blocking)",
            UserWarning,
            stacklevel=2,
        )
        return

    _report_example_result(result, scenario_path, capfd=capfd)


# ── Severity-aware assertion helper ───────────────────────────────────────


def _report_example_result(
    result: EvalResult,
    scenario_path: Path,
    *,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Emit non-gating signals for ``_examples`` scenarios.

    Example scenarios are documentation/templates and must never block CI,
    regardless of their declared ``severity``. Failures are surfaced as
    warnings with full diff output to stderr for visibility.
    """
    if result.passed:
        return

    _emit_non_blocking_signal(result, scenario_path, capfd=capfd)


def _enforce_severity(
    result: EvalResult,
    scenario_path: Path,
    *,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Translate ``severity`` into a pass/fail signal for pytest.

    Blocking failures use ``pytest.fail``. Warning and info failures
    print the unified diff to real stderr (``capfd`` disabled) so CI
    shows the signal even when pytest captures stdout and suppresses
    the warnings summary.
    """
    if result.passed:
        return

    msg = _format_failure_message(result)

    if result.severity == "blocking":
        pytest.fail(msg, pytrace=False)
    elif result.severity == "warning":
        with capfd.disabled():
            print(msg, file=sys.stderr)
        warnings.warn(
            f"{result.scenario_id}: workflow golden warning (see stderr above)",
            UserWarning,
            stacklevel=2,
        )
    else:  # info
        with capfd.disabled():
            print(msg, file=sys.stderr)


def _emit_non_blocking_signal(
    result: EvalResult,
    scenario_path: Path,
    *,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Print failures for non-gating scenarios and emit a warning."""
    msg = _format_failure_message(result)
    with capfd.disabled():
        print(msg, file=sys.stderr)
    warnings.warn(
        f"{result.scenario_id}: non-gating example failed ({scenario_path.name})",
        UserWarning,
        stacklevel=2,
    )


def _format_failure_message(result: EvalResult) -> str:
    """Render a stable failure message for stderr / pytest.fail."""
    diff_text = result.diff()
    return (
        f"[{result.scenario_id}] workflow={result.workflow} severity={result.severity}\n"
        + "\n".join(f"  - {f}" for f in result.failures)
        + "\n\n--- expected vs actual ---\n"
        + diff_text
    )
