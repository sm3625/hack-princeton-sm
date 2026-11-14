import hashlib
import io
import wave

import numpy as np
import pytest

from common.contracts import SAMPLE_RATE, SoundClass
from node.record import unpack, verify
from node.runtime import CLIP_S, LimitError, NodeLimits, NodeRuntime, clip_path
from tests.pipeline_fakes import (
    BG,
    CREEK_BEND,
    GUN,
    SAW,
    SAW_WEAK,
    T0,
    Clock,
    ScriptedClassifier,
    keys_for,
)

KEY = keys_for(5)[5]
HALF = SAMPLE_RATE // 2


class Rig:
    def __init__(self, tmp_path, script, **kw):
        self.clock = Clock()
        self.sent: list[bytes] = []
        self.classifier = ScriptedClassifier(script)
        self.rt = NodeRuntime(
            CREEK_BEND, self.classifier, KEY, tmp_path, self.sent.append, self.clock, **kw
        )

    def run(self, seconds: float, chunk_s: float = 0.5, value: float = 0.0):
        """Feed `seconds` of audio in real-time-sized chunks, advancing the clock."""
        out = []
        n = int(SAMPLE_RATE * chunk_s)
        for _ in range(round(seconds / chunk_s)):
            self.clock.t += chunk_s
            out += self.rt.feed(np.full(n, value, dtype=np.float32))
        return out


def test_windows_are_1s_with_half_second_hop(tmp_path):
    rig = Rig(tmp_path, [])
    rig.run(5.0)
    assert rig.rt.counters["windows"] == 9  # 1 s windows starting every 0.5 s over 5 s
    assert all(len(w) == SAMPLE_RATE for w in rig.classifier.windows)


def test_debounce_fires_once_per_burst(tmp_path):
    rig = Rig(tmp_path, [BG] * 4 + [SAW] * 10 + [BG] * 6)
    events = rig.run(10.0)
    assert len(events) == 1 and len(rig.sent) == 1
    assert events[0].sound is SoundClass.CHAINSAW
    assert abs(events[0].confidence - 0.92) < 1 / 255
    assert verify(rig.sent[0], KEY.verify_key)
    assert unpack(rig.sent[0]) == events[0]


def test_two_of_five_does_not_fire_but_three_scattered_does(tmp_path):
    two = Rig(tmp_path / "a", [SAW, BG, BG, SAW, BG, BG, SAW, BG, BG])
    assert two.run(6.0) == []
    three = Rig(tmp_path / "b", [SAW, BG, SAW, BG, SAW])
    assert len(three.run(4.0)) == 1


def test_low_confidence_and_background_never_fire(tmp_path):
    assert Rig(tmp_path / "a", [SAW_WEAK] * 20).run(11.0) == []
    assert Rig(tmp_path / "b", [BG] * 20).run(11.0) == []


def test_suppression_holds_for_30_seconds_then_releases(tmp_path):
    # burst at ~2 s, second burst at ~17 s (suppressed), third at ~37 s (fires again)
    script = [SAW] * 6 + [BG] * 24 + [SAW] * 6 + [BG] * 34 + [SAW] * 6
    rig = Rig(tmp_path, script)
    events = rig.run(40.0)
    assert [e.seq for e in events] == [1, 2]
    assert events[1].timestamp - events[0].timestamp >= 30


def test_suppression_is_per_class(tmp_path):
    rig = Rig(tmp_path, [SAW] * 4 + [GUN] * 4)
    assert [e.sound for e in rig.run(5.0)] == [SoundClass.CHAINSAW, SoundClass.GUNSHOT]


def test_continuous_sound_refires_every_30_seconds(tmp_path):
    rig = Rig(tmp_path, [SAW] * 200)
    assert len(rig.run(60.0)) == 2  # at ~2 s and ~32 s


def test_sim_style_whole_clip_feeds_use_clock_for_suppression(tmp_path):
    clock = Clock()
    sent: list[bytes] = []
    rt = NodeRuntime(CREEK_BEND, ScriptedClassifier([SAW] * 300), KEY, tmp_path, sent.append, clock)
    clip = np.zeros(SAMPLE_RATE * 5, dtype=np.float32)
    assert len(rt.feed(clip)) == 1  # one 5 s clip at T0
    clock.t += 10
    rt.reset()
    assert rt.feed(clip) == []  # 10 s later: suppressed
    clock.t += 1000
    rt.reset()
    later = rt.feed(clip)
    assert len(later) == 1 and later[0].timestamp >= T0 + 1000


def test_chunk_size_does_not_change_results(tmp_path):
    script = [BG] * 5 + [SAW] * 5 + [BG] * 9
    big = Rig(tmp_path / "big", list(script))
    big.clock.t += 10
    one_call = big.rt.feed(np.zeros(SAMPLE_RATE * 10, dtype=np.float32))
    small = Rig(tmp_path / "small", list(script))
    many_calls = small.run(10.0, chunk_s=0.1)
    assert big.rt.counters["windows"] == small.rt.counters["windows"] == 19
    assert [(e.seq, e.timestamp) for e in one_call] == [(e.seq, e.timestamp) for e in many_calls]


