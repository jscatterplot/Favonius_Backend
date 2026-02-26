"""SQLAlchemy database models.

Reference: PRD_v2.md Section 6.1 (Database Schema)

These models define the database schema for the Favonius platform.
While the codebase primarily uses raw SQL with asyncpg for performance,
these models serve as the canonical schema definition and can be used
for migrations and ORM-based operations where convenient.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


# ============ REFERENCE DATA ============


class Depot(Base):
    """Depot (charging facility) reference data.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "depots"

    depot_id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name = Column(String(255), nullable=False)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)
    timezone = Column(String(50), default="America/Los_Angeles")
    utility_id = Column(
        String(100), nullable=True, comment="Utility provider identifier (e.g., 'PG&E', 'SCE')"
    )
    max_grid_kw = Column(Double, nullable=False)
    demand_charge_rate_kw = Column(Double, default=20.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    vehicles = relationship("Vehicle", back_populates="depot")
    chargers = relationship("Charger", back_populates="depot")
    battery_storage = relationship("BatteryStorage", back_populates="depot")


class Vehicle(Base):
    """Vehicle reference data.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "vehicles"

    vehicle_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    external_id = Column(
        String(100), unique=True, nullable=False, comment="Customer's vehicle ID (e.g., 'bus_101')"
    )
    vehicle_type = Column(String(50), nullable=False, comment="'bus_large', 'bus_small', 'van'")
    battery_kwh = Column(Double, nullable=False)
    max_charge_kw = Column(Double, nullable=False, comment="Default from config, updated by OCPP")
    id_tag = Column(
        String(100),
        nullable=True,
        comment="OCPP idTag used in Authorize messages to map sessions to vehicles",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    depot = relationship("Depot", back_populates="vehicles")
    schedules = relationship("Schedule", back_populates="vehicle")
    charger_access = relationship("ChargerVehicleAccess", back_populates="vehicle")


class Charger(Base):
    """Charger reference data.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "chargers"

    charger_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    ocpp_id = Column(String(100), unique=True, nullable=False)
    rated_kw = Column(Double, nullable=False)
    efficiency = Column(Double, default=0.95)
    connector_type = Column(String(50), default="CCS", comment="MVP: CCS only")
    status = Column(String(20), default="Available")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    depot = relationship("Depot", back_populates="chargers")
    vehicle_access = relationship("ChargerVehicleAccess", back_populates="charger")
    charging_commands = relationship("ChargingCommand", back_populates="charger")


class ChargerVehicleAccess(Base):
    """Physical accessibility: which vehicles can use which chargers.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "charger_vehicle_access"

    charger_id = Column(PGUUID(as_uuid=True), ForeignKey("chargers.charger_id"), primary_key=True)
    vehicle_id = Column(PGUUID(as_uuid=True), ForeignKey("vehicles.vehicle_id"), primary_key=True)
    is_accessible = Column(Boolean, default=True)
    notes = Column(String(255), nullable=True, comment="e.g., 'bay 3 blocked by pillar'")

    # Relationships
    charger = relationship("Charger", back_populates="vehicle_access")
    vehicle = relationship("Vehicle", back_populates="charger_access")


class BatteryStorage(Base):
    """Stationary battery storage system.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "battery_storage"

    battery_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    capacity_kwh = Column(Double, nullable=False)
    max_power_kw = Column(Double, nullable=False)
    efficiency = Column(Double, default=0.92)
    soc_min = Column(Double, default=0.2)
    soc_max = Column(Double, default=0.8)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("capacity_kwh > 0", name="battery_capacity_positive"),
        CheckConstraint("max_power_kw > 0", name="battery_power_positive"),
        CheckConstraint("efficiency > 0 AND efficiency <= 1", name="battery_efficiency_range"),
        CheckConstraint("soc_min >= 0 AND soc_min < 1", name="battery_soc_min_range"),
        CheckConstraint("soc_max > 0 AND soc_max <= 1", name="battery_soc_max_range"),
        CheckConstraint("soc_min < soc_max", name="battery_soc_range"),
    )

    # Relationships
    depot = relationship("Depot", back_populates="battery_storage")


