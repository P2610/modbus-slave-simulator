"""Datastore, configuration parsing, and shared simulator state."""

from __future__ import annotations

import csv
import json
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymodbus.constants import ExcCodes
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.pdu import ModbusPDU

from .encoder import EncoderDecoder


VALID_VALUE_MODES = {"static", "random", "sine", "ramp", "manual"}


@dataclass
class SimulatorMetadata:
    """Top-level simulator metadata and communication settings."""

    tcp_host: str
    tcp_port: int
    serial_port: str
    baudrate: int
    data_bits: int
    stop_bits: int
    parity: str
    update_interval_s: float
    device_id: str
    vendor_name: str
    product_code: str
    major_minor_revision: str


@dataclass
class SensorDefinition:
    """Normalized sensor definition with computed addressing info."""

    unit_id: int
    name: str
    modicon_address: int
    storage_address: int
    value_type: int
    byte_order: str
    value_mode: str
    value: float
    minimum: float
    maximum: float
    period_s: float
    unit: str
    area: str
    address: int
    register_count: int


@dataclass
class TransactionEvent:
    """Single transaction log event shown in monitor and optional CSV."""

    timestamp: str
    unit_id: int
    fc: str
    address: int
    count: int
    payload_hex: str


@dataclass
class DiagnosticsCounters:
    """Counters used by monitor and FC08 responses."""

    requests_total: int = 0
    exceptions: int = 0
    bus_errors: int = 0
    listen_only_mode: bool = False
    bus_message_count: int = 0
    bus_exception_error_count: int = 0


class TrackingDataBlock(ModbusSequentialDataBlock):
    """Data block that notifies shared state when external writes happen."""

    def __init__(
        self,
        address: int,
        values: list[int] | list[bool],
        state: "SimulationState",
        unit_id: int,
        area: str,
    ) -> None:
        super().__init__(address, values)
        self.state = state
        self.unit_id = unit_id
        self.area = area
        self._suppress_callbacks = 0

    def setValues(self, address, values):  # noqa: N802
        result = super().setValues(address, values)
        if result:
            return result
        if self._suppress_callbacks:
            return result

        if not isinstance(values, list):
            values = [values]
        zero_based = address - self.address
        self.state.on_external_write(self.unit_id, self.area, zero_based, values)
        return result

    def set_internal(self, zero_based_address: int, values: list[int] | list[bool]) -> None | ExcCodes:
        """Write datastore values without triggering external-write callbacks."""
        if not isinstance(values, list):
            values = [values]
        self._suppress_callbacks += 1
        try:
            return super().setValues(zero_based_address + self.address, values)
        finally:
            self._suppress_callbacks -= 1

    def get_internal(self, zero_based_address: int, count: int = 1) -> list[int] | list[bool] | ExcCodes:
        """Read datastore values using zero-based Modbus offsets."""
        return super().getValues(zero_based_address + self.address, count)


class ModiconAwareDeviceContext(ModbusDeviceContext):
    """Device context that accepts both protocol offsets and Modicon HR/IR addresses."""

    @staticmethod
    def _normalize_address(func_code: int, address: int) -> int:
        # Accept classic Modicon notation directly for holding/input register reads/writes.
        if func_code in (3, 6, 16, 22, 23) and 40001 <= address <= 49999:
            return address - 40001
        if func_code == 4 and 30001 <= address <= 39999:
            return address - 30001
        return address

    def getValues(self, func_code, address, count=1):  # noqa: N802
        normalized = self._normalize_address(func_code, int(address))
        return super().getValues(func_code, normalized, count)

    def setValues(self, func_code, address, values):  # noqa: N802
        normalized = self._normalize_address(func_code, int(address))
        return super().setValues(func_code, normalized, values)


