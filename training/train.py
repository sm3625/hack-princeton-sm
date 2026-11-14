"""Train the 4-class sound CNN and export an int8 TFLite model to models/.

    uv run python -m training.train                 # train on folds 1-4 / 1-8, test on held-out
    uv run python -m training.train --val           # also hold out ESC fold 4 / US8K fold 8
                                                    # for tuning (prints val metrics per epoch)

Writes models/classifier.tflite, models/classifier.json and (via training.evaluate)
models/eval.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from common.contracts import SoundClass
from node.classifier import (
    DEFAULT_MODEL,
    FEATURE_CONFIG,
    FEATURE_SHAPE,
    WINDOW_SAMPLES,
    log_mel,
    rms,
)
from training.dataset import (
    CLASSES,
    DATA_DIR,
    TEST_FOLDS,
    VAL_FOLDS,
    Clips,
    eval_windows,
    load_clips,
    pad_clip,
    window_starts,
)

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

N_CLASSES = len(CLASSES)
BG = CLASSES.index(SoundClass.BACKGROUND)
GUN = CLASSES.index(SoundClass.GUNSHOT)
# windows drawn per class each epoch; background gets three times the share because it
# dominates real deployments and false alarms are the expensive mistake
PER_EPOCH = {
    SoundClass.BACKGROUND: 15_000,
    SoundClass.CHAINSAW: 5_000,
    SoundClass.GUNSHOT: 5_000,
    SoundClass.VEHICLE: 5_000,
}


BIAS_FILE = DATA_DIR / "cache" / "class_bias.json"


class Sampler:
    """Draws random 1 s training windows, balanced per class and then per source category,
    so 40 ESC-50 rain clips weigh as much as 1000 US8K street-music clips."""

    def __init__(self, clips: Clips, idx: np.ndarray, rng: np.random.Generator, hop: int = 1600):
        self.clips, self.rng = clips, rng
        self.groups: dict[int, dict[str, list[tuple[int, np.ndarray]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for i in idx:
            label = int(clips.label[i])
            if label < 0:
                continue
            starts = window_starts(pad_clip(clips.wave(i)), label, hop)
            if len(starts):
                key = f"{clips.dataset[i]}/{clips.category[i]}"
                self.groups[label][key].append((int(i), starts))
        for label, cats in self.groups.items():
            n = sum(len(v) for v in cats.values())
            print(f"  {SoundClass(label).name.lower():10s} {n:5d} clips, {len(cats)} categories")

    def window(self, label: int, speed: float = 1.0) -> np.ndarray:
        """A random window; speed != 1 plays the source faster/slower (engine RPM, pitch)."""
        cats = self.groups[label]
        cat = list(cats)[self.rng.integers(len(cats))]
        i, starts = cats[cat][self.rng.integers(len(cats[cat]))]
        o, n = self.clips.offsets[i], self.clips.lengths[i]
        if n < WINDOW_SAMPLES:  # short clip: drop it at a random spot in a silent window
            out = np.zeros(WINDOW_SAMPLES, np.float32)
            at = self.rng.integers(WINDOW_SAMPLES - n + 1)
            out[at : at + n] = self.clips.audio[o : o + n] / 32768.0
            return out
        span = min(round(WINDOW_SAMPLES * speed), int(n))
        s = int(starts[self.rng.integers(len(starts))]) + int(self.rng.integers(-800, 801))
        s = min(max(s, 0), n - span)
        x = self.clips.audio[o + s : o + s + span].astype(np.float32) / 32768.0
        if span != WINDOW_SAMPLES:
            x = np.interp(np.linspace(0, span - 1, WINDOW_SAMPLES), np.arange(span), x)
        return x.astype(np.float32)

    def batch(self, per_class: dict[SoundClass, int]) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = [], []
        for sc, n in per_class.items():
            engine = sc in (SoundClass.CHAINSAW, SoundClass.VEHICLE)
            for _ in range(n):
                speed = self.rng.uniform(0.8, 1.25) if engine and self.rng.random() < 0.5 else 1.0
                xs.append(self.window(int(sc), speed))
            ys += [CLASSES.index(sc)] * n
        return np.stack(xs), np.array(ys)


_NOISE_BANK: list[np.ndarray] = []


def colored_noise(rng: np.random.Generator, n: int) -> np.ndarray:
    """A random slice of white, pink or brown noise, unit RMS (from a precomputed bank)."""
    if not _NOISE_BANK:
        for k in range(96):
            m = 4 * WINDOW_SAMPLES
            spec = np.fft.rfft(rng.standard_normal(m))
            spec /= np.maximum(np.fft.rfftfreq(m), 1.0 / m) ** (k % 3 * 0.5)
            _NOISE_BANK.append(np.fft.irfft(spec, m).astype(np.float32))
    bank = _NOISE_BANK[rng.integers(len(_NOISE_BANK))]
    s = rng.integers(len(bank) - n + 1)
    x = bank[s : s + n]
    return x / (np.sqrt(np.mean(x**2)) + 1e-12)


def augment(x: np.ndarray, y: np.ndarray, sampler: Sampler, rng: np.random.Generator):
    from scipy.signal import butter, sosfilt

    x = x.copy()
    n = len(x)
    level = rms(x) + 1e-8
    # 1) mix in background at a random SNR (forest noise, rain, a noisy venue ...)
    p_mix = np.where(y == BG, 0.3, 0.6)
    for i in np.flatnonzero(rng.random(n) < p_mix):
        bg = sampler.window(int(SoundClass.BACKGROUND))
        bg_rms = rms(bg)
        if bg_rms < 1e-6:
            continue
        snr_db = rng.uniform(3, 25) if y[i] != BG else rng.uniform(-5, 15)
        x[i] += bg * (level[i] / bg_rms) * 10 ** (-snr_db / 20)
    # 2) distance: low-pass a quarter of the windows
    for i in np.flatnonzero(rng.random(n) < 0.25):
        sos = butter(2, rng.uniform(1500, 7000), fs=16000, output="sos")
        x[i] = sosfilt(sos, x[i]).astype(np.float32)
    # 3) sensor noise floor
    level = rms(x) + 1e-8
    for i in np.flatnonzero(rng.random(n) < 0.5):
        x[i] += colored_noise(rng, WINDOW_SAMPLES) * level[i] * 10 ** (-rng.uniform(15, 45) / 20)
    # 4) some background windows are pure noise, from near-silent to loud
    for i in np.flatnonzero((y == BG) & (rng.random(n) < 0.05)):
        x[i] = colored_noise(rng, WINDOW_SAMPLES) * 10 ** (rng.uniform(-75, -10) / 20)
    # 5) polarity
    x *= np.where(rng.random(n) < 0.5, -1.0, 1.0).astype(np.float32)[:, None]
    np.clip(x, -1.0, 1.0, out=x)

    feats = np.concatenate([log_mel(x[i : i + 4096]) for i in range(0, n, 4096)])
    # 6) SpecAugment-style masks (no time masks on gunshots: they'd erase the event)
    t_len, f_len = FEATURE_SHAPE
    for i in np.flatnonzero(rng.random(n) < 0.5):
        w = rng.integers(1, 7)
        f0 = rng.integers(0, f_len - w)
        feats[i, :, f0 : f0 + w] = 0.0
        if y[i] != GUN:
            w = rng.integers(1, 9)
            t0 = rng.integers(0, t_len - w)
            feats[i, t0 : t0 + w, :] = 0.0
    return feats


def build_model():
    import keras
    from keras import layers

    inp = keras.Input((*FEATURE_SHAPE, 1))
    x = inp
    for filters in (16, 32, 48):
        x = layers.Conv2D(filters, 3, padding="same", use_bias=False)(x)
        x = layers.BatchNormalization()(x)
        x = layers.ReLU()(x)
        x = layers.MaxPooling2D(2)(x)
    x = layers.Conv2D(64, 3, padding="same", use_bias=False)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.GlobalAveragePooling2D()(x)
    x = layers.Dropout(0.3)(x)
    out = layers.Dense(N_CLASSES, activation="softmax")(x)
    return keras.Model(inp, out)


def quick_metrics(probs: np.ndarray, y: np.ndarray) -> dict:
    pred = probs.argmax(1)
    rec = {CLASSES[c].name.lower(): float((pred[y == c] == c).mean()) for c in range(N_CLASSES)}
    bg_fp = float((pred[y == BG] != BG).mean())
    bal = float(np.mean(list(rec.values())))
    # model selection: chainsaw recall and background false alarms are the SPEC targets
    score = bal + rec["chainsaw"] - 4 * max(0.0, bg_fp - 0.05)
    return {"recall": rec, "bg_fp": bg_fp, "bal_acc": bal, "score": score}


def calibrate_bias(probs: np.ndarray, y: np.ndarray, max_fp: float = 0.04) -> np.ndarray:
    """Per-class log-probability offsets that maximise target recall (chainsaw counted twice)
    while keeping the background false-positive rate <= max_fp. Background offset stays 0."""
    logp = np.log(np.clip(probs, 1e-7, 1.0))
    grid = np.arange(-3.0, 3.01, 0.25)
    best, best_b = -1.0, np.zeros(N_CLASSES)
    bg = y == BG
    for b1 in grid:
        for b2 in grid:
            for b3 in grid:
                b = np.array([0.0, b1, b2, b3])
                pred = (logp + b).argmax(1)
                if (pred[bg] != BG).mean() > max_fp:
                    continue
                rec = [(pred[y == c] == c).mean() for c in range(1, N_CLASSES)]
                score = sum(rec) + rec[0] - 1e-3 * np.abs(b).sum()
                if score > best:
                    best, best_b = score, b
    return best_b


def to_tflite(model, representative: np.ndarray) -> bytes:
    import tensorflow as tf

    def rep():
        for r in representative:
            yield [r[None, ..., None].astype(np.float32)]

    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = rep
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    return conv.convert()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--val", action="store_true", help="hold out validation folds too")
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--version", type=int, default=1, help="model_version written to the record")
    p.add_argument("--out", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--no-eval", action="store_true")
    p.add_argument("--bias", type=Path, help=f"class offsets from a --val run ({BIAS_FILE})")
    args = p.parse_args()

    import keras
    import tensorflow as tf

    rng = np.random.default_rng(args.seed)
    keras.utils.set_random_seed(args.seed)
    tf.config.threading.set_inter_op_parallelism_threads(os.cpu_count() or 4)

    clips = load_clips()
    test = clips.split(TEST_FOLDS)
    val = clips.split(VAL_FOLDS) if args.val else np.zeros(len(clips), bool)
    train = ~test & ~val
    print(f"clips: train {train.sum()}, val {val.sum()}, test {test.sum()}")
    print("train sampler:")
    sampler = Sampler(clips, np.flatnonzero(train), rng)

    if args.val:
        xv, ov = eval_windows(clips, np.flatnonzero(val & (clips.label >= 0)))
        fv = log_mel(xv)[..., None]
        yv = np.array([CLASSES.index(SoundClass(int(clips.label[i]))) for i in ov])
        print(f"val windows: {len(yv)}")

    model = build_model()
    model.summary(print_fn=lambda s: print(s) if "params" in s.lower() else None)
    steps = args.epochs * sum(PER_EPOCH.values()) // 64
    sched = keras.optimizers.schedules.CosineDecay(2e-3, steps)
    model.compile(
        # weight EMA: single-epoch val metrics swing a lot on augmented data; averaging steadies
        # the final weights so no lucky checkpoint has to be picked
        optimizer=keras.optimizers.Adam(sched, use_ema=True, ema_momentum=0.995),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )

    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        x, y = sampler.batch(PER_EPOCH)
        f = augment(x, y, sampler, rng)
        t_aug = time.time() - t0
        hist = model.fit(f[..., None], y, batch_size=64, epochs=1, shuffle=True, verbose=0)
        line = (
            f"epoch {epoch:2d}  loss {hist.history['loss'][0]:.3f} "
            f"acc {hist.history['accuracy'][0]:.3f}  ({t_aug:.0f}s aug, "
            f"{time.time() - t0:.0f}s total)"
        )
        if args.val:
            m = quick_metrics(model.predict(fv, batch_size=512, verbose=0), yv)
            line += "  val " + " ".join(f"{k[:5]} {v:.2f}" for k, v in m["recall"].items())
            line += f"  bgFP {m['bg_fp']:.3f}  bal {m['bal_acc']:.3f}"
        print(line, flush=True)

    # Final (EMA) weights are always used; no checkpoint is picked. Class biases are tuned on
    # the validation folds only, then folded into the output layer of the final model.
    if args.val:
        offsets = calibrate_bias(model.predict(fv, batch_size=512, verbose=0), yv)
        BIAS_FILE.write_text(json.dumps(offsets.tolist()) + "\n")
        print(f"wrote {BIAS_FILE}: {offsets.tolist()}")
    if args.val:
        bias = offsets
    elif args.bias:
        bias = np.array(json.loads(args.bias.read_text()))
    else:
        bias = np.zeros(N_CLASSES)
    if args.val or args.bias:
        dense = model.layers[-1]
        w, b = dense.get_weights()
        dense.set_weights([w, b + bias.astype(b.dtype)])
        if args.val:
            m = quick_metrics(model.predict(fv, batch_size=512, verbose=0), yv)
            print("val after bias:", m)

    # representative data for int8 calibration: augmented *training* windows only
    xr, yr = sampler.batch({sc: 250 for sc in CLASSES})
    rep = augment(xr, yr, sampler, rng)
    tfl = to_tflite(model, rep)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(tfl)
    float_path = DATA_DIR / "cache" / "classifier_float.keras"
    model.save(float_path)
    meta = {
        "model_version": args.version,
        "classes": [c.name.lower() for c in CLASSES],
        "features": FEATURE_CONFIG,
        "sha256": hashlib.sha256(tfl).hexdigest(),
        "bytes": len(tfl),
        "trained": dt.date.today().isoformat(),
        "train_folds": "ESC-50 folds 1-4, UrbanSound8K folds 1-8"
        + (" minus validation folds ESC-50 4 / US8K 8" if args.val else ""),
        "test_folds": "ESC-50 fold 5, UrbanSound8K folds 9-10",
        "epochs": args.epochs,
        "class_bias": [round(float(v), 3) for v in bias],
        "seed": args.seed,
    }
    args.out.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {args.out} ({len(tfl)} bytes); float model at {float_path}")

    if not args.no_eval:
        from training.evaluate import main as evaluate

        evaluate(["--model", str(args.out), "--float-model", str(float_path)])


if __name__ == "__main__":
    main()
