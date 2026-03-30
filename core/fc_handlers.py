"""Custom Modbus function handlers for FC08, FC22, FC23, and FC43."""

from __future__ import annotations

import struct
from typing import Any

from pymodbus.constants import ExcCodes
from pymodbus.datastore import ModbusServerContext
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.pdu.decoders import DecodePDU

from .datastore import SimulationState


_STATE: SimulationState | None = None
_IDENTITY_OBJECTS: dict[int, str] = {}
_REGISTERED = False


def _state() -> SimulationState:
    if _STATE is None:
        raise RuntimeError("Custom FC handlers used before registration")
    return _STATE


class DiagnosticsRequestCustom(ModbusPDU):
    """Custom FC08 request implementing required subcodes."""

    function_code = 0x08
    sub_function_code = -1
    rtu_frame_size = 8

    def __init__(
        self,
        sub_function_code: int = 0,
        message: bytes | int | list[int] | tuple[int, ...] | None = 0,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.request_subcode = sub_function_code
        self.message = message

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        """Disable built-in FC08 sub-decoding so custom handler receives all subcodes."""
        _ = data
        return -1

    def encode(self) -> bytes:
        packet = struct.pack(">H", self.request_subcode & 0xFFFF)
        if self.message is None:
            return packet
        if isinstance(self.message, bytes):
            return packet + self.message
        if isinstance(self.message, int):
            return packet + struct.pack(">H", self.message & 0xFFFF)
        if isinstance(self.message, (list, tuple)):
            for item in self.message:
                packet += struct.pack(">H", int(item) & 0xFFFF)
            return packet
        raise TypeError(f"Unsupported diagnostics payload type: {type(self.message)}")

    def decode(self, data: bytes) -> None:
        self.request_subcode = int.from_bytes(data[:2], byteorder="big")
        payload = data[2:]
        if not payload:
            self.message = None
        elif len(payload) == 2:
            self.message = struct.unpack(">H", payload)[0]
        elif len(payload) % 2 == 0:
            count = len(payload) // 2
            self.message = list(struct.unpack(">" + "H" * count, payload))
        else:
            self.message = payload

    async def datastore_update(self, context: ModbusServerContext, device_id: int) -> ModbusPDU:
        _ = context
        state = _state()
        subcode = self.request_subcode

        with state.lock:
            if subcode == 0x0000:
                response_message = self.message
            elif subcode == 0x0001:
                state.counters.listen_only_mode = False
                response_message = self.message if self.message is not None else 0
            elif subcode == 0x0004:
                state.counters.listen_only_mode = True
                response_message = self.message if self.message is not None else 0
            elif subcode == 0x000A:
                state.counters.requests_total = 0
                state.counters.bus_message_count = 0
                state.counters.exceptions = 0
                state.counters.bus_errors = 0
                state.counters.bus_exception_error_count = 0
                response_message = 0
            elif subcode == 0x000B:
                response_message = state.counters.bus_message_count & 0xFFFF
            elif subcode == 0x000E:
                response_message = state.counters.bus_exception_error_count & 0xFFFF
            else:
                return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)

        return DiagnosticsResponseCustom(
            sub_function_code=subcode,
            message=response_message,
            dev_id=device_id,
            transaction_id=self.transaction_id,
        )


