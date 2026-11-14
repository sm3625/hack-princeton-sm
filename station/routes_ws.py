"""WebSocket /ws: forwards every bus message to the browser (shapes in common/contracts.py)."""

import asyncio
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from station.bus import bus

router = APIRouter()

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def public_origin_allowed(origin: str) -> bool:
    return bool(os.getenv("STATION_JUDGE_PASSWORD")) and origin.startswith("https://") and origin in {
        value.strip() for value in os.getenv("STATION_HTTPS_ORIGINS", "").split(",")
    }


def origin_allowed(origin: str | None, server_port: int | None) -> bool:
    """A browser page may open /ws only if it was served by this station (localhost, same port).
    A missing Origin means a non-browser client (scripts, tests), which is allowed. This stops
    any other website the ranger visits from reading the live feed (cross-site WebSocket)."""
    if origin is None or public_origin_allowed(origin):
        return True
    try:
        parts = urlsplit(origin)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or host not in LOCAL_HOSTS:
        return False
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return port == server_port


@router.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    server = socket.scope.get("server")
    if not origin_allowed(socket.headers.get("origin"), server[1] if server else None):
        await socket.close(code=1008)  # policy violation; the handshake is answered with 403
        return
    await socket.accept()
    queue = bus.subscribe()
    # Watch for the browser leaving while we wait for bus messages; otherwise the handler
    # sits in queue.get() after the tab closes and blocks server shutdown.
    closed = asyncio.create_task(_wait_closed(socket))
    try:
        while True:
            get = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                get.cancel()
                break
            await socket.send_json(get.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        closed.cancel()
        bus.unsubscribe(queue)


async def _wait_closed(socket: WebSocket) -> None:
    try:
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return
