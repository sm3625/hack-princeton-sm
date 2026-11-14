"""In-process LoRa link for the simulator: packets go straight to a callback (the receiver)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from transport.base import LoRaLink, Scheduler, loop_scheduler, thread_scheduler


class InProcessLink(LoRaLink):
    """deliver(payload) is called after the simulated latency.

    Scheduler default: if constructed while an asyncio loop is running (e.g. inside a FastAPI
    route or lifespan), delivery runs on that loop, which is where the station receiver expects
    to be called. Otherwise a timer thread. Pass ManualScheduler to drive latency from sim time.
    """

    def __init__(
        self,
        deliver: Callable[[bytes], object],
        *,
        schedule: Scheduler | None = None,
        **link_options,
    ) -> None:
        super().__init__(**link_options)
        self._deliver = deliver
        if schedule is None:
            try:
                schedule = loop_scheduler(asyncio.get_running_loop())
            except RuntimeError:
                schedule = thread_scheduler
        self._schedule = schedule

    def _transmit(self, payload: bytes, delay_s: float) -> None:
        self._schedule(delay_s, lambda: self._deliver(payload))
