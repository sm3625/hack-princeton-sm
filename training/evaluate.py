"""Evaluate the shipped int8 model on held-out folds through node.classifier, write eval.md.

uv run python -m training.evaluate
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np

from common.contracts import SoundClass
from node.classifier import DEFAULT_MODEL, FEATURE_SHAPE, TFLiteClassifier, log_mel, rms
from training.dataset import CLASSES, EXCLUDED, TEST_FOLDS, eval_windows, load_clips

BG = CLASSES.index(SoundClass.BACKGROUND)
NAMES = [c.name.lower() for c in CLASSES]
ALARM_CONF = 0.8  # SPEC 3.2 debounce threshold
TARGETS = {"model_kb": 100, "ms": 20, "chainsaw_recall": 0.90, "bg_fp": 0.05}


def confusion(y: np.ndarray, pred: np.ndarray) -> np.ndarray:
    cm = np.zeros((len(CLASSES), len(CLASSES)), dtype=int)
    np.add.at(cm, (y, pred), 1)
    return cm


def prf(cm: np.ndarray) -> list[tuple[float, float, int]]:
    out = []
    for c in range(len(cm)):
        tp = cm[c, c]
        out.append((tp / max(cm[:, c].sum(), 1), tp / max(cm[c].sum(), 1), int(cm[c].sum())))
    return out


def debounce_fires(probs: np.ndarray, cls: int, k: int = 3, n: int = 5) -> bool:
    """SPEC 3.2: same non-background class >= 0.8 in >= 3 of the last 5 windows."""
    hit = (probs.argmax(1) == cls) & (probs.max(1) >= ALARM_CONF)
    return any(hit[max(0, i - n + 1) : i + 1].sum() >= k for i in range(len(hit)))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def md_cm(cm: np.ndarray) -> str:
    head = "| true \\ predicted | " + " | ".join(NAMES) + " |"
    sep = "|---" * (len(NAMES) + 1) + "|"
    rows = [
        f"| **{NAMES[i]}** | " + " | ".join(str(v) for v in cm[i]) + " |" for i in range(len(cm))
    ]
    return "\n".join([head, sep, *rows])


def md_prf(cm: np.ndarray) -> str:
    lines = ["| class | precision | recall | windows |", "|---|---|---|---|"]
    for name, (p, r, n) in zip(NAMES, prf(cm)):
        lines.append(f"| {name} | {p:.3f} | {r:.3f} | {n} |")
    return "\n".join(lines)


def ok(flag: bool) -> str:
    return "met" if flag else "**NOT met**"


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--float-model", help="Keras model to compare against (quantization loss)")
    ap.add_argument("--out", type=Path, default=Path(DEFAULT_MODEL).parent / "eval.md")
    args = ap.parse_args(argv)

    clf = TFLiteClassifier(args.model)
    clf.load()
    clips = load_clips()
    test = clips.split(TEST_FOLDS)
    idx = np.flatnonzero(test & (clips.label >= 0))
    X, owner = eval_windows(clips, idx)
    y = np.array([CLASSES.index(SoundClass(int(clips.label[i]))) for i in owner])
    print(f"evaluating {len(y)} held-out windows from {len(idx)} clips")
    P = np.stack([clf.probabilities(w) for w in X])
    pred = P.argmax(1)
    conf = P.max(1)

    # window level, argmax (strictest: any non-background argmax counts as a false alarm)
    cm = confusion(y, pred)
    bg = y == BG
    bg_fp = float((pred[bg] != BG).mean())
    cs = CLASSES.index(SoundClass.CHAINSAW)
    cs_recall = float((pred[y == cs] == cs).mean())

    # window level at the runtime threshold: below 0.8 is treated as background
    pred80 = np.where(conf >= ALARM_CONF, pred, BG)
    cm80 = confusion(y, pred80)
    bg_fp80 = float((pred80[bg] != BG).mean())

    # clip level: mean probability over the clip's windows
    clip_ids = np.unique(owner)
    clip_y = np.array([CLASSES.index(SoundClass(int(clips.label[i]))) for i in clip_ids])
    clip_pred = np.array([P[owner == i].mean(0).argmax() for i in clip_ids])
    cm_clip = confusion(clip_y, clip_pred)

    # SPEC 3.2 debounce over each clip's windows
    deb = {}
    for c, name in enumerate(NAMES):
        sel = [i for i, cy in zip(clip_ids, clip_y) if cy == c]
        if c == BG:
            fired = sum(
                any(debounce_fires(P[owner == i], k) for k in range(len(CLASSES)) if k != BG)
                for i in sel
            )
        else:
            fired = sum(debounce_fires(P[owner == i], c) for i in sel)
        deb[name] = (fired, len(sel))

    # background false alarms by source category
    cats = clips.category[owner]
    by_cat = []
    for cat in sorted(set(cats[bg])):
        m = bg & (cats == cat)
        by_cat.append((float((pred[m] != BG).mean()), int(m.sum()), cat))
    by_cat.sort(reverse=True)

    # excluded (never trained on) sounds
    ex_idx = np.flatnonzero(test & (clips.label == EXCLUDED))
    Xe, oe = eval_windows(clips, ex_idx)
    excluded = {}
    if len(Xe):
        Pe = np.stack([clf.probabilities(w) for w in Xe])
        for cat in sorted(set(clips.category[oe])):
            m = clips.category[oe] == cat
            cnt = Counter(NAMES[k] for k in Pe[m].argmax(1))
            excluded[cat] = (int(m.sum()), cnt)

    # quantization loss vs float Keras model
    float_acc = None
    if args.float_model:
        import keras

        fm = keras.models.load_model(args.float_model)
        live = rms(X) >= 1e-4
        Pf = np.zeros_like(P)
        Pf[~live, BG] = 1.0
        Pf[live] = fm.predict(log_mel(X[live])[..., None], batch_size=512, verbose=0)
        float_acc = (float((Pf.argmax(1) == y).mean()), float((pred == y).mean()))
        float_agree = float((Pf.argmax(1) == pred).mean())

    # latency: full classify() (features + int8 model), single thread
    rng = np.random.default_rng(0)
    sample = X[rng.choice(len(X), 50, replace=False)]
    for w in sample[:5]:
        clf.classify(w)
    times = []
    for w in np.concatenate([sample] * 10):
        t0 = time.perf_counter()
        clf.classify(w)
        times.append((time.perf_counter() - t0) * 1000)
    ms_med, ms_p95 = float(np.median(times)), float(np.percentile(times, 95))
    t_feat = []
    for w in sample:
        t0 = time.perf_counter()
        log_mel(w)
        t_feat.append((time.perf_counter() - t0) * 1000)

    meta_path = Path(args.model).with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    class_bias = meta.get("class_bias", "none")
    kb = clf.model_bytes / 1024
    lo, hi = wilson(int((pred[y == cs] == cs).sum()), int((y == cs).sum()))
    bglo, bghi = wilson(int((pred[bg] != BG).sum()), int(bg.sum()))
    n_cs_clips = int((clip_y == cs).sum())
    n_test = {name: (int((y == c).sum()), int((clip_y == c).sum())) for c, name in enumerate(NAMES)}

    lines = [
        "# Sound classifier evaluation",
        "",
        (
            f"Model `{Path(args.model).name}` v{clf.model_version}: int8 TFLite, "
            f"{clf.model_bytes:,} bytes ({kb:.1f} KB). Input {FEATURE_SHAPE[0]}x{FEATURE_SHAPE[1]} "
            "log-mel (16 kHz mono, 1 s window, 32 ms FFT, 20 ms hop, 40 mel bands)."
        ),
        "",
        (
            "All numbers below are on **held-out source recordings** (ESC-50 fold 5, "
            "UrbanSound8K folds 9-10), scored through `node.classifier.TFLiteClassifier`, i.e. the "
            "exact int8 model and feature code the node runs. Windows are 1 s with 0.5 s hop."
        ),
        "",
        "## Targets (SPEC 3.1)",
        "",
        "| target | result | status |",
        "|---|---|---|",
        f"| model size <= {TARGETS['model_kb']} KB | {kb:.1f} KB | {ok(kb <= TARGETS['model_kb'])} |",
        (
            f"| inference <= {TARGETS['ms']} ms on laptop | median {ms_med:.2f} ms, p95 {ms_p95:.2f} ms "
            f"| {ok(ms_med <= TARGETS['ms'])} |"
        ),
        (
            f"| chainsaw recall >= 90% | {cs_recall:.1%} of {int((y == cs).sum())} windows "
            f"(95% CI {lo:.1%}-{hi:.1%}) | {ok(cs_recall >= TARGETS['chainsaw_recall'])} |"
        ),
        (
            f"| background false-positive rate <= 5% | {bg_fp:.1%} of {int(bg.sum())} windows "
            f"(95% CI {bglo:.1%}-{bghi:.1%}) | {ok(bg_fp <= TARGETS['bg_fp'])} |"
        ),
        "",
        (
            "Recall and false-positive rate here are per window at plain argmax (no confidence "
            "threshold), the strictest reading. At the runtime's 0.8 threshold the background "
            f"false-positive rate is {bg_fp80:.1%}. Latency is the whole `classify()` call "
            f"(feature extraction {np.median(t_feat):.2f} ms + int8 model), one thread, "
            "median of 500 calls."
        ),
        "",
        "## Per-class, per window (argmax)",
        "",
        md_prf(cm),
        "",
        md_cm(cm),
        "",
        f"## Per-class, per window, confidence >= {ALARM_CONF} (else background)",
        "",
        md_prf(cm80),
        "",
        md_cm(cm80),
        "",
        "## Per clip (mean probability over the clip's windows)",
        "",
        md_prf(cm_clip).replace("windows |", "clips |"),
        "",
        md_cm(cm_clip),
        "",
        "## SPEC 3.2 debounce (>= 3 of 5 windows at >= 0.8), per clip",
        "",
        "| class | clips where an event fires | rate |",
        "|---|---|---|",
    ]
    for name, (f, n) in deb.items():
        what = "any false event" if name == "background" else "correct event"
        lines.append(f"| {name} ({what}) | {f} / {n} | {f / max(n, 1):.1%} |")
    lines += [
        "",
        (
            "Gunshots are single impulses: one shot only spans about two overlapping windows, and "
            "many UrbanSound8K gunshot slices are shorter than 1 s, so a 3-of-5 rule rarely fires "
            "for them even when every window is classified correctly. The node runtime needs a "
            "separate rule for gunshots (e.g. one window at >= 0.9)."
        ),
        "",
        "## Background false alarms by source category (argmax, per window)",
        "",
        "| category | false-alarm rate | windows |",
        "|---|---|---|",
        *[f"| {cat} | {r:.1%} | {n} |" for r, n, cat in by_cat],
        "",
        "## Sounds left out of training",
        "",
        (
            "Ambiguous human-activity sounds are neither a target nor background, so they were "
            "excluded from training and from the numbers above. What the model calls them:"
        ),
        "",
        "| category | windows | predictions |",
        "|---|---|---|",
        *[
            f"| {cat} | {n} | " + ", ".join(f"{k} {v}" for k, v in cnt.most_common()) + " |"
            for cat, (n, cnt) in excluded.items()
        ],
        "",
    ]
    if float_acc:
        lines += [
            "## Quantization",
            "",
            (
                f"Window accuracy: float32 Keras {float_acc[0]:.1%}, int8 TFLite {float_acc[1]:.1%}; "
                f"the two agree on {float_agree:.1%} of windows."
            ),
            "",
        ]
    lines += [
        "## Test set",
        "",
        "| class | windows | clips |",
        "|---|---|---|",
        *[f"| {name} | {w} | {c} |" for name, (w, c) in n_test.items()],
        "",
        (
            f"Only {n_cs_clips} chainsaw clips exist in the held-out fold (ESC-50 has 40 in total), so "
            "the chainsaw recall has a wide confidence interval; windows from one clip are "
            "correlated, so the real interval is wider than the per-window Wilson interval shown."
        ),
        "",
        "## Data, labels and splits",
        "",
        (
            "- **ESC-50** (K. J. Piczak, 2015), github.com/karolpiczak/ESC-50. CC BY-NC 3.0 "
            "(ESC-10 subset CC BY). 2000 x 5 s clips, 50 classes, 5 folds."
        ),
        (
            "- **UrbanSound8K** (J. Salamon, C. Jacoby, J. P. Bello, 2014), Zenodo record 1203745. "
            "CC BY-NC 4.0. 8732 clips <= 4 s, 10 classes, 10 folds."
        ),
        "- Both licenses are non-commercial; the trained model is derived from this data.",
        (
            "- Labels: chainsaw = ESC-50 chainsaw. gunshot = US8K gun_shot. vehicle = ESC-50 engine "
            "+ US8K engine_idling. background = every other ESC-50 class (rain, wind, birds, "
            "insects, crickets, frogs, thunderstorm, laughing, crying baby, coughing, footsteps, "
            "helicopter, airplane, train, vacuum cleaner, ...) + US8K air_conditioner, "
            "children_playing, dog_bark, siren, street_music. Excluded: ESC-50 hand_saw, fireworks, "
            "car_horn; US8K car_horn, drilling, jackhammer."
        ),
        (
            "- Splits are by the datasets' own folds, which keep all clips cut from one source "
            "recording together. Train: ESC-50 folds 1-4, US8K folds 1-8. Test: ESC-50 fold 5, "
            "US8K folds 9-10."
        ),
        (
            "- Target-class windows must contain the event (a 20 ms frame within 15 dB of the "
            "clip's loudest), so the silence ESC-50 pads clips with isn't labelled chainsaw. "
            "Windows of digital silence (< -80 dBFS RMS) are skipped in evaluation because the "
            "classifier short-circuits them to background."
        ),
        (
            "- Training balances classes by sampling (background 3 : 1 : 1 : 1) and, within a "
            "class, by source category; augmentation mixes in background at 3-25 dB SNR, changes "
            "engine speed 0.8-1.25x (chainsaw/vehicle), low-pass filters (distance), adds a noise "
            "floor, and applies frequency/time masks. Adam with weight EMA; the final weights "
            "are used (no checkpoint picked on test or validation)."
        ),
        (
            "- Class bias: a separate run that also held out ESC-50 fold 4 / US8K fold 8 tuned "
            "per-class logit offsets (best chainsaw recall with background false positives "
            "<= 4% on those validation folds). The shipped model adds the same offsets "
            f"{class_bias} to its output layer. They were not tuned on the test folds. On test "
            "they pushed false positives above target (above all air conditioner, washing "
            "machine, insects and wind called chainsaw), so chainsaw precision is low."
        ),
        "",
    ]
    args.out.write_text("\n".join(lines))
    print("\n".join(lines[8:14]))
    print(f"wrote {args.out}")
    return {"bg_fp": bg_fp, "chainsaw_recall": cs_recall, "ms": ms_med, "kb": kb}


if __name__ == "__main__":
    main()
