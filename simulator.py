# Quickstart 1/10: Install dependencies with ./app/bin/pip install -r requirements.txt
# Quickstart 2/10: Edit simulator_config.json to match your target Modbus scenario.
# Quickstart 3/10: Run TCP + RTU using ./app/bin/python simulator.py --config simulator_config.json.
# Quickstart 4/10: Run TCP only using ./app/bin/python simulator.py --tcp-only.
# Quickstart 5/10: Run RTU only using ./app/bin/python simulator.py --rtu-only.
# Quickstart 6/10: Disable rich monitor for CI with --no-monitor.
# Quickstart 7/10: Override update interval with --refresh 0.5.
# Quickstart 8/10: Persist transaction CSV with --log-file txn.csv.
# Quickstart 9/10: Print full storage memory at startup with --inspect-mem.
# Quickstart 10/10: Use REPL commands set/get/byteorder/dump/quit for runtime control.

from __future__ import annotations

import asyncio
import logging
import shlex
import threading
import time
from pathlib import Path

import click

from core.datastore import SimulationState, build_state_from_config, load_configuration
from core.fc_handlers import register_custom_handlers
from core.monitor import build_monitor
from core.server_rtu import run_rtu_server
from core.server_tcp import run_tcp_server
from core.value_engine import ValueEngine


LOGGER = logging.getLogger("modbus-simulator")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _format_raw_regs(raw: list[int]) -> str:
    if not raw:
        return ""
    return " ".join(f"0x{value:04X}" for value in raw)


def _print_active_storage(state: SimulationState) -> None:
    for storage_addr, unit_id, name, value in state.active_storage_snapshot():
        print(
            f"storage_mem[{storage_addr}] unit={unit_id} "
            f"sensor={name} value={value:.6f}"
        )


def _clear_screen_if_interactive() -> None:
    # ANSI clear screen + cursor home for terminal repaint.
    try:
        print("\033[2J\033[H", end="", flush=True)
    except BrokenPipeError:
        return


def _print_full_storage(state: SimulationState) -> None:
    print("=== storage_mem[0..65535] ===")
    for index, value in state.full_storage_snapshot():
        print(f"{index:05d}: {value:.9f}")
    print("=== end storage_mem ===")


def _update_loop(
    state: SimulationState,
    engine: ValueEngine,
    stop_event: threading.Event,
    monitor,
    interval_s: float,
    no_monitor: bool,
) -> None:
    while not stop_event.is_set():
        start = time.monotonic()
        for sensor in state.sensors:
            try:
                new_value = engine.compute(sensor, now=start)
                state.write_sensor_value(sensor, new_value)
            except Exception as exc:  # pragma: no cover - runtime safety
                LOGGER.warning("Sensor update failed for %s: %s", sensor.name, exc)

        state.refresh_storage_mirror()
        monitor.refresh()

        if no_monitor:
            _clear_screen_if_interactive()
            _print_active_storage(state)

        elapsed = time.monotonic() - start
        wait_for = max(0.0, interval_s - elapsed)
        if stop_event.wait(wait_for):
            break


def _print_repl_help() -> None:
    print("Comandos disponibles:")
    print("  set <unit_id> <name|modicon_addr> <value>")
    print("  get <unit_id> <name|modicon_addr>")
    print("  byteorder <unit_id> <name|modicon_addr> <AB|ABCD|CDAB|DCBA|BADC>")
    print("  dump")
    print("  quit")


def _run_repl(state: SimulationState, engine: ValueEngine, stop_event: threading.Event) -> None:
    _print_repl_help()

    while not stop_event.is_set():
        try:
            line = input("sim> ").strip()
        except EOFError:
            stop_event.set()
            break
        except KeyboardInterrupt:
            stop_event.set()
            break

        if not line:
            continue

        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"Error de parseo: {exc}")
            continue

        command = tokens[0].lower()

        if command == "quit":
            stop_event.set()
            break

        if command == "dump":
            _print_active_storage(state)
            continue

        if command == "set":
            if len(tokens) != 4:
                print("Uso: set <unit_id> <name|modicon_addr> <value>")
                continue
            try:
                unit_id = int(tokens[1])
                selector = tokens[2]
                value = float(tokens[3])
            except ValueError:
                print("Argumentos invalidos")
                continue

            sensor = state.find_sensor(unit_id, selector)
            if not sensor:
                print("Sensor no encontrado")
                continue

            sensor.value_mode = "manual"
            engine.set_manual_value(sensor, value)
            state.write_sensor_value(sensor, value)
            print(
                f"OK set unit={unit_id} sensor={sensor.name} "
                f"value={value:.6f} mode=manual"
            )
            continue

        if command == "get":
            if len(tokens) != 3:
                print("Uso: get <unit_id> <name|modicon_addr>")
                continue
            try:
                unit_id = int(tokens[1])
            except ValueError:
                print("unit_id invalido")
                continue

            selector = tokens[2]
            sensor = state.find_sensor(unit_id, selector)
            if not sensor:
                print("Sensor no encontrado")
                continue

            raw = state.get_sensor_raw_registers(sensor)
            current = state.current_values.get((sensor.unit_id, sensor.modicon_address), sensor.value)
            print(
                f"unit={sensor.unit_id} name={sensor.name} modicon={sensor.modicon_address} "
                f"storage={sensor.storage_address} type={sensor.value_type} "
                f"byte_order={sensor.byte_order} mode={sensor.value_mode} "
                f"raw=[{_format_raw_regs(raw)}] value={float(current):.6f}"
            )
            continue

        if command == "byteorder":
            if len(tokens) != 4:
                print("Uso: byteorder <unit_id> <name|modicon_addr> <AB|ABCD|CDAB|DCBA|BADC>")
                continue
            try:
                unit_id = int(tokens[1])
            except ValueError:
                print("unit_id invalido")
                continue

            selector = tokens[2]
            new_order = tokens[3].upper()
            sensor = state.find_sensor(unit_id, selector)
            if not sensor:
                print("Sensor no encontrado")
                continue

            try:
                state.set_sensor_byte_order(sensor, new_order)
            except Exception as exc:
                print(f"No se pudo actualizar byte_order: {exc}")
                continue

            print(
                f"OK byte_order unit={sensor.unit_id} sensor={sensor.name} "
                f"byte_order={sensor.byte_order}"
            )
            continue

        print("Comando desconocido")
        _print_repl_help()


