"""Advanced metering with signed meter values, bidirectional energy accounting, and power quality monitoring."""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class Measurand(Enum):
    """OCPP measurands for metering."""

    ENERGY_ACTIVE_IMPORT_REGISTER = "Energy.Active.Import.Register"
    ENERGY_ACTIVE_EXPORT_REGISTER = "Energy.Active.Export.Register"
    ENERGY_REACTIVE_IMPORT_REGISTER = "Energy.Reactive.Import.Register"
    ENERGY_REACTIVE_EXPORT_REGISTER = "Energy.Reactive.Export.Register"
    POWER_ACTIVE_IMPORT = "Power.Active.Import"
    POWER_ACTIVE_EXPORT = "Power.Active.Export"
    POWER_REACTIVE_IMPORT = "Power.Reactive.Import"
    POWER_REACTIVE_EXPORT = "Power.Reactive.Export"
    CURRENT_IMPORT = "Current.Import"
    CURRENT_EXPORT = "Current.Export"
    VOLTAGE = "Voltage"
    FREQUENCY = "Frequency"
    TEMPERATURE = "Temperature"
    SOC = "SoC"
    RPM = "RPM"


class ReadingContext(Enum):
    """Reading contexts for meter values."""

    INTERRUPTION_BEGIN = "Interruption.Begin"
    INTERRUPTION_END = "Interruption.End"
    SAMPLE_CLOCK = "Sample.Clock"
    SAMPLE_PERIODIC = "Sample.Periodic"
    TRANSACTION_BEGIN = "Transaction.Begin"
    TRANSACTION_END = "Transaction.End"
    TRIGGER = "Trigger"


class ValueFormat(Enum):
    """Value formats for meter values."""

    RAW = "Raw"
    SIGNED_DATA = "SignedData"


class PowerQualityEvent(Enum):
    """Power quality events."""

    VOLTAGE_SAG = "VoltageSag"
    VOLTAGE_SWELL = "VoltageSwell"
    VOLTAGE_INTERRUPTION = "VoltageInterruption"
    FREQUENCY_DEVIATION = "FrequencyDeviation"
    HARMONIC_DISTORTION = "HarmonicDistortion"
    POWER_FACTOR_LOW = "PowerFactorLow"
    PHASE_IMBALANCE = "PhaseImbalance"


@dataclass
class MeterValue:
    """Meter value with signed data."""

    timestamp: datetime
    sampled_value: List[Dict[str, Any]]
    reading_context: ReadingContext
    format: ValueFormat = ValueFormat.RAW
    measurand: Optional[Measurand] = None
    location: Optional[str] = None
    unit: Optional[str] = None
    phase: Optional[str] = None


@dataclass
class SignedMeterValue:
    """Signed meter value for billing accuracy."""

    meter_value: MeterValue
    signature: str
    signature_method: str
    encoding_method: str
    public_key: str
    signed_data: str


@dataclass
class PowerQualityReading:
    """Power quality measurement."""

    timestamp: datetime
    voltage_l1: Optional[float] = None
    voltage_l2: Optional[float] = None
    voltage_l3: Optional[float] = None
    current_l1: Optional[float] = None
    current_l2: Optional[float] = None
    current_l3: Optional[float] = None
    frequency: Optional[float] = None
    power_factor: Optional[float] = None
    thd_voltage: Optional[float] = None  # Total Harmonic Distortion
    thd_current: Optional[float] = None
    phase_imbalance: Optional[float] = None
    events: List[PowerQualityEvent] = field(default_factory=list)


@dataclass
class EnergyAccounting:
    """Energy accounting for bidirectional flows."""

    station_id: str
    evse_id: int
    connector_id: int
    transaction_id: Optional[str]
    energy_import_kwh: float = 0.0
    energy_export_kwh: float = 0.0
    reactive_energy_import_kvarh: float = 0.0
    reactive_energy_export_kvarh: float = 0.0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: Optional[datetime] = None
    billing_accuracy: float = 0.1  # 0.1% accuracy requirement


