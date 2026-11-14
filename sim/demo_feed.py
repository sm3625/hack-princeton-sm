"""STUB detection source for UI/sim development. Not the real pipeline.

Skips audio, classification and signing entirely: each scenario event becomes a fake
{"type": "detection"} bus message carrying the scenario's confidence, marked "stub": true and
with an empty signature. Used only until the pipeline's NodeRuntime is plugged in through
SimEngine.set_feed(); never use these records as evidence.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from common.contracts import Detection, SoundClass, World
from sim.scenario import ScenarioEvent

log = logging.getLogger(__name__)


class DemoFeed:
    def __init__(self, world: World, now: Callable[[], int], publish: Callable[[dict], None]):
        self.world = world
        self.now = now
        self.publish = publish
        self.seq: dict[int, int] = {}
        self.sent = 0

    def reset(self) -> None:
        self.seq.clear()
        self.sent = 0

    def on_event(self, event: ScenarioEvent) -> None:
        if event.sound == "background":
            return  # background never produces a detection record
        node = self.world.node(event.node_id)
        seq = self.seq.get(node.id, 0) + 1
        self.seq[node.id] = seq
        digest = hashlib.sha256(f"stub:{node.id}:{seq}:{event.at}".encode()).hexdigest()
        detection = Detection(
            node_id=node.id,
            seq=seq,
            timestamp=self.now(),
            lat=node.lat,
            lon=node.lon,
            sound=SoundClass[event.sound.upper()],
            confidence=round(event.confidence, 3),
            model_version=0,
            clip_hash=digest[:32],
            signature="",
            verified=True,
        )
        self.sent += 1
        log.info("STUB detection: %s %s %.2f", node.name, event.sound, event.confidence)
        self.publish({"type": "detection", "detection": detection.to_json(), "stub": True})
