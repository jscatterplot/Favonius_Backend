"""Pure helpers for OCPP MeterValues normalization and session energy math.

Two responsibilities, both pure functions:

  1. ``normalize_energy_to_wh`` — convert an OCPP ``SampledValue`` reading
     to integer Wh regardless of the charger's reported ``unit`` /
     ``multiplier``. Centralises the unit handling that previously lived
     inline in ``FleetChargePoint.on_meter_values``.

  2. ``compute_energy_kwh`` — derive the billing kWh delta between two
     meter register snapshots. Returns ``None`` whenever either value
     is missing, or when the bracket is obviously bogus (start = 0,
     stop < start). Used by both ``close_open_session`` and the
     live-metrics UPDATE so the formula lives in exactly one place.

NULL is always preferred over a confidently-wrong number for accounting.
"""

from __future__ import annotations

from typing import Optional

# Multipliers from supported OCPP 1.6 energy units to Wh.
# kVArh / kVAh / VAh are stored alongside kWh on three-phase chargers; we
# normalise them with the same scale because the caller will have already
# filtered on measurand == "Energy.Active.Import.Register" before reaching
# here. The branch exists so a chrger that mislabels the unit on the
# register measurand still produces a sensible Wh number rather than a
# silent 1000x error.
_UNIT_TO_WH_SCALE: dict[str, float] = {
    "Wh": 1.0,
    "VARh": 1.0,
    "VAh": 1.0,
    "kWh": 1000.0,
    "kVArh": 1000.0,
    "kVAh": 1000.0,
}


def normalize_energy_to_wh(
    value: float,
    unit: Optional[str] = None,
    multiplier: Optional[int] = None,
) -> float:
    """Convert an OCPP energy ``SampledValue`` to Wh.

    Args:
        value: Raw numeric value from the ``SampledValue.value`` field.
        unit: ``SampledValue.unit`` — e.g. ``"Wh"``, ``"kWh"``. ``None`` /
            empty string defaults to ``"Wh"`` per OCPP 1.6 spec.
        multiplier: Optional OCPP ``SampledValue.multiplier`` — a power-of-ten
            offset applied AFTER the unit scale. ``None`` is treated as ``0``.

    Returns:
        The value expressed in Wh as a float.

    Raises:
        ValueError: If ``unit`` is non-empty and not in the supported set.
            Silent wrong billing is worse than a loud failure.
    """
    if unit is None or unit == "":
        unit_scale = 1.0  # OCPP 1.6 default for energy measurands is Wh
    else:
        # Case-insensitive lookup so chargers that send "WH" / "kwh" still work.
        canonical = _canonical_unit(unit)
        if canonical not in _UNIT_TO_WH_SCALE:
            raise ValueError(f"Unsupported energy unit for normalization: {unit!r}")
        unit_scale = _UNIT_TO_WH_SCALE[canonical]

    multiplier_scale = 10 ** (multiplier or 0)
    return value * unit_scale * multiplier_scale


def _canonical_unit(unit: str) -> str:
    """Map mixed-case unit strings to the canonical OCPP spelling.

    Chargers in the wild use ``kwh``, ``KWH``, ``Wh``, ``WH`` — accept all.
    """
    lowered = unit.strip().lower()
    for canonical in _UNIT_TO_WH_SCALE:
        if canonical.lower() == lowered:
            return canonical
    return unit  # let the caller surface it via the ValueError branch


def compute_energy_kwh(
    meter_stop_wh: Optional[int],
    meter_start_wh: Optional[int],
) -> Optional[float]:
    """Return the billing kWh delta between two meter register snapshots.

    Returns ``None`` when the bracket cannot be trusted:

      * ``meter_stop_wh`` is missing
      * ``meter_start_wh`` is missing
      * ``meter_start_wh == 0`` — some chargers emit zero when the meter
        register is unavailable; treating that as a real start produces
        a confidently-large bogus delta (e.g. 1,234 kWh for a 50-minute
        session)
      * ``meter_stop_wh < meter_start_wh`` — meter rollover, register
        reset, or charger replacement mid-session

    Args:
        meter_stop_wh: Final register reading (Wh). Can come from
            ``StopTransaction.meterStop`` directly, from a transactionData
            sample, or from the running ``last_meter_wh`` column.
        meter_start_wh: Initial register reading (Wh) recorded at
            ``StartTransaction``.

    Returns:
        Energy delivered in kWh, or ``None`` if the bracket is
        untrustworthy.
    """
    if meter_stop_wh is None or meter_start_wh is None:
        return None
    if meter_start_wh <= 0:
        return None
    if meter_stop_wh < meter_start_wh:
        return None
    return (meter_stop_wh - meter_start_wh) / 1000.0


# Default synthesized-delta cap (Wh). Sized for our pilot fleet — all
# vehicles in operation through 2026 have battery packs ≤ 200 kWh, and
# a single AC session realistically delivers ≤ 50 kWh (single-phase 7kW
# for ~7h, or 3-phase 22kW for ~2.5h). Anything above this cap is much
# more likely to be a charger sending an absolute register value (which
# would be billed bogus) than a legitimate single-session delta.
# Override via ``OCPP_SYNTHESIZED_DELTA_CAP_KWH`` env var in
# timescale_client.py.
DEFAULT_SYNTHESIZED_DELTA_CAP_WH: int = 50_000


def synthesize_energy_kwh_from_meter_stop(
    meter_stop_wh: Optional[int],
    cap_wh: int = DEFAULT_SYNTHESIZED_DELTA_CAP_WH,
) -> Optional[float]:
    """Treat ``meter_stop_wh`` as a synthetic session delta — bounded fallback.

    Only the close path should call this, and only when:

      * ``compute_energy_kwh`` has already returned ``None`` (the proper
        bracket-based delta is unavailable), and
      * ``meter_start_wh`` is ``NULL`` (never set, never backfilled — so
        the deferred-backfill path of ``_update_session_live_metrics``
        also failed because no Energy.Active.Import.Register sample
        ever arrived), and
      * ``last_meter_wh`` is ``NULL`` (same: no running register samples).

    In that triple-NULL state, the charger is configured such that its
    only energy signal is ``StopTransaction.meterStop`` — typical of ABB
    Terra AC firmwares whose measurand config refused our
    ``ChangeConfiguration`` push (see ocpp16_adapter.py:786-795). Some of
    those firmwares emit a per-session delta in ``meterStop`` rather than
    an absolute cumulative register; treating it as the session's
    delivered energy is the best signal we have.

    The cap is the safety belt: anything above it is rejected. With the
    50 kWh default a misinterpreted absolute register (cumulative since
    install, typically tens of MWh) is filtered out, while a real
    single-session AC delta (≤ ~50 kWh on our pilot fleet) passes.

    Args:
        meter_stop_wh: ``StopTransaction.meterStop`` after
            ``_resolve_meter_stop`` fallback. Must be positive; 0 / NULL
            are rejected.
        cap_wh: Upper bound; values strictly above this are rejected.

    Returns:
        Synthesised energy delivered in kWh, or ``None`` if the value is
        absent, non-positive, or above the cap.
    """
    if meter_stop_wh is None:
        return None
    if meter_stop_wh <= 0:
        return None
    if meter_stop_wh > cap_wh:
        return None
    return meter_stop_wh / 1000.0
