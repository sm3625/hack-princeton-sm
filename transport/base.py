"""Simulated LoRa uplink (SPEC 3.4): one-way, lossy, delayed, size-capped, no retries."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import random
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from common.contracts import MAX_PACKET_BYTES

DEFAULT_LOSS = 0.05
DEFAULT_LATENCY_MS = (100.0, 2000.0)

# schedule(delay_s, fn): run fn after delay_s seconds, somewhere.
Scheduler = Callable[[float, Callable[[], None]], None]


class PacketTooLarge(ValueError):
    """Payload exceeds what the radio can carry; it is never transmitted."""


@dataclass
class LinkStats:
    sent: int = 0  # put on air (will be delivered after the latency)
    dropped: int = 0  # lost by the simulated channel
    rejected: int = 0  # refused for size


class LoRaLink(ABC):
    """Node-side radio. send() never retries: a dropped packet is gone (seq gap at the station)."""

    def __init__(
        self,
        *,
        loss: float = DEFAULT_LOSS,
        latency_ms: tuple[float, float] = DEFAULT_LATENCY_MS,
        max_bytes: int = MAX_PACKET_BYTES,
        rng: random.Random | None = None,
        seed: int | None = None,
    ) -> None:
        if not 0.0 <= loss <= 1.0:
            raise ValueError("loss must be between 0 and 1")
        lo, hi = latency_ms
        if not 0 <= lo <= hi:
            raise ValueError("latency_ms must be (min, max) with 0 <= min <= max")
        self.loss = loss
        self.latency_ms = (float(lo), float(hi))
        self.max_bytes = max_bytes
        self.stats = LinkStats()
        self._rng = rng or random.Random(seed)
        self._lock = threading.Lock()

    def send(self, payload: bytes) -> bool:
        """Transmit once. Returns False if the channel dropped it; raises PacketTooLarge."""
        if len(payload) > self.max_bytes:
            with self._lock:
                self.stats.rejected += 1
            raise PacketTooLarge(f"{len(payload)} bytes > {self.max_bytes} byte limit")
        with self._lock:
            if self._rng.random() < self.loss:
                self.stats.dropped += 1
                return False
            delay_s = self._rng.uniform(*self.latency_ms) / 1000.0
            self.stats.sent += 1
        self._transmit(bytes(payload), delay_s)
        return True

    @abstractmethod
    def _transmit(self, payload: bytes, delay_s: float) -> None:
        """Deliver payload to the station after delay_s."""

    def close(self) -> None:
        """Release resources (optional)."""


# ---------- schedulers for when the delayed delivery runs ----------


def loop_scheduler(loop: asyncio.AbstractEventLoop) -> Scheduler:
    """Run fn on `loop` after the delay. Safe to call from any thread."""

    def schedule(delay_s: float, fn: Callable[[], None]) -> None:
        loop.call_soon_threadsafe(loop.call_later, delay_s, fn)

    return schedule


def thread_scheduler(delay_s: float, fn: Callable[[], None]) -> None:
    """Run fn on a daemon timer thread after the delay."""
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()


def immediate_scheduler(delay_s: float, fn: Callable[[], None]) -> None:
    """Ignore latency and run fn now (fast replays)."""
    fn()


class ManualScheduler:
    """Deterministic scheduler driven by the caller's clock (tests, or a sim running at N x)."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self._queue: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()

    def __call__(self, delay_s: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._queue, (self.now + delay_s, next(self._order), fn))

    @property
    def pending(self) -> int:
        return len(self._queue)

    def advance(self, seconds: float) -> int:
        """Move time forward, running everything due in time order. Returns how many ran."""
        target = self.now + seconds
        ran = 0
        while self._queue and self._queue[0][0] <= target:
            due, _, fn = heapq.heappop(self._queue)
            self.now = due
            fn()
            ran += 1
        self.now = target
        return ran
