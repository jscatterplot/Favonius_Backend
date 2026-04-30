"""Unit tests for src.notifications.severity.

These tests pin the Python ↔ DB mapping. If migration 022's severity_level
generated column ever changes, both sides must be updated together.
"""

from __future__ import annotations

import pytest

from src.notifications.severity import Severity


class TestSeverityValues:
    def test_string_values_match_db_check_constraint(self):
        assert Severity.INFO.value == "info"
        assert Severity.WARNING.value == "warning"
        assert Severity.CRITICAL.value == "critical"

    def test_levels_match_db_generated_column(self):
        assert Severity.INFO.level == 1
        assert Severity.WARNING.level == 2
        assert Severity.CRITICAL.level == 3

    def test_levels_are_strictly_ordered(self):
        assert Severity.INFO.level < Severity.WARNING.level < Severity.CRITICAL.level


class TestFromStr:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("info", Severity.INFO),
            ("warning", Severity.WARNING),
            ("critical", Severity.CRITICAL),
            ("INFO", Severity.INFO),
            ("Warning", Severity.WARNING),
            ("CRITICAL", Severity.CRITICAL),
        ],
    )
    def test_parses_canonical_and_mixed_case(self, raw, expected):
        assert Severity.from_str(raw) is expected

    @pytest.mark.parametrize("bad", ["fatal", "warn", "", "high", "1"])
    def test_unknown_strings_raise(self, bad):
        with pytest.raises(ValueError):
            Severity.from_str(bad)

    @pytest.mark.parametrize("bad", [None, 1, 2.0, ["warning"]])
    def test_non_string_raises_typeerror(self, bad):
        with pytest.raises(TypeError):
            Severity.from_str(bad)


class TestFromLevel:
    @pytest.mark.parametrize(
        "level, expected",
        [(1, Severity.INFO), (2, Severity.WARNING), (3, Severity.CRITICAL)],
    )
    def test_inverse_of_level_property(self, level, expected):
        assert Severity.from_level(level) is expected
        assert expected.level == level

    @pytest.mark.parametrize("bad", [0, 4, -1, 100])
    def test_unknown_level_raises(self, bad):
        with pytest.raises(ValueError):
            Severity.from_level(bad)

    def test_round_trip(self):
        for sev in Severity:
            assert Severity.from_level(sev.level) is sev


class TestMeetsThreshold:
    """Recipient gating semantics.

    A recipient with min_severity=warning should receive warning and critical
    alerts but not info. Boundary case: severity == threshold IS included.
    """

    def test_critical_meets_all_thresholds(self):
        assert Severity.CRITICAL.meets_threshold(Severity.INFO)
        assert Severity.CRITICAL.meets_threshold(Severity.WARNING)
        assert Severity.CRITICAL.meets_threshold(Severity.CRITICAL)

    def test_warning_meets_warning_and_below(self):
        assert Severity.WARNING.meets_threshold(Severity.INFO)
        assert Severity.WARNING.meets_threshold(Severity.WARNING)
        assert not Severity.WARNING.meets_threshold(Severity.CRITICAL)

    def test_info_meets_only_info(self):
        assert Severity.INFO.meets_threshold(Severity.INFO)
        assert not Severity.INFO.meets_threshold(Severity.WARNING)
        assert not Severity.INFO.meets_threshold(Severity.CRITICAL)

    def test_threshold_at_self_is_inclusive(self):
        for sev in Severity:
            assert sev.meets_threshold(sev), f"{sev} should meet its own threshold"


class TestEnumBehavior:
    def test_str_subclass_means_db_value_serialization_works(self):
        """Severity is a str-Enum so asyncpg/json serialize it as the text value."""
        assert Severity.WARNING == "warning"
        assert str(Severity.WARNING.value) == "warning"

    def test_iteration_order(self):
        assert list(Severity) == [Severity.INFO, Severity.WARNING, Severity.CRITICAL]
