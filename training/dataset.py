"""Clip loading, label mapping, source-aware splits and 1 s window extraction.

All audio is decoded once to 16 kHz mono and cached in data/cache/clips_16k.npz.
"""

from __future__ import annotations

import csv
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common.contracts import SAMPLE_RATE, SoundClass
from node.classifier import SILENCE_RMS, WINDOW_SAMPLES

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ESC_DIR = DATA_DIR / "ESC-50-master"
US8K_DIR = DATA_DIR / "UrbanSound8K"
CACHE = DATA_DIR / "cache" / "clips_16k.npz"

CLASSES = (SoundClass.BACKGROUND, SoundClass.CHAINSAW, SoundClass.GUNSHOT, SoundClass.VEHICLE)
EXCLUDED = -1

# Sounds that are ambiguous evidence of human activity are neither a target class nor
# "background", so they are left out of training. evaluate.py still reports what the model
# says about them.
ESC_MAP = {
    "chainsaw": SoundClass.CHAINSAW,
    "engine": SoundClass.VEHICLE,
    "hand_saw": EXCLUDED,
    "fireworks": EXCLUDED,
    "car_horn": EXCLUDED,
}  # every other ESC-50 category -> BACKGROUND
US8K_MAP = {
    "gun_shot": SoundClass.GUNSHOT,
    "engine_idling": SoundClass.VEHICLE,
    "air_conditioner": SoundClass.BACKGROUND,
    "children_playing": SoundClass.BACKGROUND,
    "dog_bark": SoundClass.BACKGROUND,
    "siren": SoundClass.BACKGROUND,
    "street_music": SoundClass.BACKGROUND,
    "car_horn": EXCLUDED,
    "drilling": EXCLUDED,
    "jackhammer": EXCLUDED,
}

# Default held-out folds. Both datasets keep every clip cut from one source recording inside
# a single fold, so splitting by fold splits by source.
TEST_FOLDS = {"esc50": (5,), "us8k": (9, 10)}
VAL_FOLDS = {"esc50": (4,), "us8k": (8,)}

FRAME = 320  # 20 ms frames for event-energy checks
TARGET_PEAK_DB = 15.0  # a target-class window must hold a frame within 15 dB of the clip peak


@dataclass
class Clips:
    audio: np.ndarray  # int16, all clips concatenated
    offsets: np.ndarray
    lengths: np.ndarray
    dataset: np.ndarray  # "esc50" | "us8k"
    fold: np.ndarray
    category: np.ndarray
    label: np.ndarray  # SoundClass value, or EXCLUDED
    source: np.ndarray  # source recording id (ESC src_file / US8K fsID)
    name: np.ndarray

    def __len__(self) -> int:
        return len(self.offsets)

    def wave(self, i: int) -> np.ndarray:
        o, n = self.offsets[i], self.lengths[i]
        return self.audio[o : o + n].astype(np.float32) / 32768.0

    def split(self, folds: dict[str, tuple[int, ...]]) -> np.ndarray:
        mask = np.zeros(len(self), dtype=bool)
        for ds, fs in folds.items():
            mask |= (self.dataset == ds) & np.isin(self.fold, fs)
        return mask


def _manifest() -> list[dict]:
    rows = []
    if (ESC_DIR / "meta" / "esc50.csv").exists():
        with open(ESC_DIR / "meta" / "esc50.csv") as f:
            for r in csv.DictReader(f):
                rows.append(
                    {
                        "path": str(ESC_DIR / "audio" / r["filename"]),
                        "dataset": "esc50",
                        "fold": int(r["fold"]),
                        "category": r["category"],
                        "label": int(ESC_MAP.get(r["category"], SoundClass.BACKGROUND)),
                        "source": f"esc-{r['src_file']}",
                        "name": r["filename"],
                    }
                )
    if (US8K_DIR / "metadata" / "UrbanSound8K.csv").exists():
        with open(US8K_DIR / "metadata" / "UrbanSound8K.csv") as f:
            for r in csv.DictReader(f):
                rows.append(
                    {
                        "path": str(US8K_DIR / "audio" / f"fold{r['fold']}" / r["slice_file_name"]),
                        "dataset": "us8k",
                        "fold": int(r["fold"]),
                        "category": r["class"],
                        "label": int(US8K_MAP[r["class"]]),
                        "source": f"fs-{r['fsID']}",
                        "name": r["slice_file_name"],
                    }
                )
    return rows


