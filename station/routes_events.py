"""Detections API, tamper demo, and the LoRa UDP listener's lifecycle.

GET  /api/detections?limit=50&verified_only=false   most recent first
POST /api/dev/tamper/{node_id}/{seq}?byte=20         flip one stored byte, re-verify

The router's lifespan starts the UDP listener with the app (FastAPI merges router lifespans
on include_router), so station/app.py only needs include_router.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from common.contracts import RECORD_BYTES
from node.record import OFFSET_CONFIDENCE
from station.receiver import Receiver, get_receiver, serve_udp


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    transport = await serve_udp(get_receiver())
    app.state.lora_udp = transport
    try:
        yield
    finally:
        if transport is not None:
            transport.close()


router = APIRouter(lifespan=lifespan)
StationReceiver = Annotated[Receiver, Depends(get_receiver)]


@router.get("/api/detections")
def detections(
    receiver: StationReceiver,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    verified_only: bool = False,
) -> dict:
    events = receiver.store.recent(limit=limit, verified_only=verified_only)
    return {
        "detections": [
            {
                **e.detection.to_json(),
                "node": receiver.node_name(e.detection.node_id),
                "received_at": e.received_at,
            }
            for e in events
        ]
    }


@router.post("/api/dev/tamper/{node_id}/{seq}")
def tamper(
    node_id: int,
    seq: int,
    receiver: StationReceiver,
    byte: Annotated[int, Query(ge=0, lt=RECORD_BYTES)] = OFFSET_CONFIDENCE,
) -> dict:
    result = receiver.tamper(node_id, seq, byte)
    if result is None:
        raise HTTPException(status_code=404, detail="No such record")
    return result
