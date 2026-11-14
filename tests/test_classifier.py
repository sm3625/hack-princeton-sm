import json
import subprocess
import sys
import time

import numpy as np
import pytest

from common.contracts import ROOT, Classifier, Prediction, SoundClass
from node.classifier import (
    DEFAULT_MODEL,
    FEATURE_CONFIG,
    FEATURE_SHAPE,
    WINDOW_SAMPLES,
    TFLiteClassifier,
    log_mel,
)

DATA = ROOT / "data"


@pytest.fixture(scope="module")
def clf():
    c = TFLiteClassifier()
    c.load()
    return c


def noise(level_dbfs: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(WINDOW_SAMPLES) * 10 ** (level_dbfs / 20)).astype(np.float32)


def test_model_file_at_most_100kb():
    assert DEFAULT_MODEL.stat().st_size <= 100 * 1024


def test_metadata_matches_feature_code():
    meta = json.loads(DEFAULT_MODEL.with_suffix(".json").read_text())
    assert meta["features"] == FEATURE_CONFIG
    assert meta["classes"] == ["background", "chainsaw", "gunshot", "vehicle"]
    assert 0 < meta["model_version"] < 2**16  # 2-byte field in the record


def test_implements_contract(clf):
    assert isinstance(clf, Classifier)
    assert clf.model_bytes == DEFAULT_MODEL.stat().st_size
    pred = clf.classify(noise(-30))
    assert isinstance(pred, Prediction)
    assert isinstance(pred.sound, SoundClass)
    assert 0.0 <= pred.confidence <= 1.0


def test_probabilities_sum_to_one(clf):
    p = clf.probabilities(noise(-20))
    assert p.shape == (4,)
    assert abs(p.sum() - 1.0) < 0.03  # int8 output, 1/256 steps


def test_inference_under_20ms(clf):
    windows = [noise(-25, seed=i) for i in range(50)]
    clf.classify(windows[0])
    times = []
    for w in windows:
        t0 = time.perf_counter()
        clf.classify(w)
        times.append(time.perf_counter() - t0)
    assert np.median(times) * 1000 <= 20


def test_silence_is_background(clf):
    for w in (np.zeros(WINDOW_SAMPLES, np.float32), noise(-90)):
        pred = clf.classify(w)
        assert pred.sound == SoundClass.BACKGROUND
        assert pred.confidence == 1.0


def test_quiet_mic_noise_is_background(clf):
    # a quiet mic's self-noise goes through the model, not the silence shortcut
    assert clf.classify(noise(-60)).sound == SoundClass.BACKGROUND


@pytest.mark.parametrize(
    "bad",
    [
        np.zeros(WINDOW_SAMPLES - 1, np.float32),
        np.zeros(WINDOW_SAMPLES + 1, np.float32),
        np.zeros((1, WINDOW_SAMPLES), np.float32),
        np.zeros((WINDOW_SAMPLES, 2), np.float32),
        np.zeros(0, np.float32),
    ],
)
def test_window_shape_enforced(clf, bad):
    with pytest.raises(ValueError):
        clf.classify(bad)


def test_non_finite_rejected(clf):
    w = noise(-30)
    w[100] = np.nan
    with pytest.raises(ValueError):
        clf.classify(w)


def test_features_shape_and_gain_invariance():
    w = noise(-30)
    f = log_mel(w)
    assert f.shape == FEATURE_SHAPE
    assert f.dtype == np.float32
    np.testing.assert_allclose(log_mel(w * 8), f, atol=1e-4)
    batch = log_mel(np.stack([w, w]))
    assert batch.shape == (2, *FEATURE_SHAPE)


def test_lazy_load_and_no_tensorflow_at_inference():
    code = (
        "import sys, numpy as np\n"
        "from node.classifier import TFLiteClassifier\n"
        "c = TFLiteClassifier()\n"
        "assert c._interp is None\n"
        "c.classify(np.zeros(16000, np.float32))\n"
        "c.classify(np.random.default_rng(0).standard_normal(16000).astype(np.float32) * 0.1)\n"
        "assert c._interp is not None\n"
        "assert 'tensorflow' not in sys.modules, 'tensorflow imported'\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


# ---------- held-out clips (only when the datasets are downloaded) ----------

needs_data = pytest.mark.skipif(
    not (DATA / "ESC-50-master" / "meta" / "esc50.csv").exists()
    or not (DATA / "UrbanSound8K" / "metadata" / "UrbanSound8K.csv").exists(),
    reason="datasets not downloaded (uv run python training/download.py)",
)


def _held_out(dataset: str, category: str, folds: tuple[int, ...], limit: int):
    from training.dataset import _manifest

    rows = [
        r
        for r in _manifest()
        if r["dataset"] == dataset and r["category"] == category and r["fold"] in folds
    ]
    return sorted(rows, key=lambda r: r["name"])[:limit]


def _clip_prediction(clf, path: str, label: SoundClass) -> SoundClass:
    """Mean probability over the clip's event windows, as in models/eval.md."""
    import librosa

    from training.dataset import pad_clip, window_starts

    y, _ = librosa.load(path, sr=16000, mono=True)
    y = pad_clip(y)
    starts = window_starts(y, int(label), 8000)
    probs = np.mean([clf.probabilities(y[s : s + WINDOW_SAMPLES]) for s in starts], axis=0)
    return clf.classes[int(np.argmax(probs))]


# (dataset, category, held-out folds, clips, expected, minimum correct)
HELD_OUT = [
    ("esc50", "chainsaw", (5,), 8, SoundClass.CHAINSAW, 7),
    ("us8k", "gun_shot", (10,), 8, SoundClass.GUNSHOT, 6),
    ("us8k", "engine_idling", (10,), 8, SoundClass.VEHICLE, 6),
    ("esc50", "rain", (5,), 4, SoundClass.BACKGROUND, 3),
    ("esc50", "chirping_birds", (5,), 4, SoundClass.BACKGROUND, 3),
    ("esc50", "crickets", (5,), 4, SoundClass.BACKGROUND, 3),
    # known weak spot: 18% of held-out wind windows are false alarms (models/eval.md)
    ("esc50", "wind", (5,), 4, SoundClass.BACKGROUND, 2),
    ("us8k", "street_music", (10,), 4, SoundClass.BACKGROUND, 3),
    ("us8k", "children_playing", (10,), 4, SoundClass.BACKGROUND, 3),
]


@needs_data
@pytest.mark.parametrize("dataset,category,folds,n,expected,min_ok", HELD_OUT)
def test_held_out_clips(clf, dataset, category, folds, n, expected, min_ok):
    rows = _held_out(dataset, category, folds, n)
    assert len(rows) == n
    got = [_clip_prediction(clf, r["path"], expected) for r in rows]
    correct = sum(g == expected for g in got)
    assert correct >= min_ok, f"{category}: {[g.name for g in got]}"
