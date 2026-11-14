"""Node runtime (SPEC 3.2): audio -> 1 s windows -> classifier -> debounce -> clip -> signed record.

Time: feed(samples) means "these samples just arrived; the last one is clock() now". Each window's
time is derived from that, so a whole clip can be fed in one call (sim) or in small blocks (mic).
Debounce windows are consecutive in the audio stream; suppression and the events/hour budget use
those clock times, so a sim that feeds separate clips at different sim times behaves correctly.
"""

from __future__ import annotations

import io
import logging
import math
import os
import wave
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from nacl.signing import SigningKey

from common.contracts import (
    HOP_S,
    MAX_PACKET_BYTES,
    RECORD_BYTES,
    SAMPLE_RATE,
    WINDOW_S,
    Classifier,
    Detection,
    Node,
    Prediction,
    SoundClass,
)
from node.record import clip_hash, pack, unpack

log = logging.getLogger(__name__)

DEBOUNCE_WINDOWS = 5
DEBOUNCE_HITS = 3
DEBOUNCE_CONFIDENCE = 0.8
# A gunshot is an impulse: with 1 s windows every 0.5 s one shot lights up at most 2 windows,
# so "3 of 5" could never fire. Two confident windows of five (the two that share the same
# crack) still rejects a lone spurious window, and a volley fires as well. SPEC 3.2 updated.
IMPULSE_HITS = 2
HITS_REQUIRED = {SoundClass.GUNSHOT: IMPULSE_HITS}
SUPPRESS_S = 30.0
CLIP_S = 5.0


@dataclass(frozen=True)
class NodeLimits:
    """Device limits, enforced in code and reported by NodeRuntime.stats()."""

    max_model_bytes: int = 100_000
    max_record_bytes: int = MAX_PACKET_BYTES
    max_events_per_hour: int = 60


class LimitError(RuntimeError):
    """A device limit would be exceeded."""


def clip_path(clip_dir: Path, node_id: int, seq: int) -> Path:
    """Where a node keeps the clip for record `seq` (its "SD card")."""
    return Path(clip_dir) / str(node_id) / f"{seq}.wav"


def wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _as_mono_float(samples: np.ndarray) -> np.ndarray:
    x = np.asarray(samples)
    if x.ndim == 2:
        x = x.mean(axis=1)
    elif x.ndim != 1:
        raise ValueError(f"expected 1-D or (frames, channels) audio, got shape {x.shape}")
    if np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float32) / float(np.iinfo(x.dtype).max + 1)
    return x.astype(np.float32, copy=False)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class NodeRuntime:
    def __init__(
        self,
        node: Node,
        classifier: Classifier,
        signing_key: SigningKey,
        clip_dir: Path,
        send: Callable[[bytes], object],
        clock: Callable[[], float],
        *,
        limits: NodeLimits | None = None,
        on_window: Callable[[float, Prediction], None] | None = None,
    ) -> None:
        self.node = node
        self.limits = limits or NodeLimits()
        if classifier.model_bytes > self.limits.max_model_bytes:
            raise LimitError(
                f"model is {classifier.model_bytes} bytes; device limit is "
                f"{self.limits.max_model_bytes}"
            )
        if RECORD_BYTES > self.limits.max_record_bytes:
            raise LimitError(
                f"record is {RECORD_BYTES} bytes; limit {self.limits.max_record_bytes}"
            )
        self._classifier = classifier
        self._key = signing_key
        self._send = send
        self._clock = clock
        self._on_window = on_window
        self.clip_dir = Path(clip_dir)
        self.node_dir = self.clip_dir / str(node.id)
        self.node_dir.mkdir(parents=True, exist_ok=True)
        self._seq_file = self.node_dir / "seq"
        self._seq = int(self._seq_file.read_text()) if self._seq_file.exists() else 0

        self._win = int(SAMPLE_RATE * WINDOW_S)
        self._hop = int(SAMPLE_RATE * HOP_S)
        self._clip = int(SAMPLE_RATE * CLIP_S)
        self._suppressed_until: dict[SoundClass, float] = {}
        self._emitted_at: deque[float] = deque()
        self._last_t = -math.inf
        self.counters = {"windows": 0, "events": 0, "budget_dropped": 0, "send_errors": 0}
        self.reset()
        log.info(
            "node %s: model %d/%d bytes, record %d/%d bytes, budget %d events/hour",
            node.name,
            classifier.model_bytes,
            self.limits.max_model_bytes,
            RECORD_BYTES,
            self.limits.max_record_bytes,
            self.limits.max_events_per_hour,
        )

    @property
    def seq(self) -> int:
        """Last seq used (0 before the first event)."""
        return self._seq

    def reset(self) -> None:
        """Forget buffered audio and debounce history (e.g. between unrelated sim clips).
        Suppression, budget and seq are kept."""
        self._audio = np.zeros(0, dtype=np.float32)
        self._audio_start = 0  # absolute sample index of self._audio[0]
        self._total = 0  # absolute samples received
        self._next = 0  # absolute start of the next window
        self._history: deque[Prediction] = deque(maxlen=DEBOUNCE_WINDOWS)

    def feed(self, samples: np.ndarray) -> list[Detection]:
        """Process newly arrived audio; returns any detections emitted (already sent)."""
        x = _as_mono_float(samples)
        now = float(self._clock())
        self._audio = np.concatenate([self._audio, x])
        self._total += len(x)

        emitted: list[Detection] = []
        while self._next + self._win <= self._total:
            offset = self._next - self._audio_start
            window = self._audio[offset : offset + self._win]
            end = self._next + self._win
            t = now - (self._total - end) / SAMPLE_RATE
            pred = self._classifier.classify(window)
            pred = Prediction(SoundClass(pred.sound), float(pred.confidence))
            self.counters["windows"] += 1
            self._last_t = t
            if self._on_window:
                self._on_window(t, pred)
            self._history.append(pred)
            detection = self._debounce(t, end)
            if detection:
                emitted.append(detection)
            self._next += self._hop

        # Keep enough audio for the next window and for a full clip ending at any later window.
        keep_from = max(self._audio_start, self._next + self._win - self._clip)
        if keep_from > self._audio_start:
            self._audio = self._audio[keep_from - self._audio_start :]
            self._audio_start = keep_from
        return emitted

    def _debounce(self, t: float, end: int) -> Detection | None:
        hits = [
            p
            for p in self._history
            if p.sound != SoundClass.BACKGROUND and p.confidence >= DEBOUNCE_CONFIDENCE
        ]
        if not hits:
            return None
        counts = Counter(p.sound for p in hits)
        ready = [
            (sound, n)
            for sound, n in counts.most_common()
            if n >= HITS_REQUIRED.get(sound, DEBOUNCE_HITS)
            and t >= self._suppressed_until.get(sound, -math.inf)
        ]
        if not ready:
            return None
        sound, count = ready[0]
        self._suppressed_until[sound] = t + SUPPRESS_S
        if not self._within_budget(t):
            self.counters["budget_dropped"] += 1
            log.warning(
                "node %s: events/hour budget reached; dropped %s", self.node.name, sound.name
            )
            return None
        confidence = sum(p.confidence for p in hits if p.sound == sound) / count
        return self._emit(t, end, sound, confidence)

    def _within_budget(self, t: float) -> bool:
        while self._emitted_at and self._emitted_at[0] <= t - 3600:
            self._emitted_at.popleft()
        return len(self._emitted_at) < self.limits.max_events_per_hour

    def _emit(self, t: float, end: int, sound: SoundClass, confidence: float) -> Detection:
        # Persist the new seq before anything leaves the node: a crash may leave a gap,
        # never a reused seq (which the station would reject as a replay).
        seq = self._seq + 1
        _write_atomic(self._seq_file, f"{seq}\n".encode())
        self._seq = seq

        start = max(self._audio_start, end - self._clip)
        clip = wav_bytes(self._audio[start - self._audio_start : end - self._audio_start])
        _write_atomic(clip_path(self.clip_dir, self.node.id, seq), clip)

        detection = Detection(
            node_id=self.node.id,
            seq=seq,
            timestamp=int(t),
            lat=self.node.lat,
            lon=self.node.lon,
            sound=sound,
            confidence=confidence,
            model_version=self._classifier.model_version,
            clip_hash=clip_hash(clip).hex(),
        )
        record = pack(detection, self._key)
        if len(record) > self.limits.max_record_bytes:
            raise LimitError(f"record is {len(record)} bytes; limit {self.limits.max_record_bytes}")
        self._emitted_at.append(t)
        self.counters["events"] += 1
        log.info("node %s: %s %.2f seq %d", self.node.name, sound.name.lower(), confidence, seq)
        try:
            self._send(record)
        except Exception:
            self.counters["send_errors"] += 1
            log.exception("node %s: send failed for seq %d", self.node.name, seq)
        return unpack(record)

    def stats(self) -> dict:
        """Limits and counters, for logs and the UI."""
        if math.isfinite(self._last_t):
            self._within_budget(self._last_t)
        return {
            "node": self.node.name,
            "model_bytes": self._classifier.model_bytes,
            "max_model_bytes": self.limits.max_model_bytes,
            "record_bytes": RECORD_BYTES,
            "max_record_bytes": self.limits.max_record_bytes,
            "events_last_hour": len(self._emitted_at),
            "max_events_per_hour": self.limits.max_events_per_hour,
            "seq": self._seq,
            **self.counters,
        }
