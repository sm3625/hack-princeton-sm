"""Detections API, tamper demo, and the LoRa UDP listener's lifecycle.

GET  /api/detections?limit=50&verified_only=false   most recent first
POST /api/dev/tamper/{node_id}/{seq}?byte=20         flip one stored byte, re-verify
                                                     (demo only: 404 unless STATION_DEV is truthy)

The router's lifespan starts the UDP listener with the app (FastAPI merges router lifespans
on include_router), so station/app.py only needs include_router.
"""

from __future__ import annotations

import os
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

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


def dev_enabled() -> bool:
    return os.getenv("STATION_DEV", "").strip().lower() in ("1", "true", "yes", "on")


def require_dev() -> None:
    if not dev_enabled():
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/api/dev/tamper/{node_id}/{seq}", dependencies=[Depends(require_dev)])
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


@router.post("/api/dev/tamper-preview/{node_id}/{seq}", dependencies=[Depends(require_dev)])
def tamper_preview(
    node_id: int,
    seq: int,
    receiver: StationReceiver,
    field: Literal["timestamp", "recording"] = "timestamp",
) -> dict:
    """Verify an altered copy of a signed record; retain the original evidence unchanged."""
    event = receiver.store.find(node_id, seq)
    if event is None:
        raise HTTPException(status_code=404, detail="No such record")
    original = bytes(event.raw)
    changed = bytearray(original)
    if field == "timestamp":
        before = struct.unpack_from("<I", original, 7)[0]
        after = before + 3600 if before <= 0xFFFFFFFF - 3600 else before - 3600
        struct.pack_into("<I", changed, 7, after)
    else:
        before = original[23:39].hex()
        changed[23] ^= 0xFF
        after = changed[23:39].hex()
    return {"field": field, "before": before, "after": after,
            "original_verified": receiver.reverify(original),
            "modified_verified": receiver.reverify(bytes(changed)),
            "original_preserved": True}
