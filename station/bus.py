"""In-process message bus. Any track publishes UI messages (see common/contracts.py);
the WebSocket route forwards them to browsers. Subscribers get every message."""

from __future__ import annotations

import asyncio
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """The loop that owns the subscriber queues (asyncio queues are not thread-safe).
        publish() from any other thread is handed to it with call_soon_threadsafe."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        """Safe from any thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed() and loop.is_running():
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if not on_loop:
                loop.call_soon_threadsafe(self._deliver, message)
                return
        self._deliver(message)

    def _deliver(self, message: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow browser: drop rather than block the station


bus = Bus()
