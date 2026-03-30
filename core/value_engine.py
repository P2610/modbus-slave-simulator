"""Dynamic value generation engine for simulator sensors."""

from __future__ import annotations

import math
import random
import time
from typing import Any


class ValueEngine:
    """Compute sensor values for static and dynamic modes."""

    def __init__(self, start_time: float | None = None) -> None:
        self.start_time = start_time if start_time is not None else time.monotonic()
        self._manual_values: dict[tuple[int, int], float] = {}

    @staticmethod
    def _sensor_key(sensor: Any) -> tuple[int, int]:
        return (int(sensor.unit_id), int(sensor.modicon_address))

    def set_manual_value(self, sensor: Any, value: float) -> None:
        """Set manual mode value for a sensor."""
        key = self._sensor_key(sensor)
        self._manual_values[key] = float(value)

    def compute(self, sensor: Any, now: float | None = None) -> float:
        """Return the current value for a sensor according to its mode."""
        current_time = now if now is not None else time.monotonic()
        elapsed = max(0.0, current_time - self.start_time)

        mode = (sensor.value_mode or "static").lower()
        initial = float(sensor.value)
        minimum = float(sensor.minimum)
        maximum = float(sensor.maximum)
        period = float(sensor.period_s)

        if mode == "static":
            return initial

        if mode == "manual":
            return self._manual_values.get(self._sensor_key(sensor), initial)

        if mode == "random":
            low = min(minimum, maximum)
            high = max(minimum, maximum)
            return random.uniform(low, high)

        if mode == "sine":
            if period <= 0.0:
                return (minimum + maximum) / 2.0
            value_mid = (maximum + minimum) / 2.0
            amplitude = (maximum - minimum) / 2.0
            return value_mid + amplitude * math.sin((2.0 * math.pi * elapsed) / period)

        if mode == "ramp":
            if period <= 0.0:
                return maximum
            low = min(minimum, maximum)
            high = max(minimum, maximum)
            span = high - low
            if span == 0.0:
                return low
            progress = (elapsed % period) / period
            return low + span * progress

        raise ValueError(f"Unsupported value_mode: {sensor.value_mode}")
