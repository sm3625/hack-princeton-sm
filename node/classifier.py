"""On-device sound classifier: log-mel features + int8 TFLite CNN (SPEC 3.1).

Feature extraction lives here so training (training/train.py) and the node runtime use the
exact same code. Inference uses ai_edge_litert only; TensorFlow is never imported.

    python -m node.classifier clip.wav     # classify a file, one line per 1 s window
    python -m node.classifier --mic        # live microphone
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from common.contracts import HOP_S, SAMPLE_RATE, WINDOW_S, Prediction, SoundClass

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
DEFAULT_MODEL = MODEL_DIR / "classifier.tflite"

# ---------- features (shared by training and inference) ----------

WINDOW_SAMPLES = int(SAMPLE_RATE * WINDOW_S)  # 16000
N_FFT = 512  # 32 ms
FRAME_HOP = 320  # 20 ms
N_MELS = 40
FMIN_HZ = 20.0
FMAX_HZ = SAMPLE_RATE / 2
TOP_DB = 80.0  # dynamic range kept below each window's peak
N_FRAMES = 1 + (WINDOW_SAMPLES - N_FFT) // FRAME_HOP  # 49
FEATURE_SHAPE = (N_FRAMES, N_MELS)
# Windows quieter than this (-80 dBFS RMS) are treated as silence without running the model.
SILENCE_RMS = 1e-4

FEATURE_CONFIG = {
    "sample_rate": SAMPLE_RATE,
    "window_samples": WINDOW_SAMPLES,
    "n_fft": N_FFT,
    "frame_hop": FRAME_HOP,
    "n_mels": N_MELS,
    "fmin_hz": FMIN_HZ,
    "fmax_hz": FMAX_HZ,
    "top_db": TOP_DB,
    "mel_scale": "htk",
    "normalize": "db_minus_window_mean_div20",
}


def _mel_filterbank() -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)

    edges = mel_to_hz(np.linspace(hz_to_mel(FMIN_HZ), hz_to_mel(FMAX_HZ), N_MELS + 2))
    bins = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    lo, mid, hi = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    fb = np.maximum(0.0, np.minimum((bins - lo) / (mid - lo), (hi - bins) / (hi - mid)))
    assert (fb.sum(axis=1) > 0).all(), "empty mel band"
    return fb.T.astype(np.float32)  # (n_fft/2+1, n_mels)


_MEL_FB = _mel_filterbank()
_HANN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # periodic Hann


def log_mel(audio: np.ndarray) -> np.ndarray:
    """Log-mel features for one window (16000,) -> (49, 40), or a batch (N, 16000) -> (N, 49, 40).

    Gain-invariant: each window is expressed in dB relative to its own mean, so mic level and
    distance only matter through signal-to-noise ratio.
    """
    x = np.asarray(audio, dtype=np.float32)
    single = x.ndim == 1
    x = np.atleast_2d(x)
    if x.shape[-1] != WINDOW_SAMPLES:
        raise ValueError(f"expected {WINDOW_SAMPLES} samples per window, got {x.shape[-1]}")
    frames = sliding_window_view(x, N_FFT, axis=-1)[:, ::FRAME_HOP]  # (N, 49, 512) view
    spec = np.abs(np.fft.rfft(frames * _HANN, axis=-1)) ** 2
    db = 10.0 * np.log10(np.maximum(spec @ _MEL_FB, 1e-10))
    db = np.maximum(db, db.max(axis=(1, 2), keepdims=True) - TOP_DB)
    feats = ((db - db.mean(axis=(1, 2), keepdims=True)) / 20.0).astype(np.float32)
    return feats[0] if single else feats


def rms(audio: np.ndarray) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32)
    return np.sqrt(np.mean(np.square(x, dtype=np.float64), axis=-1))


# ---------- runtime classifier ----------


class TFLiteClassifier:
    """Implements contracts.Classifier with an int8 TFLite model.

    The interpreter loads on first use (or call load()). Safe to share between threads.
    """

    def __init__(self, model_path: Path | str = DEFAULT_MODEL, num_threads: int = 1):
        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise FileNotFoundError(f"model not found: {self.model_path}")
        meta_path = self.model_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        if meta.get("features", FEATURE_CONFIG) != FEATURE_CONFIG:
            raise ValueError(f"{meta_path} was trained with different feature settings")
        self.model_version: int = int(meta.get("model_version", 1))
        self.model_bytes: int = self.model_path.stat().st_size
        names = meta.get("classes", ["background", "chainsaw", "gunshot", "vehicle"])
        self.classes: tuple[SoundClass, ...] = tuple(SoundClass[n.upper()] for n in names)
        # per-class debounce thresholds calibrated on validation folds (node.runtime reads these)
        self.thresholds: dict[SoundClass, float] = {
            SoundClass[k.upper()]: float(v) for k, v in (meta.get("thresholds") or {}).items()
        }
        self.num_threads = num_threads
        self._interp = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Create the interpreter now instead of on the first classify()."""
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._interp is not None:
            return
        from ai_edge_litert.interpreter import Interpreter

        interp = Interpreter(model_path=str(self.model_path), num_threads=self.num_threads)
        interp.allocate_tensors()
        inp = interp.get_input_details()[0]
        out = interp.get_output_details()[0]
        if tuple(inp["shape"][1:3]) != FEATURE_SHAPE or out["shape"][-1] != len(self.classes):
            raise ValueError(f"model shapes {inp['shape']} -> {out['shape']} don't match config")
        self._in, self._out = inp, out
        self._interp = interp

    def _check(self, window: np.ndarray) -> np.ndarray:
        x = np.asarray(window, dtype=np.float32)
        if x.shape != (WINDOW_SAMPLES,):
            raise ValueError(f"window must have shape ({WINDOW_SAMPLES},), got {x.shape}")
        if not np.isfinite(x).all():
            raise ValueError("window contains NaN or inf")
        return x

    def probabilities(self, window: np.ndarray) -> np.ndarray:
        """Class probabilities, ordered like self.classes."""
        x = self._check(window)
        if rms(x) < SILENCE_RMS:
            p = np.zeros(len(self.classes), dtype=np.float32)
            p[self.classes.index(SoundClass.BACKGROUND)] = 1.0
            return p
        feats = log_mel(x)
        with self._lock:
            self._load_locked()
            scale, zero = self._in["quantization"]
            q = np.clip(np.round(feats / scale + zero), -128, 127).astype(self._in["dtype"])
            self._interp.set_tensor(self._in["index"], q.reshape(self._in["shape"]))
            self._interp.invoke()
            raw = self._interp.get_tensor(self._out["index"])[0]
        oscale, ozero = self._out["quantization"]
        p = (raw.astype(np.float32) - ozero) * oscale if oscale else raw.astype(np.float32)
        return np.clip(p, 0.0, 1.0)

    def classify(self, window: np.ndarray) -> Prediction:
        """window: float32 mono, 16000 samples, range -1..1."""
        p = self.probabilities(window)
        i = int(np.argmax(p))
        return Prediction(sound=self.classes[i], confidence=float(p[i]))