# ============ TIME-SERIES DATA (Hypertables) ============
# Note: These tables are created as TimescaleDB hypertables in timescale_schema.py


class Telemetry(Base):
    """Vehicle telemetry time-series data.

    Reference: PRD_v2.md Section 6.1
    Note: This is a TimescaleDB hypertable, managed in timescale_schema.py
    """

    __tablename__ = "telemetry"

    # TimescaleDB hypertable - time is part of primary key
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    vehicle_id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    charger_id = Column(PGUUID(as_uuid=True), ForeignKey("chargers.charger_id"), nullable=True)
    soc = Column(Double, nullable=True)
    location_lat = Column(Double, nullable=True)
    location_lon = Column(Double, nullable=True)
    is_plugged = Column(Boolean, nullable=True)
    charging_kw = Column(Double, nullable=True)
    odometer_km = Column(Double, nullable=True)
    max_charge_kw = Column(Double, nullable=True, comment="From OCPP MeterValues")

    __table_args__ = (
        CheckConstraint("soc >= 0 AND soc <= 1", name="telemetry_soc_range"),
        CheckConstraint("location_lat >= -90 AND location_lat <= 90", name="telemetry_lat_range"),
        CheckConstraint("location_lon >= -180 AND location_lon <= 180", name="telemetry_lon_range"),
        CheckConstraint("charging_kw >= 0", name="telemetry_charging_positive"),
        CheckConstraint("odometer_km >= 0", name="telemetry_odometer_positive"),
        CheckConstraint("max_charge_kw > 0", name="telemetry_max_charge_positive"),
        Index("idx_telemetry_vehicle", "vehicle_id", "time"),
    )


class Price(Base):
    """Electricity price time-series data.

    Reference: PRD_v2.md Section 6.1
    Note: This is a TimescaleDB hypertable
    """

    __tablename__ = "prices"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    depot_id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    energy_kwh = Column(Double, nullable=False, comment="$/kWh")
    demand_kw = Column(Double, nullable=True, comment="$/kW (if different by period)")
    source = Column(String(50), nullable=True, comment="'caiso_dam', 'utility_tou'")


class WeatherForecast(Base):
    """Weather forecast time-series data.

    Reference: PRD_v2.md Section 6.1
    Note: This is a TimescaleDB hypertable
    """

    __tablename__ = "weather_forecasts"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    depot_id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    temp_f = Column(Double, nullable=True)
    temp_max_f = Column(Double, nullable=True)
    temp_min_f = Column(Double, nullable=True)
    precip_in = Column(Double, nullable=True)
    solar_rad = Column(Double, nullable=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())


class BuildingLoad(Base):
    """Building load time-series data.

    Reference: PRD_v2.md Section 6.1
    Note: This is a TimescaleDB hypertable
    """

    __tablename__ = "building_load"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    depot_id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    power_kw = Column(Double, nullable=False, comment="Building load (kW)")
    source = Column(String(50), nullable=False, comment="'meter', 'api', 'forecast'")


# ============ OPERATIONAL DATA ============


