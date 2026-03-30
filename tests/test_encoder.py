"""Round-trip and known-value tests for EncoderDecoder."""

from __future__ import annotations

import math

import pytest

from core.encoder import EncoderDecoder


VALID_COMBINATIONS = [
    (0, "AB"),
    (1, "AB"),
    (2, "ABCD"),
    (2, "CDAB"),
    (2, "DCBA"),
    (2, "BADC"),
    (3, "ABCD"),
    (3, "CDAB"),
    (3, "DCBA"),
    (3, "BADC"),
    (4, "ABCD"),
    (4, "CDAB"),
    (4, "DCBA"),
    (4, "BADC"),
]


KNOWN_CASES = [
    # int16 / uint16
    (0, "AB", -12345, [0xCFC7]),
    (1, "AB", 50000, [0xC350]),
    # int32
    (2, "ABCD", 0x11223344, [0x1122, 0x3344]),
    (2, "CDAB", 0x11223344, [0x3344, 0x1122]),
    (2, "DCBA", 0x11223344, [0x4433, 0x2211]),
    (2, "BADC", 0x11223344, [0x2211, 0x4433]),
    # uint32
    (3, "ABCD", 0x89ABCDEF, [0x89AB, 0xCDEF]),
    (3, "CDAB", 0x89ABCDEF, [0xCDEF, 0x89AB]),
    (3, "DCBA", 0x89ABCDEF, [0xEFCD, 0xAB89]),
    (3, "BADC", 0x89ABCDEF, [0xAB89, 0xEFCD]),
    # float32: IEEE-754 value 25.0 -> 41 C8 00 00
    (4, "ABCD", 25.0, [0x41C8, 0x0000]),
    (4, "CDAB", 25.0, [0x0000, 0x41C8]),
    (4, "DCBA", 25.0, [0x0000, 0xC841]),
    (4, "BADC", 25.0, [0xC841, 0x0000]),
]


BOUNDARIES = {
    0: (-32768, 32767),
    1: (0, 0xFFFF),
    2: (-2147483648, 2147483647),
    3: (0, 0xFFFFFFFF),
    4: (-3.4028235e38, 3.4028235e38),
}


@pytest.mark.parametrize("value_type,byte_order", VALID_COMBINATIONS)
def test_round_trip_registers(value_type: int, byte_order: str) -> None:
    """For every valid combination, encode(decode(regs)) must preserve raw regs."""
    raw_regs = [0x1234] if EncoderDecoder.register_count(value_type) == 1 else [0x1234, 0x5678]
    decoded = EncoderDecoder.decode(raw_regs, value_type, byte_order)
    reencoded = EncoderDecoder.encode(decoded, value_type, byte_order)
    assert reencoded == raw_regs


@pytest.mark.parametrize("value_type,byte_order,value,expected_regs", KNOWN_CASES)
def test_known_value(
    value_type: int,
    byte_order: str,
    value: int | float,
    expected_regs: list[int],
) -> None:
    """Known deterministic encodings for each valid combination."""
    assert EncoderDecoder.encode(value, value_type, byte_order) == expected_regs


@pytest.mark.parametrize("value_type,byte_order", VALID_COMBINATIONS)
def test_boundary(value_type: int, byte_order: str) -> None:
    """Boundary values for each valid combination must encode without exceptions."""
    minimum, maximum = BOUNDARIES[value_type]
    regs_min = EncoderDecoder.encode(minimum, value_type, byte_order)
    regs_max = EncoderDecoder.encode(maximum, value_type, byte_order)
    assert len(regs_min) == EncoderDecoder.register_count(value_type)
    assert len(regs_max) == EncoderDecoder.register_count(value_type)


@pytest.mark.parametrize("value_type,byte_order,value,expected_regs", KNOWN_CASES)
def test_round_trip_value(
    value_type: int,
    byte_order: str,
    value: int | float,
    expected_regs: list[int],
) -> None:
    """decode(encode(value)) must return the original value in each valid pair."""
    encoded = EncoderDecoder.encode(value, value_type, byte_order)
    assert encoded == expected_regs
    decoded = EncoderDecoder.decode(encoded, value_type, byte_order)

    if value_type == 4:
        assert math.isclose(float(decoded), float(value), rel_tol=1e-6, abs_tol=1e-6)
    else:
        assert int(decoded) == int(value)
