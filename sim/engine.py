"""SimEngine: the simulated physical world. Implements common.contracts.Sim.

    from sim.engine import get_engine
    sim = get_engine()            # process-wide instance shared with the station routes
    sim.plan_route(x, y); sim.fly(route); sim.weather("B3"); sim.now(); sim.drone_status()

Detections come from the pipeline once it is plugged in with `sim.set_feed(feed)` (a
callable (node_id, audio) -> None, e.g. NodeRuntime.feed). Until then scenarios run through
the STUB in sim/demo_feed.py. Every bus message the engine publishes is thread-safe, so the
agent may call fly() from a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from common.contracts import DroneStatus, Route, Weather, World
from sim import audio as audiolib
from sim import world as worldmap
from sim.clock import SimClock
from sim.demo_feed import DemoFeed
from sim.drone import Drone, Observation
from sim.scenario import (
    SCENARIO_DIR,
    Scenario,
    ScenarioRunner,
    list_scenarios,
    load_scenario,
    offset_of,
)
from sim.weather import WeatherModel

log = logging.getLogger(__name__)

Feed = Callable[[int, np.ndarray], None]
_TICK_S = 0.25
_CLOCK_EVERY_S = 1.0
_IDLE_DRONE_EVERY_S = 5.0


def _bus_publish(msg: dict) -> None:
    from station.bus import bus

    bus.publish(msg)


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class SimEngine:
    def __init__(
        self,
        world: World | None = None,
        publish: Callable[[dict], None] | None = None,
        scenario_dir: Path = SCENARIO_DIR,
        data_dir: Path = audiolib.DATA_DIR,
    ) -> None:
        self.world = world or worldmap.load()
        self.scenario_dir = scenario_dir
        self.data_dir = data_dir
        self._target = publish or _bus_publish
        self.clock = SimClock()
        self.weather_model = WeatherModel(self.world, publish=self.publish)
        self.drone = Drone(self.world, self.clock)
        self.demo = DemoFeed(self.world, self.clock.now, self.publish)
        self.scenario: Scenario | None = None
        self.runner: ScenarioRunner | None = None
        self._feed: Feed | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._ticker: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._clock_dirty = True
        self.clock.on_change(self._mark_clock)

    # ---------- plumbing ----------

    def publish(self, msg: dict) -> None:
        """Publish on the bus from any thread."""
        loop = self._loop
        off_loop = threading.get_ident() != self._loop_thread
        if loop is not None and off_loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._target, msg)
        else:
            self._target(msg)

    def attach(self) -> None:
        """Bind to the running event loop and start the ticker (idempotent)."""
        loop = asyncio.get_running_loop()
        if loop is self._loop and self._ticker is not None and not self._ticker.done():
            return
        self._loop = loop
        self._loop_thread = threading.get_ident()
        self._ticker = loop.create_task(self._tick())
        if self._run_task is not None and self._run_task.get_loop() is not loop:
            self._run_task = None  # belonged to a loop that is gone

    async def shutdown(self) -> None:
        for task in (self._run_task, self._ticker):
            if task is not None and not task.done() and task.get_loop() is self._loop:
                task.cancel()
        self._run_task = self._ticker = None
        self._loop = None
        self._loop_thread = None

    def set_feed(self, feed: Feed | None) -> None:
        """Plug in the real detection pipeline (applies from the next scenario start)."""
        self._feed = feed

    @property
    def feed_kind(self) -> str:
        return "nodes" if self._feed is not None else "stub"

    def _mark_clock(self) -> None:
        self._clock_dirty = True

    # ---------- contracts.Sim ----------

    def now(self) -> int:
        return self.clock.now()

    def weather(self, sector: str) -> Weather:
        return self.weather_model.get(sector)

    def plan_route(self, target_x_m: float, target_y_m: float) -> Route:
        return self.drone.plan_route(target_x_m, target_y_m)

    def drone_status(self) -> DroneStatus:
        return self.drone.status()

    def fly(self, route: Route, *, wait: bool = True) -> str:
        """Fly to route's target and return what the camera saw there.

        Blocks (in wall time, following the sim clock) until the drone reaches the target when
        called from a worker thread. Called on an event-loop thread it returns at once, since
        blocking there would freeze the station; use fly_async from async code.
        """
        flight, why = self.drone.launch(route)
        if flight is None:
            return f"Not flown: {why}"
        self.publish(self.drone.message())
        if wait:
            if _in_event_loop():
                log.warning("fly() called on the event loop; not waiting for arrival")
            else:
                self.clock.block_until(flight.arrive, lambda: self.drone.cancelled(flight))
        if self.drone.cancelled(flight):
            return "Not completed: the simulation was reset"
        return flight.observation

    async def fly_async(self, route: Route) -> str:
        flight, why = self.drone.launch(route)
        if flight is None:
            return f"Not flown: {why}"
        self.publish(self.drone.message())
        await self.clock.sleep_until(flight.arrive)
        if self.drone.cancelled(flight):
            return "Not completed: the simulation was reset"
        return flight.observation

    # ---------- control ----------

    async def start_scenario(
        self, name: str, speed: float | None = None, from_time: str | None = None
    ) -> dict:
        """Start (or restart) a scenario. from_time ('02:10') skips the night up to then."""
        self.attach()
        scenario = load_scenario(name, self.scenario_dir, self.world)
        skip = offset_of(scenario, from_time) if from_time else 0.0
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        self.clock.set_paused(True)
        self.clock.set_time(scenario.start + skip)
        self.clock.set_speed(speed if speed is not None else scenario.speed)
        self.weather_model.reset()
        self.drone.reset(100.0)
        self.drone.set_observations(
            [
                Observation(
                    sector=s.sector,
                    text=s.text,
                    start=None if s.start is None else scenario.start + s.start,
                    end=None if s.end is None else scenario.start + s.end,
                )
                for s in scenario.sightings
            ]
        )
        self.demo.reset()
        self.scenario = scenario
        self.runner = ScenarioRunner(
            self.world,
            self._feed,
            scenario=scenario,
            clock=self.clock,
            weather=self.weather_model,
            on_event=None if self._feed is not None else self.demo.on_event,
            data_dir=self.data_dir,
            skip_to=skip,
        )
        if self._feed is None:
            log.info("scenario %s: STUB detections (no NodeRuntime plugged in)", name)
        self.publish(
            {
                "type": "scenario",
                "name": scenario.name,
                "title": scenario.title,
                "start": int(scenario.start),
                "end": int(scenario.end),
                "feed": self.feed_kind,
            }
        )
        self.clock.resume()
        self._run_task = asyncio.get_running_loop().create_task(self._run(self.runner))
        self.publish(self.drone.message())
        self.publish(self.clock_message())
        return self.state()

    async def _run(self, runner: ScenarioRunner) -> None:
        await runner.run()
        if runner is self.runner:
            self.clock.pause()
            self.publish(self.clock_message())
            log.info("scenario %s finished (%d events)", runner.scenario.name, runner.fired)

    def set_paused(self, paused: bool | None = None) -> dict:
        self.clock.set_paused(not self.clock.paused if paused is None else bool(paused))
        self.publish(self.clock_message())
        return self.state()

    def set_speed(self, speed: float) -> dict:
        self.clock.set_speed(speed)
        self.publish(self.clock_message())
        return self.state()

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def clock_message(self) -> dict:
        return {
            "type": "clock",
            "sim_time": self.clock.now(),
            "speed": self.clock.speed,
            "paused": self.clock.paused,
            "running": self.running,
            "scenario": self.scenario.name if self.scenario else None,
            "feed": self.feed_kind,
        }

    def state(self) -> dict:
        """Snapshot for GET /api/sim/state (a page that just loaded has missed the bus)."""
        sc = self.scenario
        clock = {k: v for k, v in self.clock_message().items() if k != "type"}
        return {
            **clock,
            "title": sc.title if sc else None,
            "start": int(sc.start) if sc else None,
            "end": int(sc.end) if sc else None,
            "drone": self.drone.message(),
            "weather": self.weather_model.snapshot(),
            "scenarios": list_scenarios(self.scenario_dir, self.world),
        }

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        last_clock = last_drone = 0.0
        was_flying = False
        while True:
            now = loop.time()
            flying = self.drone.status().flying
            if flying or was_flying or now - last_drone >= _IDLE_DRONE_EVERY_S:
                self.publish(self.drone.message())
                last_drone = now
            was_flying = flying
            if self._clock_dirty or now - last_clock >= _CLOCK_EVERY_S:
                self._clock_dirty = False
                self.publish(self.clock_message())
                last_clock = now
            await asyncio.sleep(_TICK_S)


_engine: SimEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> SimEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = SimEngine()
        return _engine


def reset_engine(engine: SimEngine | None = None) -> SimEngine | None:
    """Replace the shared engine (tests)."""
    global _engine
    with _engine_lock:
        _engine = engine
        return _engine
