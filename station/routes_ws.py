"""WebSocket /ws: forwards every bus message to the browser (shapes in common/contracts.py)."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from station.bus import bus

router = APIRouter()


@router.websocket("/ws")
async def ws(socket: WebSocket) -> None:
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
