"""Encoding and decoding utilities for Modbus register payloads."""

from __future__ import annotations

import struct
from typing import Iterable


class EncoderDecoder:
    """Encode and decode values according to firmware-compatible byte ordering."""

    _REGISTER_COUNTS = {
        0: 1,  # int16
        1: 1,  # uint16
        2: 2,  # int32
        3: 2,  # uint32
        4: 2,  # float32
    }

    _VALID_16_ORDERS = {"AB"}
    _VALID_32_ORDERS = {"ABCD", "CDAB", "DCBA", "BADC"}

    @classmethod
    def register_count(cls, data_type: int) -> int:
        """Return the number of 16-bit registers required by a value type."""
        if data_type not in cls._REGISTER_COUNTS:
            raise ValueError(f"Unsupported data_type: {data_type}")
        return cls._REGISTER_COUNTS[data_type]

    @classmethod
    def validate_combination(cls, data_type: int, byte_order: str) -> str:
        """Validate and normalize a (data_type, byte_order) pair."""
        normalized = (byte_order or "").upper()
        if data_type in (0, 1):
            if normalized not in cls._VALID_16_ORDERS:
                raise ValueError(
                    "16-bit types only support byte_order='AB' for deterministic behavior"
                )
            return normalized
        if data_type in (2, 3, 4):
            if normalized not in cls._VALID_32_ORDERS:
                raise ValueError(
                    "32-bit types require byte_order in {'ABCD','CDAB','DCBA','BADC'}"
                )
            return normalized
        raise ValueError(f"Unsupported data_type: {data_type}")

    @classmethod
    def encode(cls, value: float | int, data_type: int, byte_order: str) -> list[int]:
        """Encode a numeric value into Modbus registers."""
        normalized = cls.validate_combination(data_type, byte_order)

        if data_type == 0:
            as_int = int(value)
            if not -32768 <= as_int <= 32767:
                raise ValueError(f"int16 out of range: {value}")
            packed = struct.pack(">h", as_int)
            return [struct.unpack(">H", packed)[0]]

        if data_type == 1:
            as_int = int(value)
            if not 0 <= as_int <= 0xFFFF:
                raise ValueError(f"uint16 out of range: {value}")
            return [as_int]

        packed = cls._pack_32(value, data_type)
        regs = cls._bytes_to_registers_32(packed, normalized)
        return regs

    @classmethod
    def decode(cls, regs: Iterable[int], data_type: int, byte_order: str) -> float | int:
        """Decode Modbus registers into a numeric value."""
        normalized = cls.validate_combination(data_type, byte_order)
        reg_list = [int(r) & 0xFFFF for r in regs]
        expected_regs = cls.register_count(data_type)

        if len(reg_list) != expected_regs:
            raise ValueError(
                f"Expected {expected_regs} register(s) for data_type={data_type}, got {len(reg_list)}"
            )

        if data_type == 0:
            packed = struct.pack(">H", reg_list[0])
            return struct.unpack(">h", packed)[0]

        if data_type == 1:
            return reg_list[0]

        packed = cls._registers_to_bytes_32(reg_list, normalized)

        if data_type == 2:
            return struct.unpack(">i", packed)[0]
        if data_type == 3:
            return struct.unpack(">I", packed)[0]
        if data_type == 4:
            return struct.unpack(">f", packed)[0]

        raise ValueError(f"Unsupported data_type: {data_type}")

    @classmethod
    def _pack_32(cls, value: float | int, data_type: int) -> bytes:
        if data_type == 2:
            as_int = int(value)
            if not -2147483648 <= as_int <= 2147483647:
                raise ValueError(f"int32 out of range: {value}")
            return struct.pack(">i", as_int)
        if data_type == 3:
            as_int = int(value)
            if not 0 <= as_int <= 0xFFFFFFFF:
                raise ValueError(f"uint32 out of range: {value}")
            return struct.pack(">I", as_int)
        if data_type == 4:
            return struct.pack(">f", float(value))
        raise ValueError(f"Unsupported 32-bit data_type: {data_type}")

    @classmethod
    def _bytes_to_registers_32(cls, packed: bytes, byte_order: str) -> list[int]:
        A, B, C, D = packed[0], packed[1], packed[2], packed[3]

        if byte_order == "ABCD":
            ordered = [A, B, C, D]

        elif byte_order == "CDAB":
            ordered = [C, D, A, B]

        elif byte_order == "BADC":
            ordered = [B, A, D, C]

        elif byte_order == "DCBA":
            ordered = [D, C, B, A]

        else:
            raise ValueError(f"Invalid byte_order: {byte_order}")

        return [
            (ordered[0] << 8) | ordered[1],
            (ordered[2] << 8) | ordered[3],
        ]

    @classmethod
    def _registers_to_bytes_32(cls, regs: list[int], byte_order: str) -> bytes:
        A = (regs[0] >> 8) & 0xFF
        B = regs[0] & 0xFF
        C = (regs[1] >> 8) & 0xFF
        D = regs[1] & 0xFF

        if byte_order == "ABCD":
            ordered = [A, B, C, D]

        elif byte_order == "CDAB":
            ordered = [C, D, A, B]

        elif byte_order == "BADC":
            ordered = [B, A, D, C]

        elif byte_order == "DCBA":
            ordered = [D, C, B, A]

        else:
            raise ValueError(f"Invalid byte_order: {byte_order}")

        return bytes(ordered)
