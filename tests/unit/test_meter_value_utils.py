"""Unit tests for src.websocket_handler.meter_value_utils helpers.

Pure functions, pure tests — no mocks, no DB. Each equivalence class
gets one test so a regression flips a single named case.
"""

from __future__ import annotations

import pytest

from src.websocket_handler.meter_value_utils import (
    compute_energy_kwh,
    normalize_energy_to_wh,
)


# ─── normalize_energy_to_wh ──────────────────────────────────────────────


class TestNormalizeEnergyToWh:
    def test_wh_identity(self):
        """Wh values pass through unchanged — chargers that already report Wh."""
        assert normalize_energy_to_wh(1234.0, "Wh") == 1234.0

    def test_kwh_scales_by_thousand(self):
        """kWh must be scaled up by 1000 — some chargers report register in kWh."""
        assert normalize_energy_to_wh(1.234, "kWh") == 1234.0

    def test_missing_unit_defaults_to_wh(self):
        """OCPP 1.6 spec: energy measurands default to Wh when unit is omitted."""
        assert normalize_energy_to_wh(5000.0, None) == 5000.0
        assert normalize_energy_to_wh(5000.0, "") == 5000.0

    def test_case_insensitive_unit(self):
        """Real chargers send 'WH', 'kwh', 'Wh' interchangeably — accept all."""
        assert normalize_energy_to_wh(100.0, "WH") == 100.0
        assert normalize_energy_to_wh(0.1, "kwh") == 100.0
        assert normalize_energy_to_wh(0.1, "KWH") == 100.0

    def test_multiplier_applies_after_unit_scale(self):
        """multiplier=3 with Wh shifts by 10^3 — same magnitude as kWh."""
        # OCPP 1.6 allows e.g. value=1, unit=Wh, multiplier=3 to mean 1000 Wh.
        assert normalize_energy_to_wh(1.0, "Wh", multiplier=3) == 1000.0
        assert normalize_energy_to_wh(1.0, "kWh", multiplier=3) == 1_000_000.0

    def test_negative_multiplier(self):
        """multiplier=-1 with Wh shifts down by a factor of 10."""
        assert normalize_energy_to_wh(100.0, "Wh", multiplier=-1) == 10.0

    def test_unsupported_unit_raises(self):
        """Loud failure beats silent wrong billing on unknown units."""
        with pytest.raises(ValueError, match="Unsupported energy unit"):
            normalize_energy_to_wh(100.0, "MWh")

    def test_apparent_and_reactive_energy_units_accepted(self):
        """kVArh / kVAh appear on three-phase chargers — treat as Wh-scale."""
        assert normalize_energy_to_wh(1.0, "kVArh") == 1000.0
        assert normalize_energy_to_wh(1000.0, "VAh") == 1000.0


# ─── compute_energy_kwh ──────────────────────────────────────────────────


class TestComputeEnergyKwh:
    def test_happy_path(self):
        """Normal bracket: stop 5000 Wh - start 1000 Wh = 4.0 kWh."""
        assert compute_energy_kwh(meter_stop_wh=5000, meter_start_wh=1000) == 4.0

    def test_zero_kwh_when_no_charging(self):
        """Identical stop and start = 0 kWh (vehicle plugged but not charging)."""
        assert compute_energy_kwh(meter_stop_wh=1000, meter_start_wh=1000) == 0.0

    def test_none_when_meter_stop_missing(self):
        """Missing meterStop — the HRX bug pattern. Must return None, not 0."""
        assert compute_energy_kwh(meter_stop_wh=None, meter_start_wh=1000) is None

    def test_none_when_meter_start_missing(self):
        """Missing meterStart — handler restart with no row to subtract from."""
        assert compute_energy_kwh(meter_stop_wh=5000, meter_start_wh=None) is None

    def test_none_when_both_missing(self):
        """Defensive: both None still returns None, not a crash."""
        assert compute_energy_kwh(meter_stop_wh=None, meter_start_wh=None) is None

    def test_none_when_meter_start_is_zero(self):
        """Issue 8A: meterStart=0 is a charger-side error signal, not a real reading.

        Some chargers emit 0 when the meter register is unavailable. Using it
        as a real start produces e.g. 1,234,567 Wh = 1,234 kWh for a session
        that actually delivered a few kWh. NULL is the correct answer.
        """
        assert compute_energy_kwh(meter_stop_wh=5000, meter_start_wh=0) is None

    def test_none_when_meter_stop_less_than_start(self):
        """Meter rollover, register reset, or charger replacement — leave NULL."""
        assert compute_energy_kwh(meter_stop_wh=1000, meter_start_wh=5000) is None

    def test_none_when_meter_start_is_negative(self):
        """Defensive: negative start (impossible per spec, but guard against it)."""
        assert compute_energy_kwh(meter_stop_wh=5000, meter_start_wh=-100) is None

    def test_precision_is_kwh_with_three_decimals(self):
        """Wh -> kWh divides by 1000 — preserves Wh-level precision."""
        result = compute_energy_kwh(meter_stop_wh=1234, meter_start_wh=1)
        assert result == pytest.approx(1.233)
