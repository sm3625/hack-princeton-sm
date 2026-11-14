"""Deterministic event/agent scheduling through the real acoustic pipeline.

Inference consumes no simulated time. During a blocking drone tool, the scheduler
advances to arrival and delivers intervening weather/audio in order. No worker,
wall clock, HTTP polling, fallback classifier, or lossy transport drives the replay.
Real LLM responses may still vary; deterministic scheduling is not deterministic inference.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from nacl.signing import SigningKey

from agent.commander import Commander
from agent.evidence import Ed25519Verifier
from agent.incidents import WakeQueue
from agent.llm import HAIKU_MODEL, ROLES, get_chat_model
from common.contracts import load_world
from eval import metrics
from node.classifier import DEFAULT_MODEL, TFLiteClassifier
from node.runtime import NodeRuntime
from sim.drone import Observation
from sim.engine import SimEngine
from sim.scenario import ScenarioRunner, load_scenario
from station.config import load_settings
from station.receiver import Receiver
from station.store import SqliteEventStore


class LockstepSim(SimEngine):
    def fly(self, route, *, wait=True):
        drone = self._drone_for(route)
        if drone is None:
            return "Not flown: no drone at this site"
        flight, reason = drone.launch(route)
        if flight is None:
            return f"Not flown: {reason}"
        if wait:
            self.advance_to(flight.arrive)
        return flight.observation

    def advance_to(self, target):
        """Weather precedes audio at equal times; each clip is fully received before the next."""
        while self.cursor < len(self.timeline) and self.timeline[self.cursor][0] <= target:
            stamp, kind, item = self.timeline[self.cursor]
            index = self.cursor
            self.cursor += 1
            self.clock.set_time(stamp)
            if kind == "weather":
                self.weather_model.apply(item)
            else:
                # Unlike ScenarioRunner.fire, fail loudly rather than score a dropped clip.
                self.runner.feed(item.node_id, self.runner.audio_for(item, index))
                self.runner.fired += 1
        self.clock.set_time(max(target, self.clock.now_f()))


def run_lockstep(scenario_name, mode="mock", *, memory_from=None, max_wall_min=30, log=print):
    world = load_world()
    scenario = load_scenario(scenario_name, world=world)
    folder = Path(tempfile.mkdtemp(prefix=f"lockstep-{scenario_name}-{mode}-"))
    agent_fs = folder / "agent_fs"
    if memory_from:
        shutil.copytree(memory_from / "memories", agent_fs / "memories")
    sim = LockstepSim(world=world, publish=lambda _: None)
    sim.clock.pause()
    sim.clock.set_time(scenario.start)
    for drone in sim.drones:
        drone.reset(100)
        drone.set_observations([
            Observation(s.sector, s.text,
                        None if s.start is None else scenario.start + s.start,
                        None if s.end is None else scenario.start + s.end)
            for s in scenario.sightings
        ])
    store = SqliteEventStore(folder / "events.sqlite", clock=sim.clock.now_f)
    # Disposable evaluation keys only, never the deployment's private keys.
    keys = {n.id: SigningKey.generate() for n in world.nodes}
    public = {n: k.verify_key for n, k in keys.items()}
    receiver = Receiver(store, public, world, publish=lambda _: None)
    settings = replace(load_settings(), llm_mode=mode)
    classifier = TFLiteClassifier(DEFAULT_MODEL)
    commander = Commander(
        world=world, store=store, sim=sim, fs_root=agent_fs,
        db_path=folder / "agent.sqlite", background=False,
        models={r: get_chat_model(r, settings) for r in ROLES},
        verify=Ed25519Verifier({n: bytes(k) for n, k in public.items()}),
        publish=lambda _: None, clips_root=folder / "clips",
    )
    # Debounce and retries must use simulation time too, including during slow inference.
    commander.wakes = WakeQueue(wall=sim.clock.now_f)
    commander.services.wakes = commander.wakes
    receiver.on_verified.append(commander.on_verified)
    runtimes = {n.id: NodeRuntime(n, classifier, keys[n.id], folder / "clips",
                                 receiver.handle, sim.clock.now_f) for n in world.nodes}

    def feed(node_id, audio):
        runtimes[node_id].reset()
        runtimes[node_id].feed(audio)

    sim.runner = ScenarioRunner(world, feed, scenario=scenario, clock=sim.clock,
                                weather=sim.weather_model)
    sim.timeline = sim.runner.timeline()
    sim.cursor = 0
    began = time.monotonic()
    partial = None
    try:
        # Fixed one-second ticks: model latency cannot change batching or weather.
        while sim.clock.now_f() <= scenario.end or commander.busy() or any(
            d.status().flying for d in sim.drones
        ):
            if time.monotonic() - began > max_wall_min * 60:
                partial = "wall-time cap reached between agent calls"
                break
            sim.advance_to(sim.clock.now_f())
            commander.tick()
            for approval in list(commander.approvals.values()):
                if approval.status != "pending":
                    continue
                rows = _rows(store, world)
                truth = {(r["node"], r["seq"]): metrics.group_of(metrics.match_event(
                    scenario.events, scenario.start, r["node"], r["timestamp"])) for r in rows}
                decision = "approve" if metrics.incident_groups(approval.public(), truth) else "reject"
                commander.decide(approval.id, decision)
            sim.advance_to(sim.clock.now_f() + 1)
        rows = _rows(store, world)
        views = [commander.incident_view(i.id) for i in commander.incidents.all()]
        result = {
            "scenario": scenario_name, "mode": mode, "memory": "carried" if memory_from else "fresh",
            "model": settings.ollama_model if mode == "ollama" else HAIKU_MODEL if mode == "haiku" else "mock",
            "pace": "lockstep", "speed": None, "partial": partial,
            "wall_s": round(time.monotonic() - began, 2),
            "classifier_sha256": hashlib.sha256(DEFAULT_MODEL.read_bytes()).hexdigest(),
            "synthetic_clips": sim.runner.synthetic,
            "events_processed": sim.runner.fired,
            "real_incidents": metrics.real_groups(scenario.events),
            "detections": {"verified": len(rows), "events": len(scenario.events)},
            "baseline": metrics.score_baseline(rows, scenario.events, scenario.start, world),
            "commander": metrics.score_commander(views, rows, scenario.events, scenario.start),
        }
        log(f"[{scenario_name}:{mode}:lockstep] {sim.runner.fired} events, {len(rows)} detections")
        return result, agent_fs
    finally:
        commander.stop()
        commander._conn.close()
        commander.repo._conn.close()
        store.close()


def _rows(store, world):
    return [{**d.to_json(), "node": world.node(d.node_id).name} for d in store.query()]
