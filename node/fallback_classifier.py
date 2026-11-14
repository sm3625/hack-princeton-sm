"""ScenarioHintClassifier: a FALLBACK, not the real model.

Hand-tuned spectral rules that separate the synthetic placeholder audio from sim/audio.py
(chainsaw buzz, gunshot impulses, low vehicle rumble, birds/rain background). It exists only so
the whole system can run before models/classifier.tflite is trained. It is NOT evidence of
classifier accuracy and will not work on real recordings. The station logs which one is in use
(see station/integration.py: pick_classifier). Replaced automatically by node.classifier's
TFLiteClassifier once models/classifier.tflite exists.
"""

from __future__ import annotations

import numpy as np

from common.contracts import SAMPLE_RATE, Prediction, SoundClass

LABEL = "ScenarioHintClassifier (fallback heuristic, synthetic audio only)"
_FRAME = SAMPLE_RATE // 50  # 20 ms


class ScenarioHintClassifier:
    model_version = 0  # 0 = fallback heuristic; real models start at 1
    model_bytes = 0

    def classify(self, window: np.ndarray) -> Prediction:
        w = np.asarray(window, dtype=np.float32)
        rms = float(np.sqrt(np.mean(w * w))) + 1e-9
        if rms < 0.01:
            return Prediction(SoundClass.BACKGROUND, 0.95)
        crest = float(np.abs(w).max()) / rms
        spectrum = np.abs(np.fft.rfft(w * np.hanning(len(w)))) ** 2
        freqs = np.fft.rfftfreq(len(w), 1 / SAMPLE_RATE)
        total = float(spectrum.sum()) + 1e-12
        low = float(spectrum[freqs < 250].sum()) / total
        mid = float(spectrum[(freqs >= 250) & (freqs < 2000)].sum()) / total
        high = 1.0 - low - mid
        frames = w[: len(w) // _FRAME * _FRAME].reshape(-1, _FRAME)
        energy = np.sqrt(np.mean(frames * frames, axis=1)) + 1e-9
        spikiness = float(energy.max() / np.median(energy))  # impulses stand out of the median

        if crest >= 6.0 and (spikiness >= 8.0 or high >= 0.45):
            if mid >= 0.85:
                return Prediction(SoundClass.AXE, 0.85)
            return Prediction(SoundClass.GUNSHOT, 0.9)
        if rms >= 0.1 and crest < 4.5:
            if low >= 0.7:
                return Prediction(SoundClass.VEHICLE, 0.88)
            if high <= 0.25 and mid >= 0.3:
                return Prediction(SoundClass.CHAINSAW, 0.9)
        return Prediction(SoundClass.BACKGROUND, 0.9)
