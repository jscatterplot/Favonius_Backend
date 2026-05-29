"""Traffic-fine triage agent — domain layer.

LLM-independent building blocks for the traffic-fine workflow:

* :mod:`~src.core.traffic_fines.models` — the structured-output schema the LLM
  fills in (``TrafficFineExtraction``) plus the deterministic verdict type
  (``EarlyPaymentEvaluation``).
* :mod:`~src.core.traffic_fines.evaluator` — the pure, ``now``-injectable
  early-payment-window evaluation and operator-message builder.
* :mod:`~src.core.traffic_fines.media` — magic-byte media detection and
  Anthropic content-block construction.
* :mod:`~src.core.traffic_fines.config` — environment-driven feature flag and
  knobs.

The workflow wiring (Anthropic tool-use, alert raising, persistence) lives in
``src/api/agent_workflows/traffic_fine.py`` and ``src/api/traffic_fines.py``.
"""
