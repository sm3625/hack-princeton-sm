"""WebSocket /ws: forwards every bus message to the browser (shapes in common/contracts.py)."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from station.bus import bus

router = APIRouter()


@router.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    queue = bus.subscribe()
    try:
        while True:
            await socket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
