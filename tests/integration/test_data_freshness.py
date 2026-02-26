"""Integration tests for data freshness validation.

Per PRD Section 5.3, data freshness thresholds are:
- Telemetry: 15 minutes
- Prices: 24 hours
- Weather: 6 hours
- Building load: 30 minutes
- Schedules: 1 hour

Reference: PRD_v2.md#5-3-data-freshness
"""

from datetime import datetime, timedelta

import pytest

from src.security.data_freshness import (
    MAX_BUILDING_LOAD_AGE,
    MAX_PRICE_AGE,
    MAX_SCHEDULE_AGE,
    MAX_TELEMETRY_AGE,
    MAX_WEATHER_AGE,
    check_data_freshness,
)


@pytest.mark.integration
class TestDataFreshness:
    """Test data freshness validation."""

    def test_telemetry_freshness_15_minute_threshold(self):
        """Test stale telemetry detection (15 min threshold)."""
        now = datetime.utcnow()

        # Fresh telemetry (5 minutes old)
        fresh_time = now - timedelta(minutes=5)
        status = check_data_freshness(telemetry_time=fresh_time, now=now)
        assert status.telemetry_fresh is True
        assert len(status.warnings) == 0

        # Stale telemetry (20 minutes old)
        stale_time = now - timedelta(minutes=20)
        status = check_data_freshness(telemetry_time=stale_time, now=now)
        assert status.telemetry_fresh is False
        assert any("Telemetry data stale" in w for w in status.warnings)

    def test_price_freshness_24_hour_threshold(self):
        """Test stale price detection (24 hour threshold)."""
        now = datetime.utcnow()

        # Fresh prices (12 hours old)
        fresh_time = now - timedelta(hours=12)
        status = check_data_freshness(price_time=fresh_time, now=now)
        assert status.price_fresh is True

        # Stale prices (25 hours old)
        stale_time = now - timedelta(hours=25)
        status = check_data_freshness(price_time=stale_time, now=now)
        assert status.price_fresh is False
        assert any("Price data stale" in w for w in status.warnings)

    def test_weather_freshness_6_hour_threshold(self):
        """Test stale weather detection (6 hour threshold)."""
        now = datetime.utcnow()

        # Fresh weather (3 hours old)
        fresh_time = now - timedelta(hours=3)
        status = check_data_freshness(weather_time=fresh_time, now=now)
        assert status.weather_fresh is True

        # Stale weather (7 hours old)
        stale_time = now - timedelta(hours=7)
        status = check_data_freshness(weather_time=stale_time, now=now)
        assert status.weather_fresh is False
        assert any("Weather data stale" in w for w in status.warnings)

    def test_building_load_freshness_30_minute_threshold(self):
        """Test stale building load detection (30 min threshold)."""
        now = datetime.utcnow()

        # Fresh building load (15 minutes old)
        fresh_time = now - timedelta(minutes=15)
        status = check_data_freshness(building_load_time=fresh_time, now=now)
        assert status.building_load_fresh is True

        # Stale building load (35 minutes old)
        stale_time = now - timedelta(minutes=35)
        status = check_data_freshness(building_load_time=stale_time, now=now)
        assert status.building_load_fresh is False
        assert any("Building load data stale" in w for w in status.warnings)

    def test_schedule_freshness_1_hour_threshold(self):
        """Test stale schedule detection (1 hour threshold)."""
        now = datetime.utcnow()

        # Fresh schedule (30 minutes old)
        fresh_time = now - timedelta(minutes=30)
        status = check_data_freshness(schedule_time=fresh_time, now=now)
        assert status.schedule_fresh is True

        # Stale schedule (70 minutes old)
        stale_time = now - timedelta(minutes=70)
        status = check_data_freshness(schedule_time=stale_time, now=now)
        assert status.schedule_fresh is False
        assert any("Schedule data stale" in w for w in status.warnings)

    def test_missing_data_handling(self):
        """Test fallback behavior when data is missing."""
        now = datetime.utcnow()

        # All data missing
        status = check_data_freshness(now=now)

        assert status.telemetry_fresh is False
        assert status.price_fresh is False
        assert status.weather_fresh is False
        assert status.building_load_fresh is False
        assert status.schedule_fresh is False
        assert status.all_fresh is False
        assert len(status.warnings) > 0
        assert any("No telemetry data" in w for w in status.warnings)
        assert any("No price data" in w for w in status.warnings)
        assert any("No schedule data" in w for w in status.warnings)

    def test_all_data_fresh(self):
        """Test when all data is fresh."""
        now = datetime.utcnow()

        status = check_data_freshness(
            telemetry_time=now - timedelta(minutes=5),
            price_time=now - timedelta(hours=12),
            weather_time=now - timedelta(hours=3),
            building_load_time=now - timedelta(minutes=15),
            schedule_time=now - timedelta(minutes=30),
            now=now,
        )

        assert status.telemetry_fresh is True
        assert status.price_fresh is True
        assert status.weather_fresh is True
        assert status.building_load_fresh is True
        assert status.schedule_fresh is True
        assert status.all_fresh is True
        assert len(status.warnings) == 0

    def test_mixed_fresh_and_stale_data(self):
        """Test when some data is fresh and some is stale."""
        now = datetime.utcnow()

        status = check_data_freshness(
            telemetry_time=now - timedelta(minutes=5),  # Fresh
            price_time=now - timedelta(hours=25),  # Stale
            weather_time=now - timedelta(hours=3),  # Fresh
            building_load_time=now - timedelta(minutes=35),  # Stale
            schedule_time=now - timedelta(minutes=30),  # Fresh
            now=now,
        )

        assert status.telemetry_fresh is True
        assert status.price_fresh is False
        assert status.weather_fresh is True
        assert status.building_load_fresh is False
        assert status.schedule_fresh is True
        assert status.all_fresh is False  # Not all critical data is fresh
        assert len(status.warnings) >= 2  # At least 2 warnings for stale data

    def test_exactly_at_threshold(self):
        """Test data exactly at freshness threshold."""
        now = datetime.utcnow()

        # Exactly at threshold (should be considered fresh)
        status = check_data_freshness(
            telemetry_time=now - MAX_TELEMETRY_AGE,
            price_time=now - MAX_PRICE_AGE,
            weather_time=now - MAX_WEATHER_AGE,
            building_load_time=now - MAX_BUILDING_LOAD_AGE,
            schedule_time=now - MAX_SCHEDULE_AGE,
            now=now,
        )

        # At threshold should be considered fresh (age <= threshold)
        assert status.telemetry_fresh is True
        assert status.price_fresh is True
        assert status.weather_fresh is True
        assert status.building_load_fresh is True
        assert status.schedule_fresh is True

    def test_freshness_does_not_block_optimization(self):
        """Test that stale data generates warnings but doesn't block optimization.

        Per PRD Section 5.3, stale data should generate warnings but
        optimization should proceed with fallback mechanisms.
        """
        now = datetime.utcnow()

        # All data is stale
        status = check_data_freshness(
            telemetry_time=now - timedelta(minutes=20),  # Stale
            price_time=now - timedelta(hours=25),  # Stale
            schedule_time=now - timedelta(hours=2),  # Stale
            now=now,
        )

        # Should have warnings but optimization can still proceed
        assert len(status.warnings) > 0
        # all_fresh is False, but optimization should still be possible
        # (actual blocking happens in optimization code, not here)
