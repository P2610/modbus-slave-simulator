"""RTU serial server bootstrap for the Modbus simulator."""

from __future__ import annotations

import logging
import os

from pymodbus import FramerType, ModbusDeviceIdentification
from pymodbus.server import StartAsyncSerialServer

from .datastore import SimulationState


LOGGER = logging.getLogger(__name__)


def _build_identity(state: SimulationState) -> ModbusDeviceIdentification:
    identity = ModbusDeviceIdentification()
    identity.VendorName = state.metadata.vendor_name
    identity.ProductCode = state.metadata.product_code
    identity.ProductName = state.metadata.device_id
    identity.ModelName = state.metadata.device_id
    identity.MajorMinorRevision = state.metadata.major_minor_revision
    return identity


def _normalize_parity(parity: str) -> str:
    normalized = parity.strip().lower()
    mapping = {
        "none": "N",
        "n": "N",
        "even": "E",
        "e": "E",
        "odd": "O",
        "o": "O",
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported parity value: {parity}")
    return mapping[normalized]


async def run_rtu_server(state: SimulationState) -> None:
    """Run Modbus RTU server forever. If serial is missing, return gracefully."""
    serial_port = state.metadata.serial_port
    if not os.path.exists(serial_port):
        LOGGER.warning(
            "Serial port %s does not exist. RTU server skipped; TCP can continue.",
            serial_port,
        )
        return

    LOGGER.info(
        "Starting Modbus RTU server on %s (baud=%s data_bits=%s stop_bits=%s parity=%s)",
        serial_port,
        state.metadata.baudrate,
        state.metadata.data_bits,
        state.metadata.stop_bits,
        state.metadata.parity,
    )

    try:
        await StartAsyncSerialServer(
            context=state.context,
            framer=FramerType.RTU,
            identity=_build_identity(state),
            port=serial_port,
            baudrate=state.metadata.baudrate,
            bytesize=state.metadata.data_bits,
            stopbits=state.metadata.stop_bits,
            parity=_normalize_parity(state.metadata.parity),
            timeout=1,
            ignore_missing_devices=False,
            broadcast_enable=False,
            trace_pdu=state.trace_pdu,
        )
    except Exception as exc:  # pragma: no cover - depends on serial hardware
        LOGGER.warning("RTU server could not start (%s). Continuing without RTU.", exc)
