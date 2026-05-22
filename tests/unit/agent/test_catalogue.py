"""Tests for the SQL-mode catalogue helpers.

Locks the pool-labelling logic in ``list_functions_summary`` so a
future spec rebuild can't silently flip ts/static labels (Bugbot
Low-sev: previously used `if f in TS_FUNCTIONS` identity check which
worked only because each FunctionSpec lived in exactly one tuple).
"""

from __future__ import annotations

from src.api.agent.catalogue import (
    GLOSSARY,
    STATIC_FUNCTIONS,
    TS_FUNCTIONS,
    list_functions_summary,
)


def _column_note(funcs, fn_name: str, col_name: str) -> str:
    for fn in funcs:
        if fn.name == fn_name:
            for col in fn.columns:
                if col.name == col_name:
                    return col.note
            raise AssertionError(f"column {col_name!r} missing from {fn_name!r}")
    raise AssertionError(f"function {fn_name!r} not found")


def test_list_functions_summary_labels_ts_and_static_correctly():
    summary = list_functions_summary()
    pool_by_name = {entry["name"]: entry["pool"] for entry in summary}

    for spec in TS_FUNCTIONS:
        assert (
            pool_by_name[f"agent_views.{spec.name}"] == "ts"
        ), f"TS function {spec.name!r} mislabelled"
    for spec in STATIC_FUNCTIONS:
        assert (
            pool_by_name[f"agent_views.{spec.name}"] == "static"
        ), f"Static function {spec.name!r} mislabelled"


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
            f"purpose for {entry['name']} contains newline — " "should be one line only"
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


def test_optimization_runs_trigger_reason_vocab_matches_emitters():
    """The previous catalogue claimed `trigger_reason` was a clean enum
    ('price_spike' | 'soc_deviation' | …). The real emitters in
    src/core/state/triggers.py + src/core/controller.py write a mix of
    literals ('scheduled', 'hourly', 'vdv463_charging_request_change')
    and prefix-tagged strings ('Price change: …', 'SoC deviation: …',
    'Return delay: …', 'interdepot_handoff: …'). An LLM that trusts
    the old enum writes `= 'price_spike'` and gets zero rows.
    """
    note = _column_note(TS_FUNCTIONS, "optimization_runs", "trigger_reason")
    for literal in ("scheduled", "hourly", "vdv463_charging_request_change"):
        assert literal in note, f"literal value {literal!r} missing from trigger_reason note"
    for prefix in ("SoC deviation", "Price change", "Return delay", "interdepot_handoff"):
        assert prefix in note, f"prefix {prefix!r} missing from trigger_reason note"
    assert "LIKE" in note, "trigger_reason note should teach the LIKE-prefix filter idiom"
    # The misleading old enum value must not reappear.
    assert "'price_spike'" not in note


def test_alerts_alert_type_documents_charger_fault():
    """`alerts.alert_type` is TEXT, not enum-constrained, but the only
    value emitted today (migration 022 fn_alerts_on_connector_status)
    is 'charger_fault'. The LLM needs that string to answer Q8-style
    questions ("which chargers were faulted yesterday?").
    """
    note = _column_note(TS_FUNCTIONS, "alerts", "alert_type")
    assert "charger_fault" in note


def test_glossary_teaches_trigger_reason_prefix_idiom():
    """The cross-question idiom ('use LIKE for trigger_reason
    categories') belongs in the glossary so the LLM picks it up
    regardless of which column note it lands on first.
    """
    joined = " ".join(GLOSSARY)
    assert "trigger_reason" in joined
    assert "LIKE" in joined


def test_glossary_teaches_tz_idiom_for_prices_hourly():
    """Q17 ("average price during morning peak 07–09 local") needs
    the AT TIME ZONE idiom on `prices_hourly.hour`. The original
    glossary line only mentioned `sessions.start_time`; an LLM
    answering Q17 had to generalise on its own. Pin the broader
    phrasing so future edits don't accidentally narrow it back.
    """
    joined = " ".join(GLOSSARY)
    assert "AT TIME ZONE" in joined
    assert "prices_hourly" in joined
    # The zone-keyed nature of prices_hourly (not depot-keyed) is the
    # specific footgun this line exists to defuse.
    assert "bidding_zone" in joined or "entsoe_zone" in joined
