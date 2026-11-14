import asyncio
import math
import time
from collections import Counter, defaultdict

import numpy as np
import pytest
import soundfile as sf

from common.contracts import SAMPLE_RATE
from sim import world as worldmap
from sim.audio import resolve_clip, synthesize
from sim.clock import SimClock
from sim.scenario import (
    ScenarioError,
    ScenarioRunner,
    UnknownScenario,
    list_scenarios,
    load_scenario,
)
from sim.weather import WeatherModel

WORLD = worldmap.load()


class FakeClock:
    """Jumps straight to each requested time."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def now(self) -> int:
        return int(self.t)

    def now_f(self) -> float:
        return self.t

    async def sleep_until(self, t: float) -> None:
        self.t = max(self.t, t)


def write_scenario(tmp_path, name, text):
    (tmp_path / f"{name}.yaml").write_text(text)
    return tmp_path


# ---------- the shipped scenarios ----------


def test_night1_shape():
    s = load_scenario("night1")
    assert 35 <= len(s.events) <= 45
    assert s.duration_s == 8 * 3600
    assert set(Counter(e.incident for e in s.events if e.incident)) == {
        "logging-e4",
        "poaching-g2",
    }
    assert all(e.label == "real" for e in s.events if e.incident)

    logging = [e for e in s.events if e.incident == "logging-e4" and e.sound == "chainsaw"]
    assert 2 <= len({e.node for e in logging}) <= 3
    first_three = sorted(logging, key=lambda e: e.at)[:3]
    assert first_three[-1].at - first_three[0].at <= 60

    poaching = sorted([e for e in s.events if e.incident == "poaching-g2"], key=lambda e: e.at)
    assert poaching[0].sound == "gunshot" and poaching[-1].sound == "vehicle"

    truck = [e for e in s.events if e.source == "truck" and e.label == "benign"]
    assert len({e.node for e in truck}) == 4
    assert max(e.at for e in truck) - min(e.at for e in truck) <= 30

    assert any(e.source == "branch_fall" for e in s.events)

    rain = [e for e in s.events if e.source == "rain"]
    assert Counter(e.node for e in rain).most_common(1)[0][0] == "El Bajo"
    assert sum(e.node == "El Bajo" for e in rain) > len(rain) / 2


def test_rain_alarms_happen_while_it_rains_there():
    for name in ("night1", "night2"):
        s = load_scenario(name)
        model = WeatherModel(WORLD)
        changes = list(s.weather)
        for e in sorted(s.events, key=lambda e: e.at):
            while changes and changes[0].at <= e.at:
                model.apply(changes.pop(0))
            if e.source == "rain":
                assert model.get(WORLD.node(e.node_id).sector).rain, (name, e)


def test_scenarios_respect_the_30s_debounce():
    for name in ("night1", "night2"):
        last: dict = defaultdict(lambda: -math.inf)
        for e in load_scenario(name).events:
            key = (e.node, e.sound)
            assert e.at - last[key] >= 30, (name, e)
            last[key] = e.at


def test_night2_keeps_the_noisy_node_pattern():
    n1, n2 = load_scenario("night1"), load_scenario("night2")
    noisy = {e.node for e in n1.events if e.source == "rain"}
    assert {e.node for e in n2.events if e.source == "rain"} <= noisy
    assert any(e.label == "real" for e in n2.events)


def test_sightings_and_listing():
    s = load_scenario("night1")
    assert any(x.sector == "E4" and "vehicle" in x.text for x in s.sightings)
    names = [x["name"] for x in list_scenarios()]
    assert "night1" in names and "night2" in names and "world" not in names


# ---------- parsing ----------


def test_clock_times_wrap_past_midnight_and_unquoted_times_are_safe(tmp_path):
    d = write_scenario(
        tmp_path,
        "t",
        """
start: "22:00"
end: "06:00"
events:
  - {at: 02:14, node: El Bajo, sound: chainsaw}
  - {at: "22:30:15", node: Arroyo Recodo, sound: vehicle}
  - {at: "+90", node: Arroyo Recodo, sound: gunshot}
