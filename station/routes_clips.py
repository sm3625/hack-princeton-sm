"""Node audio for the dashboard: each node's saved recordings, and what it is hearing now.

GET /api/nodes/{node_id}/clips            recordings on the node's "SD card", newest first, with
                                          the matching detection record when the station has it
GET /api/nodes/{node_id}/clips/{seq}.wav  the 5 s WAV the node saved around detection `seq`
GET /api/nodes/{node_id}/live.wav?after=T what the node is hearing: the newest sound it was fed
                                          after sim time T, else 5 s of ambient forest sound

Clip responses carry X-Clip-Hash: the first 16 bytes of SHA-256 of the file, which is the value
the node signed into its detection record, so the UI can show whether audio and record match.
Live responses carry X-Live-At (sim time of the sound) and X-Live-Kind (heard | ambient-rain |
ambient-night). Ambient sound is a stand-in for the forest between scenario events, and says so.
Node ids and seqs are integers, so no path from the request reaches the filesystem.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from common.contracts import ROOT
from node.record import clip_hash
from node.runtime import SAMPLE_RATE, clip_path, wav_bytes

log = logging.getLogger(__name__)
router = APIRouter()

MAX_CLIP_BYTES = 2_000_000  # a 5 s 16 kHz mono clip is ~160 KB
LIST_LIMIT = 30
AMBIENT_S = 5.0
# Real ESC-50 recordings used as the forest bed when a node hears nothing in particular.
AMBIENT = {
    "ambient-rain": "ESC-50-master/audio/2-73260-A-10.wav",   # rain
    "ambient-night": "ESC-50-master/audio/4-171823-A-13.wav",  # crickets
}


def clips_dir() -> Path:
    return Path(os.getenv("NODE_DATA_DIR") or ROOT / "node_data")


@router.get("/api/nodes/{node_id}/clips")
def list_clips(node_id: int, request: Request) -> dict:
    if not 0 < node_id < 10_000:
        raise HTTPException(404, "No such node.")
    folder = clips_dir() / str(node_id)
    seqs = sorted((int(p.stem) for p in folder.glob("*.wav") if p.stem.isdigit()), reverse=True)[:LIST_LIMIT]
    records = {}
    store = getattr(request.app.state, "event_store", None)
    if store is not None and seqs:
        for d in store.query(node_ids=[node_id], verified_only=False):
            records[d.seq] = d
    out = []
    for seq in seqs:
        d = records.get(seq)
        out.append({
            "seq": seq,
            "url": f"/api/nodes/{node_id}/clips/{seq}.wav",
            "timestamp": d.timestamp if d else None,
            "sound": getattr(d.sound, "name", str(d.sound)).lower() if d else None,
            "confidence": round(float(d.confidence), 2) if d else None,
            "verified": bool(d.verified) if d else None,
            "clip_hash": d.clip_hash if d else None,
        })
    return {"node_id": node_id, "clips": out}


@router.get("/api/nodes/{node_id}/clips/{seq}.wav")
def get_clip(node_id: int, seq: int) -> Response:
    if not (0 < node_id < 10_000 and 0 < seq < 2**32):
        raise HTTPException(404, "No such clip.")
    path = clip_path(clips_dir(), node_id, seq)
    try:
        data = path.read_bytes() if path.stat().st_size <= MAX_CLIP_BYTES else None
    except OSError:
        data = None
    if data is None:
        raise HTTPException(404, "No such clip.")
    return Response(data, media_type="audio/wav",
                    headers={"X-Clip-Hash": clip_hash(data).hex(), "Cache-Control": "no-store"})


@router.get("/api/nodes/{node_id}/live.wav")
def live(node_id: int, request: Request, after: float = 0.0) -> Response:
    if not 0 < node_id < 10_000:
        raise HTTPException(404, "No such node.")
    pipeline = getattr(request.app.state, "pipeline", None)
    heard = list(getattr(pipeline, "heard", {}).get(node_id, ())) if pipeline is not None else []
    fresh = [(t, a) for t, a in heard if t > after]
    if fresh:
        t, audio = fresh[0]  # oldest unplayed first, so a listener hears every sound in order
        kind, data = "heard", wav_bytes(np.asarray(audio, dtype=np.float32))
    else:
        t, kind = after, _ambient_kind(request, node_id)
        data = _ambient_wav(kind)
    return Response(data, media_type="audio/wav",
                    headers={"X-Live-At": f"{t:.1f}", "X-Live-Kind": kind, "Cache-Control": "no-store"})


def _ambient_kind(request: Request, node_id: int) -> str:
    try:
        from sim.engine import get_engine

        engine = get_engine()
        node = engine.world.node(node_id)
        return "ambient-rain" if engine.weather(node.sector).rain else "ambient-night"
    except Exception:  # noqa: BLE001 - ambient sound is cosmetic; never fail the request
        return "ambient-night"


@lru_cache(maxsize=4)
def _ambient_wav(kind: str) -> bytes:
    from sim.audio import DATA_DIR, load_clip, resolve_clip

    path = resolve_clip(AMBIENT.get(kind), DATA_DIR)
    if path is not None:
        try:
            return wav_bytes(load_clip(path, AMBIENT_S) * 0.5)
        except Exception:  # noqa: BLE001 - fall back to quiet noise below
            log.warning("ambient clip %s unreadable; using quiet noise", path.name)
    rng = np.random.default_rng(1)
    return wav_bytes((rng.standard_normal(int(AMBIENT_S * SAMPLE_RATE)) * 0.01).astype(np.float32))
