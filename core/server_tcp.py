"""TCP server bootstrap for the Modbus simulator."""

from __future__ import annotations

import logging

from pymodbus import FramerType, ModbusDeviceIdentification
from pymodbus.server import StartAsyncTcpServer

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


async def run_tcp_server(state: SimulationState) -> None:
    """Run Modbus TCP server forever."""
    address = (state.metadata.tcp_host, state.metadata.tcp_port)
    LOGGER.info("Starting Modbus TCP server on %s:%s", *address)

    await StartAsyncTcpServer(
        context=state.context,
        address=address,
        framer=FramerType.SOCKET,
        identity=_build_identity(state),
        ignore_missing_devices=False,
        broadcast_enable=False,
        trace_pdu=state.trace_pdu,
    )
