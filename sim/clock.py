"""Simulation clock: sim time runs at 1-60x wall time and can be paused.

Sim time is unix seconds. With no scenario loaded the clock follows real time at 1x,
so a live microphone node gets sensible timestamps.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

MIN_SPEED = 1.0
MAX_SPEED = 60.0
_POLL_S = 0.25  # max wall seconds between re-checks while waiting (pause/speed changes)


def clamp_speed(speed: float) -> float:
    return max(MIN_SPEED, min(MAX_SPEED, float(speed)))


class SimClock:
    def __init__(
        self,
        start: float | None = None,
        speed: float = 1.0,
        paused: bool = False,
        wall: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wall = wall
        self._lock = threading.Lock()
        self._base_sim = time.time() if start is None else float(start)
        self._base_wall = wall()
        self._speed = clamp_speed(speed)
        self._paused = paused
        self._listeners: list[Callable[[], None]] = []

    # ----- reading -----

    def now_f(self) -> float:
        with self._lock:
            return self._now_locked()

    def now(self) -> int:
        return int(self.now_f())

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def paused(self) -> bool:
        return self._paused

    def _now_locked(self) -> float:
        if self._paused:
            return self._base_sim
        return self._base_sim + (self._wall() - self._base_wall) * self._speed

    def _rebase_locked(self) -> None:
        self._base_sim = self._now_locked()
        self._base_wall = self._wall()

    # ----- control -----

    def set_time(self, sim_time: float) -> None:
        with self._lock:
            self._base_sim = float(sim_time)
            self._base_wall = self._wall()
        self._notify()

    def set_speed(self, speed: float) -> float:
        with self._lock:
            self._rebase_locked()
            self._speed = clamp_speed(speed)
        self._notify()
        return self._speed

    def pause(self) -> None:
        self.set_paused(True)

    def resume(self) -> None:
        self.set_paused(False)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if paused == self._paused:
                return
            self._rebase_locked()
            self._paused = paused
        self._notify()

    def advance(self, seconds: float) -> None:
        """Jump forward in sim time (also works while paused)."""
        with self._lock:
            self._rebase_locked()
            self._base_sim += max(0.0, float(seconds))
        self._notify()

    # ----- waiting -----

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            fn()

    def wall_seconds_until(self, sim_time: float) -> float | None:
        """Wall seconds until sim_time at the current speed; None while paused."""
        with self._lock:
            remaining = sim_time - self._now_locked()
            if remaining <= 0:
                return 0.0
            if self._paused:
                return None
            return remaining / self._speed

    async def sleep_until(self, sim_time: float) -> None:
        """Wait until the clock reaches sim_time, following pause and speed changes."""
        while True:
            wait = self.wall_seconds_until(sim_time)
            if wait == 0.0:
                return
            await asyncio.sleep(_POLL_S if wait is None else min(wait, _POLL_S))

    def block_until(self, sim_time: float, cancelled: Callable[[], bool] = lambda: False) -> None:
        """Thread version of sleep_until (for sync callers such as agent tools)."""
        while not cancelled():
            wait = self.wall_seconds_until(sim_time)
            if wait == 0.0:
                return
            time.sleep(_POLL_S if wait is None else min(wait, _POLL_S, 0.05))
