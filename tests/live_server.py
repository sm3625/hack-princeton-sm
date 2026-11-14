"""Run the station in-process on a free port, for browser tests and screenshots.

uvicorn runs on its own event loop in a daemon thread. The bus is an asyncio structure owned
by that loop, so messages from the test thread go through publish(), which hands them over
with call_soon_threadsafe (calling bus.publish directly from another thread would not wake
the WebSocket sender).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import uvicorn


class LiveServer:
    def __init__(self, llm_mode: str = "mock") -> None:
        os.environ["LLM_MODE"] = llm_mode
        from station.app import create_app

        self.app = create_app()
        self.loop = asyncio.new_event_loop()
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.port = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def start(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("station did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def publish(self, message: dict) -> None:
        from station.bus import bus

        self.loop.call_soon_threadsafe(bus.publish, message)

    def call(self, fn, *args):
        """Run fn(*args) on the server loop and wait for the result (coroutines allowed)."""

        async def runner():
            result = fn(*args)
            return await result if asyncio.iscoroutine(result) else result

        return asyncio.run_coroutine_threadsafe(runner(), self.loop).result(10)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
