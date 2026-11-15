"""Fakes for agent tests: a controllable Sim, signed records, and a Commander on mock models."""

from __future__ import annotations

import hashlib
import math
import struct
import time
from pathlib import Path

from nacl.signing import SigningKey

from agent.commander import Commander
from agent.evidence import Ed25519Verifier
from agent.fallbacks import MemoryEventStore
from agent.llm import ROLES
from agent.mock import MockChatModel
from common.contracts import Detection, DroneStatus, Route, SoundClass, Weather, World, load_world
from common.sites import site_map

NIGHT = int(time.mktime((2026, 11, 14, 0, 0, 0, 0, 0, -1)))  # station-local midnight


def at(hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return NIGHT + h * 3600 + m * 60 + s


class FakeSim:
    def __init__(self, world: World, start: int = NIGHT) -> None:
        self.world = world
        self.t = start
        self.rain: set[str] = set()
        self.seen: dict[str, str] = {}
        self.flights: list[Route] = []
        self.battery = 100.0
        self.x, self.y = world.base_x_m, world.base_y_m

    def now(self) -> int:
        return self.t

    def weather(self, sector: str) -> Weather:
        return Weather(rain=sector in self.rain, wind_kph=25.0 if sector in self.rain else 6.0)

    def plan_route(self, target_x_m: float, target_y_m: float) -> Route:
        dist = 2 * math.hypot(target_x_m - self.x, target_y_m - self.y)
        need = dist / 1000 * 8
        sector = self.world.sector_of(target_x_m, target_y_m)
        if sector in self.world.no_fly:
            return Route([], dist, need, False, f"{sector} is a no-fly zone")
        return Route([(self.x, self.y), (target_x_m, target_y_m)], dist, need, need < self.battery - 10)

    def fly(self, route: Route) -> str:
        self.flights.append(route)
        self.battery -= route.battery_needed_pct
        tx, ty = route.waypoints[-1]
        return self.seen.get(self.world.sector_of(tx, ty), "Fresh clearing, one vehicle.")

    def drone_status(self) -> DroneStatus:
        return DroneStatus(self.x, self.y, self.battery, False)


class Nodes:
    """Signs records the way a node does (SPEC 3.3), and keeps the matching public keys."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.keys = {n.id: SigningKey(hashlib.sha256(f"test-node-{n.id}".encode()).digest()) for n in world.nodes}
        self.public = {i: bytes(k.verify_key) for i, k in self.keys.items()}

    def record(self, node_name: str, seq: int, ts: int, sound: SoundClass, conf: float,
               verified: bool = True) -> tuple[Detection, bytes]:
        node = next(n for n in self.world.nodes if n.name == node_name)
        clip = hashlib.sha256(f"clip-{node.id}-{seq}".encode()).digest()[:16]
        body = struct.pack(
            "<BHIIiiBBH16s5s", 1, node.id, seq, ts, int(node.lat * 1e7), int(node.lon * 1e7),
            int(sound), round(conf * 255), 1, clip, b"\0" * 5,
        )
        raw = body + self.keys[node.id].sign(body).signature
        if not verified:  # what the receiver flags as unverified: the signature doesn't check
            raw = raw[:-1] + bytes([raw[-1] ^ 0xFF])
        det = Detection(
            node_id=node.id, seq=seq, timestamp=ts, lat=node.lat, lon=node.lon, sound=sound,
            confidence=conf, model_version=1, clip_hash=clip.hex(), signature=raw[44:].hex(), verified=verified,
        )
        return det, raw


class Harness:
    """A Commander wired to fakes, on mock models. Inline (no threads) unless background=True."""

    def __init__(
        self, tmp: Path, models: dict | None = None, background: bool = False, deployment=None
    ) -> None:
        self.world = load_world(site_map(deployment.map).world_file) if deployment else load_world()
        self.sim = FakeSim(self.world)
        self.store = MemoryEventStore()
        self.nodes = Nodes(self.world)
        self.messages: list[dict] = []
        self.tmp = tmp
        self.background = background
        self.deployment = deployment
        self.models = models or {role: MockChatModel(role=role) for role in ROLES}
        self.commander = self.build()

    def build(self) -> Commander:
        return Commander(
            world=self.world,
            store=self.store,
            sim=self.sim,
            verify=Ed25519Verifier(self.nodes.public),
            fs_root=self.tmp / "agent_fs",
            db_path=self.tmp / "state.sqlite",
            models=self.models,
            publish=self.messages.append,
            background=self.background,
            clips_root=self.tmp / "node_data",
            deployment=self.deployment,
        )

    def hear(self, node: str, seq: int, hhmmss: str, sound: SoundClass, conf: float, verified: bool = True):
        """A node's record arrives: store it (like the receiver), then the on_verified hook."""
        ts = at(hhmmss)
        self.sim.t = max(self.sim.t, ts)
        det, raw = self.nodes.record(node, seq, ts, sound, conf, verified)
        self.store.add(det, raw)
        if det.verified:  # the receiver only calls on_verified for verified records
            self.commander.on_verified(det)
        return det

    def tick(self, hhmmss: str) -> None:
        self.sim.t = at(hhmmss)
        self.commander.tick()

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == kind]

    def pending(self):
        return [a for a in self.commander.approvals.values() if a.status == "pending"]

    def file(self, incident_id: str, name: str) -> str | None:
        return self.commander.files.read(f"/incidents/{incident_id}/{name}")

    def tamper(self, node: str, seq: int) -> None:
        """Flip one byte of a stored record (the UI's tamper demo)."""
        n = next(x for x in self.world.nodes if x.name == node)
        key = (n.id, seq)
        det, raw = self.store._rows[key]
        self.store._rows[key] = (det, raw[:20] + bytes([raw[20] ^ 0xFF]) + raw[21:])
