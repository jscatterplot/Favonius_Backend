"""Incident response automation for NIS2 compliance.

NIS2 Article 23 requires:
- 24-hour initial notification to national CSIRT
- 72-hour detailed incident report
- Automated detection of security incidents

Lithuanian CSIRT: CERT-LT (cert.lt)
Reporting email: cert@cert.lt

This module provides:
- Incident severity classification per NIS2
- Automated detection triggers (brute force, geo-block storms, anomalies)
- Incident lifecycle management (detect → classify → notify → report)
- Structured incident records for audit trail
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class IncidentSeverity(str, Enum):
    """Incident severity levels per NIS2 Article 23."""

    CRITICAL = "critical"  # Service disruption, data breach
    HIGH = "high"  # Significant security event
    MEDIUM = "medium"  # Notable but contained event
    LOW = "low"  # Informational, no impact


class IncidentType(str, Enum):
    """Types of security incidents."""

    BRUTE_FORCE = "brute_force"
    GEO_BLOCK_STORM = "geo_block_storm"
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    DATA_EXFILTRATION = "data_exfiltration"
    SERVICE_DISRUPTION = "service_disruption"
    ANOMALOUS_TRAFFIC = "anomalous_traffic"
    SUPPLY_CHAIN = "supply_chain"
    CONFIGURATION_TAMPERING = "configuration_tampering"


class IncidentStatus(str, Enum):
    """Incident lifecycle status."""

    DETECTED = "detected"
    INVESTIGATING = "investigating"
    CONTAINED = "contained"
    NOTIFIED = "notified"  # CSIRT notified
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass
class Incident:
    """A security incident record.

    Attributes:
        incident_id: Unique identifier.
        incident_type: Classification of the incident.
        severity: NIS2 severity level.
        status: Current lifecycle status.
        title: Short description.
        description: Detailed description.
        detected_at: When the incident was detected.
        source_ips: IP addresses involved.
        affected_systems: Systems impacted.
        details: Additional structured data.
        csirt_notified_at: When CSIRT was notified (NIS2: within 24h).
        resolved_at: When the incident was resolved.
    """

    incident_id: str
    incident_type: IncidentType
    severity: IncidentSeverity
    title: str
    description: str
    status: IncidentStatus = IncidentStatus.DETECTED
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_ips: list[str] = field(default_factory=list)
    affected_systems: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    csirt_notified_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


# ── Detection Thresholds ─────────────────────────────────────────────────

@dataclass
class DetectionConfig:
    """Thresholds for automated incident detection.

    Attributes:
        brute_force_threshold: Failed auth attempts per station before alert.
        brute_force_window_seconds: Time window for brute force detection.
        geo_block_storm_threshold: Geo-blocked requests per minute before alert.
        rate_limit_storm_threshold: Rate limit violations per minute before alert.
    """

    brute_force_threshold: int = 20
    brute_force_window_seconds: int = 300
    geo_block_storm_threshold: int = 50
    rate_limit_storm_threshold: int = 100


class IncidentDetector:
    """Detects security incidents from audit events.

    Monitors patterns in security audit events and creates incidents
    when thresholds are exceeded. Designed to be called periodically
    from the audit logger's flush cycle.
    """

    def __init__(self, config: Optional[DetectionConfig] = None) -> None:
        self.config = config or DetectionConfig()
        self._incidents: list[Incident] = []
        self._event_counts: dict[str, int] = {}
        self._incident_counter = 0

    def check_brute_force(
        self, station_id: str, failed_count: int
    ) -> Optional[Incident]:
        """Check for brute force attack pattern.

        Args:
            station_id: The station being targeted.
            failed_count: Number of failed auth attempts in the window.

        Returns:
            Incident if threshold exceeded, None otherwise.
        """
        if failed_count >= self.config.brute_force_threshold:
            incident = self._create_incident(
                incident_type=IncidentType.BRUTE_FORCE,
                severity=IncidentSeverity.HIGH,
                title=f"Brute force attack detected on station {station_id}",
                description=(
                    f"Station {station_id} received {failed_count} failed "
                    f"authentication attempts within "
                    f"{self.config.brute_force_window_seconds} seconds."
                ),
                details={
                    "station_id": station_id,
                    "failed_attempts": failed_count,
                    "window_seconds": self.config.brute_force_window_seconds,
                },
                affected_systems=["ocpp_websocket_handler"],
            )
            return incident
        return None

    def check_geo_block_storm(
        self, blocked_count: int, window_minutes: int = 1
    ) -> Optional[Incident]:
        """Check for geo-block storm (coordinated access from blocked countries).

        Args:
            blocked_count: Number of geo-blocked requests in the window.
            window_minutes: Time window in minutes.

        Returns:
            Incident if threshold exceeded, None otherwise.
        """
        if blocked_count >= self.config.geo_block_storm_threshold:
            incident = self._create_incident(
                incident_type=IncidentType.GEO_BLOCK_STORM,
                severity=IncidentSeverity.MEDIUM,
                title="Geo-block storm detected",
                description=(
                    f"{blocked_count} requests blocked from restricted countries "
                    f"within {window_minutes} minute(s). Possible coordinated "
                    f"reconnaissance or DDoS attempt."
                ),
                details={
                    "blocked_count": blocked_count,
                    "window_minutes": window_minutes,
                },
                affected_systems=["main_api", "ocpp_websocket_handler"],
            )
            return incident
        return None

    def _create_incident(
        self,
        incident_type: IncidentType,
        severity: IncidentSeverity,
        title: str,
        description: str,
        details: Optional[dict] = None,
        source_ips: Optional[list[str]] = None,
        affected_systems: Optional[list[str]] = None,
    ) -> Incident:
        """Create and register a new incident."""
        self._incident_counter += 1
        incident = Incident(
            incident_id=f"INC-{self._incident_counter:06d}",
            incident_type=incident_type,
            severity=severity,
            title=title,
            description=description,
            details=details or {},
            source_ips=source_ips or [],
            affected_systems=affected_systems or [],
        )
        self._incidents.append(incident)
        logger.warning(
            "Security incident detected: [%s] %s — %s",
            incident.severity.value,
            incident.incident_id,
            incident.title,
        )
        return incident

    def get_incidents(
        self, status: Optional[IncidentStatus] = None
    ) -> list[Incident]:
        """Get all incidents, optionally filtered by status."""
        if status is not None:
            return [i for i in self._incidents if i.status == status]
        return self._incidents.copy()

    def get_open_incidents(self) -> list[Incident]:
        """Get all non-resolved incidents."""
        closed = {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}
        return [i for i in self._incidents if i.status not in closed]

    def mark_notified(self, incident_id: str) -> bool:
        """Mark an incident as notified to CSIRT.

        NIS2 requires notification within 24 hours of detection.
        """
        for incident in self._incidents:
            if incident.incident_id == incident_id:
                incident.status = IncidentStatus.NOTIFIED
                incident.csirt_notified_at = datetime.now(timezone.utc)
                logger.info(
                    "Incident %s marked as CSIRT-notified at %s",
                    incident_id,
                    incident.csirt_notified_at.isoformat(),
                )
                return True
        return False

    def resolve_incident(self, incident_id: str) -> bool:
        """Mark an incident as resolved."""
        for incident in self._incidents:
            if incident.incident_id == incident_id:
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = datetime.now(timezone.utc)
                logger.info("Incident %s resolved", incident_id)
                return True
        return False


# ── CSIRT Notification ───────────────────────────────────────────────────


def generate_csirt_notification(incident: Incident) -> dict[str, Any]:
    """Generate a structured CSIRT notification payload.

    Per NIS2 Article 23, the initial notification (within 24 hours)
    must include:
    - Whether the incident is suspected to be caused by unlawful actions
    - Whether it could have cross-border impact
    - Initial assessment of severity

    The 72-hour detailed report adds:
    - Nature and impact assessment
    - Indicators of compromise
    - Mitigation measures taken

    Returns:
        Dict suitable for JSON serialization and submission to CERT-LT.
    """
    return {
        "notification_type": "initial_24h",
        "reporting_entity": "Favonius Energy (EV Charging CPO)",
        "csirt": "CERT-LT (cert@cert.lt)",
        "incident_id": incident.incident_id,
        "detected_at": incident.detected_at.isoformat(),
        "incident_type": incident.incident_type.value,
        "severity": incident.severity.value,
        "title": incident.title,
        "description": incident.description,
        "affected_systems": incident.affected_systems,
        "source_ips": incident.source_ips,
        "cross_border_impact": False,  # Assess per incident
        "suspected_unlawful": incident.incident_type
        in {IncidentType.BRUTE_FORCE, IncidentType.UNAUTHORIZED_ACCESS},
        "initial_mitigation": "Automated blocking and rate limiting active",
        "contact": {
            "organization": "Favonius Energy",
            "email": "security@favonius.energy",
        },
    }
