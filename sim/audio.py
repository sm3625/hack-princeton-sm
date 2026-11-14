"""Audio for scenario events: real clips from data/ when present, else synthetic placeholders.

Placeholders are deterministic, 16 kHz mono float32, and sound like the class they stand in
for (two-stroke buzz, impulse with a tail, low rumble, chops). They let the pipeline run end to
end before the datasets are downloaded; they are NOT evidence of classifier accuracy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from common.contracts import ROOT, SAMPLE_RATE

log = logging.getLogger(__name__)

DATA_DIR = ROOT / "data"
SR = SAMPLE_RATE


def resolve_clip(clip: str | None, data_dir: Path = DATA_DIR) -> Path | None:
    """Path of a clip inside data_dir if it exists; None otherwise (also for escapes)."""
    if not clip:
        return None
    base = data_dir.resolve()
    path = (base / clip).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def load_clip(path: Path, duration_s: float) -> np.ndarray:
    import soundfile as sf

    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SR:
        import soxr  # librosa's resampler, without librosa's slow import

        audio = soxr.resample(audio, sr, SR)
    return _fit(audio.astype(np.float32), duration_s)


def _fit(audio: np.ndarray, duration_s: float) -> np.ndarray:
    n = round(duration_s * SR)
    if len(audio) == 0:
        return np.zeros(n, dtype=np.float32)
    if len(audio) < n:
        audio = np.tile(audio, int(np.ceil(n / len(audio))))
    return np.clip(audio[:n], -1.0, 1.0).astype(np.float32)


# ---------- synthesis ----------


def _noise(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.standard_normal(n).astype(np.float32)


def _lowpass(x: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """First-order low-pass response applied in the frequency domain."""
    spectrum = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spectrum / np.sqrt(1 + (f / cutoff_hz) ** 2), len(x)).astype(np.float32)


def _pink(rng: np.random.Generator, n: int) -> np.ndarray:
    spectrum = np.fft.rfft(_noise(rng, n))
    f = np.arange(len(spectrum), dtype=np.float64)
    f[0] = 1.0
    out = np.fft.irfft(spectrum / np.sqrt(f), n)
    return (out / (np.abs(out).max() + 1e-9)).astype(np.float32)


def _harmonic(freq: np.ndarray, n_harm: int, rolloff: float) -> np.ndarray:
    phase = 2 * np.pi * np.cumsum(freq) / SR
    out = np.zeros_like(freq)
    for k in range(1, n_harm + 1):
        out += np.sin(k * phase) / (k**rolloff)
    return out


def _chainsaw(rng: np.random.Generator, n: int, t: np.ndarray) -> np.ndarray:
    # Two-stroke engine: ~120 Hz idle revving to ~190 Hz while cutting, buzzy harmonics.
    rev = 0.5 + 0.5 * np.sin(2 * np.pi * 0.35 * t + rng.uniform(0, 6))
    freq = 120 + 70 * rev + 3 * np.sin(2 * np.pi * 7 * t)
    tone = _harmonic(freq, 24, 0.8)
    grit = _lowpass(_noise(rng, n), 1400) * (0.4 + 0.6 * rev)
    return tone * (0.6 + 0.4 * rev) + 0.5 * grit


def _gunshot(rng: np.random.Generator, n: int, t: np.ndarray) -> np.ndarray:
    # Sharp crack, low thump and a reverberant tail; clips of 3 s or more hold a short volley,
    # so the shots span enough 1 s windows for the node's 3-of-5 debounce.
    out = 0.02 * _pink(rng, n)
    starts = [s for s in (0.4, 1.3, 2.1) if s < t[-1] - 0.3] or [min(0.4, t[-1] / 3)]
    if t[-1] < 2.9:
        starts = starts[:1]
    for t0 in starts:
        dt = np.clip(t - t0, 0, None)
        on = t >= t0
        crack = _noise(rng, n) * np.exp(-dt / 0.012) * on
        thump = np.sin(2 * np.pi * 70 * dt) * np.exp(-dt / 0.08) * on
        tail = _lowpass(_noise(rng, n), 650) * np.exp(-dt / 0.45) * on
        out = out + 1.0 * crack + 0.6 * thump + 0.35 * tail
    return out


def _vehicle(rng: np.random.Generator, n: int, t: np.ndarray) -> np.ndarray:
    # Diesel rumble passing by: low harmonics, swelling then fading.
    mid = t[-1] / 2
    swell = np.exp(-(((t - mid) / (0.45 * t[-1] + 1e-9)) ** 2))
    freq = 42 + 8 * np.tanh((t - mid) / 2)  # slight Doppler-ish drop
    tone = _harmonic(freq, 14, 1.1)
    road = _lowpass(_noise(rng, n), 220)
    return (0.8 * tone + 1.2 * road) * (0.25 + 0.75 * swell)


def _axe(rng: np.random.Generator, n: int, t: np.ndarray) -> np.ndarray:
    out = 0.02 * _pink(rng, n)
    for start in np.arange(0.3, t[-1], 1.25):
        dt = np.clip(t - start, 0, None)
        on = t >= start
        out += (np.sin(2 * np.pi * 520 * dt) * np.exp(-dt / 0.05) * on).astype(np.float32)
        out += (_noise(rng, n) * np.exp(-dt / 0.006) * on * 0.5).astype(np.float32)
    return out


def _background(rng: np.random.Generator, n: int, t: np.ndarray) -> np.ndarray:
    out = 0.05 * _pink(rng, n)
    for start in rng.uniform(0, max(t[-1] - 0.3, 0.1), size=4):
        dt = np.clip(t - start, 0, None)
        on = (t >= start) & (dt < 0.18)
        sweep = 2 * np.pi * (3200 * dt + 6000 * dt**2)
        out += (0.08 * np.sin(sweep) * on).astype(np.float32)
    return out


def _rain(rng: np.random.Generator, n: int) -> np.ndarray:
    hiss = _noise(rng, n) - _lowpass(_noise(rng, n), 135)
    drops = (rng.random(n) < 0.002).astype(np.float32) * rng.uniform(0.3, 1.0, n)
    return (0.25 * hiss + _lowpass(drops.astype(np.float32), 3800)).astype(np.float32)


_SYNTH = {
    "chainsaw": _chainsaw,
    "gunshot": _gunshot,
    "vehicle": _vehicle,
    "axe": _axe,
    "background": _background,
}


def synthesize(sound: str, duration_s: float, seed: int = 0, rain: bool = False) -> np.ndarray:
    """Deterministic placeholder for `sound`; `rain` mixes in rain noise."""
    rng = np.random.default_rng(seed)
    n = max(1, round(duration_s * SR))
    t = np.arange(n, dtype=np.float32) / SR
    gen = _SYNTH.get(sound, _SYNTH["background"])
    audio = gen(rng, n, t).astype(np.float32)
    audio /= np.abs(audio).max() + 1e-9
    audio *= 0.7
    if rain:
        audio += 0.25 * _rain(rng, n)
    fade = min(n // 2, int(0.05 * SR))
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        audio[:fade] *= ramp
        audio[-fade:] *= ramp[::-1]
    return np.clip(audio, -0.95, 0.95).astype(np.float32)