class Schedule(Base):
    """Vehicle schedule/route data.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "schedules"

    schedule_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    vehicle_id = Column(PGUUID(as_uuid=True), ForeignKey("vehicles.vehicle_id"), nullable=False)
    route_id = Column(String(100), nullable=True)
    departure_time = Column(DateTime(timezone=True), nullable=False)
    return_time = Column(DateTime(timezone=True), nullable=False)
    actual_return_time = Column(
        DateTime(timezone=True), nullable=True, comment="Updated when vehicle actually returns"
    )
    energy_kwh = Column(
        Double, nullable=True, comment="Estimated energy consumption (kWh) from surrogate model"
    )
    required_soc = Column(Double, default=1.0)
    dest_depot_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("depots.depot_id"),
        nullable=True,
        comment="if different from home depot",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("idx_schedules_vehicle_depart", "vehicle_id", "departure_time"),)

    # Relationships
    vehicle = relationship("Vehicle", back_populates="schedules")


class OptimizationRun(Base):
    """Optimization run history.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "optimization_runs"

    run_id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    run_time = Column(DateTime(timezone=True), server_default=func.now())
    trigger_reason = Column(
        String(50),
        nullable=False,
        comment="'scheduled', 'price_spike', 'soc_deviation', 'return_time_deviation', 'interdepot_handoff'",
    )
    horizon_start = Column(DateTime(timezone=True), nullable=False)
    horizon_end = Column(DateTime(timezone=True), nullable=False)
    solve_time_s = Column(Double, nullable=True)
    objective_value = Column(Double, nullable=True)
    peak_demand_kw = Column(Double, nullable=True)
    status = Column(String(20), default="completed")
    schedule_json = Column(
        JSONB, nullable=False, comment="Full optimization schedule (includes solver_used metadata)"
    )

    # Relationships
    charging_commands = relationship("ChargingCommand", back_populates="optimization_run")
    trigger_logs = relationship("TriggerLog", back_populates="optimization_run")


class ChargingCommand(Base):
    """Charging commands sent to chargers.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "charging_commands"

    command_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("optimization_runs.run_id"), nullable=True)
    charger_id = Column(PGUUID(as_uuid=True), ForeignKey("chargers.charger_id"), nullable=False)
    vehicle_id = Column(PGUUID(as_uuid=True), ForeignKey("vehicles.vehicle_id"), nullable=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
    profile_json = Column(JSONB, nullable=False)
    status = Column(String(20), default="pending", comment="'pending', 'accepted', 'rejected'")
    response_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    optimization_run = relationship("OptimizationRun", back_populates="charging_commands")
    charger = relationship("Charger", back_populates="charging_commands")


class InterdepotMessage(Base):
    """Inter-depot vehicle handoff messages.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "interdepot_messages"

    message_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    origin_depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    dest_depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    vehicle_id = Column(PGUUID(as_uuid=True), ForeignKey("vehicles.vehicle_id"), nullable=False)
    departure_time = Column(DateTime(timezone=True), nullable=False)
    expected_soc = Column(Double, nullable=False)
    arrival_time = Column(DateTime(timezone=True), nullable=False)
    battery_kwh = Column(Double, nullable=False)
    max_charge_kw = Column(Double, nullable=False)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    arrived_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("expected_soc >= 0 AND expected_soc <= 1", name="interdepot_soc_range"),
        CheckConstraint("battery_kwh > 0", name="interdepot_battery_positive"),
        CheckConstraint("max_charge_kw > 0", name="interdepot_max_charge_positive"),
        CheckConstraint(
            "status IN ('pending', 'acknowledged', 'arrived')", name="interdepot_status_valid"
        ),
        CheckConstraint("origin_depot_id != dest_depot_id", name="valid_depot_pair"),
        CheckConstraint("arrival_time > departure_time", name="valid_timing"),
        Index("idx_interdepot_dest_status", "dest_depot_id", "status"),
    )


class TriggerLog(Base):
    """Trigger event log for audit trail.

    Reference: PRD_v2.md Section 6.1
    """

    __tablename__ = "trigger_log"

    trigger_id = Column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    depot_id = Column(PGUUID(as_uuid=True), ForeignKey("depots.depot_id"), nullable=False)
    trigger_type = Column(
        String(50),
        nullable=False,
        comment="'soc_deviation', 'price_change', 'return_time_deviation', 'interdepot_handoff', 'scheduled'",
    )
    trigger_time = Column(DateTime(timezone=True), server_default=func.now())
    details = Column(
        JSONB, nullable=True, comment="e.g., {vehicle_id, expected_soc, actual_soc, deviation}"
    )
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("optimization_runs.run_id"), nullable=True)

    # Relationships
    optimization_run = relationship("OptimizationRun", back_populates="trigger_logs")
