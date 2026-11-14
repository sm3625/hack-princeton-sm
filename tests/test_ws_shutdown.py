"""A closed browser tab must not block server shutdown (regression: /ws sat in queue.get())."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

import websockets


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_shuts_down_after_ws_client_leaves():
    port = free_port()
    env = {**os.environ, "LLM_MODE": "mock", "LORA_UDP_PORT": "off"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "station.app:app", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:

        async def connect_and_leave():
            for _ in range(60):
                try:
                    async with websockets.connect(f"ws://127.0.0.1:{port}/ws"):
                        await asyncio.sleep(0.2)
                    return
                except OSError:
                    await asyncio.sleep(0.1)
            raise AssertionError("server never came up")

        asyncio.run(connect_and_leave())
        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
