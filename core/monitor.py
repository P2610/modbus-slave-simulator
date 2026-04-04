"""Runtime monitor rendering for sensors, transactions, and counters."""

from __future__ import annotations

import threading
from typing import Protocol

from .datastore import SimulationState

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
except Exception:  # pragma: no cover - fallback path if rich is unavailable
    Console = None  # type: ignore[assignment]
    Group = None  # type: ignore[assignment]
    Live = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]


class Monitor(Protocol):
    def start(self) -> None: ...

    def refresh(self) -> None: ...

    def stop(self) -> None: ...


class NullMonitor:
    """No-op monitor used for --no-monitor mode."""

    def start(self) -> None:
        return

    def refresh(self) -> None:
        return

    def stop(self) -> None:
        return


class PlainMonitor:
    """Plain text monitor used when rich is not available."""

    def __init__(self, state: SimulationState) -> None:
        self.state = state
        self._lock = threading.Lock()

    def start(self) -> None:
        # Enter alternate screen buffer to avoid polluting terminal scrollback.
        try:
            print("\033[?1049h", end="", flush=True)
        except BrokenPipeError:
            pass
        print("Monitor iniciado en modo plano")

    @staticmethod
    def _clear_screen() -> None:
        # ANSI clear screen + cursor home for a true repaint effect.
        try:
            print("\033[2J\033[H", end="", flush=True)
        except BrokenPipeError:
            return

    def refresh(self) -> None:
        with self._lock:
            self._clear_screen()
            counters = self.state.counters_snapshot()
            print(
                "[monitor] "
                f"requests={counters.requests_total} "
                f"exceptions={counters.exceptions} "
                f"bus_errors={counters.bus_errors} "
                f"listen_only={counters.listen_only_mode}"
            )
            for storage, unit_id, name, value in self.state.active_storage_snapshot():
                print(
                    f"[storage] unit={unit_id} sensor={name} "
                    f"storage[{storage}]={value:.6f}"
                )

    def stop(self) -> None:
        # Exit alternate screen buffer and show stop message in main buffer.
        try:
            print("\033[?1049l", end="", flush=True)
        except BrokenPipeError:
            pass
        print("Monitor detenido")


class RichMonitor:
    """Rich dashboard monitor with 3 sections required by the project spec."""

    def __init__(self, state: SimulationState) -> None:
        if Console is None or Live is None:
            raise RuntimeError("rich is not available")
        self.state = state
        self.console = Console()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._render_thread: threading.Thread | None = None
        self._render_interval_s = max(0.1, float(self.state.metadata.update_interval_s))

    def start(self) -> None:
        # Enter alternate screen buffer to avoid polluting terminal scrollback.
        with self._lifecycle_lock:
            if self._render_thread is not None and self._render_thread.is_alive():
                return
            self._stop_event.clear()
        try:
            print("\033[?1049h", end="", flush=True)
        except BrokenPipeError:
            pass
        with self._lifecycle_lock:
            self._render_thread = threading.Thread(
                target=self._run_live_loop,
                daemon=True,
                name="rich-monitor",
            )
            self._render_thread.start()

    def refresh(self) -> None:
        # Rich monitor updates in its own loop using state.metadata.update_interval_s.
        return

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            render_thread = self._render_thread

        if render_thread is not None:
            render_thread.join(timeout=max(2.0, self._render_interval_s * 2))

        with self._lifecycle_lock:
            self._render_thread = None

        # Exit alternate screen buffer and show stop message in main buffer.
        try:
            print("\033[?1049l", end="", flush=True)
        except BrokenPipeError:
            pass
        self.console.print("[bold yellow]Monitor detenido[/bold yellow]")

    def _run_live_loop(self) -> None:
        refresh_per_second = max(1.0, 1.0 / self._render_interval_s)
        try:
            with Live(
                self._build_dashboard(),
                console=self.console,
                refresh_per_second=refresh_per_second,
                transient=False,
            ) as live:
                while not self._stop_event.wait(self._render_interval_s):
                    live.update(self._build_dashboard(), refresh=True)
        except BrokenPipeError:
            return
        except Exception:
            # Keep simulator runtime alive even if monitor rendering fails.
            return

    def _build_dashboard(self):
        sensors_panel = Panel(self._build_sensor_table(), title="Panel 1 - Sensores", border_style="cyan")
        tx_panel = Panel(self._build_tx_table(), title="Panel 2 - Transacciones", border_style="magenta")
        counters_panel = Panel(
            self._build_counter_table(),
            title="Panel 3 - Contadores",
            border_style="green",
        )
        return Group(sensors_panel, tx_panel, counters_panel)

    def _build_sensor_table(self) -> Table:
        rows = self.state.sensor_rows_snapshot()
        table = Table(show_header=True, header_style="bold")
        table.add_column("unit_id")
        table.add_column("name")
        table.add_column("modicon_addr")
        table.add_column("storage_addr")
        table.add_column("value_type")
        table.add_column("byte_order")
        table.add_column("raw_regs (hex)")
        table.add_column("valor actual")
        table.add_column("unit")
        table.add_column("mode")

        for row in rows:
            raw_hex = " ".join(f"0x{value:04X}" for value in row["raw_regs"])
            table.add_row(
                str(row["unit_id"]),
                str(row["name"]),
                str(row["modicon_address"]),
                str(row["storage_address"]),
                str(row["value_type"]),
                str(row["byte_order"]),
                raw_hex,
                f"{float(row['value']):.6f}",
                str(row["unit"]),
                str(row["mode"]),
            )
        return table

    def _build_tx_table(self) -> Table:
        table = Table(show_header=True, header_style="bold")
        table.add_column("timestamp")
        table.add_column("unit_id")
        table.add_column("FC")
        table.add_column("direccion")
        table.add_column("count")
        table.add_column("hex payload")

        for event in self.state.get_recent_transactions():
            table.add_row(
                event.timestamp,
                str(event.unit_id),
                event.fc,
                str(event.address),
                str(event.count),
                event.payload_hex,
            )
        return table

    def _build_counter_table(self) -> Table:
        counters = self.state.counters_snapshot()
        table = Table(show_header=True, header_style="bold")
        table.add_column("metric")
        table.add_column("value")

        table.add_row("requests_total", str(counters.requests_total))
        table.add_row("exceptions", str(counters.exceptions))
        table.add_row("bus_errors", str(counters.bus_errors))
        table.add_row("listen_only_mode", str(counters.listen_only_mode))
        return table


def build_monitor(state: SimulationState, no_monitor: bool) -> Monitor:
    """Create monitor implementation based on runtime mode and installed deps."""
    if no_monitor:
        return NullMonitor()
    if Console is None or Live is None:
        return PlainMonitor(state)
    return RichMonitor(state)
