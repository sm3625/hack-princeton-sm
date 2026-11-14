"""Disposable commander state for interactive simulator runs.

Each run owns its files/checkpoints and a revocable view of the simulator. An old
model request may finish late, but cannot publish or operate the next run.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from agent.commander import Commander
from agent.llm import ROLES, get_chat_model
from station.bus import bus


class RunSim:
    def __init__(self, engine):
        self.engine = engine
        self.active = True
        self.ended_at = engine.now()
        self.lock = threading.RLock()

    def retire(self):
        with self.lock:
            self.ended_at = self.engine.now()
            self.active = False

    def now(self):
        return self.engine.now() if self.active else self.ended_at

    def fly(self, route, *, wait=True):
        with self.lock:
            if not self.active:
                raise RuntimeError("Demo run has ended")
            drone = self.engine._drone_for(route)
            if drone is None:
                return "Not flown: no drone at this site"
            flight, why = drone.launch(route)
            if flight is None:
                return f"Not flown: {why}"
            self.engine.publish(drone.message())
        if wait:
            self.engine.clock.block_until(flight.arrive, lambda: not self.active or drone.cancelled(flight))
        if not self.active or drone.cancelled(flight):
            return "Not completed: the simulation was reset"
        return flight.observation

    def __getattr__(self, name):
        method = getattr(self.engine, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            with self.lock:
                if not self.active:
                    raise RuntimeError("Demo run has ended")
                return method(*args, **kwargs)
        return call


class DemoSession:
    def __init__(self, app, configure, hooks):
        self.app = app
        self.configure = configure
        self.hooks = hooks
        self.current = None
        self.sim = None
        self.root = None
        self.lock = asyncio.Lock()

    def new_commander(self):
        root = Path(tempfile.mkdtemp(prefix="forest-demo-"))
        sim = RunSim(self.app.state.sim)
        loop = asyncio.get_running_loop()

        def publish(message):
            def deliver():
                if sim.active:
                    bus.publish(message)
            loop.call_soon_threadsafe(deliver)

        commander = Commander(
            store=self.app.state.event_store, sim=sim,
            verify=self.app.state.verify_record,
            fs_root=root / "files", db_path=root / "state.sqlite",
            models={role: get_chat_model(role, self.app.state.settings) for role in ROLES},
            publish=publish, deployment=self.app.state.deployment,
        )
        self.current, self.sim = commander, sim
        self.root = root
        self.configure(commander)
        if self.hooks is not None:
            self.hooks.append(commander.on_verified)
        commander.start(loop)

    def retire(self):
        if self.current is not None:
            self.sim.retire()
            if self.hooks is not None and self.current.on_verified in self.hooks:
                self.hooks.remove(self.current.on_verified)
            self.current.stop()
            commander, root = self.current, self.root
            self.current = None

            def cleanup():
                # Wait off-loop: a late provider response owns its old checkpoint
                # until it exits, but its revoked publisher cannot reach the UI.
                if commander._executor:
                    commander._executor.shutdown(wait=True, cancel_futures=True)
                if commander._ticker:
                    commander._ticker.join()
                commander._conn.close()
                commander.repo._conn.close()
                shutil.rmtree(root, ignore_errors=True)

            threading.Thread(target=cleanup, daemon=True, name="demo-cleanup").start()

    async def reset(self):
        async with self.lock:
            self.retire()
            engine = self.app.state.sim
            for drone in engine.drones:
                drone.reset(100.0)
            pipeline = getattr(self.app.state, "pipeline", None)
            if pipeline is not None:
                await pipeline.drain()
            self.app.state.event_store.clear()
            self.app.state.receiver.counts.clear()
            self.new_commander()
            engine.run_id = uuid.uuid4().hex
            bus.publish({"type": "run_reset", "run_id": engine.run_id})
