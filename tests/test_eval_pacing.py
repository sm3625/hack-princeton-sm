"""Evaluation clock control must not strand tools that wait on simulated time."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from eval import run
from eval.run import pace_clock
from sim.drone import DEFAULT_OBSERVATION
from sim.engine import SimEngine


class Station:
    def __init__(self, engine=None, status=200):
        self.engine = engine
        self.status = status
        self.calls = []

    def request(self, method, path, body):
        self.calls.append((method, path, body))
        if self.engine and self.status == 200:
            self.engine.set_paused(body["paused"])
        return self.status, {}


@pytest.mark.parametrize("running,busy,flying,paused,waits,expected", [
    (True, True, False, False, True, True),
    (True, False, False, True, True, False),
    (True, True, True, True, True, False),
    (True, True, False, False, False, None),
    (True, True, False, True, True, None),
    (False, True, True, True, True, False),
    (False, True, True, True, False, False),
    (False, False, False, False, True, True),
])
def test_clock_holds_thinking_but_not_flight(running, busy, flying, paused, waits, expected):
    station = Station()
    sim = {"running": running, "paused": paused, "drone": {"flying": flying}}
    pace_clock(station, sim, busy, waits)
    assert station.calls == ([] if expected is None else [
        ("POST", "/api/sim/pause", {"paused": expected}),
    ])


def test_failed_clock_control_stops_evaluation():
    station = Station(status=503)
    sim = {"running": True, "paused": False, "drone": {"flying": False}}
    with pytest.raises(RuntimeError, match="could not pause simulation: 503"):
        pace_clock(station, sim, busy=True, sim_waits=True)


def test_finished_scenario_waits_for_a_silent_busy_agent(monkeypatch, tmp_path):
    class ReplayStation:
        def __init__(self, *args):
            self.proc = SimpleNamespace(poll=lambda: None)
            self.last_activity = 0
            self.messages = []
            self.agent_fs = tmp_path
            self.busy_reads = 0
            self.stopped = False

        def wait_ready(self):
            pass

        def follow(self):
            pass

        def stop(self):
            self.stopped = True

        def request(self, method, path, body=None):
            if path == "/api/sim/scenario":
                return 200, {"feed": "nodes"}
            if path.startswith("/api/detections"):
                return 200, {"detections": []}
            if path == "/api/sim/state":
                return 200, {"running": False, "paused": True, "drone": {"flying": False}}
            assert path == "/api/incidents"
            self.busy_reads += 1
            return 200, {"incidents": [], "busy": self.busy_reads == 1}

    station = ReplayStation()
    monkeypatch.setattr(run, "Station", lambda *args: station)
    monkeypatch.setattr(run.time, "sleep", lambda _: None)
    monkeypatch.setattr(run.time, "monotonic", lambda: 100)
    result, _ = run.run_night("night1", "mock", log=lambda *_: None)
    # First poll: finished scenario + long silence, but model still working.
    # Second poll: genuinely idle. Last read collects incident results.
    assert station.busy_reads == 3
    assert station.stopped and result["partial"] is None


@pytest.mark.parametrize("running", [True, False])
def test_busy_worker_flight_completes_after_clock_is_held(monkeypatch, running):
    engine = SimEngine(publish=lambda _: None)
    engine.clock.set_speed(60)
    engine.clock.pause()
    waiting = threading.Event()
    block_until = engine.clock.block_until

    def mark_wait(*args):
        waiting.set()
        block_until(*args)

    monkeypatch.setattr(engine.clock, "block_until", mark_wait)
    route = engine.plan_route(engine.world.base_x_m + 50, engine.world.base_y_m)
    assert route.feasible
    with ThreadPoolExecutor(max_workers=1) as pool:
        flight = pool.submit(engine.fly, route)
        try:
            assert waiting.wait(2), "flight should reach its clock wait"
            assert not flight.done()
            sim = engine.state()
            sim["running"] = running
            pace_clock(Station(engine), sim, busy=True, sim_waits=True)
            assert not engine.clock.paused
            assert flight.result(timeout=3) == DEFAULT_OBSERVATION
        finally:
            engine.drone.reset()  # release the worker even when an assertion fails
            engine.clock.resume()