# ---------- CLI: classify a file or the live mic ----------


def _windows(audio: np.ndarray):
    hop = int(SAMPLE_RATE * HOP_S)
    if len(audio) < WINDOW_SAMPLES:
        audio = np.pad(audio, (0, WINDOW_SAMPLES - len(audio)))
    for start in range(0, len(audio) - WINDOW_SAMPLES + 1, hop):
        yield start / SAMPLE_RATE, audio[start : start + WINDOW_SAMPLES]


def main(argv: list[str] | None = None) -> None:
    import argparse
    import time

    p = argparse.ArgumentParser(description="Classify 1 s windows from a file or the mic.")
    p.add_argument("wav", nargs="?", help="audio file (any rate; resampled to 16 kHz)")
    p.add_argument("--mic", action="store_true", help="listen on the default microphone")
    p.add_argument("--model", default=str(DEFAULT_MODEL))
    args = p.parse_args(argv)
    clf = TFLiteClassifier(args.model)
    clf.load()
    print(f"model v{clf.model_version}, {clf.model_bytes / 1024:.1f} KB")

    def show(t: float, w: np.ndarray) -> None:
        t0 = time.perf_counter()
        pred = clf.classify(w)
        ms = (time.perf_counter() - t0) * 1000
        print(f"{t:7.1f}s  {pred.sound.name.lower():10s} {pred.confidence:4.2f}  ({ms:.1f} ms)")

    if args.mic:
        import queue

        import sounddevice as sd

        hop = int(SAMPLE_RATE * HOP_S)
        blocks: queue.Queue[np.ndarray] = queue.Queue()
        buf = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=hop,
            callback=lambda data, *_: blocks.put(data[:, 0].copy()),
        ):
            print("listening, ctrl-c to stop")
            t = 0.0
            try:
                while True:
                    buf = np.concatenate([buf[hop:], blocks.get()])
                    t += HOP_S
                    show(t, buf)
            except KeyboardInterrupt:
                pass
    elif args.wav:
        import librosa

        audio, _ = librosa.load(args.wav, sr=SAMPLE_RATE, mono=True)
        for t, w in _windows(audio):
            show(t, w)
    else:
        p.error("give a wav file or --mic")


if __name__ == "__main__":
    main()
