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
