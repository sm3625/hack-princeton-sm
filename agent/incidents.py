"""Incidents and wake-ups. Plain code, no LLM.

- Groups verified detections into incidents (same threat family, close in time, nearby nodes).
- Estimates the source location as the confidence-weighted centroid of the nodes that heard it.
- Keeps one folder and one LangGraph thread per incident.
- Decides when the commander wakes: new detections (batched), its own scheduled checks,
  ranger messages.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common.contracts import Detection, Node, SoundClass, World

JOIN_WINDOW_S = 45 * 60  # a detection joins an incident active within this window
JOIN_RADIUS_M = 1500.0  # ...if its node is this close to the incident's estimated location
BATCH_S = 10  # detections arriving within this many seconds wake the commander once

FAMILY = {
    SoundClass.CHAINSAW: "logging",
    SoundClass.AXE: "logging",
    SoundClass.GUNSHOT: "poaching",
    SoundClass.VEHICLE: "vehicle",
}
THREAT_FAMILIES = ("logging", "poaching")

# open: agent working · watching: waiting on a check · awaiting_approval: dispatch needs a person
# dispatched: rangers sent · dismissed: false alarm · closed: case file written
STATUSES = ("open", "watching", "awaiting_approval", "dispatched", "dismissed", "closed")

NODES_MEMORY = "# Node notes\n\nWhat the station has learned about individual sensor nodes.\n\n"
SECTORS_MEMORY = "# Sector notes\n\nPast incidents and patterns per sector.\n\n"


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts))


def fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def fmt_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def record_tag(node_name: str, seq: int) -> str:
    """How a record is cited everywhere the LLM or a person reads it."""
    return f"[{node_name} #{seq}]"


def sound_name(sound: SoundClass) -> str:
    return SoundClass(sound).name.lower()


# ---------- files on disk (the deepagents FilesystemBackend root) ----------


class AgentFiles:
    """Paths under agent_fs/. Virtual paths (what the LLM sees) start with '/'."""

    CODE_OWNED = ("detections.jsonl", "approvals.jsonl")  # agent may read, never write

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.Lock()

    def ensure(self) -> None:
        (self.root / "incidents").mkdir(parents=True, exist_ok=True)
        mem = self.root / "memories"
        mem.mkdir(parents=True, exist_ok=True)
        for name, header in (("nodes.md", NODES_MEMORY), ("sectors.md", SECTORS_MEMORY)):
            if not (mem / name).exists():
                (mem / name).write_text(header)

    def disk(self, virtual: str) -> Path:
        path = (self.root / virtual.lstrip("/")).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("path escapes agent_fs")
        return path

    @staticmethod
    def folder(incident_id: str) -> str:
        return f"/incidents/{incident_id}/"

    def read(self, virtual: str) -> str | None:
        path = self.disk(virtual)
        return path.read_text() if path.is_file() else None

    def write(self, virtual: str, text: str) -> None:
        path = self.disk(virtual)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(text)

    def append(self, virtual: str, text: str) -> None:
        path = self.disk(virtual)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a") as f:
            f.write(text)

    def append_jsonl(self, virtual: str, row: dict) -> None:
        self.append(virtual, json.dumps(row, separators=(", ", ": ")) + "\n")


# ---------- incident model ----------


@dataclass
class Incident:
    id: str
    family: str
    sector: str
    status: str
    title: str
    created_at: int
    updated_at: int
    last_detection_at: int
    x_m: float
    y_m: float
    summary: str = ""
    records: list[list[int]] = field(default_factory=list)  # [node_id, seq]; internal only
    weights: dict[str, float] = field(default_factory=dict)  # node_id -> max confidence
    sounds: list[str] = field(default_factory=list)
    next_check_at: int | None = None
    dispatches: list[dict] = field(default_factory=list)
    in_rain: bool | None = None  # True while every record so far was heard in rain; None = unknown
    rejected_at_records: int | None = None  # record count when a ranger last rejected dispatch

    @property
    def thread_id(self) -> str:
        return f"incident-{self.id}"

    @property
    def folder(self) -> str:
        return AgentFiles.folder(self.id)

    def node_ids(self) -> list[int]:
        return [int(k) for k in self.weights]

    def public(self, world: World) -> dict[str, Any]:
        """What the UI may see: names, never numeric node ids."""
        return {
            "id": self.id,
            "sector": self.sector,
            "status": self.status,
            "title": self.title,
            "summary": self.summary,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "family": self.family,
            "nodes": sorted(world.node(n).name for n in self.node_ids()),
            "records": len(self.records),
            "location": {"x_m": round(self.x_m), "y_m": round(self.y_m)},
            "next_check_at": self.next_check_at,
        }

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Incident:
        return cls(**json.loads(text))


class IncidentRepo:
    """SQLite persistence for incidents and approvals (parameterized SQL only)."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, data TEXT)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, incident_id TEXT, data TEXT)"
            )

    def save_incident(self, inc: Incident) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO incidents (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (inc.id, inc.to_json()),
            )

    def load_incidents(self) -> list[Incident]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM incidents").fetchall()
        return [Incident.from_json(r[0]) for r in rows]

    def save_approval(self, approval_id: str, incident_id: str, data: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, incident_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (approval_id, incident_id, json.dumps(data)),
            )

    def load_approvals(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM approvals").fetchall()
        return [json.loads(r[0]) for r in rows]


# ---------- grouping ----------


def centroid(world: World, weights: dict[str, float]) -> tuple[float, float]:
    total = sum(weights.values()) or 1.0
    x = sum(world.node(int(n)).x_m * w for n, w in weights.items()) / total
    y = sum(world.node(int(n)).y_m * w for n, w in weights.items()) / total
    return x, y


def compatible(incident_family: str, family: str) -> bool:
    """Same family, or a vehicle (which can belong to logging or poaching)."""
    return incident_family == family or "vehicle" in (incident_family, family)


class IncidentManager:
    """Owns incidents. Thread-safe; called from the receiver hook and from agent tools."""

    def __init__(
        self,
        world: World,
        files: AgentFiles,
        repo: IncidentRepo | None = None,
        on_change: Callable[[Incident], None] | None = None,
        raining: Callable[[str], bool] | None = None,
    ) -> None:
        self.world = world
        self.raining = raining  # sector -> is it raining now; keeps rain and dry detections apart
        self.files = files
        self.repo = repo
        self.on_change = on_change
        self._lock = threading.RLock()
        self._incidents: dict[str, Incident] = {}
        self._seen: set[tuple[int, int]] = set()
        if repo:
            for inc in repo.load_incidents():
                self._incidents[inc.id] = inc
                self._seen.update((n, s) for n, s in inc.records)

    # --- reads ---

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            return self._incidents.get(incident_id)

    def all(self) -> list[Incident]:
        with self._lock:
            return sorted(self._incidents.values(), key=lambda i: i.updated_at, reverse=True)

    # --- writes ---

    def add(self, d: Detection) -> Incident | None:
        """Attach a verified detection to an incident (creating one if needed).

        Unverified records and background sounds are dropped here: they never reach the agent.
        """
        if not d.verified or d.sound not in FAMILY:
            return None
        try:
            node = self.world.node(d.node_id)
        except StopIteration:
            return None
        with self._lock:
            if (d.node_id, d.seq) in self._seen:
                return None
            self._seen.add((d.node_id, d.seq))
            family = FAMILY[d.sound]
            inc = self._match(d, node, family)
            if inc is None:
                inc = self._create(d, node, family)
            self._attach(inc, d, node, family)
            self.files.append_jsonl(inc.folder + "detections.jsonl", self.record_row(d, node))
            self._save(inc)
        return inc

    def update(self, incident_id: str, **changes: Any) -> Incident | None:
        with self._lock:
            inc = self._incidents.get(incident_id)
            if inc is None:
                return None
            for k, v in changes.items():
                setattr(inc, k, v)
            self._save(inc)
            return inc

    def touch(self, incident_id: str, now: int) -> None:
        self.update(incident_id, updated_at=now)

    def record_row(self, d: Detection, node: Node) -> dict:
        return {
            "record": record_tag(node.name, d.seq),
            "node": node.name,
            "seq": d.seq,
            "time": fmt_iso(d.timestamp),
            "sound": sound_name(d.sound),
            "confidence": round(d.confidence, 2),
            "sector": node.sector,
            "clip_hash": d.clip_hash,
            "model_version": d.model_version,
        }

    def _rain_at(self, node: Node) -> bool | None:
        if self.raining is None:
            return None
        return bool(self.raining(node.sector))

    def _match(self, d: Detection, node: Node, family: str) -> Incident | None:
        best, best_dist = None, JOIN_RADIUS_M
        for inc in self._incidents.values():
            if inc.status == "closed" or not compatible(inc.family, family):
                continue
            if abs(d.timestamp - inc.last_detection_at) > JOIN_WINDOW_S:
                continue
            # A sound heard in dry weather does not join an incident made only of rain detections:
            # rain false alarms and a real event nearby are different incidents. Rain detections
            # may still join an incident with dry evidence (rain starting mid-event).
            if inc.in_rain is True and self._rain_at(node) is False:
                continue
            dist = math.hypot(node.x_m - inc.x_m, node.y_m - inc.y_m)
            if dist <= best_dist:
                best, best_dist = inc, dist
        return best

    def _create(self, d: Detection, node: Node, family: str) -> Incident:
        sector = self.world.sector_of(node.x_m, node.y_m)
        base = f"{fmt_date(d.timestamp)}_{sector}"
        inc_id, n = base, 2
        while inc_id in self._incidents:
            inc_id, n = f"{base}-{n}", n + 1
        inc = Incident(
            id=inc_id,
            family=family,
            sector=sector,
            status="open",
            title=f"{sound_name(d.sound).capitalize()} near {node.name}",
            created_at=d.timestamp,
            updated_at=d.timestamp,
            last_detection_at=d.timestamp,
            x_m=node.x_m,
            y_m=node.y_m,
            in_rain=self._rain_at(node),
        )
        self._incidents[inc_id] = inc
        return inc

    def _attach(self, inc: Incident, d: Detection, node: Node, family: str) -> None:
        inc.records.append([d.node_id, d.seq])
        if self._rain_at(node) is False:
            inc.in_rain = False  # it now has dry evidence
        key = str(d.node_id)
        inc.weights[key] = max(inc.weights.get(key, 0.0), float(d.confidence))
        name = sound_name(d.sound)
        if name not in inc.sounds:
            inc.sounds.append(name)
        if inc.family == "vehicle" and family in THREAT_FAMILIES:
            inc.family = family
            inc.title = f"{name.capitalize()} near {node.name}"
        inc.x_m, inc.y_m = centroid(self.world, inc.weights)
        inc.sector = self.world.sector_of(inc.x_m, inc.y_m)
        inc.last_detection_at = max(inc.last_detection_at, d.timestamp)
        inc.updated_at = max(inc.updated_at, d.timestamp)

    def _save(self, inc: Incident) -> None:
        if self.repo:
            self.repo.save_incident(inc)
        if self.on_change:
            self.on_change(inc)


# ---------- wake-ups ----------


@dataclass
class Wake:
    incident_id: str
    new_records: list[tuple[int, int]] = field(default_factory=list)
    check_due: bool = False
    check_note: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.new_records:
            return "new_detections"
        if self.check_due:
            return "scheduled_check"
        return "ranger_message"


@dataclass
class _Pending:
    records: list[tuple[int, int]] = field(default_factory=list)
    det_due: float | None = None  # sim time
    det_wall_due: float | None = None  # wall clock, so a paused sim can't hold wakes forever
    check_at: float | None = None  # sim time
    check_note: str = ""
    messages: list[str] = field(default_factory=list)
    retry_wall_due: float | None = None  # after a failed run, nothing fires before this


class WakeQueue:
    """When should the commander wake up for each incident? Driven by tick(now)."""

    def __init__(self, batch_s: float = BATCH_S, wall: Callable[[], float] = time.monotonic) -> None:
        self.batch_s = batch_s
        self.wall = wall
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def detection(self, incident_id: str, record: tuple[int, int], now: float) -> None:
        with self._lock:
            p = self._pending.setdefault(incident_id, _Pending())
            p.records.append(record)
            if p.det_due is None:
                p.det_due = now + self.batch_s
                p.det_wall_due = self.wall() + self.batch_s

    def schedule(self, incident_id: str, at: float, note: str = "") -> None:
        """A newer check replaces an older one for the same incident."""
        with self._lock:
            p = self._pending.setdefault(incident_id, _Pending())
            p.check_at, p.check_note = at, note

    def message(self, incident_id: str, text: str) -> None:
        with self._lock:
            self._pending.setdefault(incident_id, _Pending()).messages.append(text)

    def requeue(self, wake: Wake, delay_s: float) -> None:
        """Put a wake back after a failed run (e.g. the model server is down), with backoff."""
        with self._lock:
            p = self._pending.setdefault(wake.incident_id, _Pending())
            if wake.new_records:
                p.records = wake.new_records + p.records
                p.det_due = p.det_due if p.det_due is not None else 0
                p.det_wall_due = p.det_wall_due if p.det_wall_due is not None else 0
            if wake.check_due and p.check_at is None:
                p.check_at, p.check_note = 0, wake.check_note
            p.messages = wake.messages + p.messages
            p.retry_wall_due = self.wall() + delay_s

    def has_pending(self) -> bool:
        """Detections or ranger messages still waiting for a wake-up (scheduled checks do not count)."""
        with self._lock:
            return any(p.records or p.messages for p in self._pending.values())

    def pending_check(self, incident_id: str) -> float | None:
        with self._lock:
            p = self._pending.get(incident_id)
            return p.check_at if p else None

    def pop_due(self, now: float, skip: set[str] | frozenset[str] = frozenset()) -> list[Wake]:
        wall = self.wall()
        out: list[Wake] = []
        with self._lock:
            for inc_id, p in list(self._pending.items()):
                if inc_id in skip or (p.retry_wall_due is not None and wall < p.retry_wall_due):
                    continue
                p.retry_wall_due = None
                wake = Wake(inc_id)
                if p.records and (now >= (p.det_due or 0) or wall >= (p.det_wall_due or 0)):
                    wake.new_records, p.records, p.det_due, p.det_wall_due = p.records, [], None, None
                if p.check_at is not None and now >= p.check_at:
                    wake.check_due, wake.check_note, p.check_at = True, p.check_note, None
                if p.messages:
                    wake.messages, p.messages = p.messages, []
                if wake.new_records or wake.check_due or wake.messages:
                    out.append(wake)
                if not (p.records or p.check_at is not None or p.messages):
                    del self._pending[inc_id]
        return out
