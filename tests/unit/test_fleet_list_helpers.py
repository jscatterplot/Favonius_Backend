"""Unit tests for the pure fleet-list helpers in ``src/api/fleet_list.py``.

The helpers are intentionally pure (no DB, no FastAPI) so we can pin the
state-derivation rules independently of the I/O layer. Every branch of
``derive_charger_status`` and ``derive_vehicle_state`` is covered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api.fleet_list import (
    CHARGER_OFFLINE_AGE_S,
    DEFAULT_DEPARTURE_SOC,
    VEHICLE_OFFLINE_AGE_S,
    _latest,
    derive_charger_status,
    derive_vehicle_state,
    format_charger_item,
    format_charger_session,
    format_vehicle_item,
    format_vehicle_next_departure,
)

NOW = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


# ─── derive_charger_status ────────────────────────────────────────────────


class TestDeriveChargerStatus:
    def test_faulted_wins_over_everything(self):
        # Even with a fresh heartbeat and an open session, Faulted → fault.
        assert (
            derive_charger_status(
                ocpp_status="Faulted",
                last_heartbeat_at=_ago(1),
                has_open_session=True,
                now=NOW,
            )
            == "fault"
        )

    def test_no_heartbeat_is_offline(self):
        assert (
            derive_charger_status(
                ocpp_status="Available",
                last_heartbeat_at=None,
                has_open_session=False,
                now=NOW,
            )
            == "offline"
        )

    def test_stale_heartbeat_is_offline(self):
        assert (
            derive_charger_status(
                ocpp_status="Available",
                last_heartbeat_at=_ago(CHARGER_OFFLINE_AGE_S + 5),
                has_open_session=False,
                now=NOW,
            )
            == "offline"
        )

    def test_unavailable_status_is_offline_even_when_fresh(self):
        assert (
            derive_charger_status(
                ocpp_status="Unavailable",
                last_heartbeat_at=_ago(1),
                has_open_session=False,
                now=NOW,
            )
            == "offline"
        )

    def test_charging_status_is_charging(self):
        assert (
            derive_charger_status(
                ocpp_status="Charging",
                last_heartbeat_at=_ago(1),
                has_open_session=True,
                now=NOW,
            )
            == "charging"
        )

    def test_open_session_makes_charging_even_when_status_is_idle_like(self):
        # Some chargers report Available + a still-open session row briefly
        # while transitioning. Trust the session.
        assert (
            derive_charger_status(
                ocpp_status="Available",
                last_heartbeat_at=_ago(1),
                has_open_session=True,
                now=NOW,
            )
            == "charging"
        )

    def test_available_with_no_session_is_idle(self):
        assert (
            derive_charger_status(
                ocpp_status="Available",
                last_heartbeat_at=_ago(1),
                has_open_session=False,
                now=NOW,
            )
            == "idle"
        )

    def test_preparing_finishing_suspended_all_idle(self):
        for status_value in ("Preparing", "Finishing", "SuspendedEV", "SuspendedEVSE", "Reserved"):
            assert (
                derive_charger_status(
                    ocpp_status=status_value,
                    last_heartbeat_at=_ago(1),
                    has_open_session=False,
                    now=NOW,
                )
                == "idle"
            ), status_value

    def test_unknown_status_falls_through_to_idle(self):
        # Defensive: an OCPP literal we haven't seen before with a fresh
        # heartbeat and no session should not stop the page from rendering.
        assert (
            derive_charger_status(
                ocpp_status="SomeFutureValue",
                last_heartbeat_at=_ago(1),
                has_open_session=False,
                now=NOW,
            )
            == "idle"
        )


# ─── derive_vehicle_state ─────────────────────────────────────────────────


class TestDeriveVehicleState:
    def test_no_telemetry_is_offline(self):
        assert (
            derive_vehicle_state(
                last_seen_at=None,
                has_open_session=False,
                has_active_schedule=False,
                current_soc=0.5,
                required_soc=1.0,
                now=NOW,
            )
            == "offline"
        )

    def test_stale_telemetry_is_offline(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(VEHICLE_OFFLINE_AGE_S + 60),
                has_open_session=True,  # ignored when offline wins
                has_active_schedule=True,
                current_soc=0.99,
                required_soc=1.0,
                now=NOW,
            )
            == "offline"
        )

    def test_open_session_is_charging(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=True,
                has_active_schedule=True,  # still charging — session beats schedule
                current_soc=0.4,
                required_soc=1.0,
                now=NOW,
            )
            == "charging"
        )

    def test_active_schedule_without_session_is_in_route(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=True,
                current_soc=0.5,
                required_soc=1.0,
                now=NOW,
            )
            == "in_route"
        )

    def test_parked_above_required_soc_is_ready(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=False,
                current_soc=0.99,
                required_soc=0.95,
                now=NOW,
            )
            == "ready"
        )

    def test_parked_below_required_soc_is_at_risk(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=False,
                current_soc=0.50,
                required_soc=0.95,
                now=NOW,
            )
            == "at_risk"
        )

    def test_no_required_soc_uses_default_threshold_ready(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=False,
                current_soc=DEFAULT_DEPARTURE_SOC,
                required_soc=None,
                now=NOW,
            )
            == "ready"
        )

    def test_no_required_soc_uses_default_threshold_at_risk(self):
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=False,
                current_soc=DEFAULT_DEPARTURE_SOC - 0.01,
                required_soc=None,
                now=NOW,
            )
            == "at_risk"
        )

    def test_missing_current_soc_falls_through_to_at_risk(self):
        # No SoC means we can't claim "ready"; default to the conservative
        # at_risk so the operator sees the row in the FleetReadiness card.
        assert (
            derive_vehicle_state(
                last_seen_at=_ago(10),
                has_open_session=False,
                has_active_schedule=False,
                current_soc=None,
                required_soc=1.0,
                now=NOW,
            )
            == "at_risk"
        )


# ─── format_charger_item / format_vehicle_item ───────────────────────────


def _static_charger_row(**overrides) -> dict:
    base = {
        "id": "0a8c1d7e-1c5b-4f93-9c3a-3e8f1ad24b9c",
        "depot_id": "9ce85c8e-c982-4803-9972-5d93bbcd8d37",
        "ocpp_id": "fav_nemunaitis-001",
        "display_name": "Bay 1",
        "vendor": "Kempower",
        "model": "C500",
        "serial_number": "KPW-001",
        "firmware": "2.4.1",
        "rated_kw": 150.0,
        "efficiency": 0.95,
        "connector_type": "CCS",
        "connector_count": 2,
        "connector_ids": [1, 2],
        "auth_required": True,
        "network_notes": None,
        "created_at": datetime(2026, 4, 12, 10, 14, 33, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _static_vehicle_row(**overrides) -> dict:
    base = {
        "id": "5c1e1d20-77a4-4d9e-8a36-2bc3a7f88f01",
        "depot_id": "9ce85c8e-c982-4803-9972-5d93bbcd8d37",
        "external_id": "BUS-217",
        "display_name": "Route 12 Bus 217",
        "vehicle_type": "transit_bus",
        "vin": "WMA5xx217",
        "license_plate": "AA-217-FAV",
        "id_tag": "ID-217",
        "battery_capacity_kwh": 350.0,
        "max_charge_rate_kw": 150.0,
        "max_discharge_rate_kw": 100.0,
        "v2g_capable": False,
        "make": "Volvo",
        "model": "BZL Electric",
        "year": 2024,
        "status": "active",
        "created_at": datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


class TestFormatChargerItem:
    def test_charger_with_no_runtime_is_offline(self):
        item = format_charger_item(
            _static_charger_row(),
            connector_status=None,
            open_session=None,
            now=NOW,
        )
        assert item["status"] == "offline"
        assert item["ocpp_connector_status"] is None
        assert item["last_heartbeat_at"] is None
        assert item["current_session"] is None
        # All static fields preserved.
        assert item["display_name"] == "Bay 1"
        assert item["connector_count"] == 2
        assert item["connector_ids"] == [1, 2]

    def test_charger_with_open_session_emits_session_object(self):
        session = {
            "session_id": "tx-100231",
            "vehicle_id": "5c1e1d20-77a4-4d9e-8a36-2bc3a7f88f01",
            "started_at": datetime(2026, 5, 3, 13, 42, 11, tzinfo=timezone.utc),
            "current_power_kw": 78.4,
            "current_soc": 0.62,
            "target_soc": 1.0,
            "estimated_end_at": datetime(2026, 5, 3, 14, 25, tzinfo=timezone.utc),
        }
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Charging", "last_heartbeat_at": _ago(5)},
            open_session=session,
            now=NOW,
        )
        assert item["status"] == "charging"
        assert item["ocpp_connector_status"] == "Charging"
        assert item["current_session"]["session_id"] == "tx-100231"
        assert item["current_session"]["current_power_kw"] == 78.4
        assert item["current_session"]["target_soc"] == 1.0

    def test_charger_with_faulted_status_marks_fault(self):
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Faulted", "last_heartbeat_at": _ago(5)},
            open_session=None,
            now=NOW,
        )
        assert item["status"] == "fault"

    def test_liveness_override_wins_over_stale_connector_status(self):
        """LivenessHub cache (fresh) supersedes stale connector_status MAX.

        Regression for "frontend says offline despite a heartbeat 1 min ago":
        connector_status only updates on state changes, so an idle charger
        with Heartbeats flowing every 5 min has a stale MAX(timestamp).
        The override carries the actual most-recent OCPP frame timestamp
        and must drive both ``last_interaction_at`` AND ``status``.
        """
        fresh = _ago(30)  # 30 s ago
        stale = _ago(50 * 60)  # 50 min ago
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Available", "last_heartbeat_at": stale},
            open_session=None,
            now=NOW,
            last_interaction_override=fresh,
        )
        assert item["status"] == "idle", "fresh override → not offline"
        assert item["last_interaction_at"] == fresh.isoformat()

    def test_no_override_falls_back_to_connector_status(self):
        """Cache cold → endpoint passes None override → fall back to DB value."""
        recent = _ago(5)
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Available", "last_heartbeat_at": recent},
            open_session=None,
            now=NOW,
            last_interaction_override=None,
        )
        assert item["status"] == "idle"
        assert item["last_interaction_at"] == recent.isoformat()

    def test_telemetry_freshness_rescues_stale_connector_status(self):
        """MeterValues freshness keeps an actively-metering charger online.

        Regression for "charging charger shows offline on the dashboard":
        connector_status MAX only advances on StatusNotification and the
        liveness pg_notify bridge can be down, so a steadily-charging charger
        would flip to ``offline`` once the connector_status row ages past the
        threshold. telemetry MAX (a row per MeterValues frame) is the third
        signal that prevents that.
        """
        stale = _ago(50 * 60)  # last StatusNotification 50 min ago
        fresh_meter = _ago(20)  # MeterValues 20 s ago
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Available", "last_heartbeat_at": stale},
            open_session=None,
            now=NOW,
            last_interaction_override=None,
            telemetry_last_seen=fresh_meter,
        )
        assert item["status"] == "idle", "fresh MeterValues → not offline"
        assert item["last_interaction_at"] == fresh_meter.isoformat()

    def test_freshest_of_all_three_signals_wins(self):
        """``last_interaction`` is the max of override / connector / telemetry."""
        override = _ago(90)
        connector = _ago(40)
        telemetry = _ago(10)  # freshest
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Available", "last_heartbeat_at": connector},
            open_session=None,
            now=NOW,
            last_interaction_override=override,
            telemetry_last_seen=telemetry,
        )
        assert item["last_interaction_at"] == telemetry.isoformat()

    def test_telemetry_only_signal_marks_online(self):
        """No connector_status and no override, but fresh telemetry → idle."""
        fresh_meter = _ago(15)
        item = format_charger_item(
            _static_charger_row(),
            connector_status=None,
            open_session=None,
            now=NOW,
            telemetry_last_seen=fresh_meter,
        )
        assert item["status"] == "idle"
        assert item["ocpp_connector_status"] is None
        assert item["last_interaction_at"] == fresh_meter.isoformat()

    def test_all_signals_stale_is_offline(self):
        """Telemetry that's also stale does not rescue — charger is offline."""
        item = format_charger_item(
            _static_charger_row(),
            connector_status={
                "ocpp_status": "Available",
                "last_heartbeat_at": _ago(CHARGER_OFFLINE_AGE_S + 30),
            },
            open_session=None,
            now=NOW,
            telemetry_last_seen=_ago(CHARGER_OFFLINE_AGE_S + 10),
        )
        assert item["status"] == "offline"

    def test_fresh_telemetry_does_not_override_faulted(self):
        """A real Faulted state still wins even when MeterValues are fresh."""
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Faulted", "last_heartbeat_at": _ago(5)},
            open_session=None,
            now=NOW,
            telemetry_last_seen=_ago(5),
        )
        assert item["status"] == "fault"

    def test_future_telemetry_timestamp_clamped_to_now(self):
        """A charger-supplied MeterValues timestamp far in the future is clamped
        to ``now`` so ``last_interaction_at`` stays sane and ``age()`` never
        goes negative (which would permanently suppress the offline threshold).
        """
        future = NOW + timedelta(hours=6)
        item = format_charger_item(
            _static_charger_row(),
            connector_status={"ocpp_status": "Available", "last_heartbeat_at": None},
            open_session=None,
            now=NOW,
            telemetry_last_seen=future,
        )
        # Age is 0 after clamping — charger is online (it just sent MeterValues).
        assert item["status"] == "idle"
        # The response timestamp must be now, not the future raw value.
        assert item["last_interaction_at"] == NOW.isoformat()


