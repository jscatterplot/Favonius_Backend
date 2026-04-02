"""Tests for incident response and CSIRT notification.

Tests cover:
- Incident creation and lifecycle
- Brute force detection
- Geo-block storm detection
- CSIRT notification generation
- Incident status management
"""

from __future__ import annotations

import pytest

from src.security.incident_response import (
    DetectionConfig,
    Incident,
    IncidentDetector,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    generate_csirt_notification,
)


class TestIncidentDetector:
    """Test automated incident detection."""

    @pytest.fixture
    def detector(self) -> IncidentDetector:
        """Detector with low thresholds for testing."""
        config = DetectionConfig(
            brute_force_threshold=5,
            brute_force_window_seconds=60,
            geo_block_storm_threshold=10,
        )
        return IncidentDetector(config=config)

    def test_brute_force_detected(self, detector: IncidentDetector):
        """Brute force detected when threshold exceeded."""
        incident = detector.check_brute_force("station_001", failed_count=5)
        assert incident is not None
        assert incident.incident_type == IncidentType.BRUTE_FORCE
        assert incident.severity == IncidentSeverity.HIGH
        assert "station_001" in incident.title

    def test_brute_force_below_threshold(self, detector: IncidentDetector):
        """No incident when below threshold."""
        incident = detector.check_brute_force("station_001", failed_count=3)
        assert incident is None

    def test_geo_block_storm_detected(self, detector: IncidentDetector):
        """Geo-block storm detected when threshold exceeded."""
        incident = detector.check_geo_block_storm(blocked_count=15)
        assert incident is not None
        assert incident.incident_type == IncidentType.GEO_BLOCK_STORM
        assert incident.severity == IncidentSeverity.MEDIUM

    def test_geo_block_storm_below_threshold(self, detector: IncidentDetector):
        """No incident when below threshold."""
        incident = detector.check_geo_block_storm(blocked_count=5)
        assert incident is None

    def test_incident_ids_sequential(self, detector: IncidentDetector):
        """Incident IDs are sequential."""
        i1 = detector.check_brute_force("s1", 10)
        i2 = detector.check_brute_force("s2", 10)
        assert i1.incident_id == "INC-000001"
        assert i2.incident_id == "INC-000002"


class TestIncidentLifecycle:
    """Test incident status management."""

    @pytest.fixture
    def detector_with_incident(self) -> IncidentDetector:
        """Detector with one existing incident."""
        detector = IncidentDetector(config=DetectionConfig(brute_force_threshold=1))
        detector.check_brute_force("station_001", failed_count=5)
        return detector

    def test_get_incidents(self, detector_with_incident: IncidentDetector):
        """Get all incidents."""
        incidents = detector_with_incident.get_incidents()
        assert len(incidents) == 1

    def test_get_open_incidents(self, detector_with_incident: IncidentDetector):
        """Get open (non-resolved) incidents."""
        open_incidents = detector_with_incident.get_open_incidents()
        assert len(open_incidents) == 1

    def test_mark_notified(self, detector_with_incident: IncidentDetector):
        """Mark incident as CSIRT-notified."""
        incident_id = detector_with_incident.get_incidents()[0].incident_id
        assert detector_with_incident.mark_notified(incident_id) is True

        incident = detector_with_incident.get_incidents()[0]
        assert incident.status == IncidentStatus.NOTIFIED
        assert incident.csirt_notified_at is not None

    def test_resolve_incident(self, detector_with_incident: IncidentDetector):
        """Resolve an incident."""
        incident_id = detector_with_incident.get_incidents()[0].incident_id
        assert detector_with_incident.resolve_incident(incident_id) is True

        incident = detector_with_incident.get_incidents()[0]
        assert incident.status == IncidentStatus.RESOLVED
        assert incident.resolved_at is not None

    def test_resolved_not_in_open(self, detector_with_incident: IncidentDetector):
        """Resolved incidents excluded from open list."""
        incident_id = detector_with_incident.get_incidents()[0].incident_id
        detector_with_incident.resolve_incident(incident_id)
        assert len(detector_with_incident.get_open_incidents()) == 0

    def test_mark_nonexistent_returns_false(self, detector_with_incident: IncidentDetector):
        """Marking nonexistent incident returns False."""
        assert detector_with_incident.mark_notified("INC-999999") is False
        assert detector_with_incident.resolve_incident("INC-999999") is False

    def test_filter_by_status(self, detector_with_incident: IncidentDetector):
        """Filter incidents by status."""
        detected = detector_with_incident.get_incidents(status=IncidentStatus.DETECTED)
        assert len(detected) == 1
        resolved = detector_with_incident.get_incidents(status=IncidentStatus.RESOLVED)
        assert len(resolved) == 0


class TestCSIRTNotification:
    """Test CSIRT notification payload generation."""

    def test_generates_valid_payload(self):
        """Generates a structured notification payload."""
        incident = Incident(
            incident_id="INC-000001",
            incident_type=IncidentType.BRUTE_FORCE,
            severity=IncidentSeverity.HIGH,
            title="Brute force on station_001",
            description="20 failed auth attempts",
            source_ips=["93.184.216.34"],
            affected_systems=["ocpp_websocket_handler"],
        )

        payload = generate_csirt_notification(incident)

        assert payload["incident_id"] == "INC-000001"
        assert payload["severity"] == "high"
        assert payload["csirt"] == "CERT-LT (cert@cert.lt)"
        assert payload["notification_type"] == "initial_24h"
        assert payload["suspected_unlawful"] is True  # Brute force
        assert "Favonius Energy" in payload["reporting_entity"]

    def test_non_unlawful_incident(self):
        """Non-unlawful incidents flagged correctly."""
        incident = Incident(
            incident_id="INC-000002",
            incident_type=IncidentType.GEO_BLOCK_STORM,
            severity=IncidentSeverity.MEDIUM,
            title="Geo-block storm",
            description="50 blocked requests",
        )

        payload = generate_csirt_notification(incident)
        assert payload["suspected_unlawful"] is False
