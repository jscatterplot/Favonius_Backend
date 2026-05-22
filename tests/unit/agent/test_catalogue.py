"""Tests for the SQL-mode catalogue helpers.

Locks the pool-labelling logic in ``list_functions_summary`` so a
future spec rebuild can't silently flip ts/static labels (Bugbot
Low-sev: previously used `if f in TS_FUNCTIONS` identity check which
worked only because each FunctionSpec lived in exactly one tuple).
"""

from __future__ import annotations

from src.api.agent.catalogue import (
    STATIC_FUNCTIONS,
    TS_FUNCTIONS,
    list_functions_summary,
)


def test_list_functions_summary_labels_ts_and_static_correctly():
    summary = list_functions_summary()
    pool_by_name = {entry["name"]: entry["pool"] for entry in summary}

    for spec in TS_FUNCTIONS:
        assert pool_by_name[f"agent_views.{spec.name}"] == "ts", (
            f"TS function {spec.name!r} mislabelled"
        )
    for spec in STATIC_FUNCTIONS:
        assert pool_by_name[f"agent_views.{spec.name}"] == "static", (
            f"Static function {spec.name!r} mislabelled"
        )


def test_list_functions_summary_pool_label_uses_name_not_identity():
    """If a TS function spec were ever rebuilt as a fresh object (same
    name, different instance), the previous `f in TS_FUNCTIONS`
    identity check would mislabel it as `static`. The new name-keyed
    check should still produce 'ts'.
    """
    summary = list_functions_summary()
    # Every name appears exactly once.
    names = [entry["name"] for entry in summary]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_list_functions_summary_includes_purpose_first_line():
    """The summary shows the first line of `purpose` so the LLM gets
    a one-line gist without the full description."""
    summary = list_functions_summary()
    for entry in summary:
        assert entry["purpose"], f"empty purpose for {entry['name']}"
        assert "\n" not in entry["purpose"], (
            f"purpose for {entry['name']} contains newline — "
            "should be one line only"
        )


def test_list_functions_summary_marks_hypertable_time_predicate():
    """The validator enforces a time predicate on the two hypertable
    functions. The summary surfaces that flag so the LLM knows up front.
    """
    summary = list_functions_summary()
    by_name = {entry["name"]: entry for entry in summary}

    assert by_name["agent_views.prices_hourly"]["requires_time_predicate"] is True
    assert by_name["agent_views.building_load_hourly"]["requires_time_predicate"] is True
    # Non-hypertable functions should NOT require a time predicate.
    assert by_name["agent_views.sessions"]["requires_time_predicate"] is False
    assert by_name["agent_views.depots"]["requires_time_predicate"] is False