""",
    )
    s = load_scenario("t", d)
    assert [e.at for e in s.events] == [90, 30 * 60 + 15, 4 * 3600 + 14 * 60]


@pytest.mark.parametrize(
    "body",
    [
        "events: [{at: '23:00', node: Nowhere, sound: chainsaw}]",
        "events: [{at: '23:00', node: El Bajo, sound: banjo}]",
        "events: [{at: '23:00', node: El Bajo, sound: chainsaw, confidence: 3}]",
        "events: [{at: 'soon', node: El Bajo, sound: chainsaw}]",
        "weather: [{at: '23:00', sectors: [Z9], rain: true}]",
    ],
)
def test_bad_scenarios_are_rejected(tmp_path, body):
    d = write_scenario(tmp_path, "bad", 'start: "22:00"\nend: "06:00"\n' + body)
    with pytest.raises(ScenarioError):
        load_scenario("bad", d)


@pytest.mark.parametrize("name", ["world", "../scenarios/night1", "nope", "Night1", ""])
def test_unknown_names_are_rejected(name):
    with pytest.raises(UnknownScenario):
        load_scenario(name)


# ---------- audio ----------


def test_synthetic_audio_is_16k_float32_and_deterministic():
    a = synthesize("chainsaw", 2.5, seed=3)
    assert a.dtype == np.float32 and len(a) == int(2.5 * SAMPLE_RATE)
    assert np.abs(a).max() <= 1.0
    assert np.array_equal(a, synthesize("chainsaw", 2.5, seed=3))
    assert not np.array_equal(a, synthesize("vehicle", 2.5, seed=3))


def test_clip_paths_cannot_escape_data_dir(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "secret.wav").write_bytes(b"x")
    assert resolve_clip("../secret.wav", tmp_path / "data") is None
    assert resolve_clip(None, tmp_path / "data") is None


# ---------- runner timing ----------

TINY = """
start: "22:00"
end: "22:10"
weather:
  - {at: "22:01", sectors: "A1:C6", rain: true}
events:
  - {at: "22:01", node: El Bajo, sound: chainsaw, duration: 2, clip: real.wav}
  - {at: "22:00:30", node: Arroyo Recodo, sound: vehicle, duration: 1.5}
  - {at: "22:05", node: Paso Musgo, sound: gunshot, duration: 3, clip: missing.wav}
"""


def test_runner_fires_in_order_at_sim_times_with_audio(tmp_path):
    d = write_scenario(tmp_path, "tiny", TINY)
    data = tmp_path / "data"
    data.mkdir()
    sf.write(data / "real.wav", np.full(8000, 0.25, dtype=np.float32), 8000)  # 1 s at 8 kHz
    s = load_scenario("tiny", d)
    clock = FakeClock(s.start)
    weather = WeatherModel(WORLD)
    fed = []

    def feed(node_id, audio):
        rain = weather.get(WORLD.node(node_id).sector).rain
        fed.append((clock.now_f() - s.start, WORLD.node(node_id).name, len(audio), rain, audio))

    runner = ScenarioRunner(WORLD, feed, scenario=s, clock=clock, weather=weather, data_dir=data)
    asyncio.run(runner.run())

    assert [(t, n) for t, n, *_ in fed] == [
        (30, "Arroyo Recodo"),
        (60, "El Bajo"),
        (300, "Paso Musgo"),
    ]
    assert [length for _, _, length, _, _ in fed] == [24000, 32000, 48000]
    assert fed[1][3] is True  # weather applied before the event at the same instant
    assert np.allclose(fed[1][4][2000:3000], 0.25, atol=0.01)  # real clip, resampled
    assert runner.synthetic == 2 and runner.fired == 3
    assert runner.finished and clock.now_f() == s.end


def test_runner_on_event_without_audio(tmp_path):
    s = load_scenario("tiny", write_scenario(tmp_path, "tiny", TINY))
    seen = []
    runner = ScenarioRunner(WORLD, None, scenario=s, clock=FakeClock(s.start), on_event=seen.append)
    asyncio.run(runner.run())
    assert [e.node for e in seen] == ["Arroyo Recodo", "El Bajo", "Paso Musgo"]
    assert runner.synthetic == 0


def test_runner_in_real_time_at_60x(tmp_path):
    d = write_scenario(
        tmp_path,
        "quick",
        """
start: "22:00"
end: "22:00:40"
events:
  - {at: "22:00:12", node: El Bajo, sound: chainsaw}
  - {at: "22:00:30", node: Arroyo Recodo, sound: chainsaw}
""",
    )
    s = load_scenario("quick", d)
    clock = SimClock(start=s.start, speed=60)
    stamps = []
    runner = ScenarioRunner(
        WORLD,
        None,
        scenario=s,
        clock=clock,
        on_event=lambda e: stamps.append(clock.now_f() - s.start),
    )
    t0 = time.monotonic()
    asyncio.run(runner.run())
    wall = time.monotonic() - t0
    assert stamps == [pytest.approx(12, abs=1), pytest.approx(30, abs=1)]
    assert 0.5 < wall < 2.0  # 40 sim-s at 60x is ~0.67 s


def test_runner_can_start_part_way_through_the_night(tmp_path):
    s = load_scenario("tiny", write_scenario(tmp_path, "tiny", TINY))
    weather = WeatherModel(WORLD)
    seen = []
    runner = ScenarioRunner(
        WORLD,
        None,
        scenario=s,
        clock=FakeClock(s.start + 120),
        weather=weather,
        on_event=seen.append,
        skip_to=120,
    )
    asyncio.run(runner.run())
    assert [e.node for e in seen] == ["Paso Musgo"]  # the 22:00:30 and 22:01 events are skipped
    assert weather.get("B3").rain  # but the 22:01 rain still applies