class SimulationState:
    """Runtime state shared by updater, monitor, REPL, and Modbus servers."""

    STORAGE_MEM_SIZE = 65536
    STORAGE_MIRROR_BASE = 60000
    STORAGE_MIRROR_LAST = 65535
    STORAGE_MIRROR_FLOAT_SLOTS = (STORAGE_MIRROR_LAST - STORAGE_MIRROR_BASE + 1) // 2

    def __init__(
        self,
        metadata: SimulatorMetadata,
        sensors: list[SensorDefinition],
        log_file_path: str | None = None,
    ) -> None:
        self.metadata = metadata
        self.sensors = sensors
        self.storage_mem: list[float] = [0.0] * self.STORAGE_MEM_SIZE
        self.current_values: dict[tuple[int, int], float] = {}
        self.transactions: deque[TransactionEvent] = deque(maxlen=20)
        self.counters = DiagnosticsCounters()
        self.lock = threading.RLock()

        self.sensors_by_unit: dict[int, list[SensorDefinition]] = defaultdict(list)
        self.sensors_by_unit_area: dict[tuple[int, str], list[SensorDefinition]] = defaultdict(list)
        self._name_lookup: dict[tuple[int, str], SensorDefinition] = {}
        self._addr_lookup: dict[tuple[int, int], SensorDefinition] = {}

        self.blocks: dict[tuple[int, str], TrackingDataBlock] = {}
        self.devices: dict[int, ModbusDeviceContext] = {}

        self._csv_file = None
        self._csv_writer = None
        self._init_csv_logger(log_file_path)

        self._index_sensors()
        self._validate_layout()
        self.context = self._build_context()
        self._initialize_sensor_values()

    def _init_csv_logger(self, log_file_path: str | None) -> None:
        if not log_file_path:
            return
        path = Path(log_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        self._csv_file = path.open("a", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        if new_file:
            self._csv_writer.writerow(["timestamp", "unit_id", "fc", "address", "hex_value"])
            self._csv_file.flush()

    def close(self) -> None:
        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

    def _index_sensors(self) -> None:
        for sensor in self.sensors:
            self.sensors_by_unit[sensor.unit_id].append(sensor)
            self.sensors_by_unit_area[(sensor.unit_id, sensor.area)].append(sensor)
            self._name_lookup[(sensor.unit_id, sensor.name.lower())] = sensor
            self._addr_lookup[(sensor.unit_id, sensor.modicon_address)] = sensor

    def _validate_layout(self) -> None:
        used_addresses: dict[tuple[int, str], set[int]] = defaultdict(set)
        used_storage: set[int] = set()

        for sensor in self.sensors:
            if sensor.storage_address in used_storage:
                raise ValueError(f"Duplicate storage_address detected: {sensor.storage_address}")
            used_storage.add(sensor.storage_address)

            addr_set = used_addresses[(sensor.unit_id, sensor.area)]
            for addr in range(sensor.address, sensor.address + sensor.register_count):
                if addr in addr_set:
                    raise ValueError(
                        f"Address collision unit={sensor.unit_id} area={sensor.area} at {addr}"
                    )
                addr_set.add(addr)

    def _build_context(self) -> ModbusServerContext:
        size_by_unit_area: dict[tuple[int, str], int] = defaultdict(lambda: 1)

        for sensor in self.sensors:
            end = sensor.address + sensor.register_count
            key = (sensor.unit_id, sensor.area)
            size_by_unit_area[key] = max(size_by_unit_area[key], end)

        for unit_id in self.sensors_by_unit:
            hr_size = max(65536, size_by_unit_area[(unit_id, "hr")])
            ir_size = max(1, size_by_unit_area[(unit_id, "ir")])
            co_size = max(1, size_by_unit_area[(unit_id, "co")])
            di_size = max(1, size_by_unit_area[(unit_id, "di")])

            hr_block = TrackingDataBlock(1, [0] * hr_size, self, unit_id, "hr")
            ir_block = TrackingDataBlock(1, [0] * ir_size, self, unit_id, "ir")
            co_block = TrackingDataBlock(1, [False] * co_size, self, unit_id, "co")
            di_block = TrackingDataBlock(1, [False] * di_size, self, unit_id, "di")

            self.blocks[(unit_id, "hr")] = hr_block
            self.blocks[(unit_id, "ir")] = ir_block
            self.blocks[(unit_id, "co")] = co_block
            self.blocks[(unit_id, "di")] = di_block

            self.devices[unit_id] = ModiconAwareDeviceContext(
                di=di_block,
                co=co_block,
                hr=hr_block,
                ir=ir_block,
            )

        return ModbusServerContext(devices=self.devices, single=False)

    def _initialize_sensor_values(self) -> None:
        for sensor in self.sensors:
            self.write_sensor_value(sensor, sensor.value)

    def find_sensor(self, unit_id: int, selector: str) -> SensorDefinition | None:
        """Find sensor by name or Modicon address for a specific unit."""
        selector = selector.strip()
        if not selector:
            return None
        if selector.isdigit():
            addr = int(selector)
            found = self._addr_lookup.get((unit_id, addr))
            if found:
                return found
        return self._name_lookup.get((unit_id, selector.lower()))

    def set_sensor_byte_order(self, sensor: SensorDefinition, byte_order: str) -> None:
        """Change byte_order at runtime and rewrite current value to datastore."""
        normalized = EncoderDecoder.validate_combination(sensor.value_type, byte_order)
        with self.lock:
            sensor.byte_order = normalized
            value = self.current_values.get((sensor.unit_id, sensor.modicon_address), sensor.value)
            self._write_sensor_value_no_lock(sensor, value)

    def write_sensor_value(self, sensor: SensorDefinition, value: float) -> None:
        """Encode and write a sensor value into datastore and storage mirror."""
        with self.lock:
            self._write_sensor_value_no_lock(sensor, value)

    def _write_sensor_value_no_lock(self, sensor: SensorDefinition, value: float) -> None:
        if sensor.area in ("hr", "ir"):
            regs = EncoderDecoder.encode(value, sensor.value_type, sensor.byte_order)
            block = self.blocks[(sensor.unit_id, sensor.area)]
            rc = block.set_internal(sensor.address, regs)
            if rc:
                raise ValueError(f"Datastore write failed with code {rc} for sensor {sensor.name}")
        else:
            bit_value = bool(round(float(value)))
            block = self.blocks[(sensor.unit_id, sensor.area)]
            rc = block.set_internal(sensor.address, [bit_value])
            if rc:
                raise ValueError(f"Datastore write failed with code {rc} for sensor {sensor.name}")

        sensor.value = float(value)
        self.current_values[(sensor.unit_id, sensor.modicon_address)] = float(value)
        self.storage_mem[sensor.storage_address] = float(value)
        self._write_storage_mirror_cell_no_lock(sensor.storage_address)

    def on_external_write(
        self,
        unit_id: int,
        area: str,
        start_address: int,
        values: list[int] | list[bool],
    ) -> None:
        """Handle writes performed by Modbus clients (FC05/06/15/16/22/23)."""
        end_address = start_address + len(values) - 1

        with self.lock:
            for sensor in self.sensors_by_unit_area.get((unit_id, area), []):
                sensor_end = sensor.address + sensor.register_count - 1
                if sensor.address <= end_address and sensor_end >= start_address:
                    self._refresh_sensor_from_datastore_no_lock(sensor)

    def _refresh_sensor_from_datastore_no_lock(self, sensor: SensorDefinition) -> None:
        if sensor.area in ("hr", "ir"):
            block = self.blocks[(sensor.unit_id, sensor.area)]
            raw = block.get_internal(sensor.address, sensor.register_count)
            if isinstance(raw, ExcCodes):
                return
            value = EncoderDecoder.decode(raw, sensor.value_type, sensor.byte_order)
            as_float = float(value)
        else:
            block = self.blocks[(sensor.unit_id, sensor.area)]
            raw = block.get_internal(sensor.address, 1)
            if isinstance(raw, ExcCodes):
                return
            as_float = 1.0 if bool(raw[0]) else 0.0

        sensor.value = as_float
        self.current_values[(sensor.unit_id, sensor.modicon_address)] = as_float
        self.storage_mem[sensor.storage_address] = as_float
        self._write_storage_mirror_cell_no_lock(sensor.storage_address)

    def _write_storage_mirror_cell_no_lock(self, storage_index: int) -> None:
        if not 0 <= storage_index < self.STORAGE_MEM_SIZE:
            return
        if storage_index >= self.STORAGE_MIRROR_FLOAT_SLOTS:
            return

        mirror_addr = self.STORAGE_MIRROR_BASE + (storage_index * 2)
        regs = EncoderDecoder.encode(self.storage_mem[storage_index], 4, "CDAB")

        for unit_id in self.sensors_by_unit:
            hr_block = self.blocks[(unit_id, "hr")]
            hr_block.set_internal(mirror_addr, regs)

    def refresh_storage_mirror(self) -> None:
        """Refresh mirror window for all active sensors."""
        with self.lock:
            seen: set[int] = set()
            for sensor in self.sensors:
                if sensor.storage_address in seen:
                    continue
                seen.add(sensor.storage_address)
                self._write_storage_mirror_cell_no_lock(sensor.storage_address)

    def get_sensor_raw_registers(self, sensor: SensorDefinition) -> list[int]:
        """Return raw datastore representation for one sensor."""
        with self.lock:
            return self._get_sensor_raw_registers_no_lock(sensor)

    def _get_sensor_raw_registers_no_lock(self, sensor: SensorDefinition) -> list[int]:
        block = self.blocks[(sensor.unit_id, sensor.area)]
        raw = block.get_internal(sensor.address, sensor.register_count)
        if isinstance(raw, ExcCodes):
            return []
        if sensor.area in ("co", "di"):
            return [1 if bool(raw[0]) else 0]
        return [int(v) & 0xFFFF for v in raw]

    def sensor_rows_snapshot(self) -> list[dict[str, Any]]:
        """Build monitor-ready row data for all sensors."""
        rows: list[dict[str, Any]] = []
        with self.lock:
            for sensor in self.sensors:
                raw_regs = self._get_sensor_raw_registers_no_lock(sensor)
                current = self.current_values.get((sensor.unit_id, sensor.modicon_address), sensor.value)
                rows.append(
                    {
                        "unit_id": sensor.unit_id,
                        "name": sensor.name,
                        "modicon_address": sensor.modicon_address,
                        "storage_address": sensor.storage_address,
                        "value_type": sensor.value_type,
                        "byte_order": sensor.byte_order,
                        "raw_regs": raw_regs,
                        "value": current,
                        "unit": sensor.unit,
                        "mode": sensor.value_mode,
                    }
                )
        return rows

    def active_storage_snapshot(self) -> list[tuple[int, int, str, float]]:
        """Return active sensor storage values as tuples."""
        rows: list[tuple[int, int, str, float]] = []
        with self.lock:
            for sensor in self.sensors:
                value = self.current_values.get((sensor.unit_id, sensor.modicon_address), sensor.value)
                rows.append((sensor.storage_address, sensor.unit_id, sensor.name, float(value)))
        return rows

    def full_storage_snapshot(self) -> list[tuple[int, float]]:
        """Return complete storage_mem map."""
        with self.lock:
            return [(idx, value) for idx, value in enumerate(self.storage_mem)]

    def get_recent_transactions(self) -> list[TransactionEvent]:
        with self.lock:
            return list(self.transactions)

    def counters_snapshot(self) -> DiagnosticsCounters:
        with self.lock:
            return DiagnosticsCounters(
                requests_total=self.counters.requests_total,
                exceptions=self.counters.exceptions,
                bus_errors=self.counters.bus_errors,
                listen_only_mode=self.counters.listen_only_mode,
                bus_message_count=self.counters.bus_message_count,
                bus_exception_error_count=self.counters.bus_exception_error_count,
            )

    def trace_pdu(self, sending: bool, pdu: ModbusPDU) -> ModbusPDU:
        """Trace callback wired into pymodbus servers for logging and counters."""
        with self.lock:
            if not sending:
                self.counters.requests_total += 1
                self.counters.bus_message_count = self.counters.requests_total

                unit_id = int(getattr(pdu, "dev_id", 0))
                address = self._extract_address(pdu)
                count = self._extract_count(pdu)
                payload_hex = self._payload_hex_from_pdu(pdu)
                fc = self._fc_label(pdu)
                self._append_transaction(unit_id, fc, address, count, payload_hex)
            elif pdu.isError():
                self.counters.exceptions += 1
                self.counters.bus_errors += 1
                self.counters.bus_exception_error_count = self.counters.exceptions
        return pdu

    def _extract_address(self, pdu: ModbusPDU) -> int:
        if hasattr(pdu, "address"):
            return int(getattr(pdu, "address", 0))
        if hasattr(pdu, "read_address"):
            return int(getattr(pdu, "read_address", 0))
        return 0

    def _extract_count(self, pdu: ModbusPDU) -> int:
        if hasattr(pdu, "count") and int(getattr(pdu, "count", 0)) > 0:
            return int(getattr(pdu, "count", 0))
        if hasattr(pdu, "read_count"):
            return int(getattr(pdu, "read_count", 0))
        if hasattr(pdu, "registers"):
            return len(getattr(pdu, "registers", []))
        if hasattr(pdu, "write_registers"):
            return len(getattr(pdu, "write_registers", []))
        if hasattr(pdu, "bits"):
            return len(getattr(pdu, "bits", []))
        return 1

    def _payload_hex_from_pdu(self, pdu: ModbusPDU) -> str:
        if hasattr(pdu, "write_registers"):
            regs = [int(v) & 0xFFFF for v in getattr(pdu, "write_registers", [])]
            return " ".join(f"{v:04X}" for v in regs)
        if hasattr(pdu, "registers") and getattr(pdu, "registers", None):
            regs = [int(v) & 0xFFFF for v in getattr(pdu, "registers", [])]
            return " ".join(f"{v:04X}" for v in regs)
        if hasattr(pdu, "bits") and getattr(pdu, "bits", None):
            bits = [1 if bool(v) else 0 for v in getattr(pdu, "bits", [])]
            return " ".join(f"{v:02X}" for v in bits)
        if hasattr(pdu, "message") and getattr(pdu, "message", None) is not None:
            message = getattr(pdu, "message")
            if isinstance(message, bytes):
                return message.hex().upper()
            if isinstance(message, int):
                return f"{message & 0xFFFF:04X}"
            if isinstance(message, (list, tuple)):
                return " ".join(f"{int(v) & 0xFFFF:04X}" for v in message)
        if hasattr(pdu, "and_mask") and hasattr(pdu, "or_mask"):
            and_mask = int(getattr(pdu, "and_mask", 0)) & 0xFFFF
            or_mask = int(getattr(pdu, "or_mask", 0)) & 0xFFFF
            return f"AND={and_mask:04X} OR={or_mask:04X}"
        return ""

    def _fc_label(self, pdu: ModbusPDU) -> str:
        fc = int(getattr(pdu, "function_code", 0)) & 0xFF
        sub = int(getattr(pdu, "sub_function_code", -1))
        if sub >= 0:
            return f"{fc:02X}:{sub:04X}"
        return f"{fc:02X}"

    def _append_transaction(
        self,
        unit_id: int,
        fc: str,
        address: int,
        count: int,
        payload_hex: str,
    ) -> None:
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        event = TransactionEvent(
            timestamp=timestamp,
            unit_id=unit_id,
            fc=fc,
            address=address,
            count=count,
            payload_hex=payload_hex,
        )
        self.transactions.append(event)

        if self._csv_writer:
            self._csv_writer.writerow([timestamp, unit_id, fc, address, payload_hex])
            self._csv_file.flush()


def modicon_to_area_address(modicon_address: int) -> tuple[str, int]:
    """Convert Modicon notation address to datastore area and zero-based offset."""
    if 40001 <= modicon_address <= 49999:
        return "hr", modicon_address - 40001
    if 30001 <= modicon_address <= 39999:
        return "ir", modicon_address - 30001
    if 10001 <= modicon_address <= 19999:
        return "di", modicon_address - 10001
    if 1 <= modicon_address <= 9999:
        return "co", modicon_address - 1
    raise ValueError(f"Unsupported Modicon address: {modicon_address}")


def _parse_metadata(raw_config: dict[str, Any]) -> SimulatorMetadata:
    simulator = raw_config.get("simulator", {})
    return SimulatorMetadata(
        tcp_host=str(simulator.get("tcp_host", "0.0.0.0")),
        tcp_port=int(simulator.get("tcp_port", 502)),
        serial_port=str(simulator.get("serial_port", "/dev/ttyUSB0")),
        baudrate=int(simulator.get("baudrate", 9600)),
        data_bits=int(simulator.get("data_bits", 8)),
        stop_bits=int(simulator.get("stop_bits", 1)),
        parity=str(simulator.get("parity", "none")),
        update_interval_s=float(simulator.get("update_interval_s", 1.0)),
        device_id=str(simulator.get("device_id", "Modbus Slave Simulator")),
        vendor_name=str(simulator.get("vendor_name", "SimLab")),
        product_code=str(simulator.get("product_code", "SIM-001")),
        major_minor_revision=str(simulator.get("major_minor_revision", "1.0")),
    )


def _parse_sensors(raw_config: dict[str, Any]) -> list[SensorDefinition]:
    slaves = raw_config.get("slaves", [])
    if not isinstance(slaves, list):
        raise ValueError("'slaves' must be a list")

    sensors: list[SensorDefinition] = []
    for slave in slaves:
        unit_id = int(slave["unit_id"])
        registers = slave.get("registers", [])
        if not isinstance(registers, list):
            raise ValueError(f"'registers' must be a list for unit_id={unit_id}")

        for reg in registers:
            modicon_address = int(reg["modicon_address"])
            area, address = modicon_to_area_address(modicon_address)
            storage_address = int(reg["storage_address"])
            if not 0 <= storage_address < SimulationState.STORAGE_MEM_SIZE:
                raise ValueError(
                    f"storage_address out of range [0..65535]: {storage_address}"
                )

            value_type = int(reg["value_type"])
            byte_order = EncoderDecoder.validate_combination(
                value_type,
                str(reg.get("byte_order", "AB")),
            )
            register_count = EncoderDecoder.register_count(value_type)

            if area in ("co", "di") and register_count != 1:
                raise ValueError(
                    f"Area {area} only supports single-point values (value_type 0/1), got {value_type}"
                )

            value_mode = str(reg.get("value_mode", "static")).lower()
            if value_mode not in VALID_VALUE_MODES:
                raise ValueError(
                    f"Invalid value_mode '{value_mode}'. Valid: {sorted(VALID_VALUE_MODES)}"
                )

            initial = float(reg.get("value", 0.0))
            minimum = float(reg.get("min", initial))
            maximum = float(reg.get("max", initial))
            period_s = float(reg.get("period_s", 1.0))

            sensors.append(
                SensorDefinition(
                    unit_id=unit_id,
                    name=str(reg.get("name", f"Sensor_{unit_id}_{modicon_address}")),
                    modicon_address=modicon_address,
                    storage_address=storage_address,
                    value_type=value_type,
                    byte_order=byte_order,
                    value_mode=value_mode,
                    value=initial,
                    minimum=minimum,
                    maximum=maximum,
                    period_s=period_s,
                    unit=str(reg.get("unit", "")),
                    area=area,
                    address=address,
                    register_count=register_count,
                )
            )
    if not sensors:
        raise ValueError("No sensors found in configuration")
    return sensors


def load_configuration(config_path: str | Path) -> dict[str, Any]:
    """Load JSON configuration from disk."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def build_state_from_config(
    config: dict[str, Any],
    refresh_override: float | None = None,
    log_file_path: str | None = None,
) -> SimulationState:
    """Build fully initialized simulation state from parsed JSON config."""
    metadata = _parse_metadata(config)
    if refresh_override is not None:
        metadata.update_interval_s = float(refresh_override)
    sensors = _parse_sensors(config)
    return SimulationState(metadata=metadata, sensors=sensors, log_file_path=log_file_path)