class DiagnosticsResponseCustom(ModbusPDU):
    """Custom FC08 response supporting required subcodes."""

    function_code = 0x08
    sub_function_code = -1

    def __init__(
        self,
        sub_function_code: int = 0,
        message: bytes | int | list[int] | tuple[int, ...] | None = 0,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.response_subcode = sub_function_code
        self.message = message

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        _ = data
        return -1

    def encode(self) -> bytes:
        packet = struct.pack(">H", self.response_subcode & 0xFFFF)
        if self.message is None:
            return packet
        if isinstance(self.message, bytes):
            return packet + self.message
        if isinstance(self.message, int):
            return packet + struct.pack(">H", self.message & 0xFFFF)
        if isinstance(self.message, (list, tuple)):
            for item in self.message:
                packet += struct.pack(">H", int(item) & 0xFFFF)
            return packet
        raise TypeError(f"Unsupported diagnostics payload type: {type(self.message)}")

    def decode(self, data: bytes) -> None:
        self.response_subcode = int.from_bytes(data[:2], byteorder="big")
        payload = data[2:]
        if not payload:
            self.message = None
        elif len(payload) == 2:
            self.message = struct.unpack(">H", payload)[0]
        elif len(payload) % 2 == 0:
            count = len(payload) // 2
            self.message = list(struct.unpack(">" + "H" * count, payload))
        else:
            self.message = payload


class MaskWriteRegisterRequestCustom(ModbusPDU):
    """Custom FC22 request with raw bitmask operation."""

    function_code = 0x16
    rtu_frame_size = 10

    def __init__(
        self,
        address: int = 0x0000,
        and_mask: int = 0xFFFF,
        or_mask: int = 0x0000,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id, address=address)
        self.and_mask = and_mask
        self.or_mask = or_mask

    def encode(self) -> bytes:
        return struct.pack(">HHH", self.address, self.and_mask, self.or_mask)

    def decode(self, data: bytes) -> None:
        self.address, self.and_mask, self.or_mask = struct.unpack(">HHH", data[:6])

    async def datastore_update(self, context: ModbusServerContext, device_id: int) -> ModbusPDU:
        if not 0x0000 <= self.and_mask <= 0xFFFF:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)
        if not 0x0000 <= self.or_mask <= 0xFFFF:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)

        current = await context.async_getValues(device_id, self.function_code, self.address, 1)
        if isinstance(current, ExcCodes):
            return ExceptionResponse(self.function_code, current)

        current_value = int(current[0]) & 0xFFFF
        new_value = (current_value & self.and_mask) | (self.or_mask & (~self.and_mask & 0xFFFF))

        rc = await context.async_setValues(device_id, self.function_code, self.address, [new_value])
        if rc:
            return ExceptionResponse(self.function_code, rc)

        return MaskWriteRegisterResponseCustom(
            address=self.address,
            and_mask=self.and_mask,
            or_mask=self.or_mask,
            dev_id=device_id,
            transaction_id=self.transaction_id,
        )


class MaskWriteRegisterResponseCustom(ModbusPDU):
    """Custom FC22 response."""

    function_code = 0x16
    rtu_frame_size = 10

    def __init__(
        self,
        address: int = 0x0000,
        and_mask: int = 0xFFFF,
        or_mask: int = 0x0000,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id, address=address)
        self.and_mask = and_mask
        self.or_mask = or_mask

    def encode(self) -> bytes:
        return struct.pack(">HHH", self.address, self.and_mask, self.or_mask)

    def decode(self, data: bytes) -> None:
        self.address, self.and_mask, self.or_mask = struct.unpack(">HHH", data[:6])


class ReadWriteMultipleRegistersRequestCustom(ModbusPDU):
    """Custom FC23 request enforcing write-then-read order."""

    function_code = 23
    rtu_byte_count_pos = 10

    def __init__(
        self,
        read_address: int = 0x00,
        read_count: int = 1,
        write_address: int = 0x00,
        write_registers: list[int] | None = None,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.read_address = read_address
        self.read_count = read_count
        self.write_address = write_address
        self.write_registers = write_registers or []
        self.write_count = len(self.write_registers)
        self.write_byte_count = self.write_count * 2

    def encode(self) -> bytes:
        packet = struct.pack(
            ">HHHHB",
            self.read_address,
            self.read_count,
            self.write_address,
            self.write_count,
            self.write_byte_count,
        )
        for register in self.write_registers:
            packet += struct.pack(">H", int(register) & 0xFFFF)
        return packet

    def decode(self, data: bytes) -> None:
        (
            self.read_address,
            self.read_count,
            self.write_address,
            self.write_count,
            self.write_byte_count,
        ) = struct.unpack(">HHHHB", data[:9])
        self.write_registers = []
        for index in range(9, 9 + self.write_byte_count, 2):
            self.write_registers.append(struct.unpack(">H", data[index : index + 2])[0])

    async def datastore_update(self, context: ModbusServerContext, device_id: int) -> ModbusPDU:
        if not 1 <= self.read_count <= 125:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)
        if not 1 <= self.write_count <= 121:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)

        # Modbus spec requires write first, then read.
        rc = await context.async_setValues(
            device_id,
            self.function_code,
            self.write_address,
            [int(v) & 0xFFFF for v in self.write_registers],
        )
        if rc:
            return ExceptionResponse(self.function_code, rc)

        read_back = await context.async_getValues(
            device_id,
            self.function_code,
            self.read_address,
            self.read_count,
        )
        if isinstance(read_back, ExcCodes):
            return ExceptionResponse(self.function_code, read_back)

        return ReadWriteMultipleRegistersResponseCustom(
            registers=[int(v) & 0xFFFF for v in read_back],
            dev_id=device_id,
            transaction_id=self.transaction_id,
        )


