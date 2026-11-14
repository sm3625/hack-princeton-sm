import numpy as np
import pytest

from common.contracts import SAMPLE_RATE, Classifier, SoundClass
from node.fallback_classifier import ScenarioHintClassifier
from sim.audio import synthesize

CLF = ScenarioHintClassifier()
EXPECT = {
    "chainsaw": SoundClass.CHAINSAW,
    "vehicle": SoundClass.VEHICLE,
    "background": SoundClass.BACKGROUND,
}


def windows(audio):
    for i in range(0, len(audio) - SAMPLE_RATE + 1, SAMPLE_RATE // 2):
        yield audio[i : i + SAMPLE_RATE]


def test_is_a_classifier():
    assert isinstance(CLF, Classifier)


@pytest.mark.parametrize("rain", [False, True])
@pytest.mark.parametrize("sound", ["chainsaw", "vehicle", "background"])
def test_steady_sounds_classified_in_most_windows(sound, rain):
    right = total = 0
    for seed in range(6):
        for w in windows(synthesize(sound, 8, seed=seed, rain=rain)):
            total += 1
            right += CLF.classify(w).sound is EXPECT[sound]
    assert right / total >= 0.9


@pytest.mark.parametrize("rain", [False, True])
def test_gunshot_clip_has_two_confident_windows(rain):
    for seed in range(6):
        hits = [
            p for w in windows(synthesize("gunshot", 6, seed=seed, rain=rain))
            if (p := CLF.classify(w)).sound is SoundClass.GUNSHOT and p.confidence >= 0.8
        ]
        assert len(hits) >= 2


def test_silence_is_background():
    assert CLF.classify(np.zeros(SAMPLE_RATE, dtype=np.float32)).sound is SoundClass.BACKGROUND
