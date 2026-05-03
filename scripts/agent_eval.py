#!/usr/bin/env python3
"""Side-by-side LLM evaluation for the depot chat agent.

Runs the same user message through ``extract_plan`` against multiple
Anthropic models and prints the results next to each other so a human
can choose which to default to via :envvar:`AGENT_LLM_MODEL`.

Usage:
    python scripts/agent_eval.py \\
        --message "how much did John charge last month" \\
        --models claude-sonnet-4-6,claude-haiku-4-5

The :envvar:`ANTHROPIC_API_KEY` env var must be set. This script makes
real LLM calls — keep the message count modest while iterating.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
from pathlib import Path
from typing import Any

# Allow running as a plain script (``python scripts/agent_eval.py``) without
# requiring callers to set PYTHONPATH; mirrors the pip-install editable layout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.api.agent.llm import KNOWN_GOOD_MODELS, LLMExtractionError, extract_plan  # noqa: E402


async def _run_one(message: str, model: str) -> tuple[str, dict[str, Any] | str]:
    """Run extraction against one model.

    Returns the model name and either the parsed plan as a dict (success)
    or an error string (failure). Failures are caught here so a bad
    model in the comparison set does not abort the whole eval.
    """
    try:
        plan = await extract_plan(message, model=model)
    except LLMExtractionError as exc:
        return model, f"<LLMExtractionError: {exc}>"
    except Exception as exc:  # pragma: no cover - defensive for unknown SDK errors
        return model, f"<{type(exc).__name__}: {exc}>"
    return model, plan.model_dump()


async def _main_async(message: str, models: list[str]) -> int:
    results = await asyncio.gather(*(_run_one(message, m) for m in models))

    print("=" * 78)
    print(f"MESSAGE: {message}")
    print("=" * 78)

    serialized: list[tuple[str, str]] = []
    for model, result in results:
        if isinstance(result, dict):
            text = json.dumps(result, indent=2, sort_keys=True)
        else:
            text = result
        serialized.append((model, text))

        print()
        print(f"--- {model} ---")
        print(text)

    if len(serialized) >= 2:
        a_model, a_text = serialized[0]
        b_model, b_text = serialized[1]
        if a_text == b_text:
            print()
            print(f"=== diff: {a_model} vs {b_model} ===")
            print("(identical)")
        else:
            print()
            print(f"=== diff: {a_model} vs {b_model} ===")
            diff = difflib.unified_diff(
                a_text.splitlines(),
                b_text.splitlines(),
                fromfile=a_model,
                tofile=b_model,
                lineterm="",
            )
            for line in diff:
                print(line)

    return 0


def _parse_models(arg: str) -> list[str]:
    models = [m.strip() for m in arg.split(",") if m.strip()]
    if not models:
        raise argparse.ArgumentTypeError("--models must list at least one model")
    unknown = [m for m in models if m not in KNOWN_GOOD_MODELS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"Unknown model(s): {unknown}. Allowed: {list(KNOWN_GOOD_MODELS)}"
        )
    return models


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the agent's extract_plan step across two or more models "
            "and print a side-by-side diff."
        )
    )
    parser.add_argument(
        "--message",
        required=True,
        help="The user-message text to extract a plan from.",
    )
    parser.add_argument(
        "--models",
        type=_parse_models,
        default=["claude-sonnet-4-6", "claude-haiku-4-5"],
        help=(
            "Comma-separated model IDs (default: "
            "claude-sonnet-4-6,claude-haiku-4-5). Must be from "
            "KNOWN_GOOD_MODELS in src/api/agent/llm.py."
        ),
    )
    args = parser.parse_args()

    return asyncio.run(_main_async(args.message, args.models))


if __name__ == "__main__":
    sys.exit(main())
