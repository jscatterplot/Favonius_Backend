"""Regression tests for optimization_input_snapshots INSERT SQL."""

from src.db.snapshot_store import _INSERT_SNAPSHOT_SQL


def test_insert_snapshot_sql_has_weather_forecast_comma():
    assert "weather_forecast_id,\n    recent_telemetry" in _INSERT_SNAPSHOT_SQL


def test_insert_snapshot_sql_has_17_placeholders_and_correct_casts():
    # weather_forecast_id is UUID (no jsonb cast), recent_telemetry is JSONB.
    assert "$13, $14::jsonb, $15, $16, $17" in _INSERT_SNAPSHOT_SQL
