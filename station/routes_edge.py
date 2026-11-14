"""Edge backend status and real audio injection into the existing signed detection path."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from common.contracts import ROOT
from emulators.bridge import EdgeClassifier
from emulators.run import inputs

router = APIRouter()


class PlayRequest(BaseModel):
    sound: Literal["background", "chainsaw", "gunshot", "vehicle"]
    node_id: int = 5


def connected(request):
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None or not isinstance(pipeline.classifier, EdgeClassifier):
        raise HTTPException(
            409, "Start the station with python -m emulators.demo to connect an edge worker."
        )
    return pipeline


@router.get("/api/edge/status")
def status(request: Request):
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None or not isinstance(pipeline.classifier, EdgeClassifier):
        return {"connected": False, "label": "Edge worker not connected", "backend": "off"}
    return {
        **pipeline.classifier.status(),
        "nodes": [{"id": r.node.id, "name": r.node.name} for r in pipeline.runtimes.values()],
    }


@router.post("/api/edge/play")
async def play(body: PlayRequest, request: Request):
    pipeline = connected(request)
    if body.node_id not in pipeline.runtimes:
        raise HTTPException(404, "Unknown sensor node")
    path = ROOT / "emulators/audio" / f"{body.sound}.wav"
    if not path.is_file():
        raise HTTPException(
            409, "Demo recordings missing; run python -m emulators.run --prepare emulators/audio"
        )
    # Inputs uses PCM directly, the same 16kHz preparation as the edge benchmark.
    audio = next(audio for name, audio, _ in inputs(path.parent) if name == body.sound)
    future = pipeline.feed(body.node_id, audio)
    result = await future
    if result.get("error"):
        raise HTTPException(503, result["error"])
    return {
        **result,
        "sound": body.sound,
        "note": "Normal thresholds, suppression and radio loss apply. See Feed for verified arrivals.",
    }