class AdvancedMetering:
    """Advanced metering with signed values and power quality monitoring."""

    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Meter calibration cache
        self.meter_calibration: Dict[str, Dict[str, Any]] = {}

        # Power quality thresholds
        self.power_quality_thresholds = {
            "voltage_min": 380.0,  # V
            "voltage_max": 420.0,  # V
            "frequency_min": 49.5,  # Hz
            "frequency_max": 50.5,  # Hz
            "thd_voltage_max": 5.0,  # %
            "thd_current_max": 8.0,  # %
            "power_factor_min": 0.85,
            "phase_imbalance_max": 3.0,  # %
        }

        # Energy accounting cache
        self.energy_accounting: Dict[str, EnergyAccounting] = {}

        # Meter signing key
        self.meter_signing_key = secrets.token_urlsafe(32)

    async def process_meter_values(
        self, station_id: str, evse_id: int, meter_value: List[Dict[str, Any]]
    ) -> bool:
        """Process meter values with validation and signing."""
        try:
            for mv_data in meter_value:
                # Parse meter value
                meter_value_obj = await self._parse_meter_value(mv_data)

                # Validate meter value
                if not await self._validate_meter_value(station_id, meter_value_obj):
                    continue

                # Apply meter calibration
                calibrated_value = await self._apply_meter_calibration(station_id, meter_value_obj)

                # Create signed meter value
                signed_value = await self._create_signed_meter_value(calibrated_value)

                # Store signed meter value
                await self._store_signed_meter_value(station_id, evse_id, signed_value)

                # Update energy accounting
                await self._update_energy_accounting(station_id, evse_id, calibrated_value)

                # Check power quality
                await self._check_power_quality(station_id, calibrated_value)

            return True

        except Exception as e:
            self.logger.error(f"Error processing meter values: {e}")
            return False

    async def get_energy_accounting(
        self, station_id: str, evse_id: int, transaction_id: Optional[str] = None
    ) -> Optional[EnergyAccounting]:
        """Get energy accounting for station/EVSE/transaction."""
        try:
            # Check cache first
            cache_key = f"{station_id}:{evse_id}:{transaction_id or 'none'}"
            if cache_key in self.energy_accounting:
                return self.energy_accounting[cache_key]

            # Get from database
            accounting_data = await self.timescale_client.get_energy_accounting(
                station_id, evse_id, transaction_id
            )

            if accounting_data:
                accounting = EnergyAccounting(
                    station_id=accounting_data["station_id"],
                    evse_id=accounting_data["evse_id"],
                    connector_id=accounting_data["connector_id"],
                    transaction_id=accounting_data.get("transaction_id"),
                    energy_import_kwh=accounting_data["energy_import_kwh"],
                    energy_export_kwh=accounting_data["energy_export_kwh"],
                    reactive_energy_import_kvarh=accounting_data["reactive_energy_import_kvarh"],
                    reactive_energy_export_kvarh=accounting_data["reactive_energy_export_kvarh"],
                    start_time=accounting_data["start_time"],
                    end_time=accounting_data.get("end_time"),
                    billing_accuracy=accounting_data.get("billing_accuracy", 0.1),
                )

                # Cache the result
                self.energy_accounting[cache_key] = accounting

                return accounting

            return None

        except Exception as e:
            self.logger.error(f"Error getting energy accounting: {e}")
            return None

    async def get_power_quality_report(
        self, station_id: str, start_time: datetime, end_time: datetime
    ) -> Dict[str, Any]:
        """Get power quality report for time period."""
        try:
            # Get power quality readings
            readings = await self.timescale_client.get_power_quality_readings(
                station_id, start_time, end_time
            )

            if not readings:
                return {"error": "No power quality data available"}

            # Calculate statistics
            statistics = await self._calculate_power_quality_statistics(readings)

            # Identify events
            events = await self._identify_power_quality_events(readings)

            return {
                "station_id": station_id,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "statistics": statistics,
                "events": events,
                "compliance": await self._check_power_quality_compliance(statistics),
            }

        except Exception as e:
            self.logger.error(f"Error getting power quality report: {e}")
            return {"error": str(e)}

    async def calibrate_meter(self, station_id: str, calibration_data: Dict[str, Any]) -> bool:
        """Calibrate meter for billing accuracy."""
        try:
            # Validate calibration data
            if not await self._validate_calibration_data(calibration_data):
                return False

            # Store calibration
            await self.timescale_client.store_meter_calibration(
                {
                    "station_id": station_id,
                    "calibration_data": json.dumps(calibration_data),
                    "calibrated_at": datetime.now(timezone.utc),
                    "accuracy": calibration_data.get("accuracy", 0.1),
                }
            )

            # Update cache
            self.meter_calibration[station_id] = calibration_data

            self.logger.info(f"Calibrated meter for station {station_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error calibrating meter: {e}")
            return False

    async def get_meter_accuracy(self, station_id: str) -> float:
        """Get meter accuracy for station."""
        try:
            # Check cache first
            if station_id in self.meter_calibration:
                return self.meter_calibration[station_id].get("accuracy", 0.1)

            # Get from database
            accuracy = await self.timescale_client.get_meter_accuracy(station_id)
            return accuracy or 0.1

        except Exception as e:
            self.logger.error(f"Error getting meter accuracy: {e}")
            return 0.1

    async def _parse_meter_value(self, mv_data: Dict[str, Any]) -> MeterValue:
        """Parse meter value from OCPP message."""
        try:
            timestamp = datetime.fromisoformat(mv_data["timestamp"].replace("Z", "+00:00"))
            sampled_value = mv_data.get("sampledValue", [])

            # Determine reading context
            reading_context = ReadingContext.SAMPLE_PERIODIC
            if "context" in mv_data:
                reading_context = ReadingContext(mv_data["context"])

            # Determine format
            format_type = ValueFormat.RAW
            if "format" in mv_data:
                format_type = ValueFormat(mv_data["format"])

            return MeterValue(
                timestamp=timestamp,
                sampled_value=sampled_value,
                reading_context=reading_context,
                format=format_type,
                measurand=Measurand(mv_data.get("measurand", "Energy.Active.Import.Register")),
                location=mv_data.get("location"),
                unit=mv_data.get("unit"),
                phase=mv_data.get("phase"),
            )

        except Exception as e:
            self.logger.error(f"Error parsing meter value: {e}")
            raise

    async def _validate_meter_value(self, station_id: str, meter_value: MeterValue) -> bool:
        """Validate meter value."""
        try:
            # Check timestamp
            now = datetime.now(timezone.utc)
            if meter_value.timestamp > now + timedelta(minutes=5):
                self.logger.warning(f"Future timestamp in meter value: {meter_value.timestamp}")
                return False

            # Check sampled values
            if not meter_value.sampled_value:
                self.logger.warning("Empty sampled value in meter value")
                return False

            # Validate each sampled value
            for sv in meter_value.sampled_value:
                if not await self._validate_sampled_value(sv):
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating meter value: {e}")
            return False

    async def _validate_sampled_value(self, sampled_value: Dict[str, Any]) -> bool:
        """Validate sampled value."""
        try:
            # Check value
            value = sampled_value.get("value")
            if value is None:
                return False

            # Check measurand
            measurand = sampled_value.get("measurand")
            if measurand:
                try:
                    Measurand(measurand)
                except ValueError:
                    self.logger.warning(f"Unknown measurand: {measurand}")
                    return False

            # Check unit
            unit = sampled_value.get("unit")
            if unit:
                # Validate unit for measurand
                if not await self._validate_unit_for_measurand(measurand, unit):
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating sampled value: {e}")
            return False

    async def _validate_unit_for_measurand(self, measurand: str, unit: str) -> bool:
        """Validate unit for measurand."""
        try:
            # Define valid units for each measurand
            valid_units = {
                "Energy.Active.Import.Register": ["Wh", "kWh"],
                "Energy.Active.Export.Register": ["Wh", "kWh"],
                "Energy.Reactive.Import.Register": ["varh", "kvarh"],
                "Energy.Reactive.Export.Register": ["varh", "kvarh"],
                "Power.Active.Import": ["W", "kW"],
                "Power.Active.Export": ["W", "kW"],
                "Power.Reactive.Import": ["var", "kvar"],
                "Power.Reactive.Export": ["var", "kvar"],
                "Current.Import": ["A"],
                "Current.Export": ["A"],
                "Voltage": ["V"],
                "Frequency": ["Hz"],
                "Temperature": ["Celsius", "Fahrenheit"],
                "SoC": ["Percent"],
                "RPM": ["RPM"],
            }

            if measurand in valid_units:
                return unit in valid_units[measurand]

            return True  # Unknown measurand, allow any unit

        except Exception as e:
            self.logger.error(f"Error validating unit: {e}")
            return False

    async def _apply_meter_calibration(
        self, station_id: str, meter_value: MeterValue
    ) -> MeterValue:
        """Apply meter calibration to meter value."""
        try:
            # Get calibration data
            calibration = await self._get_meter_calibration(station_id)

            if not calibration:
                return meter_value

            # Apply calibration factors
            calibrated_sampled_value = []
            for sv in meter_value.sampled_value:
                calibrated_sv = sv.copy()

                # Apply calibration factor
                value = float(sv.get("value", 0))
                measurand = sv.get("measurand", "Energy.Active.Import.Register")

                if measurand in calibration.get("calibration_factors", {}):
                    factor = calibration["calibration_factors"][measurand]
                    calibrated_value = value * factor
                    calibrated_sv["value"] = str(calibrated_value)

                calibrated_sampled_value.append(calibrated_sv)

            # Create calibrated meter value
            calibrated_meter_value = MeterValue(
                timestamp=meter_value.timestamp,
                sampled_value=calibrated_sampled_value,
                reading_context=meter_value.reading_context,
                format=meter_value.format,
                measurand=meter_value.measurand,
                location=meter_value.location,
                unit=meter_value.unit,
                phase=meter_value.phase,
            )

            return calibrated_meter_value

        except Exception as e:
            self.logger.error(f"Error applying meter calibration: {e}")
            return meter_value

    async def _create_signed_meter_value(self, meter_value: MeterValue) -> SignedMeterValue:
        """Create signed meter value for billing accuracy."""
        try:
            # Create signed data
            signed_data = {
                "timestamp": meter_value.timestamp.isoformat(),
                "sampled_value": meter_value.sampled_value,
                "reading_context": meter_value.reading_context.value,
                "format": meter_value.format.value,
            }

            # Sign the data
            signed_data_json = json.dumps(signed_data, sort_keys=True)
            signature = hmac.new(
                self.meter_signing_key.encode(), signed_data_json.encode(), hashlib.sha256
            ).hexdigest()

            return SignedMeterValue(
                meter_value=meter_value,
                signature=signature,
                signature_method="HMAC-SHA256",
                encoding_method="JSON",
                public_key="",  # Would be set in production
                signed_data=signed_data_json,
            )

        except Exception as e:
            self.logger.error(f"Error creating signed meter value: {e}")
            raise

    async def _store_signed_meter_value(
        self, station_id: str, evse_id: int, signed_value: SignedMeterValue
    ) -> None:
        """Store signed meter value."""
        try:
            await self.timescale_client.store_signed_meter_value(
                {
                    "station_id": station_id,
                    "evse_id": evse_id,
                    "timestamp": signed_value.meter_value.timestamp,
                    "sampled_value": json.dumps(signed_value.meter_value.sampled_value),
                    "reading_context": signed_value.meter_value.reading_context.value,
                    "format": signed_value.meter_value.format.value,
                    "signature": signed_value.signature,
                    "signature_method": signed_value.signature_method,
                    "encoding_method": signed_value.encoding_method,
                    "public_key": signed_value.public_key,
                    "signed_data": signed_value.signed_data,
                }
            )

        except Exception as e:
            self.logger.error(f"Error storing signed meter value: {e}")

    async def _update_energy_accounting(
        self, station_id: str, evse_id: int, meter_value: MeterValue
    ) -> None:
        """Update energy accounting."""
        try:
            # Get or create energy accounting
            cache_key = f"{station_id}:{evse_id}:none"
            if cache_key not in self.energy_accounting:
                self.energy_accounting[cache_key] = EnergyAccounting(
                    station_id=station_id, evse_id=evse_id, connector_id=1
                )

            accounting = self.energy_accounting[cache_key]

            # Update energy values
            for sv in meter_value.sampled_value:
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                value = float(sv.get("value", 0))
                unit = sv.get("unit", "kWh")

                # Convert to kWh
                if unit == "Wh":
                    value = value / 1000
                elif unit == "kWh":
                    pass  # Already in kWh
                else:
                    continue  # Skip non-energy measurands

                # Update accounting based on measurand
                if measurand == "Energy.Active.Import.Register":
                    accounting.energy_import_kwh += value
                elif measurand == "Energy.Active.Export.Register":
                    accounting.energy_export_kwh += value
                elif measurand == "Energy.Reactive.Import.Register":
                    accounting.reactive_energy_import_kvarh += value
                elif measurand == "Energy.Reactive.Export.Register":
                    accounting.reactive_energy_export_kvarh += value

            # Store updated accounting
            await self.timescale_client.store_energy_accounting(
                {
                    "station_id": accounting.station_id,
                    "evse_id": accounting.evse_id,
                    "connector_id": accounting.connector_id,
                    "transaction_id": accounting.transaction_id,
                    "energy_import_kwh": accounting.energy_import_kwh,
                    "energy_export_kwh": accounting.energy_export_kwh,
                    "reactive_energy_import_kvarh": accounting.reactive_energy_import_kvarh,
                    "reactive_energy_export_kvarh": accounting.reactive_energy_export_kvarh,
                    "start_time": accounting.start_time,
                    "end_time": accounting.end_time,
                    "billing_accuracy": accounting.billing_accuracy,
                }
            )

        except Exception as e:
            self.logger.error(f"Error updating energy accounting: {e}")

    async def _check_power_quality(self, station_id: str, meter_value: MeterValue) -> None:
        """Check power quality and detect events."""
        try:
            # Extract power quality measurements
            pq_reading = await self._extract_power_quality_reading(meter_value)

            if not pq_reading:
                return

            # Check for power quality events
            events = await self._detect_power_quality_events(pq_reading)

            if events:
                # Store power quality events
                await self.timescale_client.store_power_quality_events(
                    {
                        "station_id": station_id,
                        "timestamp": pq_reading.timestamp,
                        "events": [event.value for event in events],
                        "voltage_l1": pq_reading.voltage_l1,
                        "voltage_l2": pq_reading.voltage_l2,
                        "voltage_l3": pq_reading.voltage_l3,
                        "current_l1": pq_reading.current_l1,
                        "current_l2": pq_reading.current_l2,
                        "current_l3": pq_reading.current_l3,
                        "frequency": pq_reading.frequency,
                        "power_factor": pq_reading.power_factor,
                        "thd_voltage": pq_reading.thd_voltage,
                        "thd_current": pq_reading.thd_current,
                        "phase_imbalance": pq_reading.phase_imbalance,
                    }
                )

                self.logger.warning(
                    f"Power quality events detected for {station_id}: {[e.value for e in events]}"
                )

        except Exception as e:
            self.logger.error(f"Error checking power quality: {e}")

    async def _extract_power_quality_reading(
        self, meter_value: MeterValue
    ) -> Optional[PowerQualityReading]:
        """Extract power quality measurements from meter value."""
        try:
            pq_reading = PowerQualityReading(timestamp=meter_value.timestamp)

            for sv in meter_value.sampled_value:
                measurand = sv.get("measurand", "")
                value = float(sv.get("value", 0))
                phase = sv.get("phase", "")

                if measurand == "Voltage":
                    if phase == "L1":
                        pq_reading.voltage_l1 = value
                    elif phase == "L2":
                        pq_reading.voltage_l2 = value
                    elif phase == "L3":
                        pq_reading.voltage_l3 = value

                elif measurand == "Current.Import":
                    if phase == "L1":
                        pq_reading.current_l1 = value
                    elif phase == "L2":
                        pq_reading.current_l2 = value
                    elif phase == "L3":
                        pq_reading.current_l3 = value

                elif measurand == "Frequency":
                    pq_reading.frequency = value

                elif measurand == "Power.Factor":
                    pq_reading.power_factor = value

                elif measurand == "THD.Voltage":
                    pq_reading.thd_voltage = value

                elif measurand == "THD.Current":
                    pq_reading.thd_current = value

            # Calculate phase imbalance
            if pq_reading.voltage_l1 and pq_reading.voltage_l2 and pq_reading.voltage_l3:
                voltages = [pq_reading.voltage_l1, pq_reading.voltage_l2, pq_reading.voltage_l3]
                avg_voltage = np.mean(voltages)
                max_deviation = max(abs(v - avg_voltage) for v in voltages)
                pq_reading.phase_imbalance = (max_deviation / avg_voltage) * 100

            return pq_reading

        except Exception as e:
            self.logger.error(f"Error extracting power quality reading: {e}")
            return None

    async def _detect_power_quality_events(
        self, pq_reading: PowerQualityReading
    ) -> List[PowerQualityEvent]:
        """Detect power quality events."""
        try:
            events = []

            # Check voltage events
            if pq_reading.voltage_l1:
                if pq_reading.voltage_l1 < self.power_quality_thresholds["voltage_min"]:
                    events.append(PowerQualityEvent.VOLTAGE_SAG)
                elif pq_reading.voltage_l1 > self.power_quality_thresholds["voltage_max"]:
                    events.append(PowerQualityEvent.VOLTAGE_SWELL)

            # Check frequency events
            if pq_reading.frequency:
                if (
                    pq_reading.frequency < self.power_quality_thresholds["frequency_min"]
                    or pq_reading.frequency > self.power_quality_thresholds["frequency_max"]
                ):
                    events.append(PowerQualityEvent.FREQUENCY_DEVIATION)

            # Check harmonic distortion
            if (
                pq_reading.thd_voltage
                and pq_reading.thd_voltage > self.power_quality_thresholds["thd_voltage_max"]
            ):
                events.append(PowerQualityEvent.HARMONIC_DISTORTION)

            # Check power factor
            if (
                pq_reading.power_factor
                and pq_reading.power_factor < self.power_quality_thresholds["power_factor_min"]
            ):
                events.append(PowerQualityEvent.POWER_FACTOR_LOW)

            # Check phase imbalance
            if (
                pq_reading.phase_imbalance
                and pq_reading.phase_imbalance
                > self.power_quality_thresholds["phase_imbalance_max"]
            ):
                events.append(PowerQualityEvent.PHASE_IMBALANCE)

            return events

        except Exception as e:
            self.logger.error(f"Error detecting power quality events: {e}")
            return []

    async def _get_meter_calibration(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get meter calibration data."""
        try:
            # Check cache first
            if station_id in self.meter_calibration:
                return self.meter_calibration[station_id]

            # Get from database
            calibration_data = await self.timescale_client.get_meter_calibration(station_id)

            if calibration_data:
                calibration = json.loads(calibration_data["calibration_data"])
                self.meter_calibration[station_id] = calibration
                return calibration

            return None

        except Exception as e:
            self.logger.error(f"Error getting meter calibration: {e}")
            return None

    async def _validate_calibration_data(self, calibration_data: Dict[str, Any]) -> bool:
        """Validate calibration data."""
        try:
            # Check required fields
            required_fields = ["accuracy", "calibration_factors"]
            for field in required_fields:
                if field not in calibration_data:
                    return False

            # Check accuracy
            accuracy = calibration_data["accuracy"]
            if not 0.01 <= accuracy <= 1.0:  # 0.01% to 1% accuracy
                return False

            # Check calibration factors
            factors = calibration_data["calibration_factors"]
            for measurand, factor in factors.items():
                if not 0.5 <= factor <= 2.0:  # Reasonable calibration range
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating calibration data: {e}")
            return False

    async def _calculate_power_quality_statistics(
        self, readings: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate power quality statistics."""
        try:
            if not readings:
                return {}

            # Extract values
            voltages_l1 = [r["voltage_l1"] for r in readings if r.get("voltage_l1")]
            [r["voltage_l2"] for r in readings if r.get("voltage_l2")]
            [r["voltage_l3"] for r in readings if r.get("voltage_l3")]
            frequencies = [r["frequency"] for r in readings if r.get("frequency")]
            power_factors = [r["power_factor"] for r in readings if r.get("power_factor")]
            thd_voltages = [r["thd_voltage"] for r in readings if r.get("thd_voltage")]
            thd_currents = [r["thd_current"] for r in readings if r.get("thd_current")]
            phase_imbalances = [r["phase_imbalance"] for r in readings if r.get("phase_imbalance")]

            statistics = {}

            if voltages_l1:
                statistics["voltage_l1"] = {
                    "min": min(voltages_l1),
                    "max": max(voltages_l1),
                    "avg": np.mean(voltages_l1),
                    "std": np.std(voltages_l1),
                }

            if frequencies:
                statistics["frequency"] = {
                    "min": min(frequencies),
                    "max": max(frequencies),
                    "avg": np.mean(frequencies),
                    "std": np.std(frequencies),
                }

            if power_factors:
                statistics["power_factor"] = {
                    "min": min(power_factors),
                    "max": max(power_factors),
                    "avg": np.mean(power_factors),
                    "std": np.std(power_factors),
                }

            if thd_voltages:
                statistics["thd_voltage"] = {
                    "min": min(thd_voltages),
                    "max": max(thd_voltages),
                    "avg": np.mean(thd_voltages),
                    "std": np.std(thd_voltages),
                }

            if thd_currents:
                statistics["thd_current"] = {
                    "min": min(thd_currents),
                    "max": max(thd_currents),
                    "avg": np.mean(thd_currents),
                    "std": np.std(thd_currents),
                }

            if phase_imbalances:
                statistics["phase_imbalance"] = {
                    "min": min(phase_imbalances),
                    "max": max(phase_imbalances),
                    "avg": np.mean(phase_imbalances),
                    "std": np.std(phase_imbalances),
                }

            return statistics

        except Exception as e:
            self.logger.error(f"Error calculating power quality statistics: {e}")
            return {}

    async def _identify_power_quality_events(
        self, readings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Identify power quality events from readings."""
        try:
            events = []

            for reading in readings:
                # Check each reading for events
                pq_reading = PowerQualityReading(
                    timestamp=reading["timestamp"],
                    voltage_l1=reading.get("voltage_l1"),
                    voltage_l2=reading.get("voltage_l2"),
                    voltage_l3=reading.get("voltage_l3"),
                    current_l1=reading.get("current_l1"),
                    current_l2=reading.get("current_l2"),
                    current_l3=reading.get("current_l3"),
                    frequency=reading.get("frequency"),
                    power_factor=reading.get("power_factor"),
                    thd_voltage=reading.get("thd_voltage"),
                    thd_current=reading.get("thd_current"),
                    phase_imbalance=reading.get("phase_imbalance"),
                )

                detected_events = await self._detect_power_quality_events(pq_reading)

                if detected_events:
                    events.append(
                        {
                            "timestamp": reading["timestamp"],
                            "events": [event.value for event in detected_events],
                            "values": {
                                "voltage_l1": reading.get("voltage_l1"),
                                "frequency": reading.get("frequency"),
                                "power_factor": reading.get("power_factor"),
                                "thd_voltage": reading.get("thd_voltage"),
                                "phase_imbalance": reading.get("phase_imbalance"),
                            },
                        }
                    )

            return events

        except Exception as e:
            self.logger.error(f"Error identifying power quality events: {e}")
            return []

    async def _check_power_quality_compliance(self, statistics: Dict[str, Any]) -> Dict[str, bool]:
        """Check power quality compliance."""
        try:
            compliance = {}

            # Check voltage compliance
            if "voltage_l1" in statistics:
                voltage_stats = statistics["voltage_l1"]
                compliance["voltage"] = (
                    voltage_stats["min"] >= self.power_quality_thresholds["voltage_min"]
                    and voltage_stats["max"] <= self.power_quality_thresholds["voltage_max"]
                )

            # Check frequency compliance
            if "frequency" in statistics:
                frequency_stats = statistics["frequency"]
                compliance["frequency"] = (
                    frequency_stats["min"] >= self.power_quality_thresholds["frequency_min"]
                    and frequency_stats["max"] <= self.power_quality_thresholds["frequency_max"]
                )

            # Check THD compliance
            if "thd_voltage" in statistics:
                thd_stats = statistics["thd_voltage"]
                compliance["thd_voltage"] = (
                    thd_stats["max"] <= self.power_quality_thresholds["thd_voltage_max"]
                )

            # Check power factor compliance
            if "power_factor" in statistics:
                pf_stats = statistics["power_factor"]
                compliance["power_factor"] = (
                    pf_stats["min"] >= self.power_quality_thresholds["power_factor_min"]
                )

            # Check phase imbalance compliance
            if "phase_imbalance" in statistics:
                imbalance_stats = statistics["phase_imbalance"]
                compliance["phase_imbalance"] = (
                    imbalance_stats["max"] <= self.power_quality_thresholds["phase_imbalance_max"]
                )

            return compliance

        except Exception as e:
            self.logger.error(f"Error checking power quality compliance: {e}")
            return {}
