"""Tools the incident commander and its sub-agents call. Exact work lives here, in plain code.

Everything returned is short text the LLM can read and cite: node NAMES (never numeric ids),
records as [Node Name #seq], sim time as HH:MM:SS. Only verified records are ever returned.
The current incident comes from the run config (configurable.incident_id), set by the commander.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from agent.evidence import Verifier
from agent.incidents import (
    AgentFiles,
    Incident,
    IncidentManager,
    WakeQueue,
    fmt_date,
    fmt_time,
    record_tag,
    sound_name,
)
from common.contracts import ROOT, EventStore, Node, Sim, World

AREA_RADIUS_M = 1500.0  # get_detections(sector) covers nodes this close to the sector centre
MAX_NOTE = 500
INCIDENT_LOOKBACK_S = 300  # get_detections always reaches this far before the incident began
_SECTOR = re.compile(r"^\s*([A-Za-z])\s*(\d{1,2})\s*$")


class ApprovalGate:
    """A dispatch runs only after the station recorded a person's approval for that incident.

    Defence in depth behind interrupt_on: even if the interrupt were misconfigured, the
    dispatch tool refuses without a grant, and each grant is used once.
    """

    def __init__(self) -> None:
        self._grants: dict[str, int] = {}
        self._lock = threading.Lock()

    def grant(self, incident_id: str, count: int = 1) -> None:
        with self._lock:
            self._grants[incident_id] = self._grants.get(incident_id, 0) + count

    def revoke(self, incident_id: str) -> None:
        with self._lock:
            self._grants.pop(incident_id, None)

    def consume(self, incident_id: str) -> bool:
        with self._lock:
            left = self._grants.get(incident_id, 0)
            if left <= 0:
                return False
            self._grants[incident_id] = left - 1
            return True


@dataclass
class Services:
    world: World
    store: EventStore
    sim: Sim
    incidents: IncidentManager
    files: AgentFiles
    wakes: WakeQueue
    verify: Verifier
    gate: ApprovalGate = field(default_factory=ApprovalGate)
    publish: Callable[[dict], None] = lambda msg: None
    clips_root: Path = ROOT / "node_data"

    # --- helpers shared by tools and the commander ---

    def node_by_name(self, name: str) -> Node | None:
        key = (name or "").strip().lower()
        return next((n for n in self.world.nodes if n.name.lower() == key), None)

    def node_names(self) -> str:
        return ", ".join(n.name for n in self.world.nodes)

    def parse_sector(self, sector: str) -> str | None:
        m = _SECTOR.match(sector or "")
        if not m:
            return None
        col, row = ord(m.group(1).upper()) - ord("A"), int(m.group(2)) - 1
        cols = int(self.world.width_m // self.world.sector_m)
        rows = int(self.world.height_m // self.world.sector_m)
        if not (0 <= col < cols and 0 <= row < rows):
            return None
        return f"{chr(ord('A') + col)}{row + 1}"

    def sector_centre(self, sector: str) -> tuple[float, float]:
        col, row = ord(sector[0]) - ord("A"), int(sector[1:]) - 1
        s = self.world.sector_m
        return (col + 0.5) * s, (row + 0.5) * s

    def nodes_near(self, x: float, y: float, radius: float = AREA_RADIUS_M) -> list[Node]:
        return [n for n in self.world.nodes if math.hypot(n.x_m - x, n.y_m - y) <= radius]

    def current(self, config: RunnableConfig | None) -> Incident | None:
        inc_id = ((config or {}).get("configurable") or {}).get("incident_id")
        return self.incidents.get(inc_id) if inc_id else None

    def detection_lines(self, detections) -> list[str]:
        lines = []
        for d in detections:
            if not d.verified:  # belt and braces: the store query already excludes these
                continue
            node = self.world.node(d.node_id)
            lines.append(
                f"- {record_tag(node.name, d.seq)} {fmt_time(d.timestamp)} "
                f"{sound_name(d.sound)} {d.confidence:.2f} (sector {node.sector})"
            )
        return lines


    def incident_evidence(self, inc: Incident) -> str:
        """Code-counted evidence so a small model does not have to count or search for it."""
        wanted = {(int(n), int(q)) for n, q in inc.records}
        node_ids = sorted({n for n, _ in wanted})
        found = sorted(
            (d for d in self.store.query(node_ids=node_ids, verified_only=True) if (d.node_id, d.seq) in wanted),
            key=lambda d: (d.timestamp, d.node_id, d.seq),
        )
        out = [f"Incident {inc.id}, estimated sector {inc.sector}. {len(found)} verified record(s):"]
        out += self.detection_lines(found)
        by_node: dict[int, list] = {}
        for d in found:
            by_node.setdefault(d.node_id, []).append(d)
        if found:
            span = (found[-1].timestamp - found[0].timestamp) / 60
            out.append(
                f"Independent nodes: {len(by_node)} "
                f"({', '.join(self.world.node(n).name for n in by_node)}), over {span:.0f} min."
            )
            wet = [n for n in by_node if self.sim.weather(self.world.node(n).sector).rain]
            dry = len(by_node) - len(wet)
            out.append(f"Nodes in rain now: {len(wet)} of {len(by_node)}.")
            if dry >= 2:
                out.append("Corroborated: YES, two or more nodes in dry weather heard it.")
            elif len(by_node) > 1:
                out.append(
                    "Corroborated: NO. Several nodes heard it, but rain can fool neighbouring nodes "
                    "together, so nodes in rain do not confirm each other."
                )
            else:
                out.append("Corroborated: NO, only one node heard it.")
        for n, ds in by_node.items():
            node = self.world.node(n)
            sounds = sorted({sound_name(d.sound) for d in ds})
            past = sum(1 for i in self.incidents.all() if i.status == "dismissed" and n in i.node_ids())
            out.append(
                f"- {node.name} (sector {node.sector}): {len(ds)} record(s), {', '.join(sounds)}, "
                f"max confidence {max(d.confidence for d in ds):.2f}, past dismissed incidents {past}"
            )
        w = self.sim.weather(inc.sector)
        out.append(f"Weather in {inc.sector}: {'raining' if w.rain else 'dry'}, wind {w.wind_kph:.0f} kph.")
        out.append(f"Valid node names (use only these): {self.node_names()}.")
        return "\n".join(out)


def _bad_sector(sector: str) -> str:
    return f"Unknown sector '{sector}'. Sectors run A1 to H8."


def make_tools(s: Services) -> dict[str, BaseTool]:
    """Build every tool bound to these services. Agents pick subsets by name."""

    @tool
    def get_detections(sector: str, config: RunnableConfig, window_s: int = 1800) -> str:
        """Verified detections heard near a sector in the last window_s seconds (always back to the incident start).

        Args:
            sector: Sector name like "E5".
            window_s: How far back to look, in seconds (60 to 86400).
        """
        sec = s.parse_sector(sector)
        if sec is None:
            return _bad_sector(sector)
        window = max(60, min(int(window_s), 86400))
        now = s.sim.now()
        since = now - window
        inc = s.current(config)
        # While triaging, a slow model at fast sim speed must not miss the incident's own records.
        # After dispatch, follow-up checks ask "is it quiet now?", so the plain window applies.
        if inc is not None and inc.status in ("open", "watching"):
            since = min(since, inc.created_at - INCIDENT_LOOKBACK_S)
        x, y = s.sector_centre(sec)
        node_ids = [n.id for n in s.nodes_near(x, y)]
        found = s.store.query(node_ids=node_ids, since=since, until=now, verified_only=True)
        lines = s.detection_lines(found)
        head = f"{len(lines)} verified records near {sec} from {fmt_time(since)} to {fmt_time(now)}"
        return head + (":\n" + "\n".join(lines) if lines else ".")

    @tool
    def get_incident_evidence(config: RunnableConfig) -> str:
        """Every verified record in this incident, how many different nodes heard it, and the weather.
        Call this first: it is the full evidence, already counted for you."""
        inc = s.current(config)
        if inc is None:
            return "No active incident."
        return s.incident_evidence(inc)

    @tool
    def get_node_info(node_name: str) -> str:
        """Location, battery and false-alarm history of a sensor node.

        Args:
            node_name: The node's name, like "Creek Bend".
        """
        node = s.node_by_name(node_name)
        if node is None:
            return f"Unknown node '{node_name}'. Nodes: {s.node_names()}."
        now = s.sim.now()
        alerts = s.store.query(node_ids=[node.id], since=now - 86400, until=now, verified_only=True)
        dismissed = [
            i for i in s.incidents.all() if i.status == "dismissed" and node.id in i.node_ids()
        ]
        notes = s.files.read("/memories/nodes.md") or ""
        history = [ln.strip("- ").strip() for ln in notes.splitlines() if node.name.lower() in ln.lower()]
        return json.dumps(
            {
                "node": node.name,
                "sector": node.sector,
                "position_m": [round(node.x_m), round(node.y_m)],
                "battery": "not reported",
                "verified_alerts_24h": len(alerts),
                "dismissed_incidents": len(dismissed),
                "memory": history[-5:],
            }
        )

    @tool
    def get_weather(sector: str) -> str:
        """Current rain and wind in a sector. Rain and wind are the main false-alarm source.

        Args:
            sector: Sector name like "E5".
        """
        sec = s.parse_sector(sector)
        if sec is None:
            return _bad_sector(sector)
        w = s.sim.weather(sec)
        return f"{sec}: {'raining' if w.rain else 'dry'}, wind {w.wind_kph:.0f} kph"

    @tool
    def schedule_check(incident_id: str, minutes: int, config: RunnableConfig) -> str:
        """Wake yourself up later to re-check this incident.

        Args:
            incident_id: The incident id from the wake-up message.
            minutes: How long to wait (1 to 240).
        """
        inc = s.current(config)
        if inc is None or incident_id.strip() != inc.id:
            return f"Unknown incident '{incident_id}'. Use {inc.id if inc else 'the id in the wake-up'}."
        mins = max(1, min(int(minutes), 240))
        at = s.sim.now() + mins * 60
        s.wakes.schedule(inc.id, at, note=f"check set at {fmt_time(s.sim.now())}")
        s.incidents.update(inc.id, next_check_at=at)
        return f"Check scheduled for {fmt_time(at)} (in {mins} min)."

    @tool
    def dispatch_rangers(sector: str, note: str, config: RunnableConfig) -> str:
        """Send a ranger team to a sector. A person must approve this first; the station asks them.

        Args:
            sector: Sector to send the team to, like "E5".
            note: One or two sentences for the team: what, where, how sure.
        """
        inc = s.current(config)
        sec = s.parse_sector(sector)
        if inc is None:
            return "Refused: no active incident."
        if sec is None:
            return _bad_sector(sector)
        if not s.gate.consume(inc.id):
            return "Refused: dispatch needs a ranger's approval. The station has not recorded one."
        now = s.sim.now()
        entry = {"time": fmt_time(now), "sector": sec, "note": (note or "")[:MAX_NOTE]}
        s.incidents.update(
            inc.id,
            status="dispatched",
            dispatches=[*inc.dispatches, entry],
            summary=f"Rangers dispatched to {sec}",
            updated_at=now,
        )
        return f"Rangers dispatched to {sec} at {entry['time']}. Note: {entry['note']}"

    @tool
    def set_status(status: Literal["watching", "dismissed", "closed"], reason: str, config: RunnableConfig) -> str:
        """Mark the incident watching, dismissed (false alarm) or closed (case file written).

        Args:
            status: "watching", "dismissed" or "closed".
            reason: One short sentence shown to rangers.
        """
        inc = s.current(config)
        if inc is None:
            return "No active incident."
        if status not in ("watching", "dismissed", "closed"):
            return "Status must be watching, dismissed or closed."
        if status == "closed" and s.files.read(inc.folder + "case_file.md") is None:
            return "Cannot close yet: ask the evidence sub-agent to write case_file.md first."
        s.incidents.update(inc.id, status=status, summary=(reason or "")[:200], updated_at=s.sim.now())
        return f"Incident {inc.id} marked {status}."

    @tool
    def remember(topic: Literal["nodes", "sectors"], note: str) -> str:
        """Save a lasting lesson to station memory, e.g. "Low Marsh: false alarms in heavy rain".

        Args:
            topic: "nodes" for a sensor node, "sectors" for an area.
            note: One line. Use node names and sector names.
        """
        if topic not in ("nodes", "sectors"):
            return "Topic must be nodes or sectors."
        line = " ".join((note or "").split())[:300]
        if not line:
            return "Nothing to save."
        s.files.append(f"/memories/{topic}.md", f"- {fmt_date(s.sim.now())}: {line}\n")
        return f"Saved to /memories/{topic}.md."

    def _target(sec: str, config: RunnableConfig | None) -> tuple[float, float]:
        inc = s.current(config)
        if inc is not None and inc.sector == sec:
            return inc.x_m, inc.y_m  # the incident's estimated source location
        return s.sector_centre(sec)

    @tool
    def plan_route(sector: str, config: RunnableConfig) -> str:
        """Plan a drone flight to a sector and back: distance, battery, no-fly limits.

        Args:
            sector: Sector to fly to, like "E5".
        """
        sec = s.parse_sector(sector)
        if sec is None:
            return _bad_sector(sector)
        route = s.sim.plan_route(*_target(sec, config))
        status = s.sim.drone_status()
        return json.dumps(
            {
                "sector": sec,
                "distance_km": round(route.distance_m / 1000, 2),
                "battery_needed_pct": round(route.battery_needed_pct),
                "battery_now_pct": round(status.battery_pct),
                "feasible": route.feasible,
                "reason": route.reason,
            }
        )

    @tool
    def fly_drone(sector: str, config: RunnableConfig) -> str:
        """Fly the drone to a sector and report what its camera sees.

        Args:
            sector: Sector to fly to, like "E5".
        """
        sec = s.parse_sector(sector)
        if sec is None:
            return _bad_sector(sector)
        route = s.sim.plan_route(*_target(sec, config))
        if not route.feasible:
            return f"Not flown: {route.reason or 'route not feasible'}."
        seen = s.sim.fly(route)
        status = s.sim.drone_status()
        km = route.distance_m / 1000
        inc = s.current(config)
        if inc is not None:
            s.files.append(
                inc.folder + "drone_log.md",
                f"## {fmt_time(s.sim.now())} flight to {sec}\n"
                f"- distance {km:.1f} km, battery used {route.battery_needed_pct:.0f}%, "
                f"now {status.battery_pct:.0f}%\n- camera: {seen}\n\n",
            )
        return f"Flew {km:.1f} km to {sec}. Camera: {seen} Battery now {status.battery_pct:.0f}%."

    @tool
    def get_drone_status() -> str:
        """Where the drone is, its battery, and whether it is flying."""
        st = s.sim.drone_status()
        where = s.world.sector_of(st.x_m, st.y_m)
        return f"Drone in {where}, battery {st.battery_pct:.0f}%, {'flying' if st.flying else 'idle'}."

    @tool
    def verify_signature(node: str, seq: int) -> str:
        """Re-check a record's Ed25519 signature from its stored raw bytes.

        Args:
            node: The node's name, like "Creek Bend".
            seq: The record number.
        """
        n = s.node_by_name(node)
        if n is None:
            return f"Unknown node '{node}'. Nodes: {s.node_names()}."
        tag = record_tag(n.name, int(seq))
        raw = s.store.raw(n.id, int(seq))
        if raw is None:
            return f"{tag}: no such record."
        det = next((d for d in s.store.query(node_ids=[n.id], verified_only=False) if d.seq == int(seq)), None)
        if det is None or not det.verified:
            return f"{tag}: INVALID, the station rejected this record on receipt. Not evidence."
        if not s.verify(n.id, int(seq), raw):
            return f"{tag}: signature INVALID. Not evidence; list it as failed."
        return f"{tag}: signature valid, record unaltered. clip hash {det.clip_hash}"

    all_tools = [
        get_incident_evidence,
        get_detections,
        get_node_info,
        get_weather,
        schedule_check,
        dispatch_rangers,
        set_status,
        remember,
        plan_route,
        fly_drone,
        get_drone_status,
        verify_signature,
    ]
    return {t.name: t for t in all_tools}


TOOLSETS = {
    "commander": [
        "get_incident_evidence",
        "get_detections",
        "get_node_info",
        "get_weather",
        "schedule_check",
        "dispatch_rangers",
        "set_status",
        "remember",
    ],
    "triage": ["get_incident_evidence", "get_detections", "get_node_info", "get_weather"],
    "drone-pilot": ["plan_route", "fly_drone", "get_drone_status"],
    "evidence": ["get_incident_evidence", "verify_signature", "get_detections", "remember"],
}
