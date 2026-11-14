"""Rules baseline (SPEC 3.9): one SMS-style alert per verified detection with confidence >= 0.8.

The eval compares this against incidents the commander raises for approval.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from common.contracts import Detection, SoundClass, World

THRESHOLD = 0.8


@dataclass(frozen=True)
class Alert:
    node: str
    seq: int
    timestamp: int
    sound: str
    confidence: float


def baseline_alerts(detections: Iterable[Detection], world: World, threshold: float = THRESHOLD) -> list[Alert]:
    return [
        Alert(world.node(d.node_id).name, d.seq, d.timestamp, d.sound.name.lower(), d.confidence)
        for d in sorted(detections, key=lambda d: (d.timestamp, d.node_id, d.seq))
        if d.verified and d.sound != SoundClass.BACKGROUND and d.confidence >= threshold
    ]