class TestLatestHelper:
    def test_returns_none_when_all_none(self):
        assert _latest(None, None) is None

    def test_picks_max_and_normalizes_naive_to_utc(self):
        aware = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)
        naive_newer = datetime(2026, 5, 3, 14, 5)  # naive → treated as UTC, newer
        assert _latest(aware, naive_newer, None) == naive_newer.replace(tzinfo=timezone.utc)

    def test_skips_non_datetime_values(self):
        ts = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)
        assert _latest("not-a-date", ts, 12345) == ts


class TestFormatVehicleItem:
    def test_vehicle_offline_when_no_telemetry(self):
        item = format_vehicle_item(
            _static_vehicle_row(),
            telemetry=None,
            open_session=None,
            next_departure=None,
            active_schedule=None,
            charger_id_by_ocpp_id={},
            now=NOW,
        )
        assert item["current_state"]["state"] == "offline"
        assert item["current_state"]["current_soc"] is None
        assert item["current_state"]["connected_charger_id"] is None
        assert item["next_departure"] is None
        # external_id always present
        assert item["external_id"] == "BUS-217"

    def test_vehicle_charging_resolves_charger_id_from_ocpp_id(self):
        charger_uuid = "0a8c1d7e-1c5b-4f93-9c3a-3e8f1ad24b9c"
        ocpp_id = "fav_nemunaitis-001"
        telemetry = {
            "last_seen_at": _ago(5),
            "current_soc": 0.62,
            "current_power_kw": 78.4,
            "charger_id": charger_uuid,
            "is_plugged": True,
        }
        session = {
            "session_id": "tx-100231",
            "ocpp_id": ocpp_id,
            "started_at": _ago(60),
            "current_power_kw": 78.4,
        }
        item = format_vehicle_item(
            _static_vehicle_row(),
            telemetry=telemetry,
            open_session=session,
            next_departure=None,
            active_schedule=None,
            charger_id_by_ocpp_id={ocpp_id: charger_uuid},
            now=NOW,
        )
        assert item["current_state"]["state"] == "charging"
        assert item["current_state"]["connected_charger_id"] == charger_uuid
        assert item["current_state"]["connected_session_id"] == "tx-100231"

    def test_vehicle_ready_when_above_required_soc(self):
        telemetry = {
            "last_seen_at": _ago(5),
            "current_soc": 0.99,
            "current_power_kw": 0.0,
            "charger_id": None,
            "is_plugged": False,
        }
        next_departure = {
            "schedule_id": "sched-1",
            "route_id": "R12",
            "departure_time": NOW + timedelta(hours=2),
            "return_time": NOW + timedelta(hours=8),
            "required_soc": 0.95,
            "energy_kwh": 220.0,
        }
        item = format_vehicle_item(
            _static_vehicle_row(),
            telemetry=telemetry,
            open_session=None,
            next_departure=next_departure,
            active_schedule=None,
            charger_id_by_ocpp_id={},
            now=NOW,
        )
        assert item["current_state"]["state"] == "ready"
        assert item["next_departure"]["route_id"] == "R12"
        assert item["next_departure"]["required_soc"] == 0.95


