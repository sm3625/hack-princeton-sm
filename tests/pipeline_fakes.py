"""Test doubles for the node -> station pipeline (no real model, no real clock)."""

from __future__ import annotations

import hashlib

import numpy as np
from nacl.signing import SigningKey

from common.contracts import Detection, Prediction, SoundClass, load_world

WORLD = load_world()
CREEK_BEND = WORLD.node(5)
T0 = 1_763_000_000

BG = Prediction(SoundClass.BACKGROUND, 0.95)
SAW = Prediction(SoundClass.CHAINSAW, 0.92)
SAW_WEAK = Prediction(SoundClass.CHAINSAW, 0.6)
GUN = Prediction(SoundClass.GUNSHOT, 0.9)


class ScriptedClassifier:
    """Returns predictions in order, one per window; background once the script runs out."""

    def __init__(self, script: list[Prediction], model_version: int = 7, model_bytes: int = 60_000):
        self.script = list(script)
        self.model_version = model_version
        self.model_bytes = model_bytes
        self.windows: list[np.ndarray] = []

    def classify(self, window: np.ndarray) -> Prediction:
        self.windows.append(window)
        return self.script.pop(0) if self.script else BG


class LoudnessClassifier:
    """Chainsaw when the window is loud, background otherwise."""

    model_version = 3
    model_bytes = 40_000

    def classify(self, window: np.ndarray) -> Prediction:
        rms = float(np.sqrt(np.mean(window**2)))
        return SAW if rms > 0.1 else BG


class Clock:
    def __init__(self, t: float = T0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def detection(node_id: int = 5, seq: int = 1, **overrides) -> Detection:
    fields = {
        "node_id": node_id,
        "seq": seq,
        "timestamp": T0,
        "lat": -3.4443,
        "lon": -62.1961,
        "sound": SoundClass.CHAINSAW,
        "confidence": 0.9,
        "model_version": 7,
        "clip_hash": hashlib.sha256(b"clip").digest()[:16].hex(),
    }
    return Detection(**(fields | overrides))


def keys_for(*node_ids: int) -> dict[int, SigningKey]:
    return {n: SigningKey(hashlib.sha256(f"node-{n}".encode()).digest()) for n in node_ids}