class ReadWriteMultipleRegistersResponseCustom(ModbusPDU):
    """Custom FC23 response."""

    function_code = 23
    rtu_byte_count_pos = 2

    def __init__(
        self,
        registers: list[int] | None = None,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.registers = registers or []

    def encode(self) -> bytes:
        packet = struct.pack(">B", len(self.registers) * 2)
        for register in self.registers:
            packet += struct.pack(">H", int(register) & 0xFFFF)
        return packet

    def decode(self, data: bytes) -> None:
        byte_count = int(data[0])
        self.registers = []
        for index in range(1, 1 + byte_count, 2):
            self.registers.append(struct.unpack(">H", data[index : index + 2])[0])


class ReadDeviceIdentificationRequestCustom(ModbusPDU):
    """Custom FC43 (MEI 0x0E) request."""

    function_code = 0x2B
    sub_function_code = -1
    rtu_frame_size = 7

    def __init__(
        self,
        read_code: int = 0x01,
        object_id: int = 0x00,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.mei_type = 0x0E
        self.read_code = read_code
        self.object_id = object_id

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        _ = data
        return -1

    def encode(self) -> bytes:
        return struct.pack(">BBB", self.mei_type, self.read_code, self.object_id)

    def decode(self, data: bytes) -> None:
        self.mei_type, self.read_code, self.object_id = struct.unpack(">BBB", data[:3])

    async def datastore_update(self, context: ModbusServerContext, device_id: int) -> ModbusPDU:
        _ = context

        if self.mei_type != 0x0E:
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)
        if self.read_code not in (0x01, 0x04):
            return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)

        identity = dict(_IDENTITY_OBJECTS)

        if self.read_code == 0x04:
            if self.object_id not in identity:
                return ExceptionResponse(self.function_code, ExcCodes.ILLEGAL_VALUE)
            payload = {self.object_id: identity[self.object_id]}
        else:
            object_ids = [oid for oid in sorted(identity) if oid >= self.object_id]
            payload = {oid: identity[oid] for oid in object_ids}

        return ReadDeviceIdentificationResponseCustom(
            read_code=self.read_code,
            information=payload,
            dev_id=device_id,
            transaction_id=self.transaction_id,
        )


class ReadDeviceIdentificationResponseCustom(ModbusPDU):
    """Custom FC43 (MEI 0x0E) response."""

    function_code = 0x2B
    sub_function_code = -1

    def __init__(
        self,
        read_code: int = 0x01,
        information: dict[int, Any] | None = None,
        dev_id: int = 1,
        transaction_id: int = 0,
    ) -> None:
        super().__init__(dev_id=dev_id, transaction_id=transaction_id)
        self.mei_type = 0x0E
        self.read_code = read_code
        self.conformity = 0x01
        self.more_follows = 0x00
        self.next_object_id = 0x00
        self.information: dict[int, Any] = information or {}

    @classmethod
    def decode_sub_function_code(cls, data: bytes) -> int:
        _ = data
        return -1

    def encode(self) -> bytes:
        objects = b""
        for object_id, value in self.information.items():
            raw = str(value).encode("ascii", errors="ignore")
            objects += struct.pack(">BB", int(object_id) & 0xFF, len(raw)) + raw

        packet = struct.pack(
            ">BBBBBB",
            self.mei_type,
            self.read_code,
            self.conformity,
            self.more_follows,
            self.next_object_id,
            len(self.information),
        )
        return packet + objects

    def decode(self, data: bytes) -> None:
        (
            self.mei_type,
            self.read_code,
            self.conformity,
            self.more_follows,
            self.next_object_id,
            count,
        ) = struct.unpack(">BBBBBB", data[:6])
        self.information = {}
        index = 6
        for _ in range(count):
            object_id, size = struct.unpack(">BB", data[index : index + 2])
            index += 2
            value = data[index : index + size]
            index += size
            self.information[object_id] = value.decode("ascii", errors="ignore")


def register_custom_handlers(state: SimulationState) -> None:
    """Register custom FC handlers and bind shared runtime state."""
    global _STATE, _IDENTITY_OBJECTS, _REGISTERED

    _STATE = state
    _IDENTITY_OBJECTS = {
        0x00: state.metadata.vendor_name,
        0x01: state.metadata.product_code,
        0x02: state.metadata.major_minor_revision,
    }

    if _REGISTERED:
        return

    DecodePDU.add_pdu(DiagnosticsRequestCustom, DiagnosticsResponseCustom)
    DecodePDU.add_pdu(MaskWriteRegisterRequestCustom, MaskWriteRegisterResponseCustom)
    DecodePDU.add_pdu(
        ReadWriteMultipleRegistersRequestCustom,
        ReadWriteMultipleRegistersResponseCustom,
    )
    DecodePDU.add_pdu(
        ReadDeviceIdentificationRequestCustom,
        ReadDeviceIdentificationResponseCustom,
    )

    _REGISTERED = True
