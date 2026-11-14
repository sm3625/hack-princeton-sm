"""Regression coverage for storm safety, adaptive nodes and reproducible evaluation."""
import numpy as np
from agent_fakes import Harness

from agent.commander import P_THREAT
from agent.incidents import Wake
from common.contracts import Prediction
from common.contracts import SoundClass as S
from eval.lockstep import LockstepSim
from node.runtime import MAX_THRESHOLD, AdaptiveThresholds
from sim.clock import SimClock
from sim.scenario import ScenarioRunner, load_scenario
from training.train import calibrate_thresholds


def test_gunshots_keep_priority_in_rain(tmp_path):
    h = Harness(tmp_path)
    h.sim.rain = {n.sector for n in h.world.nodes}
    h.hear("El Bajo", 1, "03:00:00", S.GUNSHOT, .95)
    inc, = h.commander.incidents.all()
    assert h.commander.priority(inc, Wake(inc.id)) == P_THREAT


def test_adaptive_baselines_are_bounded_and_node_local():
    noisy, quiet = AdaptiveThresholds(), AdaptiveThresholds()
    for _ in range(20000):
        noisy.observe(Prediction(S.CHAINSAW, .99))
    assert .8 < noisy.threshold(S.CHAINSAW) <= MAX_THRESHOLD
    assert quiet.threshold(S.CHAINSAW) == .8
    restored = AdaptiveThresholds()
    restored.load_json(noisy.to_json())
    assert abs(restored.threshold(S.CHAINSAW) - noisy.threshold(S.CHAINSAW)) < 1e-6


def test_calibration_uses_class_specific_false_alarms():
    probs = np.array([[.1, .7, .1, .1], [.1, .1, .7, .1],
                      [.01, .97, .01, .01], [.01, .01, .97, .01],
                      [.01, .01, .01, .97]])
    thresholds = calibrate_thresholds(probs, np.array([0, 0, 1, 2, 3]))
    assert thresholds['chainsaw'] > .7
    assert thresholds['gunshot'] > .7
    assert thresholds['vehicle'] == .7  # the floor


def test_lockstep_flight_delivers_intervening_events_without_wall_time():
    sim = LockstepSim(publish=lambda _: None)
    scenario = load_scenario("night1", world=sim.world)
    sim.clock = SimClock(start=scenario.start, paused=True)
    heard = []
    sim.runner = ScenarioRunner(sim.world, lambda node, audio: heard.append(node),
                                scenario=scenario, clock=sim.clock, weather=sim.weather_model)
    # Replace audio decoding: this test checks ordering, full replay checks classification.
    sim.runner.audio_for = lambda event, index: np.zeros(1)
    sim.timeline = sim.runner.timeline()
    sim.cursor = 0
    sim.advance_to(scenario.end)
    assert heard == [e.node_id for e in scenario.events]
    assert sim.clock.now_f() == scenario.end
    assert sim.clock.paused


def test_adaptation_never_lowers_a_calibrated_threshold():
    assert AdaptiveThresholds({S.CHAINSAW: .99}).threshold(S.CHAINSAW) == .99


def test_http_guard_handles_malformed_origins():
    import asyncio

    from station.app import LocalGuard

    async def check(origin):
        sent = []
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
        async def send(message):
            sent.append(message)
        await LocalGuard(app)({"type": "http", "method": "POST", "scheme": "http",
                               "headers": [(b"host", b"localhost:8000"),
                                           (b"origin", origin)]}, None, send)
        return sent[0]["status"]
    assert asyncio.run(check(b"https://evil.example")) == 403
    assert asyncio.run(check(b"http://[invalid")) == 403
    assert asyncio.run(check(b"http://localhost:8000")) == 200


def test_heads_up_arrives_before_any_model_run(tmp_path):
    h = Harness(tmp_path)
    h.hear("Arroyo Recodo", 1, "02:14:05", S.CHAINSAW, .95)
    h.hear("Paso Musgo", 1, "02:14:09", S.CHAINSAW, .90)
    assert len(h.of_type("heads_up")) == 1
    assert not h.commander._stats
    h.hear("Arroyo Recodo", 2, "02:14:12", S.CHAINSAW, .95)
    assert len(h.of_type("heads_up")) == 1


def test_prefilter_dismisses_repeat_rain_without_model(tmp_path):
    h = Harness(tmp_path)
    h.sim.rain = {"B3"}
    h.hear("El Bajo", 1, "03:00:00", S.CHAINSAW, .82)
    h.tick("03:00:15")
    inc, = h.commander.incidents.all()
    runs = h.commander._stats[inc.id]["runs"]
    h.hear("El Bajo", 2, "03:06:00", S.CHAINSAW, .82)
    h.tick("03:06:15")
    assert h.commander._stats[inc.id]["runs"] == runs
    assert h.commander._stats[inc.id]["prefiltered"] == 1


def test_lockstep_blocking_flight_reaches_arrival():
    sim = LockstepSim(publish=lambda _: None)
    sim.clock.pause()
    sim.clock.set_time(1000)
    sim.timeline, sim.cursor = [], 0
    route = sim.plan_route(sim.world.base_x_m + 50, sim.world.base_y_m)
    assert route.feasible
    before = sim.clock.now_f()
    assert isinstance(sim.fly(route), str)
    assert sim.clock.now_f() > before
    assert sim.clock.paused