async def _wait_until_stop(
    stop_event: threading.Event,
    server_tasks: list[asyncio.Task],
) -> None:
    while not stop_event.is_set():
        for task in server_tasks:
            if not task.done():
                continue
            error = task.exception()
            if error:
                LOGGER.error("Server task failed: %s", error, exc_info=error)
                stop_event.set()
                break
        await asyncio.sleep(0.2)


async def _run_async(
    config_path: Path,
    tcp_only: bool,
    rtu_only: bool,
    no_monitor: bool,
    log_file: str | None,
    refresh: float | None,
    inspect_mem: bool,
) -> None:
    config = load_configuration(config_path)
    state = build_state_from_config(config, refresh_override=refresh, log_file_path=log_file)
    register_custom_handlers(state)

    monitor = build_monitor(state, no_monitor=no_monitor)
    monitor.start()

    # When running without the rich monitor, use the alternate screen buffer
    # so repeated updates do not pollute terminal scrollback.
    if no_monitor:
        try:
            print("\033[?1049h", end="", flush=True)
        except BrokenPipeError:
            pass

    if inspect_mem:
        _print_full_storage(state)

    stop_event = threading.Event()
    value_engine = ValueEngine()

    update_thread = threading.Thread(
        target=_update_loop,
        args=(
            state,
            value_engine,
            stop_event,
            monitor,
            state.metadata.update_interval_s,
            no_monitor,
        ),
        daemon=True,
        name="sensor-updater",
    )

    repl_thread = threading.Thread(
        target=_run_repl,
        args=(state, value_engine, stop_event),
        daemon=True,
        name="repl-thread",
    )

    update_thread.start()
    repl_thread.start()

    server_tasks: list[asyncio.Task] = []
    if not rtu_only:
        server_tasks.append(asyncio.create_task(run_tcp_server(state), name="tcp-server"))
    if not tcp_only:
        server_tasks.append(asyncio.create_task(run_rtu_server(state), name="rtu-server"))

    if not server_tasks:
        LOGGER.warning("No Modbus servers selected. Waiting for REPL quit command.")

    try:
        await _wait_until_stop(stop_event, server_tasks)
    finally:
        stop_event.set()
        for task in server_tasks:
            task.cancel()
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)
        update_thread.join(timeout=2.0)
        repl_thread.join(timeout=2.0)
        monitor.stop()
        # If we entered the alternate buffer for --no-monitor, exit it now.
        if no_monitor:
            try:
                print("\033[?1049l", end="", flush=True)
            except BrokenPipeError:
                pass
        state.close()


@click.command()
@click.option(
    "--config",
    "config_path",
    default="simulator_config.json",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--tcp-only", is_flag=True, help="Run only TCP server and ignore serial config")
@click.option("--rtu-only", is_flag=True, help="Run only RTU serial server")
@click.option("--no-monitor", is_flag=True, help="Disable rich monitor and use plain output")
@click.option("--log-file", type=click.Path(dir_okay=False), help="Append transaction log CSV")
@click.option("--refresh", type=float, help="Override simulator.update_interval_s")
@click.option("--inspect-mem", is_flag=True, help="Print complete storage_mem map at startup")
def main(
    config_path: Path,
    tcp_only: bool,
    rtu_only: bool,
    no_monitor: bool,
    log_file: str | None,
    refresh: float | None,
    inspect_mem: bool,
) -> None:
    """Run the Modbus slave simulator."""
    _configure_logging()

    if tcp_only and rtu_only:
        raise click.UsageError("--tcp-only and --rtu-only are mutually exclusive")

    try:
        asyncio.run(
            _run_async(
                config_path=config_path,
                tcp_only=tcp_only,
                rtu_only=rtu_only,
                no_monitor=no_monitor,
                log_file=log_file,
                refresh=refresh,
                inspect_mem=inspect_mem,
            )
        )
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user")


if __name__ == "__main__":
    main()
