"""Shared contracts between tracks. Change only by agreement: every track codes against these.

Owners:
  node/, transport/, station/receiver.py, station/store.py, station/routes_events.py -> pipeline track
  node/classifier.py, models/, training/                                           -> classifier track
  sim/, ui/, station/routes_sim.py                                                  -> sim+UI track
  agent/, station/routes_agent.py                                                   -> agent track
  station/app.py wires routers together (integration step only).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
WORLD_FILE = ROOT / "scenarios" / "world.yaml"

SAMPLE_RATE = 16_000
WINDOW_S = 1.0
HOP_S = 0.5
MAX_PACKET_BYTES = 222  # real LoRa upper bound; transport rejects anything larger
RECORD_BYTES = 108  # SPEC 3.3


class SoundClass(IntEnum):
    BACKGROUND = 0
    CHAINSAW = 1
    GUNSHOT = 2
    VEHICLE = 3
    AXE = 4  # stretch


# ---------- classifier (classifier track implements) ----------


@dataclass(frozen=True)
class Prediction:
    sound: SoundClass
    confidence: float  # 0..1


@runtime_checkable
class Classifier(Protocol):
    model_version: int
    model_bytes: int

    def classify(self, window: np.ndarray) -> Prediction:
        """window: float32 mono, SAMPLE_RATE * WINDOW_S samples, range -1..1."""


# ---------- detection record (pipeline track implements pack/sign/verify) ----------


@dataclass(frozen=True)
class Detection:
    """Parsed form of the 108-byte signed record (SPEC 3.3)."""

    node_id: int
    seq: int
    timestamp: int  # unix seconds (sim time when simulated)
    lat: float
    lon: float
    sound: SoundClass
    confidence: float  # 0..1 (stored as 0..255 on the wire)
    model_version: int
    clip_hash: str  # hex, 16 bytes
    signature: str = ""  # hex, 64 bytes
    verified: bool = False

    def to_json(self) -> dict:
        d = asdict(self)
        d["sound"] = self.sound.name.lower()
        return d


# ---------- event store (pipeline track implements; agent + UI read) ----------


@runtime_checkable
class EventStore(Protocol):
    def add(self, detection: Detection, raw: bytes) -> bool:
        """Store; returns False if (node_id, seq) was already seen (replay)."""

    def query(
        self,
        node_ids: list[int] | None = None,
        since: int | None = None,
        until: int | None = None,
        verified_only: bool = True,
    ) -> list[Detection]: ...

    def raw(self, node_id: int, seq: int) -> bytes | None: ...


# ---------- world (shared data in scenarios/world.yaml) ----------


@dataclass(frozen=True)
class Node:
    id: int
    name: str  # shown in UI; never show the numeric id
    x_m: float
    y_m: float
    lat: float
    lon: float
    sector: str


@dataclass(frozen=True)
class World:
    width_m: float
    height_m: float
    sector_m: float
    origin_lat: float
    origin_lon: float
    nodes: list[Node]
    no_fly: list[str] = field(default_factory=list)  # sector names
    base_x_m: float = 0.0
    base_y_m: float = 0.0

    def node(self, node_id: int) -> Node:
        return next(n for n in self.nodes if n.id == node_id)

    def sector_of(self, x_m: float, y_m: float) -> str:
        col = int(x_m // self.sector_m)
        row = int(y_m // self.sector_m)
        return f"{chr(ord('A') + col)}{row + 1}"


def load_world(path: Path = WORLD_FILE) -> World:
    import yaml

    data = yaml.safe_load(path.read_text())
    nodes = [Node(**n) for n in data.pop("nodes")]
    return World(nodes=nodes, **data)


# ---------- simulated physical world (sim track implements; agent tools call) ----------


@dataclass(frozen=True)
class Weather:
    rain: bool
    wind_kph: float


@dataclass(frozen=True)
class Route:
    waypoints: list[tuple[float, float]]  # metres
    distance_m: float
    battery_needed_pct: float
    feasible: bool
    reason: str = ""
    drone_id: str = ""  # which drone this route is for (empty: the only / first drone)


@dataclass(frozen=True)
class DroneStatus:
    x_m: float
    y_m: float
    battery_pct: float
    flying: bool
    id: str = "drone-1"


class Sim(Protocol):
    def now(self) -> int:
        """Current sim time, unix seconds."""

    def weather(self, sector: str) -> Weather: ...

    def plan_route(self, target_x_m: float, target_y_m: float) -> Route: ...

    def fly(self, route: Route) -> str:
        """Fly the route (animates in UI); returns what the camera saw at the target."""

    def drone_status(self) -> DroneStatus: ...

    # Optional: sims with several drones also offer drone_statuses() -> list[DroneStatus].


# ---------- UI messages over WebSocket /ws (server -> browser) ----------
# {"type": "detection",  "detection": Detection.to_json()}
# {"type": "rejected",   "node": name, "reason": "bad_signature" | "replay" | "oversize"}
# {"type": "incident",   "incident": {"id", "sector", "status", "title", "updated_at"}}
# {"type": "plan",       "incident_id", "markdown"}
# {"type": "agent_step", "incident_id", "agent": "commander"|"triage"|"drone-pilot"|"evidence",
#                        "summary"}
# {"type": "approval",   "incident_id", "approval_id", "question", "action"}
# {"type": "drone",      "x_m", "y_m", "battery_pct", "flying"}
# {"type": "weather",    "sector", "rain", "wind_kph"}
# {"type": "clock",      "sim_time"}
#
# Browser -> server (HTTP):
# POST /api/approvals/{approval_id}   {"decision": "approve"|"edit"|"reject", "note": str}
# GET  /api/incidents, /api/incidents/{id}, /api/incidents/{id}/case_file
# POST /api/sim/scenario {"name": "night1", "speed": 30}
#
# A single in-process async broadcaster lives in station/bus.py (see there).
