"""Unit tests for _resolve_meter_stop — StopTransaction.transactionData fallback.

The function exists because chargers in the wild (ABB Terra AC, budget
wallboxes) send ``meterStop=None`` or ``meterStop=0`` while populating
``StopTransaction.transactionData[]`` with the actual final register
reading. Tests cover the three cases that matter for billing.
"""

from __future__ import annotations

import pytest

from src.websocket_handler.ocpp16_adapter import _resolve_meter_stop


class TestResolveMeterStop:
    def test_prefers_meter_stop_when_valid(self):
        """Happy path: spec-compliant meterStop is taken verbatim."""
        result = _resolve_meter_stop(meter_stop=5000, transaction_data=[])
        assert result == 5000

    def test_prefers_meter_stop_even_when_transaction_data_present(self):
        """A non-zero meterStop is authoritative — transactionData is fallback only."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "9999",
                        "unit": "Wh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=5000, transaction_data=tx_data)
        # The spec-compliant top-level wins; we don't override good data.
        assert result == 5000

    def test_falls_back_when_meter_stop_is_none(self):
        """None meterStop -> use transactionData register reading."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "12345",
                        "unit": "Wh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 12345

    def test_falls_back_when_meter_stop_is_zero(self):
        """meterStop=0 is the Terra AC failure signal — prefer transactionData."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "8000",
                        "unit": "Wh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=0, transaction_data=tx_data)
        assert result == 8000

    def test_handles_kwh_unit_in_transaction_data(self):
        """Some chargers emit kWh in transactionData; normalize via the helper."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "12.345",
                        "unit": "kWh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 12345

    def test_handles_snake_case_sampled_value_key(self):
        """OCPP libraries vary on camelCase vs snake_case — handle both."""
        tx_data = [
            {
                "sampled_value": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "7777",
                        "unit": "Wh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 7777

    def test_returns_max_register_when_multiple_samples(self):
        """Multiple register snapshots — pick the largest (latest in monotonic series)."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "5000",
                        "unit": "Wh",
                    }
                ]
            },
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "6500",
                        "unit": "Wh",
                    }
                ]
            },
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 6500

    def test_ignores_non_register_measurands(self):
        """Power/Current/SoC samples in transactionData are not energy registers."""
        tx_data = [
            {
                "sampledValue": [
                    {"measurand": "Power.Active.Import", "value": "7000", "unit": "W"},
                    {"measurand": "SoC", "value": "85", "unit": "Percent"},
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        # No register samples -> propagates the None meterStop unchanged.
        assert result is None

    def test_empty_transaction_data_propagates_none(self):
        """No transactionData and no valid meterStop -> NULL bubble through."""
        assert _resolve_meter_stop(meter_stop=None, transaction_data=None) is None
        assert _resolve_meter_stop(meter_stop=None, transaction_data=[]) is None
        assert _resolve_meter_stop(meter_stop=0, transaction_data=[]) == 0

    def test_skips_malformed_value(self):
        """A garbled value field doesn't crash the resolver; skip it and continue."""
        tx_data = [
            {
                "sampledValue": [
                    {"measurand": "Energy.Active.Import.Register", "value": "not-a-number"},
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "4000",
                        "unit": "Wh",
                    },
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 4000

    def test_skips_unsupported_unit(self):
        """Unknown unit raises ValueError in normalize_energy_to_wh — skip that sample."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "100",
                        "unit": "MWh",  # not in the helper's allowlist
                    },
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "2000",
                        "unit": "Wh",
                    },
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        assert result == 2000

    def test_negative_register_rejected(self):
        """Negative energy register is physically impossible — skip the sample."""
        tx_data = [
            {
                "sampledValue": [
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "-50",
                        "unit": "Wh",
                    }
                ]
            }
        ]
        result = _resolve_meter_stop(meter_stop=None, transaction_data=tx_data)
        # No valid samples -> propagates None
        assert result is None