def _decode(path: str) -> np.ndarray:
    import librosa

    y, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True, res_type="soxr_hq")
    peak = np.abs(y).max() if y.size else 0.0
    if peak > 1.0:  # a few US8K files are float WAVs above full scale
        y = y / peak
    return np.round(y * 32767).astype(np.int16)


def build_cache(force: bool = False) -> Path:
    rows = _manifest()
    if not rows:
        raise SystemExit("no datasets in data/; run: uv run python training/download.py")
    if CACHE.exists() and not force:
        with np.load(CACHE, allow_pickle=False) as z:
            if len(z["offsets"]) == len(rows):
                return CACHE
    print(f"decoding {len(rows)} clips to 16 kHz (one-time)...", flush=True)
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as pool:
        waves = list(pool.map(_decode, [r["path"] for r in rows], chunksize=32))
    lengths = np.array([len(w) for w in waves], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        CACHE,
        audio=np.concatenate(waves),
        offsets=offsets,
        lengths=lengths,
        **{k: np.array([r[k] for r in rows]) for k in rows[0] if k != "path"},
    )
    print(f"cached {CACHE} ({CACHE.stat().st_size / 1e9:.2f} GB)")
    return CACHE


def load_clips() -> Clips:
    path = build_cache()
    with np.load(path, allow_pickle=False) as z:
        return Clips(**{k: z[k] for k in z.files})


def window_starts(wave: np.ndarray, label: int, hop: int) -> np.ndarray:
    """Start offsets of usable 1 s windows in a clip (already padded to >= 1 s).

    Background: every window that isn't digital silence.
    Target classes: only windows that contain the event (a 20 ms frame within TARGET_PEAK_DB
    of the clip's loudest frame), so silence padded onto ESC-50 clips isn't labelled chainsaw.
    """
    n = len(wave)
    starts = np.arange(0, n - WINDOW_SAMPLES + 1, hop)
    if (
        len(starts)
        and starts[-1] != n - WINDOW_SAMPLES
        and n - WINDOW_SAMPLES - starts[-1] > hop // 2
    ):
        starts = np.append(starts, n - WINDOW_SAMPLES)
    sq = np.square(wave, dtype=np.float64)
    csum = np.concatenate([[0.0], np.cumsum(sq)])
    win_rms = np.sqrt((csum[starts + WINDOW_SAMPLES] - csum[starts]) / WINDOW_SAMPLES)
    keep = win_rms >= SILENCE_RMS
    if label != SoundClass.BACKGROUND:
        nf = n // FRAME
        frame_db = 10 * np.log10(sq[: nf * FRAME].reshape(nf, FRAME).mean(axis=1) + 1e-12)
        peak = frame_db.max()
        frames_per_win = WINDOW_SAMPLES // FRAME
        win_peak = np.array(
            [frame_db[s // FRAME : s // FRAME + frames_per_win].max() for s in starts]
        )
        keep &= win_peak >= peak - TARGET_PEAK_DB
    return starts[keep]


def pad_clip(wave: np.ndarray) -> np.ndarray:
    """Centre clips shorter than one window in zeros."""
    if len(wave) >= WINDOW_SAMPLES:
        return wave
    extra = WINDOW_SAMPLES - len(wave)
    return np.pad(wave, (extra // 2, extra - extra // 2))


def eval_windows(clips: Clips, idx: np.ndarray, hop: int = SAMPLE_RATE // 2):
    """Fixed, non-augmented windows for evaluation. Returns (windows, clip_index)."""
    wins, owner = [], []
    for i in idx:
        w = pad_clip(clips.wave(i))
        for s in window_starts(w, int(clips.label[i]), hop):
            wins.append(w[s : s + WINDOW_SAMPLES])
            owner.append(i)
    if not wins:
        return np.zeros((0, WINDOW_SAMPLES), np.float32), np.zeros(0, np.int64)
    return np.stack(wins).astype(np.float32), np.array(owner)