def test_clip_is_the_5s_before_the_trigger_and_hash_matches(tmp_path):
    clock = Clock(T0 + 100)
    sent: list[bytes] = []
    script = [BG] * 12 + [SAW] * 3  # fires on window 14, which ends at 8.0 s
    rt = NodeRuntime(CREEK_BEND, ScriptedClassifier(script), KEY, tmp_path, sent.append, clock)
    audio = (np.arange(SAMPLE_RATE * 12) % 1000 / 1000 - 0.5).astype(np.float32)
    (event,) = rt.feed(audio)

    path = clip_path(tmp_path, CREEK_BEND.id, event.seq)
    assert path == tmp_path / "5" / "1.wav"
    data = path.read_bytes()
    assert hashlib.sha256(data).digest()[:16].hex() == event.clip_hash
    assert unpack(sent[0]).clip_hash == event.clip_hash
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SAMPLE_RATE)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    end = 8 * SAMPLE_RATE
    expected = (audio[end - int(CLIP_S * SAMPLE_RATE) : end] * 32767).astype("<i2")
    assert len(pcm) == CLIP_S * SAMPLE_RATE
    assert np.array_equal(pcm, expected)
    # window ending at 8.0 s of a 12 s chunk that ends "now": 4 s ago
    assert event.timestamp == T0 + 100 - 4


def test_seq_persists_across_restarts(tmp_path):
    first = Rig(tmp_path, [SAW] * 3)
    assert [e.seq for e in first.run(3.0)] == [1]
    again = Rig(tmp_path, [SAW] * 3)
    assert again.rt.seq == 1
    assert [e.seq for e in again.run(3.0)] == [2]
    assert (tmp_path / "5" / "2.wav").exists()


def test_events_per_hour_budget(tmp_path):
    # four bursts 32 s apart (past suppression each time); only two fit the budget
    burst = [SAW] * 4 + [BG] * 60
    rig = Rig(tmp_path, burst * 4, limits=NodeLimits(max_events_per_hour=2))
    events = rig.run(4 * 32.0)
    assert len(events) == 2
    assert rig.rt.counters["budget_dropped"] == 2
    assert rig.rt.stats()["events_last_hour"] == 2
    rig.clock.t += 3600
    rig.classifier.script = [SAW] * 4
    assert len(rig.run(3.0)) == 1


def test_model_and_record_limits_are_enforced(tmp_path):
    big_model = ScriptedClassifier([], model_bytes=150_000)
    with pytest.raises(LimitError, match="model"):
        NodeRuntime(CREEK_BEND, big_model, KEY, tmp_path, print, Clock())
    with pytest.raises(LimitError, match="record"):
        NodeRuntime(
            CREEK_BEND,
            ScriptedClassifier([]),
            KEY,
            tmp_path,
            print,
            Clock(),
            limits=NodeLimits(max_record_bytes=51),
        )


def test_stats_report_limits(tmp_path):
    stats = Rig(tmp_path, []).rt.stats()
    assert stats["node"] == "Arroyo Recodo"
    assert (stats["model_bytes"], stats["max_model_bytes"]) == (60_000, 100_000)
    assert (stats["record_bytes"], stats["max_record_bytes"]) == (108, 222)
    assert stats["max_events_per_hour"] == 60


def test_send_failure_is_counted_not_raised(tmp_path):
    def broken(_: bytes) -> None:
        raise OSError("radio down")

    rt = NodeRuntime(CREEK_BEND, ScriptedClassifier([SAW] * 3), KEY, tmp_path, broken, Clock())
    assert len(rt.feed(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))) == 1
    assert rt.counters["send_errors"] == 1


def test_accepts_int16_and_column_audio(tmp_path):
    rig = Rig(tmp_path, [])
    rig.rt.feed(np.zeros((SAMPLE_RATE, 1), dtype=np.float32))
    rig.rt.feed(np.full(SAMPLE_RATE, 16384, dtype=np.int16))
    assert rig.rt.counters["windows"] == 3
    assert np.isclose(rig.classifier.windows[-1].max(), 0.5)


def test_single_gunshot_spanning_two_windows_fires(tmp_path):
    # One shot is only confident in the two overlapping windows that contain it.
    rig = Rig(tmp_path, [BG] * 4 + [GUN] * 2 + [BG] * 6)
    events = rig.run(7.0)
    assert [e.sound for e in events] == [SoundClass.GUNSHOT]


def test_one_lone_gunshot_window_does_not_fire(tmp_path):
    rig = Rig(tmp_path, [BG] * 4 + [GUN] + [BG] * 10)
    assert rig.run(8.0) == []


def test_chainsaw_still_needs_three_of_five(tmp_path):
    rig = Rig(tmp_path, [BG] * 4 + [SAW] * 2 + [BG] * 8)
    assert rig.run(8.0) == []
