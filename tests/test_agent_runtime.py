"""Threads, retries and the bus: how the commander behaves inside the running station."""

import asyncio
import threading
import time

from agent_fakes import Harness
from langchain_core.language_models.chat_models import BaseChatModel

from agent.commander import BusPublisher
from agent.llm import ROLES
from agent.mock import MockChatModel
from common.contracts import SoundClass as S
from station.bus import Bus


class Down(BaseChatModel):
    """A model server that is not running (e.g. Ollama not started)."""

    @property
    def _llm_type(self) -> str:
        return "down"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        raise ConnectionError("connection refused")


class Wall:
    t = 0.0

    def __call__(self):
        return self.t


def test_failed_wake_ups_are_retried_with_backoff_then_dropped(tmp_path):
    models = {r: MockChatModel(role=r) for r in ROLES}
    models["commander"] = Down()
    h = Harness(tmp_path, models=models)
    wall = Wall()
    h.commander.wakes.wall = wall
    h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    for i in range(1, 5):
        wall.t += 1000
        h.tick("02:20:00")
        wall.t += 1  # inside the backoff window: nothing runs
        h.tick("02:20:00")
    steps = [m["summary"] for m in h.of_type("agent_step")]
    assert steps.count("agent unavailable (ConnectionError); retrying") == 3
    assert steps[-1] == "agent unavailable (ConnectionError); gave up on this wake-up"
    assert h.commander.wakes.pop_due(10**10) == []
    (inc,) = h.commander.incidents.all()
    assert inc.status == "open" and inc.dispatches == []


def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_background_mode_runs_on_worker_threads(tmp_path):
    h = Harness(tmp_path, background=True)
    c = h.commander
    c.start(interval_s=0.02)
    try:
        h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
        h.hear("Mossy Gap", 17, "02:14:09", S.CHAINSAW, 0.84)
        h.sim.t += 20
        assert wait_for(lambda: h.pending())
        (a,) = h.pending()
        c.decide(a.id, "approve")
        assert wait_for(lambda: c.incidents.all()[0].status == "dispatched")
        assert wait_for(lambda: c.wakes.pending_check(c.incidents.all()[0].id) is not None)
    finally:
        c.stop()


def test_bus_publisher_hops_onto_the_event_loop():
    async def main():
        bus = Bus()
        q = bus.subscribe()
        pub = BusPublisher(bus)
        pub.bind(asyncio.get_running_loop())
        t = threading.Thread(target=pub, args=({"type": "clock", "sim_time": 1},))
        t.start()
        t.join()
        pub({"type": "clock", "sim_time": 2})  # from the loop thread itself
        return [await asyncio.wait_for(q.get(), 2), await asyncio.wait_for(q.get(), 2)]

    got = asyncio.run(main())
    assert sorted(m["sim_time"] for m in got) == [1, 2]