class TestFormatSubObjects:
    def test_format_charger_session_none(self):
        assert format_charger_session(None) is None

    def test_format_charger_session_includes_id_tag(self):
        """``id_tag`` must round-trip from the open-sessions row so the
        frontend can render \"Unknown vehicle charging · RFID <tag>\" when
        the auth path was a card without a vehicle assignment."""
        session = {
            "session_id": "ses-1",
            "vehicle_id": None,  # cards-only flow, no rfid_card_vehicle_assignments row
            "id_tag": "0C923A35",
            "started_at": None,
            "current_power_kw": None,
            "current_soc": None,
            "target_soc": None,
            "estimated_end_at": None,
        }
        out = format_charger_session(session)
        assert out is not None
        assert out["id_tag"] == "0C923A35"
        assert out["vehicle_id"] is None

    def test_format_charger_session_id_tag_omitted(self):
        """A session row without an ``id_tag`` key (legacy fixture) must
        emit ``id_tag: None`` rather than raising KeyError."""
        session = {
            "session_id": "ses-2",
            "vehicle_id": "veh-1",
            "started_at": None,
            "current_power_kw": None,
            "current_soc": None,
            "target_soc": None,
            "estimated_end_at": None,
        }
        out = format_charger_session(session)
        assert out is not None
        assert out["id_tag"] is None

    def test_format_vehicle_next_departure_none(self):
        assert format_vehicle_next_departure(None) is None
